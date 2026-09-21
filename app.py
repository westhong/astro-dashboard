#!/usr/bin/env python3
"""
astro-dashboard backend
即時執行與靜態建置相同的全機位批次分析，
將結果以 JSON 俾手機 frontend。

原則（West 定）：
- 即時數據：cache 最多 10 分鐘，過期即重跑
- 分析唔到就要道歉：任何機位失敗，誠實回傳 error，絕不造假數據
"""
import asyncio
import json
import threading

import time
from collections import OrderedDict
from datetime import date as date_cls, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.process_isolation import (
    IsolatedProcessError,
    IsolatedProcessTimeout,
    run_in_spawned_process,
)

SKILL_SCRIPT = Path(__file__).parent / "backend" / "scripts" / "night_report.py"
LOCATIONS_JSON = Path(__file__).parent / "backend" / "references" / "locations.json"
STATIC_DIR = Path(__file__).parent / "static"
VERSION = (Path(__file__).parent / "VERSION").read_text().strip()


CACHE_TTL = 600  # 10 分鐘
LAN_REPORT_TIMEOUT = 420
REPORT_CACHE_MAX = 5
MAX_CONCURRENT_REPORT_BUILDS = 2
EDMONTON_TZ = ZoneInfo("America/Edmonton")


app = FastAPI(title="astro-dashboard")
_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_inflight: dict[str, asyncio.Task] = {}
_build_semaphore: asyncio.Semaphore | None = None


def _edmonton_today() -> date_cls:
    return datetime.now(EDMONTON_TZ).date()


def _validate_report_date(requested: str | None) -> str:
    today = _edmonton_today()
    date_str = requested or today.isoformat()
    try:
        parsed = date_cls.fromisoformat(date_str)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="日期格式必須為 YYYY-MM-DD") from exc
    if parsed.isoformat() != date_str or not today <= parsed <= today + timedelta(days=4):
        raise HTTPException(status_code=422, detail="日期只支援 Edmonton 當日起五天")
    return date_str


def _prune_cache() -> None:
    valid_dates = {
        (_edmonton_today() + timedelta(days=offset)).isoformat()
        for offset in range(REPORT_CACHE_MAX)
    }
    for key in list(_cache):
        if key not in valid_dates:
            _cache.pop(key, None)
    while len(_cache) > REPORT_CACHE_MAX:
        _cache.popitem(last=False)


def _report_build_semaphore() -> asyncio.Semaphore:
    global _build_semaphore
    if _build_semaphore is None:
        _build_semaphore = asyncio.Semaphore(MAX_CONCURRENT_REPORT_BUILDS)
    return _build_semaphore


def _reset_report_state_for_tests() -> None:
    global _build_semaphore
    _cache.clear()
    _inflight.clear()
    _build_semaphore = None


def location_ids():
    return list(json.loads(LOCATIONS_JSON.read_text()).keys())


def build_spots(date_str: str):
    """LAN 與靜態模式使用同一套拍攝點資料與當日日出／日落。"""
    from backend.build_report import build_spots as build_static_spots
    return build_static_spots(date_str)


def build_daylight_report(date_str: str, coordinator=None):
    from backend.daylight_report import build_daylight
    return build_daylight(date_str, coordinator=coordinator)


def _build_report_sync(date_str: str, prior_payload: dict | None = None) -> dict:
    t0 = time.time()
    ids = location_ids()
    from backend.build_report import (
        apply_forecast_revision,
        create_weather_coordinator,
        run_all_with_coordinator,
        select_best_location,
    )
    from backend.daylight_report import prepare_coordinator_horizons
    # LAN 與靜態建置共用同一個全機位入口；個別缺失由批次結果誠實回報，
    # 不再為每個機位另開 subprocess 或重新抓取整批資料。
    coordinator = create_weather_coordinator(ids)
    prepare_coordinator_horizons([date_str], coordinator)
    results = run_all_with_coordinator(ids, date_str, coordinator)
    apply_forecast_revision(results, prior_payload, date_str)
    ok = [r for r in results if not r.get("error")]
    failed = [r for r in results if r.get("error")]
    best = select_best_location(ok)
    return {
        "version": VERSION,
        "night_date": date_str,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(time.time() - t0, 1),
        "locations": results,
        "best_location_id": best["location_id"] if best else None,
        "failed_count": len(failed),
        "spots": build_spots(date_str),
        "daylight": build_daylight_report(date_str, coordinator),
    }


