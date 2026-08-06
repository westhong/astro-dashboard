# Requirement：向 fc.nekolens.tw 借鑑嘅功能清單

日期：2026-08-05
來源對照：https://fc.nekolens.tw/ （台灣業餘攝影師自製火燒雲預測站）
流程：West 逐項批准 → 批准項先郁手（未批唔做）
適用範圍：astro-dashboard 日間（日出/日落）評分系統

---

## 背景觀察（證據）

| 佢哋做法 | 實測證據 |
|---|---|
| 原始評分 + 衛星實測修正 | UI：「衛星實測顯示天空雲比預報更多（觀測/預報 0.77），下修 5 分」 |
| 用戶回報 + 自動回測 | `/api/stats` 回傳 hitRate 0.38、meanAbsError 2.13、byGrade 觀測對照（epic/good/fair/low/none） |
| 三模型信心指數 | footer：「信心指數來自 ECMWF / GFS / ICON 三模式分歧度」 |
| 峰值時段預測 | UI：「最濃 18:48–18:58」 |
| 門檻推播 | UI：「訂閱推播：分數達 60 通知我」 |
| 多日展望 | 5 日分數條（8/6–8/10，每日分數＋評語） |
| 誠實定位 | 「擅長排除沒戲的日子（沒燒場次約八成事先睇到），但高分就一定會燒還做不到」 |

---

## R1 — 回測指標 pipeline（P1，最優先）

- **現狀**：`backend/field_observations.jsonl` 有 West 實地觀察，但冇任何自動對照歷史評分嘅機制。
- **目標**：每晚 build 時自動將歷史日間評分 vs 實地觀察對照，產生：
  - 沒戲預測命中率（NPV：評分低且實際冇燒嘅比例）——對標佢哋「八成排除沒戲」
  - hitRate、meanAbsError（分數 vs 觀測等級）
  - byGrade 對照表（GO/WATCH/STAY 各級實際結果分佈）
- **做法**：純本地數據工程，零 API 成本；輸出 `docs/backtest.json`，前端 Tier-2 折疊顯示「評分往績」。
- **點解先佢**：佢哋 hitRate 只有 0.38——證明連成熟系統都測唔準「一定燒」。我哋冇回測就唔知自己把尺有幾準，所有其他改進都冇基準。
- **成本**：低（1 個 script + 前端一個折疊區）。

## R2 — 衛星實測雲量修正（P2）

- **現狀**：日間評分 100% 依賴預報雲量，當日預報偏差冇校正。
- **目標**：當日日出/日落前數小時，用 GOES-18 衛星雲量實測 vs 預報比，調整日間分（對標佢哋「觀測/預報 0.77 下修 5 分」）。
- **底子**：`fire-sky-forecast-research` skill 已有 GOES-18+DEM horizon pipeline 研究，唔使由零開始。
- **成本**：中高（衛星數據 pipeline + 山區 DEM 地平線遮擋處理）。
- **依賴**：建議 R1 先上，修正效果先可以量化驗證。

## R3 — 信心指數（三模型分歧）（P2）

- **現狀**：雙模型（best_match vs ECMWF IFS 0.25）保守風分 + 分歧 badge，但冇整體「信心 高/中/低」。
- **目標**：加入第三模型（GFS 或 ICON，Open-Meteo 免 key 支援），以三模型雲量分歧度產生信心等級，顯示喺日間卡。
- **注意**：Open-Meteo multi-model key 有後綴 quirk（Pitfall 19），parser 要照顧 `*_gfs_seamless` 等命名。
- **成本**：低中（數據源現成，主要係分歧度算法 + UI badge）。

## R4 — 峰值時段預測（「最濃 HH:MM–HH:MM」）（P3）

- **現狀**：日間卡俾幾何事件 ±1h 窗口，冇「邊 10–20 分鐘最濃」。
- **目標**：由逐小時雲層結構 + 太陽仰角曲線，標出預計色彩峰值時段（對標佢哋「最濃 18:48–18:58」）。
- **誠實限制**：逐小時預報解像度所限，精度只係指示性；文案要講明。
- **成本**：中。

## R5 — 日間分數門檻 Telegram 推播（P3）

- **現狀**：cron 每 30 分鐘更新但靜默；冇主動通知。
- **目標**：日間評分 ≥ 門檻（e.g. 75）且屬當日事件 → Telegram 通知 West（機位＋分數＋窗口）。
- **底子**：Hermes cron + Telegram delivery 現成，參考 space-weather-watchdog 嘅靜默/告警 pattern。
- **成本**：低。
- **規矩**：失敗要 retry，唔准靜默吞（West 既定原則）；重複通知去重（同一晚同一機位只通知一次）。

## R6 — 實地回報入口（P3，R1 嘅數據來源）

- **現狀**：field_observations 靠 West 口頭話我知先記錄。
- **目標**：最簡入口記錄「今晚有冇燒／燒幾勁」（Telegram 指令或 dashboard 一個簡單按鈕），寫落 field_observations.jsonl 俾 R1 用。
- **成本**：中。
- **建議**：先 Telegram 指令（零前端改動），唔好一開始做 web form。

## R7 — 五日展望條（P3）

- **現狀**：dashboard 只出 3 日（report-0/1/2）。
- **目標**：日間分數加 5 日展望條（對標佢哋 8/6–8/10 分數條）。
- **限制**：Open-Meteo 預報第 4–5 日可靠性跌，要加「遠期低信心」標示；build 時間會加長（現時 8 機位×3 日已接近 timeout 邊緣——要先解決 build 時長）。
- **成本**：低中。

---

## 明確唔借（out of scope）

- 贊助/咖啡/二手相機導流
- 地理位置自動定位（我哋係固定機位庫，唔係附近搜尋）
- Web Push API（我哋行 Telegram，唔使瀏覽器推播）
- 佢哋嘅台灣機位資料（地理無關）

## 共同原則（唔使改）

- 「條件機率，唔係保證」嘅誠實定位——兩邊一致，繼續保持
- 評分邏輯 single source of truth 喺 skill，dashboard 只顯示
- VERSION ↔ code 1-to-1、repo 零 credential、誠實失敗唔放假數據
