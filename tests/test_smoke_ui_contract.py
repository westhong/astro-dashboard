import re
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HTML_PATH = ROOT / "static" / "index.html"


class SmokeUIContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = HTML_PATH.read_text(encoding="utf-8")

    def test_shared_helpers_and_required_chinese_contract_exist(self):
        for helper in (
            "isFiniteNumber", "fmtNumber", "fmtRange", "fmtDistance", "sourceSupportZh",
            "smokeNoteZh", "unwrapSmokeAssessment",
            "pollutantZh", "smokeStatusZh", "smokeClassZh",
            "photographySmokeZh", "transparencyZh", "smokeSummaryHTML", "smokeDetailHTML",
        ):
            self.assertRegex(self.html, rf"function\s+{helper}\s*\(")
        for text in (
            "已由三模型驗證乾淨", "大致乾淨", "煙帶邊界，有變動風險",
            "有實質煙霧風險", "重煙，攝影否決", "模型分歧，結果不確定",
            "僅單一模型覆蓋，結果不確定", "乾淨", "霧霾", "煙霧", "重煙", "無資料",
            "未見明顯煙霧懲罰", "各模型對煙帶位置判斷不一致，出發前需重新確認",
            "煙帶輕微移動即可改變拍攝條件", "煙霧資料不足，不能判定乾淨",
            "資料暫缺", "PM2.5", "PM10", "O₃", "NO₂",
        ):
            self.assertIn(text, self.html)

    def test_tier_one_summary_separates_health_from_photography(self):
        self.assertIn('class="smoke-summary', self.html)
        self.assertIn('class="smoke-health"', self.html)
        self.assertIn('class="smoke-photo"', self.html)
        self.assertIn('class="smoke-transparency"', self.html)
        self.assertIn("健康 AQI", self.html)
        self.assertIn("攝影煙霧", self.html)
        self.assertIn("主要污染物", self.html)
        self.assertIn("consensus_pm2_5", self.html)
        self.assertNotIn("aqiSVG(", self.html)

    def test_tier_one_is_injected_after_mini3_for_night_and_daylight(self):
        night = re.search(r"function cardHTML\b[\s\S]*?function sorryHTML", self.html).group(0)
        day = re.search(r"function daylightCard\b[\s\S]*?function renderDaylight", self.html).group(0)
        for section in (night, day):
            self.assertRegex(section, r'mini3[\s\S]*?smokeSummaryHTML\(')
            self.assertLess(section.index("smokeSummaryHTML("), section.index('class="bonus"'))
            self.assertIn("smokeDetailHTML(", section)
        self.assertIn("condition_cap", day)

    def test_formal_assessment_drives_both_minis_with_legacy_fallback(self):
        for name, end in (("smokeMini", "windRow"), ("daySmokeMini", "daylightCard")):
            section = re.search(rf"function {name}\b[\s\S]*?function {end}", self.html).group(0)
            self.assertIn("unwrapSmokeAssessment", section)
            self.assertIn("photography_smoke_score", section)
            self.assertIn("consensus_pm2_5", section)
            self.assertIn("單一舊版資料推估", section)
            self.assertIn("尚無三模型覆蓋資料", section)
            self.assertNotIn("aqiSVG", section)
        self.assertIn("健康 AQI", self.html)

    def test_coverage_and_risk_are_visible_without_condition_cap(self):
        for text in (
            "2/3 模型覆蓋，屬部分資料", "1/3 模型覆蓋，不能判定乾淨",
            "0/3 模型覆蓋，無法評估", "⚠️", "⛔",
        ):
            self.assertIn(text, self.html)
        for css in (".smoke-summary.risk", ".smoke-summary.veto", ".smoke-coverage"):
            self.assertIn(css, self.html)

    def test_tier_two_model_details_and_safe_backend_strings(self):
        for text in (
            "煙霧模型、覆蓋與資料時間", "ECCC FireWork", "CAMS global", "BlueSky Canada",
            "窗口平均", "窗口範圍", "鄰近範圍", "參考時間", "模型週期",
            "有效時段", "擷取時間", "來源", "供應端未公開 CAMS 模型週期",
            "預報識別碼", "無資料／超出有效時段", "來源佐證", "不確定因素",
        ):
            self.assertIn(text, self.html)
        section = re.search(r"function smokeDetailHTML\b[\s\S]*?function smokeMini", self.html).group(0)
        self.assertRegex(section, r"(?:esc|fmtBackendText)\([^\n]*status")
        self.assertIn("fmtBackendText(m.source)", section)
        self.assertRegex(section, r"uncertainties[^\n]*map\(x=>smokeNoteZh\(x\)\)")
        self.assertIn("sourceSupportZh(support.classification)", section)
        self.assertIn("fmtDistance(support.nearest_confirmed_fire_km)", section)
        self.assertIn("fmtDistance(support.nearest_satellite_hotspot_km)", section)
        self.assertIn("table-wrap", section)
        self.assertIn("cycle_status==='not_exposed_by_open_meteo'", section)

    def test_photography_status_and_transparency_behavior_under_node(self):
        helpers = re.search(
            r"function isFiniteNumber\b[\s\S]*?(?=function smokeMini)", self.html
        ).group(0)
        checks = r'''
const assessment=(status,pm,valid=3)=>({consensus:{status,consensus_pm2_5:pm,coverage:{valid,total:3}}});
const expected={
  MODEL_SPLIT:'模型分歧，結果不確定',
  RISKY_BOUNDARY:'位於煙帶邊界',
  SMOKE_RISK:'有實質煙霧風險',
  VETO:'重煙，攝影否決',
  SINGLE_MODEL_ONLY:'僅單一模型覆蓋，結果不確定'
};
for(const [status,text] of Object.entries(expected)){
  const valid=status==='SINGLE_MODEL_ONLY'?1:3;
  if(photographySmokeZh(assessment(status,7.7,valid))!==text) throw new Error('photo '+status);
}
if(photographySmokeZh(assessment('LIKELY_CLEAN',null,0))!=='煙霧資料不足，不能判定乾淨') throw new Error('coverage zero');
if(transparencyZh(assessment('VETO',70))!=='重煙造成攝影否決') throw new Error('veto transparency');
if(transparencyZh(assessment('SMOKE_RISK',30))!=='低空與遠山對比可能受影響') throw new Error('risk transparency');
if(transparencyZh(assessment('LIKELY_CLEAN',18))==='未見明顯煙霧懲罰') throw new Error('haze transparency');
if(transparencyZh(assessment('VERIFIED_CLEAN',7.7))!=='未見明顯煙霧懲罰') throw new Error('clean transparency');
if(sourceSupportZh('SATELLITE_HOTSPOT_ONLY')!=='僅有衛星熱點佐證，未有官方大型火災確認') throw new Error('source support hotspot');
if(sourceSupportZh('NO_IDENTIFIED_SOURCE')!=='暫未找到足以解釋濃度的火源') throw new Error('source support none');
if(smokeNoteZh('Open-Meteo does not expose the CAMS model cycle/reference time.')!=='供應端未公開 CAMS 模型週期／參考時間') throw new Error('CAMS note');
if(smokeNoteZh('Partial model coverage: 2/3 valid models.')!=='模型僅 2/3 覆蓋') throw new Error('coverage note');
if(fmtDistance(null)!=='資料暫缺'||fmtDistance(53.8)!=='53.8 km') throw new Error('distance');
const malformed=assessment('LIKELY_CLEAN',Infinity,3);
malformed.window_local={start:Infinity,end:null,timezone:undefined};
malformed.pollutants={us_aqi_health_context:NaN,dominant_pollutant:'ozone'};
malformed.models={cams_global:{valid:true,window_avg_pm2_5:NaN,window_range:[Infinity,-1],neighbor_range:null,valid_range:[Infinity,undefined],source:'<img src=x onerror=1>',cycle_status:'not_exposed_by_open_meteo'}};
const malformedHTML=smokeSummaryHTML(malformed)+smokeDetailHTML(malformed);
if(/NaN|Infinity|null|undefined/.test(malformedHTML)) throw new Error('malformed numeric leak');
if(malformedHTML.includes('<img src=x')) throw new Error('backend string not escaped');
'''
        source = "function esc(v){return String(v??'').replace(/[&<>\\\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\\\"':'&quot;'}[c]))}\n" + helpers + checks
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
            handle.write(source)
            path = handle.name
        try:
            completed = subprocess.run(["node", path], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_css_mobile_and_details_touch_contract(self):
        for css in (
            ".smoke-summary", ".smoke-health", ".smoke-photo", ".smoke-transparency",
            ".smoke-detail", ".smoke-table-wrap", "overflow-x:auto", "overflow-wrap:anywhere",
            "min-height:44px", "[hidden]",
        ):
            self.assertIn(css, self.html)

    def test_hourly_and_footers_name_health_and_three_smoke_models(self):
        self.assertIn("<th>健康 AQI</th>", self.html)
        self.assertIn("舊版 CAMS 時序參考", self.html)
        for text in ("ECCC FireWork", "CAMS global", "BlueSky Canada", "健康 AQI 與攝影煙霧分開顯示"):
            self.assertIn(text, self.html)

    def test_no_ai_analysis_or_cantonese_ui_regression(self):
        self.assertNotIn("ai_analysis", self.html)
        self.assertNotIn("愛的判讀", self.html)
        self.assertNotIn("愛的攝影判讀", self.html)

    def test_script_parses_under_node(self):
        script = re.search(r"<script>([\s\S]*?)</script>", self.html).group(1)
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
            handle.write(script)
            path = handle.name
        try:
            completed = subprocess.run(["node", "--check", path], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
