#!/usr/bin/env python3
"""
night_report.py — 落磯山六機位銀河拍攝條件報告

用法:
  python3 night_report.py --location two_jack_lake --date 2026-07-28
  python3 night_report.py --location all --date 2026-07-28
  python3 night_report.py --location all --date 2026-07-28 --json   # 網頁資料來源用

評分哲學（West 2026-07-28 定）：
  權重：雲 45% / 月 25% / 煙 20% / 風（倒影）10%
  否決三項：雲、月、煙 — 任何一項去到否決線，今晚直接 STAY_HOME，風再靜都冇用
  風：唔會否決，但無風（≤6 km/h 鏡面）喺佢 10% 權重內攞滿分

數據源:
  - Open-Meteo Forecast API（逐小時天氣，lat/lon 精確點）
  - Open-Meteo Air Quality API（CAMS global PM2.5 / US AQI）
  - skyfield 本地計算（太陽仰角、月出月落、照明度、銀心位置）
時區: America/Edmonton
"""
import argparse
import datetime as dt
import hashlib
import json
import math
import sys
import time
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

# Keep package imports working when this file is launched directly as a subprocess.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from backend.smoke_pipeline import assess_smoke_window

import requests
from skyfield import almanac
from skyfield.api import Star, load, wgs84

TZ = ZoneInfo("America/Edmonton")
SKILL_DIR = Path(__file__).resolve().parent.parent
LOCATIONS_FILE = SKILL_DIR / "references" / "locations.json"

NIGHT_START_HOUR = 18   # 分析由當日 18:00 開始
NIGHT_END_HOUR = 10     # 到翌日 10:00
GEAR_END_HOUR = 8       # 器材建議統計到翌日 08:00

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AQ_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"

GRADES = [
    (85, "GO", "立即出發"),
    (70, "GOOD", "值得去"),
    (55, "MARGINAL", "可試有風險"),
    (40, "RISKY", "唔太建議"),
    (0, "STAY_HOME", "留喺屋企"),
]
GRADE_RANK = {"STAY_HOME": 0, "RISKY": 1, "MARGINAL": 2, "GOOD": 3, "GO": 4}


# ---------- 評分函數（門檻同 SKILL.md 文件化嘅表一致，改呢度要同步改 SKILL.md） ----------

def cloud_score(total, low, mid, high):
    """雲分。好 ≥78 / 中 48–65 / 差 ≤30。低雲重罰（擋山+星空交界）。"""
    t = total
    if t <= 10:
        s = 100
    elif t <= 20:
        s = 90
    elif t <= 30:
        s = 78
    elif t <= 40:
        s = 65
    elif t <= 55:
        s = 48
    elif t <= 70:
        s = 30
    else:
        s = 12
    if low > 30:
        s = min(s, 40)
    if low > 50:
        s = min(s, 25)
    if low + mid <= 10 and t <= 60:
        s = max(s, 72)  # 薄高雲寬容（一兩片小雲/薄高雲唔會即死）
    return s


def smoke_score(pm):
    """煙塵分。好 ≥90 (PM2.5≤10) / 中 55–75 / 差 ≤35。"""
    if pm is None:
        return 60  # 無資料，帶不確定標記
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


def moon_up_score(illum_pct):
    """月亮喺地平線上時，按照明度罰。好 = 月亮喺地平線下（100）或新月 <8%（95）。"""
    if illum_pct < 8:
        return 95
    if illum_pct < 15:
        return 80
    if illum_pct < 30:
        return 55
    if illum_pct < 50:
        return 35
    if illum_pct < 70:
        return 20
    if illum_pct < 90:
        return 8
    return 3


def wind_reflection_score(kmh):
    """風分（倒影）。好 100（≤6 鏡面）/ 中 85–60 / 差 ≤35。唔會否決。"""
    if kmh <= 6:
        return 100
    if kmh <= 10:
        return 85
    if kmh <= 15:
        return 60
    if kmh <= 20:
        return 35
    return 10


def label(score):
    """每項指標好/中/差標籤（網頁顯示用）"""
    if score >= 78:
        return "好"
    if score >= 48:
        return "中"
    return "差"


def grade_of(score):
    for threshold, code, zh in GRADES:
        if score >= threshold:
            return code, zh
    return "STAY_HOME", "留喺屋企"


# ---------- Open-Meteo 結果快取（v2.24.0：quota 保護） ----------
# 同一 URL 55 分鐘內重用結果。cron 每 30 分鐘一輪 → 每兩輪先真 fetch 一次，
# 上游 call 量減 ~50 倍。key 埋當日日期：午夜後自動冷啟動（forecast 嘅「今日」向前移）。
# 錯誤/429 永遠唔入 cache；cache 讀寫失敗靜默略過，唔影響正常 fetch。

