import unittest
import inspect
import re
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from backend import build_report
import app


def location(location_id, grade="GO", score=90, start="22:00", clouds=None):
    clouds = clouds or {"22:00": 10, "23:00": 10, "00:00": 10}
    return {
        "location_id": location_id,
        "name_zh": location_id,
        "night_date": "2026-09-04",
        "night": {"grade_code": grade, "grade_zh": grade, "score": score,
                  "caps": [], "vetoes": []},
        "best_window": {"start": start, "end": "01:00", "hours": 3},
        "hourly": [{"time": hour, "cloud_total_pct": value} for hour, value in clouds.items()],
    }


class ForecastRevisionTests(unittest.TestCase):
    def test_static_build_wires_prior_report_zero_into_revision_check(self):
        source = inspect.getsource(build_report.main)
        self.assertIn("apply_forecast_revision(results, prior_reports.get(date_str), date_str)", source)
        self.assertNotIn("if offset == 0", source)

    def test_prior_reports_are_indexed_by_night_date_across_rollover(self):
        with tempfile.TemporaryDirectory() as temporary:
            docs = Path(temporary)
            for offset, night_date in enumerate(("2026-09-07", "2026-09-08", "2026-09-09")):
                (docs / f"report-{offset}.json").write_text(
                    '{"night_date":"' + night_date + '","locations":[]}', encoding="utf-8"
                )

            prior = build_report.load_prior_reports(docs)

        self.assertEqual(set(prior), {"2026-09-07", "2026-09-08", "2026-09-09"})
        self.assertEqual(prior["2026-09-08"]["night_date"], "2026-09-08")

    def test_same_night_large_best_window_cloud_shift_marks_low_confidence_and_caps_grade(self):
        current = location("a", score=94, clouds={"22:00": 50, "23:00": 10, "00:00": 10})
        prior = {
            "night_date": "2026-09-04",
            "generated_utc": "2026-09-04T18:00:00+00:00",
            "locations": [location("a", clouds={"22:00": 10, "23:00": 10, "00:00": 10})],
        }

        build_report.apply_forecast_revision([current], prior, "2026-09-04")

        self.assertEqual(current["night"]["score"], 94)
        self.assertEqual(current["night"]["grade_code"], "MARGINAL")
        marker = current["forecast_revision"]
        self.assertTrue(marker["low_confidence"])
        self.assertEqual(marker["max_cloud_shift_pct"], 40)
        self.assertIn("cloud_shift", marker["reasons"])
        self.assertIn("預報修訂不穩定", current["night"]["caps"])

    def test_two_hour_best_window_change_marks_low_confidence(self):
        current = location("a", score=91, start="00:00")
        prior_loc = location("a", score=89, start="22:00")
        prior = {"night_date": "2026-09-04", "locations": [prior_loc]}

        build_report.apply_forecast_revision([current], prior, "2026-09-04")

        self.assertEqual(current["night"]["grade_code"], "MARGINAL")
        self.assertEqual(current["forecast_revision"]["best_window_start_shift_hours"], 2)
        self.assertIn("best_window_shift", current["forecast_revision"]["reasons"])

    def test_one_hour_window_shift_still_flags_large_change_in_dropped_prior_hour(self):
        current = location(
            "a", score=94, start="23:00",
            clouds={"22:00": 100, "23:00": 10, "00:00": 10, "01:00": 10},
        )
        current["best_window"]["end"] = "02:00"
        prior_loc = location(
            "a", score=94, start="22:00",
            clouds={"22:00": 0, "23:00": 10, "00:00": 10, "01:00": 10},
        )
        prior = {
            "night_date": "2026-09-04",
            "generated_utc": "2026-09-04T18:00:00+00:00",
            "locations": [prior_loc],
        }

        build_report.apply_forecast_revision([current], prior, "2026-09-04")

        self.assertEqual(current["night"]["grade_code"], "MARGINAL")
        self.assertEqual(current["forecast_revision"]["max_cloud_shift_pct"], 100)
        self.assertIn("cloud_shift", current["forecast_revision"]["reasons"])

    def test_old_date_report_does_not_affect_current_forecast(self):
        current = location("a", score=94, clouds={"22:00": 70})
        prior_loc = location("a", clouds={"22:00": 0})
        prior_loc["night_date"] = "2026-09-03"
        prior = {"night_date": "2026-09-03", "locations": [prior_loc]}

        build_report.apply_forecast_revision([current], prior, "2026-09-04")

        self.assertEqual(current["night"]["grade_code"], "GO")
        self.assertNotIn("forecast_revision", current)


