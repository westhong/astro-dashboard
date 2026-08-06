#!/usr/bin/env python3
"""R2 — GOES-18 衛星實測雲量修正（對照 fc.nekolens.tw「觀測/預報」修正）。

流程：build_report 完成 report-0 後，用系統 Python（有 xarray/netCDF4/pyproj）
 subprocess 執行本檔。佢會：
  1. 攞最新 GOES-18 ACHAF Full Disk frame（AWS noaa-goes18，10 分鐘一掃）
  2. 對今日（report-0）每個日間事件，喺太陽方位 ±25°、40–200km 扇區
     採樣雲頂 retrieval → 實測雲量比例
  3. 同預報地平線阻塞（低雲+0.5×中雲）對照 → 雲分修正（cut -15 / boost +12 封頂）
  4. 寫返入 docs/report-0.json：event["satellite"] + 修正後 score，並加 note

誠實失敗：任何一步失敗 → report-0 完全唔郁，print 原因（上層 log 睇到）。
絕唔准用推測數字冒充衛星實測。
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import requests
import xarray as xr
from pyproj import Geod, Transformer

HERE = Path(__file__).resolve().parent
REPORT = HERE.parent / "docs" / "report-0.json"
CACHE = Path.home() / "fire-sky-research" / "goes"

BUCKET = "https://noaa-goes18.s3.amazonaws.com"
ABI_PROJ = ("+proj=geos +h=35786023 +lon_0=-137.2 +sweep=x "
            "+R=6378137 +rf=298.2572221 +units=m +no_defs")

SECTOR_HALF_AZ = 25.0     # 太陽方位兩邊各 25°
SECTOR_MIN_KM, SECTOR_MAX_KM, SECTOR_STEP_KM = 40.0, 200.0, 20.0
AZ_STEP = 5.0
CUT_MAX, BOOST_MAX = -15, 12


def list_frames(year: int, doy: int, hour: int) -> list[str]:
    prefix = f"ABI-L2-ACHAF/{year}/{doy:03d}/{hour:02d}/"
    url = f"{BUCKET}/?list-type=2&prefix={prefix}&max-keys=20"
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    keys = [k.split("</Key>")[0] for k in r.text.split("<Key>")[1:]]
    return [k for k in keys if k.endswith(".nc")]


def latest_frame() -> str:
    now = datetime.now(timezone.utc)
    for back in (0, 1, 2):
        t = now.timestamp() - back * 3600
        dt = datetime.fromtimestamp(t, timezone.utc)
        keys = list_frames(dt.year, dt.timetuple().tm_yday, dt.hour)
        if keys:
            return keys[-1]
    raise RuntimeError("最近 3 個 UTC 小時都冇 GOES-18 frame")


def download(key: str) -> Path:
    CACHE.mkdir(parents=True, exist_ok=True)
    path = CACHE / os.path.basename(key)
    if path.exists() and path.stat().st_size > 100000:
        return path
    url = f"{BUCKET}/{key}"
    r = requests.get(url, timeout=180, stream=True)
    r.raise_for_status()
    tmp = str(path) + ".part"
    with open(tmp, "wb") as f:
        for chunk in r.iter_content(1 << 20):
            f.write(chunk)
    os.replace(tmp, path)
    return path


def sector_points(lat: float, lon: float, az: float) -> tuple[np.ndarray, np.ndarray]:
    geod = Geod(ellps="WGS84")
    pts = []
    for da in np.arange(-SECTOR_HALF_AZ, SECTOR_HALF_AZ + AZ_STEP, AZ_STEP):
        dists = np.arange(SECTOR_MIN_KM, SECTOR_MAX_KM + SECTOR_STEP_KM, SECTOR_STEP_KM) * 1000.0
        glon, glat, _ = geod.fwd(
            np.full(len(dists), lon), np.full(len(dists), lat),
            np.full(len(dists), az + da), dists)
        pts.append(np.column_stack([glon, glat]))
    arr = np.vstack(pts)
    return arr[:, 0], arr[:, 1]


def sector_cloud_fraction(frame: Path, glon: np.ndarray, glat: np.ndarray) -> float:
    with xr.open_dataset(frame) as ds:
        cth = ds["HT"].values
        dqf = ds["DQF"].values if "DQF" in ds else None
        xg, yg = ds["x"].values, ds["y"].values
        h_sat = float(ds.attrs.get("nominal_satellite_height", 35786.023)) * 1000.0
    t = Transformer.from_crs("EPSG:4326", ABI_PROJ, always_xy=True)
    xm, ym = t.transform(glon, glat)
    x_ang, y_ang = np.arctan(xm / h_sat), np.arctan(ym / h_sat)
    dx, dy = float(xg[1] - xg[0]), float(yg[1] - yg[0])
    xi = np.clip(((x_ang - xg[0]) / dx).round().astype(int), 0, cth.shape[1] - 1)
    yi = np.clip(((y_ang - yg[0]) / dy).round().astype(int), 0, cth.shape[0] - 1)
    vals = cth[yi, xi].astype(float)
    if dqf is not None:
        vals = np.where(dqf[yi, xi] > 0, np.nan, vals)
    # HT <= -1 / NaN = 無 retrieval = 晴空（research pitfall 7）；有效 retrieval = 有雲
    cloudy = np.count_nonzero((~np.isnan(vals)) & (vals > -1))
    return cloudy / len(vals)


def main() -> int:
    if not REPORT.exists():
        print("report-0 不存在，skip")
        return 0
    payload = json.loads(REPORT.read_text(encoding="utf-8"))
    dl = payload.get("daylight") or {}
    if dl.get("error"):
        print("daylight error，skip")
        return 0

    try:
        key = latest_frame()
        frame = download(key)
    except Exception as exc:
        print(f"GOES frame 取得失敗（唔郁 report）：{exc}")
        return 0

    corrected = 0
    for point in dl.get("points", []):
        if point.get("error"):
            continue
        for ev, cond in (point.get("events") or {}).items():
            if cond.get("error"):
                continue
            az = (cond.get("weather") or {}).get("horizon_azimuth_deg")
            hl = (cond.get("weather") or {}).get("horizon_low_pct")
            hm = (cond.get("weather") or {}).get("horizon_mid_pct")
            if az is None or hl is None:
                cond["satellite"] = {"status": "skipped", "reason": "缺地平線方位或預報雲量"}
                continue
            fc_block = (hl + 0.5 * (hm or 0)) / 100.0
            try:
                glon, glat = sector_points(point["lat"], point["lon"], az)
                obs = sector_cloud_fraction(frame, glon, glat)
            except Exception as exc:
                cond["satellite"] = {"status": "skipped", "reason": f"扇區採樣失敗：{exc}"}
                continue
            delta = round((fc_block - obs) * 30)
            delta = max(CUT_MAX, min(BOOST_MAX, delta))
            raw_cloud = cond["components"]["cloud"]
            raw_score = cond["score"]
            new_cloud = max(0, min(100, raw_cloud + delta))
            new_score = round(new_cloud * 0.50 + cond["components"]["smoke"] * 0.30 + cond["components"]["wind"] * 0.20)
            direction = "比預報多" if obs > fc_block else "比預報少"
            cond["satellite"] = {
                "status": "ok",
                "frame": os.path.basename(key),
                "observed_sector_cloud_pct": round(obs * 100),
                "forecast_horizon_block_pct": round(fc_block * 100),
                "cloud_delta": delta,
                "raw_score": raw_score,
                "adjusted_score": new_score,
            }
            cond["components"]["cloud"] = new_cloud
            cond["score"] = new_score
            cond.setdefault("notes", []).append(
                f"衛星實測太陽方向雲量{direction}（觀測 {round(obs*100)}% vs 預報 {round(fc_block*100)}%），雲分修正 {delta:+d}（原始評分 {raw_score} → 修正後 {new_score}）")
            corrected += 1

    payload["satellite_correction"] = {
        "status": "ok" if corrected else "unavailable",
        "frame": os.path.basename(key),
        "corrected_events": corrected,
        "note": "GOES-18 ACHAF 實測雲頂 vs 預報地平線阻塞，只修正今日事件；10km 解像度，屬趨勢校正唔係精確測量。",
    }
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"衛星修正完成：{corrected} 個事件（frame {os.path.basename(key)}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