OM_CACHE_DIR = Path.home() / ".cache" / "astro-openmeteo"
OM_CACHE_TTL = 55 * 60  # 秒


def _om_cache_key(url, params) -> str:
    raw = url + "?" + urlencode(sorted(params.items())) + "|" + dt.datetime.now(TZ).date().isoformat()
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def _om_cache_get(url, params):
    try:
        p = OM_CACHE_DIR / f"{_om_cache_key(url, params)}.json"
        if p.exists() and time.time() - p.stat().st_mtime < OM_CACHE_TTL:
            return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


def _om_cache_put(url, params, data) -> None:
    try:
        OM_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        (OM_CACHE_DIR / f"{_om_cache_key(url, params)}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ---------- 數據抓取（429/5xx 自動 retry with backoff；v2.25.0：唔入 cache 嘅失敗 → MET Norway fallback） ----------

def _get_with_retry(url, params, attempts=4):
    cached = _om_cache_get(url, params)
    if cached is not None:
        return cached
    delay = 5
    last = None
    for i in range(attempts):
        try:
            r = requests.get(url, params=params, timeout=30)
            if r.status_code == 429 or r.status_code >= 500:
                last = requests.HTTPError(f"{r.status_code} from {url}")
                time.sleep(delay)
                delay *= 3
                continue
            r.raise_for_status()
            data = r.json()
            _om_cache_put(url, params, data)
            return data
        except requests.RequestException as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay)
                delay *= 3
    raise last


def _met_norway_module():
    """載入 MET Norway 轉換器；兼容 repo 與 skill 兩個腳本位置。"""
    try:
        import met_norway_fallback as module
        return module
    except ImportError:
        import sys as _s
        _here = Path(__file__).resolve()
        for _cand in (_here.parent, _here.parent.parent):
            if (_cand / "met_norway_fallback.py").exists():
                _s.path.insert(0, str(_cand))
                break
        import met_norway_fallback as module
        return module


def _met_norway_fetch_one(lat, lon, forecast_days=5):
    """v2.25.0：Open-Meteo 完全失效時嘅後備數據源（免 key，加拿大區 = ECMWF IFS 9km）。

    import 放喺入面：正常路徑零開銷；backend/scripts 同 skill scripts 兩個
    擺位都work（搵唔到就將自己嘅 parent / grandparent 加入 sys.path）。
    """
    return _met_norway_module().fetch_one(lat, lon, forecast_days)


def _met_norway_fetch_batch(coords, forecast_days=5):
    """Open-Meteo 整批失效時逐點抓 MET Norway；只作後備，不寫 OM cache。"""
    return _met_norway_module().fetch_batch(coords, forecast_days)


def _batch_params(coords, hourly):
    return {
        "latitude": ",".join(str(lat) for lat, _lon in coords),
        "longitude": ",".join(str(lon) for _lat, lon in coords),
        "hourly": hourly,
        "timezone": "America/Edmonton",
        "forecast_days": 5,
    }


def fetch_weather_batch(coords):
    """一次 Open-Meteo request 取得全部機位；增加機位不增加 API request 次數。"""
    params = _batch_params(coords, ",".join([
        "temperature_2m", "relative_humidity_2m", "dew_point_2m",
        "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
        "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m", "visibility", "freezing_level_height",
    ]))
    params["wind_speed_unit"] = "kmh"
    try:
        raw = _get_with_retry(FORECAST_URL, params)
        forecasts = raw if isinstance(raw, list) else [raw]
        if len(forecasts) != len(coords):
            raise ValueError(f"天氣批次數量不完整：預期 {len(coords)}，收到 {len(forecasts)}")
        return forecasts
    except Exception as e:
        print(f"[警告] Open-Meteo 批次失效（{e}）— 轉用 MET Norway 後備數據源", file=sys.stderr)
        return _met_norway_fetch_batch(coords, forecast_days=params["forecast_days"])


def fetch_air_quality_batch(coords):
    """一次 Open-Meteo Air Quality request 取得全部機位。"""
    params = _batch_params(coords, "pm2_5,us_aqi")
    params["domains"] = "cams_global"
    raw = _get_with_retry(AQ_URL, params)
    forecasts = raw if isinstance(raw, list) else [raw]
    if len(forecasts) != len(coords):
        raise ValueError(f"空氣質素批次數量不完整：預期 {len(coords)}，收到 {len(forecasts)}")
    return forecasts


def fetch_weather(lat, lon):
    return fetch_weather_batch([(lat, lon)])[0]


def fetch_air_quality(lat, lon):
    return fetch_air_quality_batch([(lat, lon)])[0]


# ---------- 天文計算 ----------

