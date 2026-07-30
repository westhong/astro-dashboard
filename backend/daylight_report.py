#!/usr/bin/env python3
"""Terrain-aware sunrise and sunset photography conditions.

The three visible indicators are deliberately separate:
- cloud: fire-cloud / colour-canvas potential and low-horizon opening;
- wind: reflection potential;
- light: geometric event plus terrain-cleared direct sunlight.

A score does not promise a fire cloud or golden mountain.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen

try:  # Package import for FastAPI; script import for build_report.py.
    from .terrain_light import DirectLightCalculator
except ImportError:
    from terrain_light import DirectLightCalculator

HERE = Path(__file__).resolve().parent
TZ = "America/Edmonton"


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


def _canvas_score(coverage: float) -> int:
    """A cloud layer's capacity to catch colour; not a fire-cloud prediction."""
    if coverage < 5:
        return 35
    if coverage < 15:
        return 55
    if coverage <= 60:
        return 90
    if coverage <= 80:
        return 68
    return 28


def _event_window(hourly: dict[str, list[Any]], centre: str) -> list[int]:
    target = datetime.fromisoformat(centre)
    indices = [
        i for i, raw in enumerate(hourly["time"])
        if abs((datetime.fromisoformat(raw) - target).total_seconds()) <= 65 * 60
    ]
    if indices:
        return indices
    return [min(range(len(hourly["time"])), key=lambda i: abs((datetime.fromisoformat(hourly["time"][i]) - target).total_seconds()))]


def _shift(time_text: str, minutes: int) -> str:
    anchor = datetime.fromisoformat(f"2000-01-01T{time_text}") + timedelta(minutes=minutes)
    return anchor.strftime("%H:%M")


def _condition(event: str, centre: str, hourly: dict[str, list[Any]]) -> dict[str, Any]:
    ids = _event_window(hourly, centre)
    avg = lambda key: _mean([float(hourly[key][i] or 0) for i in ids])
    high, middle, low = avg("cloud_cover_high"), avg("cloud_cover_mid"), avg("cloud_cover_low")
    precip = max(float(hourly["precipitation_probability"][i] or 0) for i in ids)
    wind = avg("wind_speed_10m")
    gust = max(float(hourly["wind_gusts_10m"][i] or 0) for i in ids)
    visibility = avg("visibility") / 1000

    cloud = round(_canvas_score(high) * 0.50 + _canvas_score(middle) * 0.25 + max(0, 100 - low) * 0.25)
    if precip >= 30:
        cloud = max(0, cloud - 25)
    reflection = _wind_score(wind)
    notes: list[str] = []
    if high < 5 and middle < 5:
        notes.append("高／中雲極少：天空可乾淨，但火燒雲機會低")
    elif low <= 30 and (15 <= high <= 60 or 10 <= middle <= 55):
        notes.append("雲層高度與低空開口具天空色彩潛力；仍須現場確認雲的位置與厚度")
    else:
        notes.append("雲層結構未達理想火燒雲型態，較可能是漫射光或平淡天空")
    if low > 30:
        notes.append("低雲偏多，山體與低空色彩可能被遮擋")
    if precip >= 30:
        notes.append("降水機率偏高，先以安全與能見度為優先")
    if wind > 10:
        notes.append("風速偏高，倒影成功率下降")
    return {
        "event": event,
        "centre_time": centre[11:16],
        "components": {"cloud": cloud, "wind": round(reflection)},
        "weather": {
            "high_cloud_pct": round(high), "mid_cloud_pct": round(middle), "low_cloud_pct": round(low),
            "precip_probability_pct": round(precip), "wind_kmh": round(wind),
            "gust_kmh": round(gust), "visibility_km": round(visibility, 1),
        },
        "notes": notes,
        "window_hours": [hourly["time"][i][11:16] for i in ids],
    }


def _light_for_event(calculator: DirectLightCalculator, point: dict[str, Any], date_str: str, event: str, geometric: str) -> dict[str, Any]:
    model: dict[str, Any] | None = None
    model_error: str | None = None
    try:
        model = calculator.direct_light_time(point["lat"], point["lon"], date_str, event, geometric)
    except Exception as exc:
        model_error = str(exc)
    override = point.get("terrain_first_light", {}).get(date_str, {}).get(event)
    if override:
        basis = point["terrain_first_light"][date_str].get("basis", "West 現場實測／路徑判讀")
        light = {"time": override, "basis": basis, "confidence": "field", "model": model}
    elif model:
        light = {"time": model["time"], "basis": model["basis"], "confidence": "model", "model": model}
    else:
        return {"error": True, "message": f"無法計算地形直射光：{model_error}"}
    if event == "sunrise":
        light["window"] = {"start": _shift(light["time"], -30), "end": _shift(light["time"], 90)}
        light["label"] = "首束直射光"
    else:
        light["window"] = {"start": _shift(light["time"], -90), "end": _shift(light["time"], 30)}
        light["label"] = "最後直射光"
    light["geometric_time"] = geometric[11:16]
    light["score"] = 100 if light["confidence"] == "field" else 78
    return light


def _label(score: int) -> str:
    if score >= 80:
        return "條件佳"
    if score >= 65:
        return "可嘗試"
    if score >= 45:
        return "條件普通"
    return "不建議專程前往"


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
    try:
        with urlopen("https://api.open-meteo.com/v1/forecast?" + urlencode(params), timeout=30) as response:
            raw = json.load(response)
    except Exception as exc:
        return {"date": date_str, "error": True, "message": f"日出／日落天氣資料暫時無法取得：{exc}"}
    forecasts = raw if isinstance(raw, list) else [raw]
    if len(forecasts) != len(points):
        return {"date": date_str, "error": True, "message": "日出／日落天氣資料數量不完整"}

    calculator = DirectLightCalculator()
    result = []
    for point, fc in zip(points, forecasts):
        try:
            index = fc["daily"]["time"].index(date_str)
            events: dict[str, Any] = {}
            for event in point["daylight_events"]:
                geometric = fc["daily"][event][index]
                light = _light_for_event(calculator, point, date_str, event, geometric)
                if light.get("error"):
                    events[event] = {"event": event, "error": True, "message": light["message"]}
                    continue
                centre = f"{date_str}T{light['time']}"
                condition = _condition(event, centre, fc["hourly"])
                condition["light"] = light
                condition["score"] = round(condition["components"]["cloud"] * 0.45 + condition["components"]["wind"] * 0.20 + light["score"] * 0.35)
                condition["label"] = _label(condition["score"])
                events[event] = condition
            result.append({
                "id": point["id"], "name": point["name"], "lat": point["lat"], "lon": point["lon"],
                "purpose": point.get("purpose"), "season": point.get("season"), "caveat": point.get("caveat"), "events": events,
            })
        except (KeyError, ValueError, IndexError, TypeError) as exc:
            result.append({"id": point["id"], "name": point["name"], "error": True, "message": f"資料格式不完整：{exc}"})
    return {
        "date": date_str,
        "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        "method": "雲 45%（色彩雲層與低空開口）／風 20%（倒影）／光 35%（地形直射光）",
        "terrain_disclaimer": "火燒雲無法保證。DEM 直射光為地形模型，精確腳架點、樹木與現場實測優先。",
        "points": result,
        "sources": "Open-Meteo（各拍攝點天氣）＋Skyfield 太陽位置＋AWS Terrain Tiles SRTM DEM",
    }
