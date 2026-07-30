#!/usr/bin/env python3
"""Daylight photography conditions for the astro-dashboard.

This evaluates forecast conditions around geometric sunrise/sunset at the *exact
stored shooting-point coordinates*. It deliberately does not claim to know when
a mountain receives its first direct ray: terrain masking and the precise tripod
position need field observation or a terrain model.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
TZ = "America/Edmonton"
EVENTS = ("sunrise", "sunset")


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _wind_score(kmh: float) -> int:
    if kmh <= 6:
        return 100
    if kmh <= 10:
        return 82
    if kmh <= 15:
        return 58
    if kmh <= 20:
        return 32
    return 10


def _colour_cloud_score(high: float) -> int:
    """High cloud can catch colour; this is potential, not a fire-cloud forecast."""
    if high < 5:
        return 35
    if high < 15:
        return 55
    if high <= 60:
        return 90
    if high <= 80:
        return 68
    return 28


def _event_window(hourly: dict[str, list[Any]], event_time: str) -> list[int]:
    target = datetime.fromisoformat(event_time)
    indices = []
    for i, raw in enumerate(hourly["time"]):
        delta = abs((datetime.fromisoformat(raw) - target).total_seconds())
        if delta <= 65 * 60:
            indices.append(i)
    if indices:
        return indices
    return [min(range(len(hourly["time"])), key=lambda i: abs((datetime.fromisoformat(hourly["time"][i]) - target).total_seconds()))]


def _condition(event: str, event_time: str | None, hourly: dict[str, list[Any]]) -> dict[str, Any]:
    if not event_time:
        return {"event": event, "error": True, "message": "天文事件資料不足，無法評估"}
    ids = _event_window(hourly, event_time)
    get = lambda key: [_mean([float(hourly[key][i] or 0) for i in ids])][0]
    high = get("cloud_cover_high")
    low_mid = _mean([float(hourly["cloud_cover_low"][i] or 0) + float(hourly["cloud_cover_mid"][i] or 0) for i in ids])
    precip = max(float(hourly["precipitation_probability"][i] or 0) for i in ids)
    wind = get("wind_speed_10m")
    gust = max(float(hourly["wind_gusts_10m"][i] or 0) for i in ids)
    visibility = get("visibility") / 1000

    colour = _colour_cloud_score(high)
    clarity = max(0, min(100, 100 - low_mid))
    dry = max(0, min(100, 100 - precip))
    reflection = _wind_score(wind)
    score = round(colour * 0.35 + clarity * 0.40 + dry * 0.15 + reflection * 0.10)

    if score >= 80:
        label = "條件佳"
    elif score >= 65:
        label = "可嘗試"
    elif score >= 45:
        label = "條件普通"
    else:
        label = "不建議專程前往"

    notes: list[str] = []
    if high < 5:
        notes.append("高雲極少：畫面可乾淨，但火燒雲機會低")
    elif high <= 60:
        notes.append("存在可承接色彩的高雲，仍須看實際雲層位置與厚度")
    else:
        notes.append("高雲偏厚，可能只留下漫射光或遮住色彩")
    if low_mid > 30:
        notes.append("低／中雲偏多，山體與日出交界可能被遮擋")
    if precip >= 30:
        notes.append("降水機率偏高，先以安全與能見度為優先")
    if wind > 10:
        notes.append("風速偏高，倒影成功率下降")

    return {
        "event": event,
        "time": event_time[11:16],
        "score": score,
        "label": label,
        "components": {
            "sky_colour": round(colour),
            "clarity": round(clarity),
            "dryness": round(dry),
            "reflection": round(reflection),
        },
        "weather": {
            "high_cloud_pct": round(high),
            "low_mid_cloud_pct": round(low_mid),
            "precip_probability_pct": round(precip),
            "wind_kmh": round(wind),
            "gust_kmh": round(gust),
            "visibility_km": round(visibility, 1),
        },
        "notes": notes,
        "window_hours": [hourly["time"][i][11:16] for i in ids],
        "terrain_disclaimer": "這是幾何日出／日落附近的天氣條件評分；未建模山脊遮光，不能當作山體首光或金山保證。",
    }


def build_daylight(date_str: str) -> dict[str, Any]:
    data = json.loads((HERE / "spots.json").read_text(encoding="utf-8"))
    points = [p for p in data["points"] if p.get("daylight_events")]
    if not points:
        return {"date": date_str, "error": True, "message": "尚未設定日出／日落評估點"}

    params = {
        "latitude": ",".join(str(p["lat"]) for p in points),
        "longitude": ",".join(str(p["lon"]) for p in points),
        "timezone": TZ,
        "forecast_days": 7,
        "hourly": "cloud_cover_low,cloud_cover_mid,cloud_cover_high,precipitation_probability,visibility,wind_speed_10m,wind_gusts_10m",
        "daily": "sunrise,sunset",
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urlencode(params)
    try:
        with urlopen(url, timeout=30) as response:
            raw = json.load(response)
    except Exception as exc:
        return {"date": date_str, "error": True, "message": f"日出／日落天氣資料暫時無法取得：{exc}"}
    forecasts = raw if isinstance(raw, list) else [raw]
    if len(forecasts) != len(points):
        return {"date": date_str, "error": True, "message": "日出／日落天氣資料數量不完整"}

    result = []
    for point, fc in zip(points, forecasts):
        try:
            day_index = fc["daily"]["time"].index(date_str)
            events = {}
            terrain = point.get("terrain_first_light", {}).get(date_str, {})
            for event in point["daylight_events"]:
                condition = _condition(event, fc["daily"][event][day_index], fc["hourly"])
                if terrain.get(event):
                    condition["first_direct_light"] = terrain[event]
                    condition["first_direct_light_basis"] = terrain.get("basis", "現場／路徑判讀")
                events[event] = condition
            result.append({
                "id": point["id"], "name": point["name"], "mountain": point.get("name", ""),
                "lat": point["lat"], "lon": point["lon"], "purpose": point.get("purpose"),
                "season": point.get("season"), "caveat": point.get("caveat"),
                "events": events,
            })
        except (KeyError, ValueError, IndexError, TypeError) as exc:
            result.append({"id": point["id"], "name": point["name"], "error": True, "message": f"資料格式不完整：{exc}"})
    return {
        "date": date_str,
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        "method": "天空色彩潛力 35%／低中雲通透 40%／降水 15%／倒影風況 10%",
        "terrain_disclaimer": "日出／日落時間是幾何地平線事件。山體首光、金山與局部山脊遮擋需按日期、精確三腳架點與現場實測判定。",
        "points": result,
        "sources": "Open-Meteo 預報（各拍攝點精確座標）；日出／日落採 Open-Meteo 幾何事件",
    }
