#!/usr/bin/env python3
"""離線風模型實測對比（R10 field-observation 閉環）。

讀取 backend/field_observations.jsonl（每行一筆 West 現場實測）：
  {"date":"2026-08-05","point_id":"vermilion","event":"sunset",
   "observed_wind_kmh":4.0,"observed_calm_time":"21:30","note":"湖面 21:00 後平靜"}

對照 docs/report-{0,1,2}.json 內 daylight 的 wind_detail，輸出兩模型誤差統計。
注意：report 只保留最近三日，過期觀測會標「無對應 report」——統計隨 cron 累積。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
OBS_FILE = REPO / "backend" / "field_observations.jsonl"
DOCS = REPO / "docs"


def load_reports() -> dict[str, dict]:
    out = {}
    for f in sorted(DOCS.glob("report-*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        day = (d.get("daylight") or {}).get("date")
        if day:
            out[day] = d
    return out


def hhmm_to_min(t: str | None) -> int | None:
    if not t:
        return None
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def main() -> None:
    if not OBS_FILE.exists() or not OBS_FILE.read_text(encoding="utf-8").strip():
        print("field_observations.jsonl 暫無記錄——外拍後按 schema 逐行加入。")
        return
    reports = load_reports()
    errs: dict[str, list[float]] = {"best_match": [], "ecmwf": []}
    calm_errs: dict[str, list[float]] = {"best_match": [], "ecmwf": []}
    matched = 0
    for line in OBS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obs = json.loads(line)
        rep = reports.get(obs["date"])
        if not rep:
            print(f"{obs['date']} {obs['point_id']} {obs['event']}：無對應 report（已過三日窗口）")
            continue
        pt = next((p for p in rep["daylight"]["points"] if p.get("id") == obs["point_id"]), None)
        wd = (((pt or {}).get("events") or {}).get(obs["event"]) or {}).get("wind_detail")
        if not wd:
            print(f"{obs['date']} {obs['point_id']} {obs['event']}：report 無 wind_detail")
            continue
        matched += 1
        ow = obs.get("observed_wind_kmh")
        if ow is not None:
            for key, field in [("best_match", "best_mean"), ("ecmwf", "ecmwf_mean")]:
                if wd.get(field) is not None:
                    errs[key].append(abs(wd[field] - ow))
        oc = hhmm_to_min(obs.get("observed_calm_time"))
        if oc is not None:
            for key, field in [("best_match", "calm_best"), ("ecmwf", "calm_ecmwf")]:
                pred = hhmm_to_min(wd.get(field))
                if pred is not None:
                    calm_errs[key].append(abs(pred - oc))
        print(f"{obs['date']} {obs['point_id']} {obs['event']}：實測 {ow} km/h / 平靜 {obs.get('observed_calm_time')} vs "
              f"預設 {wd.get('best_mean')} / {wd.get('calm_best')} · ECMWF {wd.get('ecmwf_mean')} / {wd.get('calm_ecmwf')}")
    print(f"\n對應到 report 嘅觀測：{matched}")
    for key in errs:
        if errs[key]:
            mae = sum(errs[key]) / len(errs[key])
            print(f"{key}: 風速 MAE {mae:.1f} km/h（n={len(errs[key])}）"
                  + (f"；平靜時間 MAE {sum(calm_errs[key])/len(calm_errs[key]):.0f} 分鐘（n={len(calm_errs[key])}）" if calm_errs[key] else ""))


if __name__ == "__main__":
    sys.exit(main())
