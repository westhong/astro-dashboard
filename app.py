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

import time
from datetime import date as date_cls
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

SKILL_SCRIPT = Path(__file__).parent / "backend" / "scripts" / "night_report.py"
LOCATIONS_JSON = Path(__file__).parent / "backend" / "references" / "locations.json"
STATIC_DIR = Path(__file__).parent / "static"
VERSION = (Path(__file__).parent / "VERSION").read_text().strip()


CACHE_TTL = 600  # 10 分鐘
LAN_BATCH_TIMEOUT = 420


app = FastAPI(title="astro-dashboard")
_cache = {}  # date_str -> (timestamp, payload)
_inflight: dict[str, asyncio.Task] = {}


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
    results = run_all_with_coordinator(
        ids, date_str, coordinator, timeout=LAN_BATCH_TIMEOUT
    )
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
    """Keep the event loop free while the complete synchronous report is built."""
    return await asyncio.to_thread(_build_report_sync, date_str, prior_payload)


async def _build_and_cache(date_str: str, prior_payload: dict | None) -> dict:
    payload = await build_report(date_str, prior_payload=prior_payload)
    _cache[date_str] = (time.time(), payload)
    return payload


@app.get("/api/report")
async def report(date: str = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    date_str = date or date_cls.today().isoformat()
    cached = _cache.get(date_str)
    if cached and time.time() - cached[0] < CACHE_TTL:
        payload = dict(cached[1])
        payload["cache_age_seconds"] = round(time.time() - cached[0])
        return JSONResponse(payload)
    task = _inflight.get(date_str)
    if task is None:
        prior_payload = cached[1] if cached else None
        task = asyncio.create_task(_build_and_cache(date_str, prior_payload))
        _inflight[date_str] = task
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