class Astro:
    def __init__(self, lat, lon, elev):
        self.ts = load.timescale()
        self.eph = load("de421.bsp")
        self.earth = self.eph["earth"]
        self.sun = self.eph["sun"]
        self.moon = self.eph["moon"]
        self.topos_ll = wgs84.latlon(lat, lon, elevation_m=elev)
        self.obs = self.earth + self.topos_ll
        self.gc = Star(ra_hours=17 + 45 / 60 + 40.04 / 3600,
                       dec_degrees=-(29 + 0 / 60 + 28.1 / 3600))

    def t(self, local_dt):
        return self.ts.from_datetime(local_dt.astimezone(dt.timezone.utc))

    def sun_alt(self, local_dt):
        alt, _, _ = self.obs.at(self.t(local_dt)).observe(self.sun).apparent().altaz()
        return alt.degrees

    def moon_alt_az(self, local_dt):
        alt, az, _ = self.obs.at(self.t(local_dt)).observe(self.moon).apparent().altaz()
        return alt.degrees, az.degrees

    def moon_illum(self, local_dt):
        return almanac.fraction_illuminated(self.eph, "moon", self.t(local_dt)) * 100

    def gc_alt_az(self, local_dt):
        alt, az, _ = self.obs.at(self.t(local_dt)).observe(self.gc).apparent().altaz()
        return alt.degrees, az.degrees

    def moon_events(self, start_local, end_local):
        f = almanac.risings_and_settings(self.eph, self.moon, self.topos_ll)
        t0 = self.t(start_local)
        t1 = self.t(end_local)
        times, kinds = almanac.find_discrete(t0, t1, f)
        events = []
        for ti, k in zip(times, kinds):
            events.append((ti.utc_datetime().astimezone(TZ), "月出" if k == 1 else "月落"))
        return events


def fmt(d):
    return d.strftime("%H:%M")


