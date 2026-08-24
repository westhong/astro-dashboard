import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from backend.scripts import night_report
from backend import build_report


class FakeAstro:
    def __init__(self, *args): pass
    def sun_alt(self, when): return -20.0
    def moon_illum(self, when): return 10.0
    def moon_events(self, start, end): return []
    def moon_alt_az(self, when): return (-5.0, 0.0)
    def gc_alt_az(self, when): return (20.0, 180.0)


def assessment(status="VERIFIED_CLEAN", score=90, coverage=3, pm=7.7, veto=False):
    return {
        "smoke_assessment": {
            "consensus": {
                "status": status, "photography_smoke_score": score,
                "consensus_pm2_5": pm, "coverage": {"valid": coverage, "total": 3},
                "veto": veto, "confidence": "high" if coverage == 3 else "low",
            },
            "pollutants": {"pm2_5": pm, "us_aqi_health_context": 54, "dominant_pollutant": "ozone"},
            "models": {}, "observed_now": {}, "source_support": {}, "uncertainties": [],
        }
    }


def weather_and_aq(date_str):
    start = datetime.fromisoformat(date_str + "T18:00").replace(tzinfo=night_report.TZ)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(17)]
    hourly = {
        "time": times,
        "cloud_cover": [0] * 17, "cloud_cover_low": [0] * 17,
        "cloud_cover_mid": [0] * 17, "cloud_cover_high": [0] * 17,
        "temperature_2m": [5] * 17, "relative_humidity_2m": [50] * 17,
        "dew_point_2m": [0] * 17, "wind_speed_10m": [2] * 17,
        "wind_direction_10m": [180] * 17, "wind_gusts_10m": [5] * 17,
    }
    aq = {"hourly": {"time": times, "pm2_5": [60] * 17, "us_aqi": [180] * 17}}
    return {"hourly": hourly}, aq


class NightSmokeIntegrationTests(unittest.TestCase):
    LOC = {"lat": 51.0, "lon": -115.0, "elev_m": 1500, "name_zh": "測試", "mountain": "山", "coord_source": "fixture"}

    def test_report_subprocess_timeout_allows_first_bluesky_cycle_download(self):
        self.assertEqual(build_report.TIMEOUT, 420)

    def run_analyze(self, smoke):
        wx, aq = weather_and_aq("2026-08-24")
        with patch.object(night_report, "Astro", FakeAstro):
            return night_report.analyze("test", self.LOC, "2026-08-24", wx=wx, aq=aq, smoke_assessment=smoke)

    def test_consensus_smoke_score_replaces_legacy_aqi_pm_in_hourly_and_weighted_score(self):
        result = self.run_analyze(assessment())
        self.assertEqual(result["hourly"][0]["pm2_5"], 60)  # compatibility
        self.assertEqual(result["hourly"][0]["us_aqi"], 180)  # health context only
        self.assertEqual(result["hourly"][0]["smoke_score"], 90)
        self.assertEqual(result["night"]["score"], 98.0)
        self.assertEqual(result["night"]["grade_code"], "GO")
        self.assertEqual(result["smoke_assessment"]["pollutants"]["dominant_pollutant"], "ozone")

    def test_veto_and_uncertain_statuses_apply_honest_grade_caps(self):
        vetoed = self.run_analyze(assessment("VETO", 5, 3, 70, True))
        self.assertEqual(vetoed["night"]["grade_code"], "STAY_HOME")
        self.assertTrue(any("三模型" in reason for reason in vetoed["night"]["vetoes"]))

        split = self.run_analyze(assessment("MODEL_SPLIT", 55, 3, 20))
        self.assertEqual(split["night"]["grade_code"], "RISKY")
        partial = self.run_analyze(assessment("LIKELY_CLEAN", 90, 1, 8))
        self.assertEqual(partial["night"]["grade_code"], "MARGINAL")


if __name__ == "__main__": unittest.main()
