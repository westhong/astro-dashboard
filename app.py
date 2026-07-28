#!/usr/bin/env python3
"""
astro-dashboard backend
即時執行 rockies-milkyway-scout skill 嘅 night_report.py（6 機位並行），
將結果以 JSON 俾手機 frontend。

原則（West 定）：
- 即時數據：cache 最多 10 分鐘，過期即重跑
- 分析唔到就要道歉：任何機位失敗，誠實回傳 error，絕不造假數據
"""
import asyncio
import json
import subprocess
import sys
import time
from datetime import date as date_cls
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

SKILL_SCRIPT = Path("/home/jarvis/.hermes/skills/photography/rockies-milkyway-scout/scripts/night_report.py")
LOCATIONS_JSON = SKILL_SCRIPT.parent.parent / "references" / "locations.json"
STATIC_DIR = Path(__file__).parent / "static"

CACHE_TTL = 600  # 10 分鐘
LOCATION_TIMEOUT = 120  # 每機位 subprocess 上限

app = FastAPI(title="astro-dashboard")
_cache = {}  # date_str -> (timestamp, payload)


def location_ids():
    return list(json.loads(LOCATIONS_JSON.read_text()).keys())


async def run_one(loc_id: str, date_str: str) -> dict:
    """跑一個機位；失敗回傳誠實嘅 error object，唔會 throw"""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(SKILL_SCRIPT),
            "--location", loc_id, "--date", date_str, "--json",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=LOCATION_TIMEOUT)
        if proc.returncode != 0:
            return {
                "location_id": loc_id, "error": True,
                "message": f"分析程式回傳錯誤（exit {proc.returncode}）",
                "detail": stderr.decode()[-500:],
            }
        return json.loads(stdout.decode())
    except asyncio.TimeoutError:
        return {"location_id": loc_id, "error": True,
                "message": f"分析超時（>{LOCATION_TIMEOUT}s）——可能天氣 API 冇回應"}
    except json.JSONDecodeError as e:
        return {"location_id": loc_id, "error": True,
                "message": f"分析輸出無法解析：{e}"}
    except Exception as e:
        return {"location_id": loc_id, "error": True, "message": f"未預期錯誤：{e}"}


async def build_report(date_str: str) -> dict:
    t0 = time.time()
    ids = location_ids()
    results = await asyncio.gather(*(run_one(i, date_str) for i in ids))
    ok = [r for r in results if not r.get("error")]
    failed = [r for r in results if r.get("error")]
    best = None
    if ok:
        scored = [r for r in ok if r.get("night", {}).get("grade_code") != "NO_DATA"]
        if scored:
            best = max(scored, key=lambda r: r["night"]["score"])
    return {
        "night_date": date_str,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed_seconds": round(time.time() - t0, 1),
        "locations": results,
        "best_location_id": best["location_id"] if best else None,
        "failed_count": len(failed),
    }


@app.get("/api/report")
async def report(date: str = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")):
    date_str = date or date_cls.today().isoformat()
    cached = _cache.get(date_str)
    if cached and time.time() - cached[0] < CACHE_TTL:
        payload = dict(cached[1])
        payload["cache_age_seconds"] = round(time.time() - cached[0])
        return JSONResponse(payload)
    payload = await build_report(date_str)
    _cache[date_str] = (time.time(), payload)
    payload["cache_age_seconds"] = 0
    return JSONResponse(payload)


@app.get("/api/health")
async def health():
    return {"ok": True, "skill_script_exists": SKILL_SCRIPT.exists()}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8788, log_level="warning")
