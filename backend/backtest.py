#!/usr/bin/env python3
"""R1 — 日間評分回測（對照 fc.nekolens.tw /api/stats 思路）。

兩個職責：
1. archive_scores()：每次 build 將日間評分快照 append 到 repo 外嘅歷史檔
   （~/astro-history/daylight_scores.jsonl），唔污染 git tree（cron clean-tree gate）。
2. run_backtest()：將歷史評分同 West 嘅實地觀察（backend/field_observations.jsonl）
   逐宗對照，輸出 NPV／hitRate／MAE／byGrade。零觀察 → 誠實空狀態，唔造假數字。

觀察檔 schema（每行一宗 JSON）：
  {"date": "2026-08-05", "spot_id": "vermilion_rundle", "event": "sunset",
   "observed": "none|fair|good|epic", "note": "自由文字（可省略）"}
等級定義：none=冇燒 fair=淡 good=燒 epic=大燒
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
HISTORY_DIR = Path.home() / "astro-history"
HISTORY_FILE = HISTORY_DIR / "daylight_scores.jsonl"
OBS_FILE = HERE / "field_observations.jsonl"

OBSERVED_SCALE = {"none": 0, "fair": 1, "good": 2, "epic": 3}


def archive_scores(daylight_full: dict[str, dict]) -> int:
    """將每個 date 嘅日間 payload 快照歸檔；回傳寫入筆數。"""
    HISTORY_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    n = 0
    with HISTORY_FILE.open("a", encoding="utf-8") as fh:
        for date_str, dl in daylight_full.items():
            if dl.get("error"):
                continue
            for p in dl.get("points", []):
                if p.get("error"):
                    continue
                for ev, c in (p.get("events") or {}).items():
                    if c.get("error"):
                        continue
                    fh.write(json.dumps({
                        "archived_utc": stamp,
                        "date": date_str,
                        "spot_id": p["id"],
                        "event": ev,
                        "score": c.get("score"),
                        "components": c.get("components"),
                        "confidence": (c.get("confidence") or {}).get("level"),
                    }, ensure_ascii=False) + "\n")
                    n += 1
    return n


def _load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    rows = []
    for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _load_observations() -> list[dict]:
    if not OBS_FILE.exists():
        return []
    rows = []
    for line in OBS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if d.get("observed") in OBSERVED_SCALE and d.get("date") and d.get("spot_id") and d.get("event"):
            rows.append(d)
    return rows


def _predicted_scale(score: float | None) -> int | None:
    """分數 → 預測等級（0–3），同 label 門檻對齊：<45=0, 45–64=1, 65–79=2, ≥80=3。"""
    if score is None:
        return None
    if score >= 80:
        return 3
    if score >= 65:
        return 2
    if score >= 45:
        return 1
    return 0


def run_backtest() -> dict:
    history = _load_history()
    observations = _load_observations()
    base = {
        "history_entries": len(history),
        "observations": len(observations),
        "method": "歷史日間評分 vs West 實地觀察（field_observations.jsonl）；預測等級由分數映射（<45/45–64/65–79/≥80）",
    }
    if not observations:
        base["message"] = "暫時未有實地觀察數據——評分歷史已由今日起累積，有觀察後自動產生往績指標。"
        return base

    pairs = []
    for obs in observations:
        cands = [h for h in history
                 if h["date"] == obs["date"] and h["spot_id"] == obs["spot_id"] and h["event"] == obs["event"]]
        if not cands:
            continue
        # 用事件前最後一個快照（archived_utc 最晏嗰個）
        latest = max(cands, key=lambda h: h.get("archived_utc", ""))
        pred = _predicted_scale(latest.get("score"))
        if pred is None:
            continue
        pairs.append({
            "date": obs["date"], "spot_id": obs["spot_id"], "event": obs["event"],
            "score": latest["score"], "predicted": pred, "observed": OBSERVED_SCALE[obs["observed"]],
        })

    if not pairs:
        base["message"] = "有觀察但搵唔到對應日期嘅評分歷史（歷史由 2026-08-05 先開始累積）。"
        return base

    total = len(pairs)
    # NPV：預測 0（唔會燒）入面，實際真係 none 嘅比例 — 對標佢哋「排除沒戲」能力
    pred_none = [p for p in pairs if p["predicted"] == 0]
    npv = (sum(1 for p in pred_none if p["observed"] == 0) / len(pred_none)) if pred_none else None
    # PPV：預測 ≥2（值得去）入面，實際 good/epic 嘅比例
    pred_go = [p for p in pairs if p["predicted"] >= 2]
    ppv = (sum(1 for p in pred_go if p["observed"] >= 2) / len(pred_go)) if pred_go else None
    mae = sum(abs(p["predicted"] - p["observed"]) for p in pairs) / total
    exact = sum(1 for p in pairs if p["predicted"] == p["observed"]) / total
    by_grade = {}
    for p in pairs:
        g = by_grade.setdefault(p["predicted"], {"count": 0, "observed_sum": 0})
        g["count"] += 1
        g["observed_sum"] += p["observed"]

    base.update({
        "total_pairs": total,
        "npv_excluding_no_burn": (round(npv, 2) if npv is not None else None),
        "ppv_worth_going": (round(ppv, 2) if ppv is not None else None),
        "mean_abs_error": round(mae, 2),
        "exact_grade_rate": round(exact, 2),
        "by_predicted_grade": {str(k): {"count": v["count"], "avg_observed": round(v["observed_sum"] / v["count"], 2)}
                               for k, v in sorted(by_grade.items())},
        "pairs": pairs[-20:],
    })
    return base


if __name__ == "__main__":
    print(json.dumps(run_backtest(), ensure_ascii=False, indent=2))
