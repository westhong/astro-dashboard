import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import daylight_report
from backend.daylight_report import _apply_smoke_condition_cap, _condition


def smoke(status="VERIFIED_CLEAN", score=90, pm=7.7, coverage=3, aqi=54):
    return {
        "consensus": {"status": status, "photography_smoke_score": score,
                      "consensus_pm2_5": pm, "coverage": {"valid": coverage, "total": 3},
                      "veto": status == "VETO"},
        "pollutants": {"pm2_5": pm, "pm10": 10, "ozone": 94,
                       "nitrogen_dioxide": 3, "us_aqi_health_context": aqi,
                       "dominant_pollutant": "ozone"},
        "models": {}, "observed_now": {}, "source_support": {}, "uncertainties": [],
    }


def hourly():
    return {
        "time": ["2026-08-24T19:00", "2026-08-24T20:00", "2026-08-24T21:00"],
        "cloud_cover": [20, 20, 20],
        "cloud_cover_high": [20, 20, 20], "cloud_cover_mid": [20, 20, 20],
        "cloud_cover_low": [0, 0, 0], "wind_speed_10m": [2, 2, 2],
        "wind_gusts_10m": [4, 4, 4], "visibility": [30000, 30000, 30000],
        "precipitation_probability": [0, 0, 0],
    }


class DaylightSmokeIntegrationTests(unittest.TestCase):
    def test_build_adds_assessment_for_full_light_window(self):
        class Calculator:
            def _sun(self, *args): return (0, 250)
            def direct_light_time(self, *args): return {"time": "19:50", "basis": "fixture"}
        fc = {"daily": {"time": ["2026-08-24"], "sunset": ["2026-08-24T20:00"]}, "hourly": hourly()}
        seen = []
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "spots.json").write_text(json.dumps({"points": [{
                "id": "p", "name": "P", "lat": 51.123456, "lon": -115.654321,
                "daylight_events": ["sunset"],
            }]}), encoding="utf-8")
            with patch.object(daylight_report, "HERE", Path(directory)), \
                 patch.object(daylight_report, "DirectLightCalculator", Calculator), \
                 patch.object(daylight_report, "_fetch", side_effect=lambda coords, *a, **k: [fc] * len(coords)), \
                 patch.object(daylight_report, "_fetch_air_quality", return_value=None), \
                 patch.object(daylight_report, "_fetch_ecmwf", return_value=None), \
                 patch.object(daylight_report, "_fetch_model", return_value=None), \
                 patch.object(daylight_report, "assess_smoke_window", side_effect=lambda **kw: seen.append(kw) or {"smoke_assessment": smoke()}):
                built = daylight_report.build_daylight("2026-08-24")
        event = built["points"][0]["events"]["sunset"]
        self.assertIn("smoke_assessment", event)
        self.assertEqual(seen[0]["lat"], 51.123456)
        self.assertEqual(seen[0]["start_local"].strftime("%H:%M"), "18:45")
        self.assertEqual(seen[0]["end_local"].strftime("%H:%M"), "20:45")

    def test_ozone_high_pm_low_uses_clean_consensus_not_health_aqi(self):
        condition = _condition("sunset", "2026-08-24T20:00", hourly(),
            {"time": hourly()["time"], "pm2_5": [60, 60, 60], "us_aqi": [180, 180, 180]},
            None, None, None, smoke_assessment=smoke())
        self.assertEqual(condition["components"]["smoke"], 90)
        self.assertEqual(condition["weather"]["pm2_5"], 7.7)
        self.assertEqual(condition["weather"]["us_aqi"], 54)
        self.assertEqual(condition["smoke_assessment"]["pollutants"]["dominant_pollutant"], "ozone")

    def test_transparent_condition_caps_prevent_top_confidence(self):
        cases = [
            ("VETO", 3, 44, "不建議專程前往"),
            ("MODEL_SPLIT", 3, 64, "條件普通"),
            ("SMOKE_RISK", 3, 64, "條件普通"),
            ("RISKY_BOUNDARY", 3, 64, "條件普通"),
            ("SINGLE_MODEL_ONLY", 1, 79, "可嘗試"),
            ("LIKELY_CLEAN", 1, 79, "可嘗試"),
        ]
        for status, coverage, maximum, label in cases:
            with self.subTest(status=status, coverage=coverage):
                event = {"score": 95, "label": "條件佳", "smoke_assessment": smoke(status, 90, coverage=coverage)}
                _apply_smoke_condition_cap(event)
                self.assertEqual(event["score"], maximum)
                self.assertEqual(event["label"], label)
                self.assertEqual(event["condition_cap"]["max_score"], maximum)
                self.assertTrue(event["condition_cap"]["applied"])


if __name__ == "__main__": unittest.main()
