#!/usr/bin/env python3
"""Terrain-aware sunrise and sunset photography conditions.

The three visible indicators are deliberately separate:
- cloud: colour-canvas potential multiplied by the horizon-opening factor
  (low/mid cloud measured ~100 km toward the Sun at the event);
- smoke: PM2.5 atmospheric clarity for colour transmission;
- wind: reflection potential.

Terrain direct-light times remain displayed as information, but no longer
contribute to the score (geometric + terrain times are already shown).
A score is a fire-cloud probability heuristic, never a guarantee.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import urlopen
from zoneinfo import ZoneInfo

try:  # Package import for FastAPI; script import for build_report.py.
    from .terrain_light import DirectLightCalculator
except ImportError:
    from terrain_light import DirectLightCalculator

HERE = Path(__file__).resolve().parent
TZ = "America/Edmonton"
LOCAL = ZoneInfo(TZ)
HORIZON_OFFSET_KM = 100.0
# ECMWF 模型名：必須用 ecmwf_ifs025。舊名 ecmwf_ifs04 已廢棄——API 唔報錯，
# 靜靜雞全部回 null（2026-08-05 實測，連蘇黎世都係），禁用。
ECMWF_MODEL = "ecmwf_ifs025"
GFS_MODEL = "gfs_seamless"


def _confidence_level(spread: float | None) -> str | None:
    """三模型雲量分歧度 → 信心等級（對照 fc.nekolens.tw 嘅 ECMWF/GFS/ICON 思路）。
    spread = 各模型事件窗口平均總雲量嘅 max-min（百分點）。"""
    if spread is None:
        return None
    if spread <= 20:
        return "高"
    if spread <= 40:
        return "中"
    return "低"


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


def _wind_tier(kmh: float) -> int:
    """評分級別（6/10/15/20 km/h 邊界）——兩模型落入不同級別即視為分歧。"""
    if kmh <= 6:
        return 0
    if kmh <= 10:
        return 1
    if kmh <= 15:
        return 2
    if kmh <= 20:
        return 3
    return 4


def _smoke_score(pm: float | None) -> int:
    """煙塵分，沿用 rockies-milkyway-scout 嘅 PM2.5 表。無資料 → 60（不確定）。"""
    if pm is None:
        return 60
    if pm <= 5:
        return 100
    if pm <= 10:
        return 90
    if pm <= 15:
        return 75
    if pm <= 25:
        return 55
    if pm <= 35:
        return 35
    if pm <= 55:
        return 18
    return 5


def _gap_score(block: float | None) -> int | None:
    """地平線開口分。block = 太陽方向 100km 外嘅低雲 + 0.5×中雲（%）。"""
    if block is None:
        return None
    if block <= 10:
        return 100
    if block <= 25:
        return 80
    if block <= 40:
        return 55
    if block <= 60:
        return 30
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


def _offset_point(lat: float, lon: float, azimuth_deg: float, dist_km: float = HORIZON_OFFSET_KM) -> tuple[float, float]:
    """Destination point dist_km away along azimuth (spherical Earth)."""
    radius = 6371.0
    delta = dist_km / radius
    brg = math.radians(azimuth_deg)
    lat1, lon1 = math.radians(lat), math.radians(lon)
    lat2 = math.asin(math.sin(lat1) * math.cos(delta) + math.cos(lat1) * math.sin(delta) * math.cos(brg))
    lon2 = lon1 + math.atan2(
        math.sin(brg) * math.sin(delta) * math.cos(lat1),
        math.cos(delta) - math.sin(lat1) * math.sin(lat2),
    )
    return round(math.degrees(lat2), 4), round(math.degrees(lon2), 4)


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


def _wind_curve(
    hourly: dict[str, list[Any]],
    ecm_wind_by_time: dict[str, float | None] | None,
    centre: str,
) -> list[dict[str, Any]]:
    """逐小時雙模型風速：直射光前 1 小時 → 後 2.5 小時（日落後風勢崩塌係決策關鍵）。"""
    target = datetime.fromisoformat(centre)
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(hourly["time"]):
        minutes = (datetime.fromisoformat(raw) - target).total_seconds() / 60
        if -60 <= minutes <= 150:
            best = hourly["wind_speed_10m"][i]
            ecm = ecm_wind_by_time.get(raw) if ecm_wind_by_time else None
            out.append({
                "t": raw[11:16],
                "best": (round(float(best), 1) if best is not None else None),
                "ecmwf": (round(float(ecm), 1) if ecm is not None else None),
            })
    return out


def _calm_from(curve: list[dict[str, Any]], key: str) -> str | None:
    """曲線內首次 ≤6 km/h（鏡面門檻）嘅時間；未達 → None。"""
    for entry in curve:
        v = entry.get(key)
        if v is not None and v <= 6:
            return entry["t"]
    return None


def _condition(
    event: str,
    centre: str,
    hourly: dict[str, list[Any]],
    aq_hourly: dict[str, list[Any]] | None,
    horizon_hourly: dict[str, list[Any]] | None,
    horizon_azimuth: float | None,
    ecmwf_hourly: dict[str, list[Any]] | None = None,
) -> dict[str, Any]:
    ids = _event_window(hourly, centre)
    avg = lambda key: _mean([float(hourly[key][i] or 0) for i in ids])
    high, middle, low = avg("cloud_cover_high"), avg("cloud_cover_mid"), avg("cloud_cover_low")
    precip = max(float(hourly["precipitation_probability"][i] or 0) for i in ids)
    wind = avg("wind_speed_10m")
    gust = max(float(hourly["wind_gusts_10m"][i] or 0) for i in ids)
    visibility = avg("visibility") / 1000

    # 煙塵（CAMS PM2.5／US AQI，同一事件窗口平均）
    pm_avg = aqi_avg = None
    if aq_hourly:
        aq_ids = _event_window(aq_hourly, centre)
        pm_vals = [aq_hourly["pm2_5"][i] for i in aq_ids if aq_hourly["pm2_5"][i] is not None]
        aqi_vals = [aq_hourly["us_aqi"][i] for i in aq_ids if aq_hourly["us_aqi"][i] is not None]
        pm_avg = _mean([float(v) for v in pm_vals]) if pm_vals else None
        aqi_avg = _mean([float(v) for v in aqi_vals]) if aqi_vals else None
    smoke = _smoke_score(pm_avg)

    # 地平線開口（太陽方向 100km 外嘅低／中雲）
    horizon_low = horizon_mid = gap = None
    if horizon_hourly:
        h_ids = _event_window(horizon_hourly, centre)
        horizon_low = _mean([float(horizon_hourly["cloud_cover_low"][i] or 0) for i in h_ids])
        horizon_mid = _mean([float(horizon_hourly["cloud_cover_mid"][i] or 0) for i in h_ids])
        gap = _gap_score(horizon_low + 0.5 * horizon_mid)

    cloud = round(_canvas_score(high) * 0.50 + _canvas_score(middle) * 0.25 + max(0, 100 - low) * 0.25)
    if precip >= 30:
        cloud = max(0, cloud - 25)
    if gap is not None:
        cloud = round(cloud * gap / 100)

    # ECMWF 雙模型風速對比（R1–R5）：評分取兩模型較差者（保守），
    # 落入不同評分級別即標記分歧；ECMWF 缺失 → 退回單模型並明確標示。
    ecm_wind_by_time: dict[str, float | None] | None = None
    ecm_gust_by_time: dict[str, float | None] | None = None
    if ecmwf_hourly and ecmwf_hourly.get("time"):
        ecm_wind_by_time = {t: v for t, v in zip(ecmwf_hourly["time"], ecmwf_hourly.get("wind_speed_10m") or [])}
        ecm_gust_by_time = {t: v for t, v in zip(ecmwf_hourly["time"], ecmwf_hourly.get("wind_gusts_10m") or [])}
    ecm_vals = [float(ecm_wind_by_time[hourly["time"][i]]) for i in ids
                if ecm_wind_by_time and ecm_wind_by_time.get(hourly["time"][i]) is not None]
    ecm_wind = _mean(ecm_vals) if ecm_vals else None
    ecm_gust_vals = [float(ecm_gust_by_time[hourly["time"][i]]) for i in ids
                     if ecm_gust_by_time and ecm_gust_by_time.get(hourly["time"][i]) is not None]
    ecm_gust = max(ecm_gust_vals) if ecm_gust_vals else None
    curve = _wind_curve(hourly, ecm_wind_by_time, centre)
    ecmwf_missing = ecm_wind is None
    divergent = (not ecmwf_missing) and _wind_tier(wind) != _wind_tier(ecm_wind)
    scoring_wind = wind if ecmwf_missing else max(wind, ecm_wind)
    reflection = _wind_score(scoring_wind)
    wind_detail = {
        "ecmwf_missing": ecmwf_missing,
        "best_mean": round(wind, 1),
        "best_gust": round(gust),
        "ecmwf_mean": (round(ecm_wind, 1) if ecm_wind is not None else None),
        "ecmwf_gust": (round(ecm_gust) if ecm_gust is not None else None),
        "range": ([round(min(wind, ecm_wind), 1), round(max(wind, ecm_wind), 1)] if not ecmwf_missing else None),
        "gust_range": ([round(min(gust, ecm_gust)), round(max(gust, ecm_gust))] if (not ecmwf_missing and ecm_gust is not None) else None),
        "divergent": divergent,
        "scoring": "conservative",
        "calm_best": _calm_from(curve, "best"),
        "calm_ecmwf": (None if ecmwf_missing else _calm_from(curve, "ecmwf")),
        "curve": curve,
    }

    notes: list[str] = []
    if high < 5 and middle < 5:
        notes.append("高／中雲極少：天空可乾淨，但火燒雲機會低")
    elif low <= 30 and (15 <= high <= 60 or 10 <= middle <= 55):
        notes.append("雲層高度與低空開口具天空色彩潛力；仍須現場確認雲的位置與厚度")
    else:
        notes.append("雲層結構未達理想火燒雲型態，較可能是漫射光或平淡天空")
    if low > 30:
        notes.append("低雲偏多，山體與低空色彩可能被遮擋")
    if gap is None:
        notes.append("地平線開口資料暫缺，雲分未作開口修正")
    elif gap <= 30:
        notes.append("太陽方向地平線雲量偏高，霞光難以照射雲底")
    if pm_avg is None:
        notes.append("煙塵資料暫缺，煙分以不確定值計算")
    elif pm_avg > 35:
        notes.append("煙塵偏高，霞光色彩可能明顯受抑制")
    if precip >= 30:
        notes.append("降水機率偏高，先以安全與能見度為優先")
    if scoring_wind > 10:
        notes.append("風速偏高，倒影成功率下降")
    if ecmwf_missing:
        notes.append("ECMWF 風速資料暫缺，風分只按預設模型計算")
    elif divergent:
        notes.append(f"兩模型風速預報分歧（預設 {round(wind)} vs ECMWF {round(ecm_wind)} km/h），風分採較保守值")
    return {
        "event": event,
        "centre_time": centre[11:16],
        "components": {"cloud": cloud, "wind": round(reflection), "smoke": smoke},
        "weather": {
            "high_cloud_pct": round(high), "mid_cloud_pct": round(middle), "low_cloud_pct": round(low),
            "precip_probability_pct": round(precip), "wind_kmh": round(wind),
            "gust_kmh": round(gust), "visibility_km": round(visibility, 1),
            "pm2_5": (round(pm_avg, 1) if pm_avg is not None else None),
            "us_aqi": (round(aqi_avg, 0) if aqi_avg is not None else None),
            "horizon_low_pct": (round(horizon_low) if horizon_low is not None else None),
            "horizon_mid_pct": (round(horizon_mid) if horizon_mid is not None else None),
            "horizon_gap": gap,
            "horizon_azimuth_deg": (round(horizon_azimuth, 1) if horizon_azimuth is not None else None),
        },
        "notes": notes,
        "wind_detail": wind_detail,
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
    geo_hhmm = geometric[11:16]
    geo_min = int(geo_hhmm[:2]) * 60 + int(geo_hhmm[3:])
    light_min = int(light["time"][:2]) * 60 + int(light["time"][3:])

    def _mm(minutes: float) -> str:
        minutes = int(round(minutes)) % 1440
        return f"{minutes // 60:02d}:{minutes % 60:02d}"

    if event == "sunrise":
        # 錨定幾何日出：朝霞色彩喺日出前 ~45 分鐘開始；山體首束直射光為受光資訊。
        # 終點包首束光後 30 分鐘，但至少到日出後 60 分鐘，上限日出後 150 分鐘
        # （防 Moraine 式「首束光遲 5 個鐘」令窗口失真）。
        start = geo_min - 45
        end = min(max(light_min + 30, geo_min + 60), geo_min + 150)
        light["window"] = {"start": _mm(start), "end": _mm(end)}
        light["label"] = "首束直射光"
    else:
        # 錨定幾何日落：火燒雲色彩高峰係幾何日落前後（日落後 0–45 分鐘）。
        # 起點包山體最後金光前 1 小時，但最遲由日落前 75 分鐘開始；
        # 終點 = 幾何日落後 45 分鐘（色彩尾段＋藍調開始＋風趨平靜）。
        start = min(light_min - 60, geo_min - 75)
        end = geo_min + 45
        light["window"] = {"start": _mm(start), "end": _mm(end)}
        light["label"] = "最後直射光"
    light["geometric_time"] = geo_hhmm
    return light


def _label(score: int) -> str:
    if score >= 80:
        return "條件佳"
    if score >= 65:
        return "可嘗試"
    if score >= 45:
        return "條件普通"
    return "不建議專程前往"


def _peak_window(hourly: dict[str, list[Any]], window: dict[str, str] | None) -> str | None:
    """R4：拍攝窗口內雲層結構最佳嘅連續時段（「最濃 HH:MM–HH:MM」）。
    逐小時預報解像度所限，只屬指示性；以最佳小時為中心向兩邊延伸（分數跌 ≤15 內）。"""
    if not window or not hourly.get("time"):
        return None

    def _mm(s: str) -> int:
        return int(s[:2]) * 60 + int(s[3:])

    lo, hi = _mm(window["start"]), _mm(window["end"])
    scored: list[tuple[int, float]] = []
    for i, raw in enumerate(hourly["time"]):
        h = _mm(raw[11:16])
        if lo <= h <= hi:
            high = float(hourly["cloud_cover_high"][i] or 0)
            mid = float(hourly["cloud_cover_mid"][i] or 0)
            low = float(hourly["cloud_cover_low"][i] or 0)
            s = _canvas_score(high) * 0.50 + _canvas_score(mid) * 0.25 + max(0, 100 - low) * 0.25
            scored.append((h, s))
    if not scored:
        return None
    best_idx = max(range(len(scored)), key=lambda k: scored[k][1])
    best_s = scored[best_idx][1]
    a = b = best_idx
    while a > 0 and scored[a - 1][1] >= best_s - 15:
        a -= 1
    while b < len(scored) - 1 and scored[b + 1][1] >= best_s - 15:
        b += 1
    start = scored[a][0]
    end = scored[b][0] + 60  # hourly slot：最後一小時覆蓋到佢嘅結尾
    fmt = lambda m: f"{(m // 60) % 24:02d}:{m % 60:02d}"
    return f"{fmt(start)}–{fmt(end)}"


def _fetch(coords: list[tuple[float, float]], hourly: str, daily: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "latitude": ",".join(str(c[0]) for c in coords),
        "longitude": ",".join(str(c[1]) for c in coords),
        "timezone": TZ,
        "forecast_days": 7,
        "hourly": hourly,
    }
    if daily:
        params["daily"] = daily
    url = "https://api.open-meteo.com/v1/forecast?" + urlencode(params)
    with urlopen(url, timeout=30) as response:
        raw = json.load(response)
    return raw if isinstance(raw, list) else [raw]


def _fetch_air_quality(coords: list[tuple[float, float]]) -> list[dict[str, Any]] | None:
    params = {
        "latitude": ",".join(str(c[0]) for c in coords),
        "longitude": ",".join(str(c[1]) for c in coords),
        "timezone": TZ,
        "forecast_days": 7,
        "hourly": "pm2_5,us_aqi",
    }
    url = "https://air-quality-api.open-meteo.com/v1/air-quality?" + urlencode(params)
    try:
        with urlopen(url, timeout=30) as response:
            raw = json.load(response)
        return raw if isinstance(raw, list) else [raw]
    except Exception:
        return None


def _fetch_ecmwf(coords: list[tuple[float, float]]) -> list[dict[str, Any]] | None:
    """ECMWF IFS 風速（ecmwf_ifs025），沿用逗號分隔座標批次 pattern（每次 build 只多 1 個 call）。

    誠實失敗：任何一點風速全 null（例如誤用已廢棄嘅 ecmwf_ifs04）或整批失敗 → None，
    由呼叫方標示「ECMWF 資料暫缺」，絕不以 best_match 數字冒充。
    """
    params = {
        "latitude": ",".join(str(c[0]) for c in coords),
        "longitude": ",".join(str(c[1]) for c in coords),
        "timezone": TZ,
        "forecast_days": 4,  # 覆蓋 report-2 日落後 +2.5h 曲線
        "hourly": "wind_speed_10m,wind_gusts_10m,cloud_cover",
        "models": ECMWF_MODEL,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urlencode(params)
    try:
        with urlopen(url, timeout=30) as response:
            raw = json.load(response)
        forecasts = raw if isinstance(raw, list) else [raw]
    except Exception:
        return None
    if len(forecasts) != len(coords):
        return None
    for fc in forecasts:
        winds = (fc.get("hourly") or {}).get("wind_speed_10m") or []
        if not any(v is not None for v in winds):
            return None
    return forecasts


def _fetch_model(coords: list[tuple[float, float]], model: str, hourly: str) -> list[dict[str, Any]] | None:
    """指定單模型批次查詢（plain key）。任何一點全 null 或整批失敗 → None（誠實缺失）。"""
    params = {
        "latitude": ",".join(str(c[0]) for c in coords),
        "longitude": ",".join(str(c[1]) for c in coords),
        "timezone": TZ,
        "forecast_days": 5,  # 覆蓋 R7 五日展望
        "hourly": hourly,
        "models": model,
    }
    url = "https://api.open-meteo.com/v1/forecast?" + urlencode(params)
    try:
        with urlopen(url, timeout=30) as response:
            raw = json.load(response)
        forecasts = raw if isinstance(raw, list) else [raw]
    except Exception:
        return None
    if len(forecasts) != len(coords):
        return None
    key = hourly.split(",")[0]
    for fc in forecasts:
        vals = (fc.get("hourly") or {}).get(key) or []
        if not any(v is not None for v in vals):
            return None
    return forecasts


def _window_avg(hourly: dict[str, list[Any]] | None, key: str, centre: str) -> float | None:
    """事件窗口（±65min）內某 hourly 欄位平均；缺資料 → None。"""
    if not hourly or not hourly.get("time") or hourly.get(key) is None:
        return None
    ids = _event_window(hourly, centre)
    vals = [hourly[key][i] for i in ids if i < len(hourly[key]) and hourly[key][i] is not None]
    return _mean([float(v) for v in vals]) if vals else None


def build_daylight(date_str: str) -> dict[str, Any]:
    data = json.loads((HERE / "spots.json").read_text(encoding="utf-8"))
    points = [p for p in data["points"] if p.get("daylight_events")]
    if not points:
        return {"date": date_str, "error": True, "message": "尚未設定日出／日落評估點"}
    coords = [(p["lat"], p["lon"]) for p in points]
    try:
        forecasts = _fetch(
            coords,
            "cloud_cover,cloud_cover_low,cloud_cover_mid,cloud_cover_high,precipitation_probability,visibility,wind_speed_10m,wind_gusts_10m",
            "sunrise,sunset",
        )
    except Exception as exc:
        return {"date": date_str, "error": True, "message": f"日出／日落天氣資料暫時無法取得：{exc}"}
    if len(forecasts) != len(points):
        return {"date": date_str, "error": True, "message": "日出／日落天氣資料數量不完整"}

    calculator = DirectLightCalculator()

    # 太陽方向 100km 外嘅地平線監測點（逐 point 逐 event 一個，去重後一次過查）
    horizon_points: dict[tuple[float, float], tuple[float, float]] = {}  # (point_idx, event) -> offset coord
    unique_offsets: list[tuple[float, float]] = []
    for idx, (point, fc) in enumerate(zip(points, forecasts)):
        try:
            day_index = fc["daily"]["time"].index(date_str)
        except (KeyError, ValueError):
            continue
        for event in point["daylight_events"]:
            geometric = fc["daily"][event][day_index]
            try:
                when = datetime.fromisoformat(geometric).replace(tzinfo=LOCAL)
                _alt, az = calculator._sun(point["lat"], point["lon"], 0.0, when)
            except Exception:
                continue
            offset = _offset_point(point["lat"], point["lon"], az)
            horizon_points[(idx, event)] = offset
            if offset not in unique_offsets:
                unique_offsets.append(offset)

    horizon_forecasts: dict[tuple[float, float], dict[str, Any]] = {}
    if unique_offsets:
        try:
            for offset, hfc in zip(unique_offsets, _fetch(unique_offsets, "cloud_cover_low,cloud_cover_mid")):
                horizon_forecasts[offset] = hfc
        except Exception:
            horizon_forecasts = {}

    aq_list = _fetch_air_quality(coords)
    aq_by_point: dict[int, dict[str, Any]] = {}
    if aq_list and len(aq_list) == len(points):
        aq_by_point = {i: aq for i, aq in enumerate(aq_list)}

    ecmwf_list = _fetch_ecmwf(coords)
    ecmwf_by_point: dict[int, dict[str, Any]] = {}
    if ecmwf_list and len(ecmwf_list) == len(points):
        ecmwf_by_point = {i: fc for i, fc in enumerate(ecmwf_list)}

    # R3：GFS 第三模型（信心分歧度用；失敗 → 信心標示資料不足，唔阻塞評分）
    gfs_list = _fetch_model(coords, GFS_MODEL, "cloud_cover")
    gfs_by_point: dict[int, dict[str, Any]] = {}
    if gfs_list and len(gfs_list) == len(points):
        gfs_by_point = {i: fc for i, fc in enumerate(gfs_list)}

    result = []
    for idx, (point, fc) in enumerate(zip(points, forecasts)):
        try:
            index = fc["daily"]["time"].index(date_str)
            events: dict[str, Any] = {}
            for event in point["daylight_events"]:
                geometric = fc["daily"][event][index]
                light = _light_for_event(calculator, point, date_str, event, geometric)
                if light.get("error"):
                    events[event] = {"event": event, "error": True, "message": light["message"]}
                    continue
                centre = geometric  # 評分錨定幾何事件（v2.14.0）：火燒雲色彩圍繞幾何日出日落，唔係地形直射光
                offset = horizon_points.get((idx, event))
                horizon_hourly = horizon_forecasts.get(offset, {}).get("hourly") if offset else None
                horizon_az = None
                if offset:
                    try:
                        when = datetime.fromisoformat(geometric).replace(tzinfo=LOCAL)
                        _alt, horizon_az = calculator._sun(point["lat"], point["lon"], 0.0, when)
                    except Exception:
                        horizon_az = None
                aq_hourly = aq_by_point.get(idx, {}).get("hourly")
                ecmwf_hourly = ecmwf_by_point.get(idx, {}).get("hourly")
                condition = _condition(event, centre, fc["hourly"], aq_hourly, horizon_hourly, horizon_az, ecmwf_hourly)
                condition["light"] = light
                condition["score"] = round(
                    condition["components"]["cloud"] * 0.50
                    + condition["components"]["smoke"] * 0.30
                    + condition["components"]["wind"] * 0.20
                )
                condition["label"] = _label(condition["score"])
                # R3：三模型雲量分歧 → 信心等級
                gfs_hourly = gfs_by_point.get(idx, {}).get("hourly")
                model_clouds = {
                    "best_match": _window_avg(fc["hourly"], "cloud_cover", centre),
                    "ecmwf": _window_avg(ecmwf_hourly, "cloud_cover", centre),
                    "gfs": _window_avg(gfs_hourly, "cloud_cover", centre),
                }
                cloud_vals = [v for v in model_clouds.values() if v is not None]
                spread = (max(cloud_vals) - min(cloud_vals)) if len(cloud_vals) >= 2 else None
                condition["confidence"] = {
                    "level": _confidence_level(spread),
                    "cloud_spread": (round(spread) if spread is not None else None),
                    "models": {k: round(v) for k, v in model_clouds.items() if v is not None},
                }
                if spread is None:
                    condition["notes"].append("多模型雲量資料不足，信心未能評估")
                elif _confidence_level(spread) == "低":
                    condition["notes"].append(f"三模型雲量預報分歧大（相差 {round(spread)}%），今日預測信心低")
                # R4：峰值色彩時段（逐小時雲層結構，hourly 解像度限制屬指示性）
                condition["peak_window"] = _peak_window(fc["hourly"], light.get("window"))
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
        "method": "雲 50%（色彩雲層×地平線開口）／煙 30%（PM2.5 大氣通透度）／風 20%（倒影，best_match 與 ECMWF 雙模型對比取保守值）＋三模型雲量分歧信心（best_match／ECMWF／GFS）",
        "terrain_disclaimer": "火燒雲機率為條件估算，無法保證。DEM 直射光為地形模型，精確腳架點、樹木與現場實測優先。",
        "points": result,
        "sources": "Open-Meteo（各拍攝點天氣＋太陽方向 100km 地平線雲量）＋Open-Meteo ECMWF IFS 風速對比＋Open-Meteo CAMS 空氣質素＋Skyfield 太陽位置＋AWS Terrain Tiles SRTM DEM",
    }