def cardinal(az):
    dirs = ["北", "東北", "東", "東南", "南", "西南", "西", "西北"]
    return dirs[int((az + 22.5) // 45) % 8]


def vertical_mw_note(month):
    """垂直銀河季節提示（51°N 落磯山）"""
    if month in (8, 9, 10):
        return "現在是傍晚垂直銀河季節——天文黑夜初段銀河直立在南方至西南"
    if month in (3, 4, 5):
        return "黎明前垂直銀河季節——天文黑夜尾段銀河直立在東南至南"
    if month in (6, 7):
        return "夏季銀河橫躺在南方低空；垂直銀河要等 8 月中後的黃昏"
    return "銀河季休整中；下一個垂直銀河時段：3–5 月黎明前"


# ---------- 主分析 ----------

_NOT_PREFETCHED = object()


def analyze(loc_id, loc, date_str, wx=None, aq=_NOT_PREFETCHED, aq_error=None, smoke_assessment=None):
    night_date = dt.date.fromisoformat(date_str)
    start = dt.datetime.combine(night_date, dt.time(NIGHT_START_HOUR), TZ)
    end = dt.datetime.combine(night_date + dt.timedelta(days=1), dt.time(NIGHT_END_HOUR), TZ)

    if wx is None:
        wx = fetch_weather(loc["lat"], loc["lon"])
    if aq is _NOT_PREFETCHED:
        aq = None
        try:
            aq = fetch_air_quality(loc["lat"], loc["lon"])
        except Exception as e:
            aq_error = str(e)
            print(f"[警告] 空氣質素 API 失敗：{e} — 煙塵以「無資料」處理", file=sys.stderr)

    hourly = wx["hourly"]
    wx_by_time = {t: i for i, t in enumerate(hourly["time"])}
    aq_by_time = {}
    if aq:
        aq_by_time = {t: i for i, t in enumerate(aq["hourly"]["time"])}

    astro = Astro(loc["lat"], loc["lon"], loc["elev_m"])

    # --- 太陽仰角曲線（每 10 分鐘） ---
    samples = []
    cur = start
    while cur <= end:
        samples.append((cur, astro.sun_alt(cur)))
        cur += dt.timedelta(minutes=10)

    dark = [(t, a) for t, a in samples if a <= -18.0]
    min_alt_t, min_alt = min(samples, key=lambda x: x[1])

    if dark:
        dark_start, dark_end = dark[0][0], dark[-1][0]
        window_hours = sorted({t.replace(minute=0) for t, _ in dark})
        full_darkness = True
        darkness_note = None
    else:
        deep = [(t, a) for t, a in samples if a <= -12.0]
        if deep:
            dark_start, dark_end = deep[0][0], deep[-1][0]
            window_hours = sorted({t.replace(minute=0) for t, _ in deep})
        else:
            dark_start = dark_end = min_alt_t
            window_hours = [min_alt_t.replace(minute=0)]
        full_darkness = False
        darkness_note = (f"冇完整天文黑夜：太陽最低 {min_alt:.1f}°（{fmt(min_alt_t)}），"
                         f"唔夠 -18°。窗口以最深 range 為準，評級封頂「可試」。")

    win_len_min = (dark_end - dark_start).total_seconds() / 60

    if smoke_assessment is None:
        try:
            smoke_assessment = assess_smoke_window(
                lat=loc["lat"], lon=loc["lon"],
                start_local=dark_start, end_local=dark_end,
            )
        except Exception as exc:
            print(f"[警告] 三模型煙霧評估失敗：{exc} — 以無資料／不確定處理", file=sys.stderr)
            smoke_assessment = assess_smoke_window(
                lat=loc["lat"], lon=loc["lon"],
                start_local=dark_start, end_local=dark_end,
                firework_fetch=lambda **_: (_ for _ in ()).throw(exc),
                cams_fetch=lambda **_: (_ for _ in ()).throw(exc),
                bluesky_fetch=lambda **_: (_ for _ in ()).throw(exc),
            )
    smoke_assessment = smoke_assessment.get("smoke_assessment", smoke_assessment)
    smoke_consensus = smoke_assessment["consensus"]
    consensus_smoke_score = int(smoke_consensus["photography_smoke_score"])
    smoke_coverage = int(smoke_consensus.get("coverage", {}).get("valid", 0))

    # --- 月亮 ---
    illum = astro.moon_illum(min_alt_t)
    events = astro.moon_events(start, end)
    overlap_count = 0
    for t, a in samples:
        if dark_start <= t <= dark_end:
            malt, _ = astro.moon_alt_az(t)
            if malt > 0:
                overlap_count += 1
    overlap_min = min(overlap_count * 10, round(win_len_min))
    overlap_ratio = overlap_min / win_len_min if win_len_min > 0 else 0

    # 窗口內嘅無月時段（連續 run，10 分鐘採樣）— West：重疊先係重點，
    # 滿月早落 → 後半夜無月照拍；新月 → 根本就唔近窗口
    moon_free_periods = []
    run_start = None
    for t, a in samples:
        if dark_start <= t <= dark_end:
            malt, _ = astro.moon_alt_az(t)
            if malt <= 0:
                if run_start is None:
                    run_start = t
            else:
                if run_start is not None:
                    moon_free_periods.append((run_start, t))
                    run_start = None
    if run_start is not None:
        moon_free_periods.append((run_start, dark_end))
    moon_free_min = sum((b - a).total_seconds() / 60 for a, b in moon_free_periods)

    # 全晚（18:00→10:00）月亮在地平線上嘅時段（畀 timeline 著色用）
    moon_up_periods = []
    run_start = None
    for t, a in samples:
        malt, _ = astro.moon_alt_az(t)
        if malt > 0:
            if run_start is None:
                run_start = t
        else:
            if run_start is not None:
                moon_up_periods.append((run_start, t))
                run_start = None
    if run_start is not None:
        moon_up_periods.append((run_start, samples[-1][0]))

    # --- 銀心 ---
    gc_best = None
    gc_visible_hours = []
    for t, a in samples:
        if dark_start <= t <= dark_end:
            galt, gaz = astro.gc_alt_az(t)
            if galt > 10:
                gc_visible_hours.append(t)
            if gc_best is None or galt > gc_best[1]:
                gc_best = (t, galt, gaz)

    # --- 季節角度（West 2026-08-05 定）：山-銀河構圖關聯性 ---
    # 唔係睇銀心有冇露出山脊，係睇方位關聯：銀心方位 vs 機位構圖軸（composition_az）嘅最近角距離
    # 軌跡用航海暮光（太陽 ≤ -12°）：八月銀心係黃昏尾主體（中天 ~22:40），唔係半夜
    comp_az = loc.get("composition_az")
    season_angle = None
    if comp_az is not None:
        track = []
        for t, a in samples:
            if a <= -12.0:
                galt, gaz = astro.gc_alt_az(t)
                if galt > 0:
                    sep = abs((gaz - comp_az + 180) % 360 - 180)
                    track.append((t, gaz, galt, sep))
        if track:
            b_t, b_az, b_alt, b_sep = min(track, key=lambda x: x[3])
            s_score = 10 if b_sep <= 10 else 6 if b_sep <= 25 else 3 if b_sep <= 40 else 0
            season_angle = {
                "composition_az": comp_az,
                "anchor": "galactic_center",
                "min_separation_deg": round(b_sep, 0),
                "best_time": fmt(b_t),
                "gc_azimuth_at_best": round(b_az, 0),
                "score": s_score,
            }

    # --- 逐小時評分（窗口內） ---
    rows = []
    for h in window_hours:
        key = h.strftime("%Y-%m-%dT%H:%M")
        i = wx_by_time.get(key)
        if i is None:
            continue
        total = hourly["cloud_cover"][i] or 0
        low = hourly["cloud_cover_low"][i] or 0
        mid = hourly["cloud_cover_mid"][i] or 0
        high = hourly["cloud_cover_high"][i] or 0
        temp = hourly["temperature_2m"][i]
        rh = hourly["relative_humidity_2m"][i]
        dew = hourly["dew_point_2m"][i]
        wind = hourly["wind_speed_10m"][i] or 0
        wind_dir = hourly["wind_direction_10m"][i]
        gust = hourly["wind_gusts_10m"][i]  # v2.25.0：保留 None（MET Norway 後備冇 gust），唔好用 0 冒充
        pm = aqi_val = None
        j = aq_by_time.get(key)
        if j is not None:
            pm = aq["hourly"]["pm2_5"][j]
            aqi_val = aq["hourly"]["us_aqi"][j]
        malt, _ = astro.moon_alt_az(h)
        moon_s = 100 if malt <= 0 else moon_up_score(illum)
        cs = cloud_score(total, low, mid, high)
        ss = consensus_smoke_score
        ws = wind_reflection_score(wind)
        astro_score = cs * 0.45 + moon_s * 0.25 + ss * 0.20 + ws * 0.10
        rows.append({
            "hour": h, "total_cloud": total, "low": low, "mid": mid, "high": high,
            "temp": temp, "rh": rh, "dew": dew, "wind": wind, "wind_dir": wind_dir, "gust": gust,
            "pm": pm, "aqi": aqi_val, "moon_alt": malt,
            "cloud_score": cs, "cloud_label": label(cs),
            "moon_score": moon_s, "moon_label": label(moon_s),
            "smoke_score": ss, "smoke_label": (label(ss) if smoke_coverage else "無資料／不確定"),
            "wind_score": ws, "wind_label": label(ws),
            "score": astro_score,
        })

    # --- 最佳連續 3 小時 ---
    best3 = None
    if len(rows) >= 3:
        for k in range(len(rows) - 2):
            chunk = rows[k:k + 3]
            avg = sum(r["score"] for r in chunk) / 3
            if best3 is None or avg > best3[0]:
                best3 = (avg, chunk)
    elif rows:
        avg = sum(r["score"] for r in rows) / len(rows)
        best3 = (avg, rows)
    night_score = best3[0] if best3 else 0

    # --- 否決三項 + 封頂（風永遠唔會否決） ---
    vetoes = []   # 直接 STAY_HOME
    caps = []     # (rank_cap, reason)
    if best3:
        avg_cs = sum(r["cloud_score"] for r in best3[1]) / len(best3[1])

        if avg_cs <= 15:
            vetoes.append(f"雲量否決：建議窗口平均雲分 {avg_cs:.0f}（≤15，基本冚唪唥）")
        elif avg_cs <= 35:
            caps.append((GRADE_RANK["RISKY"], f"雲量封頂：平均雲分 {avg_cs:.0f}（≤35）"))
        smoke_status = smoke_consensus.get("status")
        if smoke_consensus.get("veto") or smoke_status == "VETO":
            vetoes.append("三模型煙霧共識否決：至少兩套模型支持重煙風險")
        elif smoke_status in {"RISKY_BOUNDARY", "SMOKE_RISK", "MODEL_SPLIT"}:
            caps.append((GRADE_RANK["RISKY"], f"三模型煙霧共識封頂：{smoke_status}"))
        if smoke_status == "SINGLE_MODEL_ONLY" or smoke_coverage < 2:
            caps.append((GRADE_RANK["MARGINAL"], f"煙霧模型覆蓋不足封頂：{smoke_coverage}/3"))
    if overlap_min > 0:
        if moon_free_min < 30 and illum >= 85:
            vetoes.append(f"月亮否決：照明 {illum:.0f}%（≥85%），窗口內無月時段 = {moon_free_min:.0f} 分鐘（成晚月光晒住）")
        elif moon_free_min < 30 and illum >= 50:
            caps.append((GRADE_RANK["RISKY"], f"月亮封頂：照明 {illum:.0f}%，窗口內無月時段 = {moon_free_min:.0f} 分鐘"))
        elif moon_free_min < 90 and illum >= 30:
            caps.append((GRADE_RANK["MARGINAL"], f"月亮封頂：照明 {illum:.0f}%，窗口內無月時段只有 {moon_free_min:.0f} 分鐘"))
    # 註：per-hour 月分已自動處理重疊（月亮喺地平線下嗰個鐘 = 100 分），
    #     所以「上弦月只遮窗口初段、後半夜無月」會由 best-3h 自動揀出嚟，唔使額外封頂
    if not full_darkness:
        caps.append((GRADE_RANK["MARGINAL"], "冇完整天文黑夜封頂"))

    grade_code, grade_zh = grade_of(night_score)
    if not rows:
        grade_code, grade_zh = "NO_DATA", "無法評估（超出天氣預報範圍，只顯示天文數據）"
        vetoes = []
        caps = []
    elif vetoes:
        grade_code, grade_zh = "STAY_HOME", "留喺屋企"
    elif caps:
        cap_rank = min(r for r, _ in caps)
        if GRADE_RANK[grade_code] > cap_rank:
            grade_code = [c for c in GRADE_RANK if GRADE_RANK[c] == cap_rank][0]
            grade_zh = dict((c, z) for _, c, z in GRADES)[grade_code]

    # --- 器材保護（全晚 18:00→翌日 08:00） ---
    gear_end = dt.datetime.combine(night_date + dt.timedelta(days=1), dt.time(GEAR_END_HOUR), TZ)
    temps, rhs, spreads, gusts = [], [], [], []
    cur = start.replace(minute=0)
    while cur <= gear_end:
        key = cur.strftime("%Y-%m-%dT%H:%M")
        i = wx_by_time.get(key)
        if i is not None:
            t2 = hourly["temperature_2m"][i]
            d2 = hourly["dew_point_2m"][i]
            if t2 is not None and d2 is not None:
                temps.append(t2)
                spreads.append(t2 - d2)
            if hourly["relative_humidity_2m"][i] is not None:
                rhs.append(hourly["relative_humidity_2m"][i])
            if hourly["wind_gusts_10m"][i] is not None:
                gusts.append(hourly["wind_gusts_10m"][i])
        cur += dt.timedelta(hours=1)

    gear = []
    if spreads:
        min_spread = min(spreads)
        max_rh = max(rhs) if rhs else 0
        if min_spread <= 1.5 or max_rh >= 90:
            gear.append(f"🔴 高結露風險（溫露差最低 {min_spread:.1f}°C / RH 最高 {max_rh:.0f}%）— dew heater / 鏡頭暖帶必帶")
        elif min_spread <= 3.0 or max_rh >= 80:
            gear.append(f"🟡 中結露風險（溫露差最低 {min_spread:.1f}°C / RH 最高 {max_rh:.0f}%）— 暖帶放喺袋，隨時上")
    if temps:
        min_t = min(temps)
        if min_t <= 0:
            gear.append(f"❄️ 最低 {min_t:.1f}°C — 結霜可能，暖寶寶貼鏡筒；凍器材入暖車前装密實袋")
        elif min_t <= 4:
            gear.append(f"🌡️ 最低 {min_t:.1f}°C — 注意鏡頭降溫後返車結露")
        if min_t <= -8:
            gear.append("🔋 電池貼身暖袋，後備電放內袋")
    if gusts and max(gusts) >= 40:
        win_gusts = [r["gust"] for r in rows] if rows else []
        win_gust_s = f"，但建議窗口內陣風只有 {max(win_gusts):.0f} km/h" if win_gusts and max(win_gusts) < 40 else "，建議窗口內都大風，倒影基本冇望"
        gear.append(f"💨 全晚陣風最高 {max(gusts):.0f} km/h — 腳架掛重物{win_gust_s}")
    if not gear:
        gear.append("✅ 全晚無特別器材威脅")

    # ---------- 結構化數據（--json 用，亦係網頁資料來源） ----------
    fetched_at = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M %Z")
    data = {
        "location_id": loc_id,
        "name_zh": loc["name_zh"],
        "mountain": loc["mountain"],
        "lat": loc["lat"], "lon": loc["lon"], "elev_m": loc["elev_m"],
        "coord_source": loc["coord_source"],
        "night_date": date_str,
        "timezone": "America/Edmonton",
        "darkness_window": {
            "start": fmt(dark_start), "end": fmt(dark_end),
            "length_hours": round(win_len_min / 60, 2),
            "full_astronomical_darkness": full_darkness,
            "min_sun_altitude": round(min_alt, 1),
            "min_sun_altitude_time": fmt(min_alt_t),
            "note": darkness_note,
        },
        "moon": {
            "illumination_pct": round(illum, 1),
            "events": [{"type": k, "time": et.strftime("%H:%M")} for et, k in events],
            "overlap_minutes_in_window": overlap_min,
            "overlap_ratio": round(overlap_ratio, 2),
            "moon_free_minutes_in_window": round(moon_free_min),
            "moon_free_periods": [[fmt(a), fmt(b)] for a, b in moon_free_periods],
            "up_periods_night": [[fmt(a), fmt(b)] for a, b in moon_up_periods],
            "verdict": "月亮唔阻" if overlap_min == 0 else f"重疊 {overlap_min} 分鐘，無月時段 {moon_free_min:.0f} 分鐘",
        },
        "sun_curve": [[fmt(t), round(a, 1)] for t, a in samples[::3]],
        "vertical_mw": vertical_mw_note(night_date.month),
        "galactic_center": {
            "max_altitude_in_window": round(gc_best[1], 1) if gc_best else None,
            "max_altitude_time": fmt(gc_best[0]) if gc_best else None,
            "azimuth_deg": round(gc_best[2], 0) if gc_best else None,
            "direction": cardinal(gc_best[2]) if gc_best else None,
            "above_10deg_period": [fmt(gc_visible_hours[0]), fmt(gc_visible_hours[-1])] if gc_visible_hours else None,
        },
        "season_angle": season_angle,
        "smoke_assessment": smoke_assessment,
        "weights": {"cloud": 0.45, "moon": 0.25, "smoke": 0.20, "wind_reflection": 0.10},
        "night": {
            "score": round(night_score, 1),
            "grade_code": grade_code,
            "grade_zh": grade_zh,
            "vetoes": vetoes,
            "caps": [reason for _, reason in caps],
        },
        "best_window": ({
            "start": fmt(best3[1][0]["hour"]),
            "end": fmt(best3[1][-1]["hour"] + dt.timedelta(hours=1)),
            "hours": len(best3[1]),
        } if best3 else None),
        "hourly": [{
            "time": fmt(r["hour"]),
            "cloud_total_pct": r["total_cloud"], "cloud_low_pct": r["low"],
            "cloud_mid_pct": r["mid"], "cloud_high_pct": r["high"],
            "cloud_score": r["cloud_score"], "cloud_label": r["cloud_label"],
            "pm2_5": (round(r["pm"], 1) if r["pm"] is not None else None),
            "us_aqi": (round(r["aqi"], 0) if r["aqi"] is not None else None),
            "smoke_score": r["smoke_score"], "smoke_label": r["smoke_label"],
            "moon_altitude": round(r["moon_alt"], 1),
            "moon_score": r["moon_score"], "moon_label": r["moon_label"],
            "wind_kmh": r["wind"], "wind_dir": (cardinal(r["wind_dir"]) if r["wind_dir"] is not None else None),
            "gust_kmh": r["gust"],
            "wind_score": r["wind_score"], "wind_label": r["wind_label"],
            "temp_c": r["temp"], "rh_pct": r["rh"], "dew_point_c": r["dew"],
            "astro_score": round(r["score"], 1),
        } for r in rows],
        "gear_advice": gear,
        "sources": {
            "weather": ("MET Norway Locationforecast（後備：ECMWF IFS 9km；缺 gust/visibility）" if wx.get("_source") == "met_norway" else "Open-Meteo Forecast API（GEM 系 model，lat/lon 精確點）"),
            "air_quality": (("Open-Meteo Air Quality API（CAMS global ~40km 網格；僅兼容健康脈絡）" if aq else f"兼容空氣質素失敗：{aq_error}") + "；攝影煙霧採 ECCC FireWork＋CAMS global＋BlueSky Canada 獨立模型共識"),
            "astronomy": "skyfield + de421（本地計算）",
        },
        "fetched_at": fetched_at,
    }
    return data


# ---------- 文字報告 ----------

def render_text(d):
    out = []
    out.append(f"══ {d['name_zh']} — {d['mountain']} ══")
    out.append(f"座標 {d['lat']}, {d['lon']}（{d['coord_source']}）海拔 ~{d['elev_m']}m")
    out.append(f"夜晚：{d['night_date']} 18:00 → 翌日 10:00（America/Edmonton）")
    out.append("")
    dw = d["darkness_window"]
    out.append(f"【天文窗口】{dw['start']} → {dw['end']}（太陽最低 {dw['min_sun_altitude']}° @ {dw['min_sun_altitude_time']}）")
    if dw["note"]:
        out.append(f"  ⚠️ {dw['note']}")
    if dw["length_hours"] < 1.5:
        out.append(f"  ⚠️ 窗口只有 {dw['length_hours']} 小時，好短")
    out.append("")
    m = d["moon"]
    out.append(f"【月亮】照明 {m['illumination_pct']:.0f}%")
    if m["events"]:
        for e in m["events"]:
            out.append(f"  {e['type']} {e['time']}")
    else:
        out.append("  今晚無月出/月落事件")
    mark = "✅" if m["overlap_minutes_in_window"] == 0 else "❌"
    out.append(f"  窗口內地平線上：{m['overlap_minutes_in_window']} 分鐘（{mark} {m['verdict']}）")
    if m["moon_free_periods"]:
        periods = "、".join(f"{a}→{b}" for a, b in m["moon_free_periods"])
        out.append(f"  窗口內無月時段：{periods}")
    out.append("")
    gc = d["galactic_center"]
    if gc["max_altitude_in_window"] and gc["max_altitude_in_window"] > 0:
        out.append(f"【銀心】窗口內最高仰角 {gc['max_altitude_in_window']}° @ {gc['max_altitude_time']}，方位{gc['direction']}（{gc['azimuth_deg']:.0f}°）")
        if gc["above_10deg_period"]:
            out.append(f"  仰角 >10° 時段：{gc['above_10deg_period'][0]} → {gc['above_10deg_period'][1]}")
        else:
            out.append("  ⚠️ 窗口內銀心仰角 ≤10°，太貼地平線")
    else:
        out.append("【銀心】窗口內喺地平線下 — 目標轉冬季銀河 / Cygnus / 星野")
    sa = d.get("season_angle")
    if sa:
        out.append(f"【季節角度】{sa['score']}/10 — 構圖軸 {sa['composition_az']:.0f}° vs 銀心最近 {sa['min_separation_deg']:.0f}°（@ {sa['best_time']}）")
    out.append("")
    n = d["night"]
    out.append(f"【評級】{n['grade_code']} — {n['grade_zh']}（最佳 3 小時平均 {n['score']:.0f} 分）")
    for v in n["vetoes"]:
        out.append(f"  ⛔ {v}")
    for c in n["caps"]:
        out.append(f"  🔒 {c}")
    if d["best_window"]:
        bw = d["best_window"]
        out.append(f"【建議窗口】{bw['start']} → {bw['end']}（{bw['hours']} 小時）")
    out.append("")
    out.append("【逐小時（窗口內）】")
    out.append(f"{'時間':<6}{'雲%':>4}{'低':>4}{'中':>4}{'高':>4}{'PM2.5':>7}{'AQI':>5}{'風':>5}{'陣風':>5}{'月alt':>7}{'分':>5}  雲/月/煙/風")
    for r in d["hourly"]:
        pm_s = f"{r['pm2_5']:.1f}" if r["pm2_5"] is not None else "—"
        aqi_s = f"{r['us_aqi']:.0f}" if r["us_aqi"] is not None else "—"
        gust_s = f"{r['gust_kmh']:.0f}" if r["gust_kmh"] is not None else "—"
        out.append(f"{r['time']:<6}{r['cloud_total_pct']:>4.0f}{r['cloud_low_pct']:>4.0f}{r['cloud_mid_pct']:>4.0f}{r['cloud_high_pct']:>4.0f}"
                   f"{pm_s:>7}{aqi_s:>5}{r['wind_kmh']:>5.0f}{gust_s:>5}{r['moon_altitude']:>7.1f}{r['astro_score']:>5.0f}"
                   f"  {r['cloud_label']}/{r['moon_label']}/{r['smoke_label']}/{r['wind_label']}")
    out.append("")
    out.append("【器材保護（全晚至 08:00）】")
    for g in d["gear_advice"]:
        out.append(f"  {g}")
    out.append("")
    out.append(f"數據：{d['sources']['weather']} + {d['sources']['air_quality']} + {d['sources']['astronomy']}；抓取時間 {d['fetched_at']}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--location", required=True, help="location id 或 all")
    ap.add_argument("--date", default=None, help="夜晚開始日期 YYYY-MM-DD（預設今日）")
    ap.add_argument("--json", action="store_true", help="輸出結構化 JSON（網頁資料來源用）")
    args = ap.parse_args()

    date_str = args.date or dt.datetime.now(TZ).date().isoformat()
    locs = json.loads(LOCATIONS_FILE.read_text())

    if args.location == "all":
        items = list(locs.items())
        coords = [(loc["lat"], loc["lon"]) for _lid, loc in items]
        weather = fetch_weather_batch(coords)
        aq_error = None
        try:
            air_quality = fetch_air_quality_batch(coords)
        except Exception as e:
            aq_error = str(e)
            air_quality = [None] * len(items)
            print(f"[警告] 空氣質素 API 批次失敗：{e} — 全部機位煙塵以「無資料」處理", file=sys.stderr)
        results = [
            analyze(lid, loc, date_str, wx=weather[i], aq=air_quality[i], aq_error=aq_error)
            for i, (lid, loc) in enumerate(items)
        ]
    else:
        if args.location not in locs:
            sys.exit(f"未知 location：{args.location}。可用：{', '.join(locs)} 或 all")
        results = [analyze(args.location, locs[args.location], date_str)]

    if args.json:
        payload = results[0] if args.location != "all" else {"night_date": date_str, "locations": results}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for d in results:
            print(render_text(d))
            print()


if __name__ == "__main__":
    main()
