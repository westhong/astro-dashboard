#!/usr/bin/env python3
"""MET Norway Locationforecast 2.0 (complete) → Open-Meteo 形狀轉換器。

v2.25.0：Open-Meteo forecast API 失效（例如每日 quota 用盡 429）時嘅後備數據源。
免 key、CC BY 4.0、20 req/s 上限（要帶 identifying User-Agent）。
加拿大地區底層係 ECMWF IFS 9km——即係話後備模式嘅預報物理同源於 Open-Meteo 嘅 ecmwf_ifs025。

欄位映射：
  air_temperature          → temperature_2m（°C，相同）
  relative_humidity        → relative_humidity_2m（%，相同）
  dew_point_temperature    → dew_point_2m
  cloud_area_fraction      → cloud_cover（%，相同）
  cloud_area_fraction_low  → cloud_cover_low      （complete endpoint 先有）
  cloud_area_fraction_medium → cloud_cover_mid
  cloud_area_fraction_high → cloud_cover_high
  wind_speed (m/s)         → wind_speed_10m（×3.6 → km/h）
  wind_from_direction      → wind_direction_10m（convention 相同）
  （缺）wind_gusts_10m / visibility / freezing_level_height / precipitation_probability → None

時間軸：MET Norway 回 UTC ISO；Open-Meteo（timezone=America/Edmonton）回本地 naive ISO。
下游全部用 naive local 時間做 key，所以呢度要轉做 America/Edmonton naive。
MET Norway 頭 ~63 小時逐小時，之後 6 小時一格——遠期（offset 3-4）會稀疏，
下游 _event_window 會搵最近 slot，屬誠實降級，唔插值造假。

daily sunrise/sunset：用 skyfield 本地計算（同 build_report.sun_events_for 同一做法）。
"""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

TZ = ZoneInfo("America/Edmonton")
API = "https://api.met.no/weatherapi/locationforecast/2.0/complete"
UA = "astro-dashboard/2.25 (github.com/westhong/astro-dashboard; Calgary personal use)"

_EPHEM = None


def _sun_events_local(lat: float, lon: float, d: date) -> tuple[str | None, str | None]:
    """當地日期 d 嘅幾何日出/日落（naive local ISO 字串），同 Open-Meteo daily 欄位對齊。"""
    global _EPHEM
    from skyfield import almanac
    from skyfield.api import load, wgs84

    if _EPHEM is None:
        _EPHEM = load("de421.bsp")
    ts = load.timescale()
    topos = wgs84.latlon(lat, lon)
    f = almanac.sunrise_sunset(_EPHEM, topos)
    nxt = d + timedelta(days=1)
    t0 = ts.utc(d.year, d.month, d.day, 0)
    t1 = ts.utc(nxt.year, nxt.month, nxt.day, 12)
    times, events = almanac.find_discrete(t0, t1, f)
    sr = ss = None
    for t, e in zip(times, events):
        lt = t.utc_datetime().replace(tzinfo=ZoneInfo("UTC")).astimezone(TZ)
        if lt.date() != d:
            continue
        if e == 1 and sr is None:
            sr = lt
        if e == 0:
            ss = lt
    fmt = lambda x: x.strftime("%Y-%m-%dT%H:%M") if x else None
    return fmt(sr), fmt(ss)


def fetch_one(lat: float, lon: float, forecast_days: int = 5) -> dict[str, Any]:
    """單點 fetch，回 Open-Meteo forecast API 形狀嘅 dict。"""
    url = f"{API}?{urlencode({'lat': round(lat, 4), 'lon': round(lon, 4)})}"
    req = Request(url, headers={"User-Agent": UA})
    with urlopen(req, timeout=30) as resp:
        raw = json.load(resp)

    today = datetime.now(TZ).date()
    last_day = today + timedelta(days=forecast_days - 1)
    times: list[str] = []
    cols: dict[str, list[Any]] = {k: [] for k in (
        "temperature_2m", "relative_humidity_2m", "dew_point_2m",
        "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
        "wind_speed_10m", "wind_direction_10m",
        "wind_gusts_10m", "visibility", "freezing_level_height",
        "precipitation_probability",
    )}
    for slot in raw["properties"]["timeseries"]:
        t_utc = datetime.fromisoformat(slot["time"].replace("Z", "+00:00"))
        t_local = t_utc.astimezone(TZ).replace(tzinfo=None)
        if t_local.date() > last_day:
            continue
        det = slot["data"]["instant"]["details"]
        times.append(t_local.strftime("%Y-%m-%dT%H:%M"))
        cols["temperature_2m"].append(det.get("air_temperature"))
        cols["relative_humidity_2m"].append(det.get("relative_humidity"))
        cols["dew_point_2m"].append(det.get("dew_point_temperature"))
        cols["cloud_cover"].append(det.get("cloud_area_fraction"))
        cols["cloud_cover_low"].append(det.get("cloud_area_fraction_low"))
        cols["cloud_cover_mid"].append(det.get("cloud_area_fraction_medium"))
        cols["cloud_cover_high"].append(det.get("cloud_area_fraction_high"))
        ws = det.get("wind_speed")
        cols["wind_speed_10m"].append(round(ws * 3.6, 1) if ws is not None else None)
        cols["wind_direction_10m"].append(det.get("wind_from_direction"))
        cols["wind_gusts_10m"].append(None)      # 全球區域冇 gust（MET 官方文件確認）
        cols["visibility"].append(None)           # 冇 visibility
        cols["freezing_level_height"].append(None)
        cols["precipitation_probability"].append(None)  # 得 precipitation amount，冇 probability

    daily_time, daily_sr, daily_ss = [], [], []
    for i in range(forecast_days):
        d = today + timedelta(days=i)
        sr, ss = _sun_events_local(lat, lon, d)
        daily_time.append(d.isoformat())
        daily_sr.append(sr)
        daily_ss.append(ss)

    return {
        "latitude": lat,
        "longitude": lon,
        "timezone": "America/Edmonton",
        "_source": "met_norway",
        "hourly": {"time": times, **cols},
        "daily": {"time": daily_time, "sunrise": daily_sr, "sunset": daily_ss},
    }


def fetch_batch(coords: list[tuple[float, float]], forecast_days: int = 5) -> list[dict[str, Any]]:
    """MET Norway 冇 multi-location batch——逐點 fetch（8 點 = 8 calls，20 req/s 內綽綽有餘）。"""
    import time as _time

    out = []
    for i, (lat, lon) in enumerate(coords):
        if i:
            _time.sleep(0.3)  # 禮貌間隔，遠低於 20 req/s
        out.append(fetch_one(lat, lon, forecast_days))
    return out
