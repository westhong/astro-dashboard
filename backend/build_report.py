#!/usr/bin/env python3
"""
build_report.py — 跑全部銀河機位分析，寫 docs/report-{0,1,2}.json
天氣與空氣質素以多座標批次取得，失敗嘅機位誠實記錄 error，絕不造假。
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


def run_all(loc_ids, date_str):
    """單一 subprocess + 兩個多座標 API requests 跑完整機位集。"""
    try:
        p = subprocess.run(
            [sys.executable, str(SCRIPT), "--location", "all", "--date", date_str, "--json"],
            capture_output=True, text=True, timeout=TIMEOUT,
        )
        if p.returncode != 0:
            detail = p.stderr[-400:]
            msg = ("天氣數據商暫時限流，下次更新會再試" if "429" in detail
                   else f"批次分析程式錯誤（exit {p.returncode}）")
            return [{"location_id": lid, "error": True, "message": msg, "detail": detail}
                    for lid in loc_ids]
        payload = json.loads(p.stdout)
        results = payload.get("locations") or []
        if len(results) != len(loc_ids):
            raise ValueError(f"批次分析數量不完整：預期 {len(loc_ids)}，收到 {len(results)}")
        return results
    except subprocess.TimeoutExpired:
        return [{"location_id": lid, "error": True, "message": "批次分析超時——天氣數據服務可能沒有回應"}
                for lid in loc_ids]
    except Exception as e:
        return [{"location_id": lid, "error": True, "message": f"批次未預期錯誤：{e}"}
                for lid in loc_ids]


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
    daylight_full: dict[str, dict] = {}

    for offset in range(3):
        date_str = (today + timedelta(days=offset)).isoformat()
        t0 = time.time()
        results = run_all(list(locs), date_str)
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
        daylight_full[date_str] = daylight
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

    # R7（v2.22.0 改版）：第 4／5 日產生完整淨日間 report（前端加開日間 tab），
    # 唔再整迷你展望條。遠期標示由前端負責。
    version = (HERE.parent / "VERSION").read_text().strip()
    for offset in range(3, 5):
        ds = (today + timedelta(days=offset)).isoformat()
        t0 = time.time()
        try:
            dl = build_daylight_report(ds)
        except Exception as exc:
            dl = {"date": ds, "error": True, "message": f"遠期日間資料失敗：{exc}"}
        daylight_full[ds] = dl
        payload = {
            "version": version,
            "night_date": ds,
            "generated_at": dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %Z"),
            "generated_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "elapsed_seconds": round(time.time() - t0, 1),
            "locations": [],
            "best_location_id": None,
            "failed_count": 0,
            "spots": build_spots(ds),
            "daylight": dl,
            "far": True,
        }
        out = DOCS / f"report-{offset}.json"
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"[{ds}] 淨日間遠期 report → {out}")

    # R1：日間評分歷史歸檔（repo 外 ~/astro-history/，避免觸發 cron clean-tree gate）+ 回測
    sys.path.insert(0, str(HERE))
    try:
        from backtest import archive_scores, run_backtest
        archive_scores(daylight_full)
        backtest = run_backtest()
    except Exception as exc:
        backtest = {"error": True, "message": f"回測計算失敗：{exc}"}

    # 注入 report-0（評分往績只屬「今日」視角）
    r0_path = DOCS / "report-0.json"
    r0 = json.loads(r0_path.read_text(encoding="utf-8"))
    r0.pop("daylight_week", None)  # v2.22.0 起停用迷你展望條
    r0["backtest"] = backtest
    r0_path.write_text(json.dumps(r0, ensure_ascii=False, indent=2), encoding="utf-8")
    print("backtest injected into report-0")

    # R2：GOES-18 衛星實測雲量修正（需要 xarray/netCDF4/pyproj —— astro venv 冇，
    # 用 PATH 上嘅系統 Python subprocess；任何失敗 report-0 保留原樣，唔阻塞 build）
    import shutil
    goes_py = (shutil.which("python") or shutil.which("python3")
               or r"C:\Users\West\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe")
    try:
        r = subprocess.run(
            [goes_py, str(HERE / "goes_correction.py")],
            capture_output=True, text=True, timeout=420, stdin=subprocess.DEVNULL,
        )
        print("[goes]", (r.stdout or "").strip()[:300], (r.stderr or "").strip()[:200])
    except Exception as exc:
        print(f"[goes] 修正失敗（report 保留原樣）：{exc}")

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
