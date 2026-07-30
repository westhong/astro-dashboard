#!/usr/bin/env python3
"""
build_report.py — GitHub Actions 用：跑 6 機位分析，寫 docs/report-{0,1,2}.json
順序執行（避免 Open-Meteo 429），失敗嘅機位誠實記錄 error，絕不造假。
v2.1.0：加入 alpenglow（金山機位）+ 各點日出日落時間。
"""
import datetime as dt
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


def sun_events_for(lat, lon, elev, y, m, d):
    """當地日期 d 嘅日出/日落時間（skyfield almanac，Edmonton 時區）"""
    sys.path.insert(0, str(HERE / "scripts"))
    from night_report import Astro
    from skyfield import almanac
    a = Astro(lat, lon, elev)
    f = almanac.sunrise_sunset(a.eph, a.topos_ll)
    nxt = date(y, m, d) + timedelta(days=1)
    t0 = a.ts.utc(y, m, d, 0)
    t1 = a.ts.utc(nxt.year, nxt.month, nxt.day, 12)
    times, events = almanac.find_discrete(t0, t1, f)
    sr = ss = None
    for t, e in zip(times, events):
        lt = t.utc_datetime().replace(tzinfo=dt.timezone.utc).astimezone(TZ)
        if lt.date() != date(y, m, d):
            continue
        if e == 1 and sr is None:
            sr = lt
        if e == 0:
            ss = lt
    return (sr.strftime("%H:%M") if sr else None, ss.strftime("%H:%M") if ss else None)


def build_spots(date_str):
    y, m, d = map(int, date_str.split("-"))
    data = json.loads((HERE / "spots.json").read_text(encoding="utf-8"))
    points = []
    for p in data["points"]:
        try:
            sr, ss = sun_events_for(p["lat"], p["lon"], p.get("elev_m", 1500), y, m, d)
        except Exception:
            sr = ss = None
        q = dict(p)
        q["sunrise"] = sr
        q["sunset"] = ss
        q["gmaps"] = f"https://www.google.com/maps/dir/?api=1&destination={p['lat']},{p['lon']}"
        points.append(q)
    return points


def build_daylight_report(date_str):
    """日出／日落評分獨立於銀河評分；失敗時誠實保留錯誤物件。"""
    sys.path.insert(0, str(HERE))
    from daylight_report import build_daylight
    return build_daylight(date_str)


def main():
    locs = json.loads((HERE / "references" / "locations.json").read_text())
    today = dt.datetime.now(TZ).date()
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
        spots = build_spots(date_str)
        daylight = build_daylight_report(date_str)
        payload = {
            "version": (HERE.parent / "VERSION").read_text().strip(),
            "night_date": date_str,
            "generated_at": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.time() - t0, 1),
            "locations": results,
            "best_location_id": best["location_id"] if best else None,
            "failed_count": len(results) - len(ok),
            "spots": spots,
            "daylight": daylight,
        }
        out = DOCS / f"report-{offset}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"[{date_str}] ok={len(ok)} failed={len(results)-len(ok)} best={payload['best_location_id']} → {out}")

    # 同步最新 frontend 去 docs/（index.html + manifest + icons）
    import shutil
    (DOCS / "index.html").write_text(STATIC.read_text())
    for extra in ["manifest.webmanifest"]:
        src = HERE.parent / "static" / extra
        if src.exists():
            shutil.copy2(src, DOCS / extra)
    icons_src = HERE.parent / "static" / "icons"
    if icons_src.exists():
        shutil.copytree(icons_src, DOCS / "icons", dirs_exist_ok=True)
    print("docs/ static assets updated")


if __name__ == "__main__":
    main()
