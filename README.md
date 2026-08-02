# 🌌 astro-dashboard

West 嘅落磯山銀河拍攝條件手機儀表板。

六個指定機位嘅即時評分，數據全部嚟自 `rockies-milkyway-scout` skill（Hermes agent）：

- **只用機位本身座標**嘅 Open-Meteo 逐小時預報（唔用附近城鎮代替）
- CAMS global PM2.5 / US AQI 煙塵數據
- skyfield 本地計算：太陽仰角、天文黑夜窗口、月出月落、銀心位置

## 評分（West 2026-07-28 定）

權重：**雲 45% / 月 25% / 煙 20% / 風（倒影）10%**

否決三項（任何一項 → STAY_HOME，湖面再靜都冇用）：

- ⛔ 雲：建議窗口平均雲分 ≤15
- ⛔ 月：照明 ≥85% 且窗口內無月時段 <30 分鐘
- ⛔ 煙：窗口 PM2.5 平均 >55

風永遠唔否決；無風（≤6 km/h 鏡面）喺佢 10% 權重內攞滿分。

完整門檻表見 Hermes skill `photography/rockies-milkyway-scout/SKILL.md`。

## 架構

```
app.py                     FastAPI backend（LAN 即時模式）— 限流並行 + 失敗自動重試
static/index.html          Mobile-first frontend（無外部依賴，暗色星空主題）
                           雙模式：LAN 試 /api/report，失敗自動轉靜態 report-N.json
backend/scripts/           Self-contained 分析（night_report.py，內建 429 retry）
backend/references/        locations.json（機位座標單一來源嘅 copy）
backend/build_report.py    產生原始 docs/report-{0,1,2}.json
backend/ai_analysis.py     驗證並合併 Hermes 的 ai_analysis
backend/local_analysis.py  本機 Llama 可用時，為三份報告加入 local_analysis
.github/workflows/         僅保留手動 deterministic fallback
```

**Public 模式（GitHub Pages）**：Hermes 的 no-agent cron 每五分鐘直接執行固定更新腳本，在 Windows 本機建置資料並更新 report JSON；Pages 只讀 JSON 並套用 template。這不是一次互動式 agent 執行；不過若本機 Llama Server 可用，腳本會額外執行 `backend/local_analysis.py`，為三份報告加入本機模型分析。若 Llama 不可用，固定資料更新仍會繼續。
**全條鏈零 credential**：Open-Meteo / CAMS / skyfield 全部唔使 key，repo 入面冇任何秘密。

**LAN 模式**：`python3 app.py` → http://\<LAN IP\>:8788（即時按掣重跑）

分析失敗嘅機位會誠實顯示道歉卡 + 原因，**絕不顯示假數據**。

## 機位

Vermilion Lakes · Two Jack Lake · Herbert Lake · Lake Louise · Bow Lake · Lake Minnewanka
（座標單一來源：skill 嘅 `references/locations.json`）
