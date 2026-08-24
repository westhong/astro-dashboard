import json
import unittest

from backend.smoke_assessment import (
    build_smoke_assessment,
    classify_pm25,
    dominant_pollutant,
    evaluate_consensus,
    evaluate_model,
    pm25_score,
)


class Pm25ScoreTests(unittest.TestCase):
    def test_uses_west_pm25_score_ladder_boundaries(self):
        cases = [
            (0, 100),
            (5, 100),
            (5.01, 90),
            (10, 90),
            (10.01, 75),
            (15, 75),
            (15.01, 55),
            (25, 55),
            (25.01, 35),
            (35, 35),
            (35.01, 18),
            (55, 18),
            (55.01, 5),
        ]
        for pm2_5, expected in cases:
            with self.subTest(pm2_5=pm2_5):
                self.assertEqual(pm25_score(pm2_5), expected)


class ModelEvaluationTests(unittest.TestCase):
    def test_classifies_model_boundaries_and_marks_missing_data_uncertain(self):
        cases = [
            (10, "CLEAN"),
            (10.01, "HAZE"),
            (25, "HAZE"),
            (25.01, "SMOKY"),
            (55, "SMOKY"),
            (55.01, "HEAVY"),
            (None, "NO_DATA"),
        ]
        for pm2_5, expected in cases:
            with self.subTest(pm2_5=pm2_5):
                self.assertEqual(classify_pm25(pm2_5), expected)

        missing = evaluate_model(None)
        self.assertEqual(missing["score"], 60)
        self.assertTrue(missing["uncertain"])
        self.assertEqual(missing["class"], "NO_DATA")

    def test_ozone_is_health_context_only_and_never_changes_smoke_score(self):
        health_subindices = {"pm2_5": 32, "ozone": 54, "nitrogen_dioxide": 8}
        self.assertEqual(dominant_pollutant(health_subindices), "ozone")
        self.assertEqual(pm25_score(7.7), 90)
        self.assertFalse(evaluate_consensus([7.7, 7.7, 7.7])["veto"])

    def test_model_vote_boundaries_are_strict(self):
        self.assertIsNone(evaluate_model(35)["vote"])
        self.assertEqual(evaluate_model(35.01)["vote"], "RISKY_CAP")
        self.assertEqual(evaluate_model(55)["vote"], "RISKY_CAP")
        self.assertEqual(evaluate_model(55.01)["vote"], "VETO")


class ConsensusTests(unittest.TestCase):
    def test_assigns_three_model_consensus_statuses(self):
        cases = [
            ([4, 6, 10], "VERIFIED_CLEAN", "high"),
            ([4, 8, 12], "LIKELY_CLEAN", "medium"),
            ([4, 8, 30], "RISKY_BOUNDARY", "medium"),
            ([4, 30, 40], "SMOKE_RISK", "medium"),
            ([4, 60, 70], "VETO", "high"),
            ([4, 15, 30], "MODEL_SPLIT", "low"),
        ]
        for values, status, confidence in cases:
            with self.subTest(values=values):
                result = evaluate_consensus(values)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["confidence"], confidence)

    def test_uses_second_highest_value_and_applies_boundary_cap_and_veto(self):
        supported = evaluate_consensus([4, 20, 40])
        self.assertEqual(supported["consensus_pm2_5"], 20)
        self.assertEqual(supported["photography_smoke_score"], 55)

        boundary = evaluate_consensus([4, 8, 60])
        self.assertEqual(boundary["status"], "RISKY_BOUNDARY")
        self.assertEqual(boundary["photography_smoke_score"], 55)
        self.assertFalse(boundary["veto"])

        veto = evaluate_consensus([4, 60, 90])
        self.assertEqual(veto["status"], "VETO")
        self.assertEqual(veto["photography_smoke_score"], 5)
        self.assertTrue(veto["veto"])

    def test_partial_coverage_is_explicit_and_never_high_confidence(self):
        two_agree = evaluate_consensus([4, 8, None])
        self.assertEqual(two_agree["status"], "VERIFIED_CLEAN")
        self.assertEqual(two_agree["confidence"], "medium")
        self.assertTrue(two_agree["partial"])
        self.assertTrue(two_agree["uncertain"])
        self.assertTrue(two_agree["uncertainties"])

        two_split = evaluate_consensus([4, 30, None])
        self.assertEqual(two_split["status"], "MODEL_SPLIT")
        self.assertEqual(two_split["confidence"], "low")

        one = evaluate_consensus([12, None, None])
        self.assertEqual(one["status"], "SINGLE_MODEL_ONLY")
        self.assertEqual(one["confidence"], "low")
        self.assertEqual(one["photography_smoke_score"], 75)
        self.assertTrue(one["uncertain"])

        none = evaluate_consensus([None, None, None])
        self.assertEqual(none["status"], "SINGLE_MODEL_ONLY")
        self.assertEqual(none["photography_smoke_score"], 60)
        self.assertTrue(none["uncertain"])


class SmokeAssessmentSchemaTests(unittest.TestCase):
    def test_builds_complete_serializable_schema_without_inventing_source_data(self):
        payload = build_smoke_assessment(
            shooting_point={"lat": 51.2, "lon": -115.5},
            window_local={
                "start": "2026-08-23T22:00:00-06:00",
                "end": "2026-08-24T01:00:00-06:00",
                "timezone": "America/Edmonton",
            },
            observed_now={},
            pollutants={
                "pm2_5": 7.7,
                "ozone": 94,
                "us_aqi_health_context": 54,
            },
            health_subindices={"pm2_5": 32, "ozone": 54},
            models={
                "eccc_firework": {"valid": True, "window_avg_pm2_5": 4},
                "cams_global": {"valid": True, "window_avg_pm2_5": 8},
                "bluesky_canada": {"valid": False},
            },
        )
        assessment = payload["smoke_assessment"]
        self.assertEqual(
            set(assessment),
            {
                "shooting_point",
                "window_local",
                "observed_now",
                "pollutants",
                "models",
                "consensus",
                "source_support",
                "uncertainties",
            },
        )
        self.assertEqual(assessment["pollutants"]["dominant_pollutant"], "ozone")
        self.assertEqual(assessment["models"]["eccc_firework"]["class"], "CLEAN")
        self.assertEqual(assessment["models"]["bluesky_canada"]["class"], "NO_DATA")
        self.assertEqual(assessment["consensus"]["status"], "VERIFIED_CLEAN")
        self.assertEqual(assessment["consensus"]["confidence"], "medium")
        self.assertIsNone(assessment["observed_now"]["aqhi"])
        self.assertIsNone(assessment["source_support"]["classification"])
        json.dumps(payload)


if __name__ == "__main__":
    unittest.main()