async def build_report(date_str: str, prior_payload: dict | None = None) -> dict:
    """Bound the complete build in a child process while keeping the loop free."""
    cancellation_event = threading.Event()
    runner_task = asyncio.create_task(asyncio.to_thread(
        run_in_spawned_process,
        "app",
        "_build_report_sync",
        args=(date_str, prior_payload),
        timeout=LAN_REPORT_TIMEOUT,
        cancellation_event=cancellation_event,
    ))
    try:
        return await asyncio.shield(runner_task)
    except asyncio.CancelledError:
        cancellation_event.set()
        await _wait_for_runner_cleanup(runner_task)
        raise
    except IsolatedProcessTimeout:
        return _honest_error_report(
            date_str,
            "完整報告超時——天氣數據服務可能沒有回應",
            "完整報告超時——日間分析未完成",
            LAN_REPORT_TIMEOUT,
        )
    except IsolatedProcessError:
        return _honest_error_report(
            date_str,
            "完整報告工作程序失敗——未取得天氣分析結果",
            "完整報告工作程序失敗——日間分析未完成",
            0,
        )


async def _wait_for_runner_cleanup(runner_task: asyncio.Task) -> None:
    """Do not let repeated caller cancellation release the build slot early."""
    while True:
        try:
            await asyncio.shield(runner_task)
            return
        except asyncio.CancelledError:
            if runner_task.done():
                return
        except Exception:
            return


def _honest_error_report(
    date_str: str, location_message: str, daylight_message: str, elapsed: float,
) -> dict:
    ids = location_ids()
    results = [
        {"location_id": location_id, "error": True, "message": location_message}
        for location_id in ids
    ]
    now = datetime.now(timezone.utc)
    return {
        "version": VERSION,
        "night_date": date_str,
        "generated_at": now.astimezone(EDMONTON_TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
        "generated_utc": now.isoformat(timespec="seconds"),
        "elapsed_seconds": elapsed,
        "locations": results,
        "best_location_id": None,
        "failed_count": len(results),
        "spots": [],
        "daylight": {"date": date_str, "error": True, "message": daylight_message},
    }


async def _build_and_cache(date_str: str, prior_payload: dict | None) -> dict:
    async with _report_build_semaphore():
        payload = await build_report(date_str, prior_payload=prior_payload)
    _cache[date_str] = (time.time(), payload)
    _cache.move_to_end(date_str)
    _prune_cache()
    return payload


def _remove_inflight(date_str: str, task: asyncio.Task) -> None:
    if _inflight.get(date_str) is task:
        _inflight.pop(date_str, None)


@app.get("/api/report")
async def report(date: str = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    date_str = _validate_report_date(date)
    _prune_cache()
    cached = _cache.get(date_str)
    if cached and time.time() - cached[0] < CACHE_TTL:
        _cache.move_to_end(date_str)
        payload = dict(cached[1])
        payload["cache_age_seconds"] = round(time.time() - cached[0])
        return JSONResponse(payload)
    task = _inflight.get(date_str)
    if task is None:
        prior_payload = cached[1] if cached else None
        task = asyncio.create_task(_build_and_cache(date_str, prior_payload))
        _inflight[date_str] = task
        task.add_done_callback(lambda done, key=date_str: _remove_inflight(key, done))
    try:
        payload = await asyncio.shield(task)
    finally:
        if task.done() and _inflight.get(date_str) is task:
            _inflight.pop(date_str, None)
    response_payload = dict(payload)
    response_payload["cache_age_seconds"] = 0
    return JSONResponse(response_payload)


@app.get("/api/health")
async def health():
    return {"ok": True, "skill_script_exists": SKILL_SCRIPT.exists()}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/icons", StaticFiles(directory=STATIC_DIR / "icons"), name="icons")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8788, log_level="warning")
