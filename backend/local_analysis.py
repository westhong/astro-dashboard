#!/usr/bin/env python3
"""local_analysis.py — pinned-node LLM 分析節點（IQ2_M @ localhost:8080）

規格：
- 每份 report 單次 completion（冇工具、冇多回合），schema FAIL 先 retry 一次（最多 2 次）
- 任何失敗都唔會令 exit code 非零——數據發佈永遠唔會被 LLM 阻塞
- 輸出寫入 report 嘅 "local_analysis" 欄位，綁 source_generated_utc
"""
import json, sys, time, datetime, urllib.request

LLM_URL = "http://127.0.0.1:8080/v1/chat/completions"
MODEL = "qwen3.6-35B-A3B-IQ2_M-local"
VERDICTS = ("值得去", "可以去", "邊緣", "留在家")
MAX_TOKENS = 4500
PER_CALL_TIMEOUT = 300

SCHEMA_PROMPT = """只輸出一個 JSON（唔准任何其他文字、唔准 markdown code fence），格式固定：
{"headline":"一句≤30字總結","best":"最佳location_id；若六個verdict全部係「留在家」則為null",
"locations":{"六個id每個都要有":{"verdict":"只准揀：值得去/可以去/邊緣/留在家","note":"≤12字原因"}}}"""


def digest_of(d):
    lines = [f"夜晚: {d['night_date']}"]
    for l in d["locations"]:
        if l.get("error"):
            lines.append(f"- {l['location_id']}: ERROR（無數據）")
            continue
        n = l["night"]
        veto = "；".join(v.split("：")[0] for v in n.get("vetoes", [])) or "無否決"
        gc = l.get("galactic_center", {})
        lines.append(f"- {l['location_id']}: {n['score']:.0f}分 {n['grade_code']} | {veto} | 銀心{gc.get('max_altitude_in_window')}°")
    return "\n".join(lines)


def call_llm(prompt):
    body = json.dumps({"model": "qwen3.6-local",
                       "messages": [{"role": "user", "content": prompt}],
                       "temperature": 0, "max_tokens": MAX_TOKENS}).encode()
    req = urllib.request.Request(LLM_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=PER_CALL_TIMEOUT) as r:
        resp = json.load(r)
    return resp["choices"][0]["message"].get("content") or ""


def validate(raw, ids):
    o = json.loads(raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip())
    assert isinstance(o.get("headline"), str) and 4 <= len(o["headline"]) <= 35, "headline 長度出界"
    best = o.get("best")
    assert best is None or best in ids, "best 唔係合法 id"
    locs = o.get("locations", {})
    assert set(locs) == set(ids), "locations key 唔齊"
    for i in ids:
        assert locs[i].get("verdict") in VERDICTS, f"{i} verdict 出界"
        assert isinstance(locs[i].get("note"), str) and len(locs[i]["note"]) <= 14, f"{i} note 太長"
    if all(locs[i]["verdict"] == "留在家" for i in ids):
        o["best"] = None  # 規則寫死：全留在家 → best=null
    return o


def analyze(path):
    d = json.load(open(path, encoding="utf-8"))
    ids = [l["location_id"] for l in d["locations"]]
    prompt = f"你是銀河拍攝助手。數據：\n{digest_of(d)}\n\n{SCHEMA_PROMPT}"
    last_err = None
    for attempt in (1, 2):
        try:
            out = validate(call_llm(prompt), ids)
            d["local_analysis"] = {
                "schema_version": 1,
                "source_generated_utc": d["generated_utc"],
                "analyzed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "model": MODEL,
                "headline": out["headline"],
                "best": out["best"],
                "locations": out["locations"],
            }
            json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
            print(f"{path}: PASS (attempt {attempt}) headline={out['headline']}")
            return True
        except Exception as e:
            last_err = e
            print(f"{path}: attempt {attempt} FAIL — {str(e)[:120]}")
    print(f"{path}: 放棄（{str(last_err)[:120]}），冇分析照出")
    return False


if __name__ == "__main__":
    t0 = time.time()
    results = [analyze(p) for p in sys.argv[1:]]
    print(f"local_analysis 完成：{sum(results)}/{len(results)} 份成功，{time.time()-t0:.1f}s")
    sys.exit(0)  # 永遠 exit 0
