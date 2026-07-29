#!/usr/bin/env python3
"""
build_report.py — GitHub Actions 用：跑 6 機位分析，寫 docs/report-{0,1,2}.json
順序執行（避免 Open-Meteo 429），失敗嘅機位誠實記錄 error，絕不造假。
"""
import json
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "scripts" / "night_report.py"
DOCS = HERE.parent / "docs"
STATIC = HERE.parent / "static" / "index.html"
TZ = ZoneInfo("America/Edmonton")
TIMEOUT = 180


def run_one(loc_id, date_str):
    try:
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "--location", loc_id, "--date", date_str, "--json"],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        if p.returncode != 0:
            detail = p.stderr[-400:]
            msg = ("天氣數據商暫時限流，下次更新會再試" if "429" in detail
                   else f"分析程式錯誤（exit {p.returncode}）")
            return {"location_id": loc_id, "error": True, "message": msg, "detail": detail}
        return json.loads(p.stdout)
    except subprocess.TimeoutExpired:
        return {"location_id": loc_id, "error": True, "message": "分析超時——天氣數據服務可能冇回應"}
    except Exception as e:
        return {"location_id": loc_id, "error": True, "message": f"未預期錯誤：{e}"}


def main():
    locs = json.loads((HERE / "references" / "locations.json").read_text())
    today = date.today()  # Actions runner 用 UTC 都冇所謂，date 以 Edmonton 為準：
    today = __import__("datetime").datetime.now(TZ).date()
    DOCS.mkdir(exist_ok=True)

    for offset in range(3):
        date_str = (today + timedelta(days=offset)).isoformat()
        t0 = time.time()
        results = [run_one(lid, date_str) for lid in locs]
        # 失敗重試一次
        for i, r in enumerate(results):
            if r.get("error"):
                time.sleep(15)
                retry = run_one(r["location_id"], date_str)
                if not retry.get("error"):
                    results[i] = retry
        ok = [r for r in results if not r.get("error")]
        best = None
        scored = [r for r in ok if r.get("night", {}).get("grade_code") != "NO_DATA"]
        if scored:
            best = max(scored, key=lambda r: r["night"]["score"])
        payload = {
            "version": (HERE.parent / "VERSION").read_text().strip(),
            "night_date": date_str,
            "generated_at": __import__("datetime").datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S MDT"),
            "elapsed_seconds": round(time.time() - t0, 1),
            "locations": results,
            "best_location_id": best["location_id"] if best else None,
            "failed_count": len(results) - len(ok),
        }
        out = DOCS / f"report-{offset}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"[{date_str}] ok={len(ok)} failed={len(results)-len(ok)} best={payload['best_location_id']} → {out}")

    # 同步最新 frontend 去 docs/
    (DOCS / "index.html").write_text(STATIC.read_text())
    print("docs/index.html updated")


if __name__ == "__main__":
    main()