class RecommendationRankingTests(unittest.TestCase):
    def test_static_selector_ranks_final_grade_then_score_and_rejects_risky(self):
        marginal = location("marginal", grade="MARGINAL", score=99)
        good = location("good", grade="GOOD", score=70)
        risky = location("risky", grade="RISKY", score=100)

        self.assertEqual(build_report.select_best_location([marginal, good, risky])["location_id"], "good")
        self.assertIsNone(build_report.select_best_location([risky]))


class LiveRecommendationRankingTests(unittest.IsolatedAsyncioTestCase):
    async def test_lan_build_report_uses_final_grade_before_score(self):
        results = {
            "marginal": location("marginal", grade="MARGINAL", score=99),
            "good": location("good", grade="GOOD", score=70),
        }

        async def run(loc_id, _date, _sem):
            return results[loc_id]

        with patch.object(app, "location_ids", return_value=list(results)), patch.object(
            app, "run_one", new=AsyncMock(side_effect=run)
        ), patch.object(app, "build_spots", return_value=[]), patch.object(
            app, "build_daylight_report", return_value={}
        ):
            payload = await app.build_report("2026-09-04")

        self.assertEqual(payload["best_location_id"], "good")

    async def test_lan_build_report_applies_same_night_forecast_revision(self):
        current = location("a", grade="GO", score=94,
                           clouds={"22:00": 50, "23:00": 10, "00:00": 10})
        prior = {
            "night_date": "2026-09-04",
            "generated_utc": "2026-09-04T18:00:00+00:00",
            "locations": [location("a", clouds={"22:00": 10, "23:00": 10, "00:00": 10})],
        }

        with patch.object(app, "location_ids", return_value=["a"]), patch.object(
            app, "run_one", new=AsyncMock(return_value=current)
        ), patch.object(app, "build_spots", return_value=[]), patch.object(
            app, "build_daylight_report", return_value={}
        ):
            payload = await app.build_report("2026-09-04", prior_payload=prior)

        self.assertEqual(payload["locations"][0]["night"]["grade_code"], "MARGINAL")
        self.assertEqual(payload["best_location_id"], "a")
        self.assertIn("forecast_revision", payload["locations"][0])


class RecommendationUIContractTests(unittest.TestCase):
    @staticmethod
    def html():
        return (Path(__file__).resolve().parents[1] / "static" / "index.html").read_text(encoding="utf-8")

    def test_frontend_ranks_final_grade_and_only_stars_good_or_go(self):
        html = self.html()
        section = re.search(r"const NIGHT_GRADE_RANK[\s\S]*?(?=function cardHTML)", html)
        self.assertIsNotNone(section)
        checks = r'''
const loc=(id,grade,score)=>({location_id:id,night:{grade_code:grade,score}});
const ranked=rankNightLocations([loc('m','MARGINAL',99),loc('g','GOOD',70),loc('r','RISKY',100)]);
if(ranked[0].location_id!=='g') throw new Error('grade rank');
const withNoData=rankNightLocations([loc('n','NO_DATA',0),loc('g','GOOD',70)]);
if(withNoData.length!==2||withNoData[1].location_id!=='n') throw new Error('NO_DATA card lost');
if(nightRecommendation([loc('n','NO_DATA',0)]).candidate!==null) throw new Error('NO_DATA became candidate');
if(!nightRecommendation(ranked).recommended) throw new Error('recommendation');
const bad=nightRecommendation([loc('r','RISKY',100),loc('s','STAY_HOME',20)]);
if(bad.recommended||bad.candidate.location_id!=='r') throw new Error('all bad');
if(!nightStar(loc('g','GOOD',70))||nightStar(loc('m','MARGINAL',99))||nightStar(loc('r','RISKY',100))) throw new Error('star policy');
'''
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
            handle.write(section.group(0) + checks)
            path = handle.name
        try:
            completed = subprocess.run(["node", path], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_hero_labels_marginal_candidate_as_conditional_not_best(self):
        html = self.html()
        helpers = re.search(r"const NIGHT_GRADE_RANK[\s\S]*?(?=function cardHTML)", html).group(0)
        hero = re.search(r"function heroHTML[\s\S]*?(?=function spotCard)", html).group(0)
        checks = r'''
const currentOffset=0;
const GRADE_COLOR={GO:'green',GOOD:'green',MARGINAL:'yellow',RISKY:'orange',STAY_HOME:'red'};
function esc(v){return String(v)}
const data={locations:[
 {location_id:'m',name_zh:'條件點',night:{grade_code:'MARGINAL',score:79}}
]};
const output=heroHTML(data);
if(!output.includes('有風險')) throw new Error('marginal risk hidden');
if(output.includes('最佳候選')) throw new Error('marginal called best');
if(output.includes('⭐')) throw new Error('starred marginal option');
'''
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
            handle.write(helpers + hero + checks)
            path = handle.name
        try:
            completed = subprocess.run(["node", path], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        finally:
            Path(path).unlink(missing_ok=True)

    def test_hero_does_not_recommend_or_star_when_all_locations_are_risky(self):
        html = self.html()
        helpers = re.search(r"const NIGHT_GRADE_RANK[\s\S]*?(?=function cardHTML)", html).group(0)
        hero = re.search(r"function heroHTML[\s\S]*?(?=function spotCard)", html).group(0)
        checks = r'''
const currentOffset=0;
const GRADE_COLOR={GO:'green',GOOD:'green',MARGINAL:'yellow',RISKY:'orange',STAY_HOME:'red'};
function esc(v){return String(v)}
const data={locations:[
 {location_id:'r',name_zh:'風險點',night:{grade_code:'RISKY',score:99}},
 {location_id:'s',name_zh:'留家點',night:{grade_code:'STAY_HOME',score:20}}
]};
const output=heroHTML(data);
if(!output.includes('今晚不建議拍攝')) throw new Error('missing no-go message');
if(output.includes('⭐')) throw new Error('starred risky option');
'''
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
            handle.write(helpers + hero + checks)
            path = handle.name
        try:
            completed = subprocess.run(["node", path], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        finally:
            Path(path).unlink(missing_ok=True)
        card = re.search(r"function cardHTML[\s\S]*?(?=function sorryHTML)", html).group(0)
        self.assertIn("isBest&&nightStar(loc)", card)

    def test_cards_never_treat_risky_backend_best_id_as_a_candidate(self):
        render = re.search(r"function render\(data\)[\s\S]*?(?=function loading)", self.html()).group(0)
        self.assertIn(
            "const analysisBestId=nightRecommendation(ok).recommended?nightRecommendation(ok).candidate.location_id:null;",
            render,
        )

    def test_risky_cards_suppress_legacy_recommendation_badges(self):
        local_verdict = re.search(r"function localVerdict[\s\S]*?(?=const NIGHT_GRADE_RANK)", self.html()).group(0)
        self.assertIn("NIGHT_GRADE_RANK[loc.night.grade_code]<NIGHT_GRADE_RANK.MARGINAL", local_verdict)

    def test_window_summary_handles_best_window_crossing_midnight(self):
        html = self.html()
        helper = re.search(r"function windowHours[\s\S]*?(?=const avg=)", html).group(0)
        checks = r'''
const loc={best_window:{start:'22:00',end:'01:00'},hourly:[
 {time:'21:00'},{time:'22:00'},{time:'23:00'},{time:'00:00'},{time:'01:00'}
]};
const result=windowHours(loc).map(h=>h.time).join(',');
if(result!=='22:00,23:00,00:00') throw new Error('midnight window: '+result);
'''
        with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
            handle.write(helper + checks)
            path = handle.name
        try:
            completed = subprocess.run(["node", path], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        finally:
            Path(path).unlink(missing_ok=True)


class VersionTests(unittest.TestCase):
    def test_lan_serves_root_icon_assets(self):
        self.assertTrue(any(getattr(route, "path", None) == "/icons" for route in app.app.routes))

    def test_patch_version_is_bumped(self):
        version = (Path(__file__).resolve().parents[1] / "VERSION").read_text(encoding="utf-8").strip()
        self.assertEqual(version, "2.28.3")


if __name__ == "__main__":
    unittest.main()
