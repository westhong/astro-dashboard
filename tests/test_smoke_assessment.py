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

    def test_invalid_measurements_are_no_data_across_public_domain_functions(self):
        for invalid in (True, "8", -1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=repr(invalid)):
                self.assertEqual(classify_pm25(invalid), "NO_DATA")
                self.assertEqual(pm25_score(invalid), 60)
                model = evaluate_model(invalid)
                self.assertEqual(model["class"], "NO_DATA")
                self.assertTrue(model["uncertain"])
                self.assertIsNone(model["vote"])
                consensus = evaluate_consensus([4, 8, invalid])
                self.assertEqual(consensus["coverage"], {"valid": 2, "total": 3})
                self.assertEqual(consensus["status"], "LIKELY_CLEAN")


class ConsensusTests(unittest.TestCase):
    def test_assigns_three_model_consensus_statuses(self):
        cases = [
            ([4, 6, 10], "VERIFIED_CLEAN", "high"),
            ([4, 8, 12], "LIKELY_CLEAN", "medium"),
            ([4, 8, 30], "RISKY_BOUNDARY", "medium"),
            ([4, 30, 40], "SMOKE_RISK", "medium"),
            ([4, 60, 70], "VETO", "high"),
            ([4, 15, 30], "SMOKE_RISK", "medium"),
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
        self.assertEqual(two_agree["status"], "LIKELY_CLEAN")
        self.assertEqual(two_agree["confidence"], "medium")
        self.assertEqual(two_agree["coverage"], {"valid": 2, "total": 3})
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
        self.assertEqual(one["coverage"], {"valid": 1, "total": 3})

        none = evaluate_consensus([None, None, None])
        self.assertEqual(none["status"], "SINGLE_MODEL_ONLY")
        self.assertEqual(none["photography_smoke_score"], 60)
        self.assertTrue(none["uncertain"])
        self.assertEqual(none["coverage"], {"valid": 0, "total": 3})
        self.assertIn("no model data", none["reason"].lower())

    def test_combined_non_clean_votes_create_smoke_risk(self):
        for values in ([4, 15, 30], [15, 30, None], [15, 60, 4]):
            with self.subTest(values=values):
                result = evaluate_consensus(values)
                self.assertEqual(result["status"], "SMOKE_RISK")
                self.assertEqual(result["confidence"], "medium")

    def test_heavy_boundary_override_respects_status_severity(self):
        risk = evaluate_consensus([15, 30, 60])
        self.assertEqual(risk["status"], "SMOKE_RISK")
        self.assertLessEqual(risk["photography_smoke_score"], 55)

        veto = evaluate_consensus([15, 60, 90])
        self.assertEqual(veto["status"], "VETO")
        self.assertTrue(veto["veto"])


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
        self.assertEqual(assessment["consensus"]["status"], "LIKELY_CLEAN")
        self.assertEqual(assessment["consensus"]["confidence"], "medium")
        self.assertIsNone(assessment["observed_now"]["aqhi"])
        self.assertIsNone(assessment["source_support"]["classification"])
        json.dumps(payload)

    def test_preserves_publish_gate_metadata_even_when_model_is_invalid(self):
        common = {
            "source": "source-name",
            "retrieval_time": "2026-08-24T05:30:00Z",
            "reference_time": "2026-08-24T00:00:00Z",
            "valid_range": ["2026-08-24T06:00:00Z", "2026-08-24T09:00:00Z"],
            "status": "outside requested window",
            "units": "µg/m³",
            "valid": False,
            "window_avg_pm2_5": 7,
        }
        bluesky = {
            **common,
            "forecast_id": "BSC18CA12-07",
            "raw_tflag_range": ["2026-08-24T07:00:00Z", "2026-08-24T10:00:00Z"],
            "tflag_semantics": "interval_end; valid_time = TFLAG - PT1H",
            "fire_locations_url": "https://example.test/fire_locations.kml",
        }
        payload = build_smoke_assessment(
            shooting_point={},
            window_local={},
            models={
                "eccc_firework": common,
                "cams_global": common,
                "bluesky_canada": bluesky,
            },
        )
        models = payload["smoke_assessment"]["models"]
        for name in ("eccc_firework", "cams_global", "bluesky_canada"):
            with self.subTest(model=name):
                model = models[name]
                for key in (
                    "source", "retrieval_time", "reference_time", "valid_range", "status", "units"
                ):
                    self.assertEqual(model[key], (bluesky if name == "bluesky_canada" else common)[key])
                self.assertIsNone(model["window_avg_pm2_5"])
                self.assertEqual(model["class"], "NO_DATA")
        for key in ("forecast_id", "raw_tflag_range", "tflag_semantics", "fire_locations_url"):
            self.assertEqual(models["bluesky_canada"][key], bluesky[key])

    def test_preserves_cams_cycle_limitation_and_source_uncertainties(self):
        cams = {
            "source": "CAMS global via Open-Meteo",
            "retrieval_time": "2026-08-24T05:30:00Z",
            "provider_retrieval_time": "2026-08-24T05:30:00Z",
            "reference_time": None,
            "cycle_status": "not_exposed_by_open_meteo",
            "uncertainties": ["Open-Meteo does not expose the CAMS model cycle/reference time."],
            "valid": True,
            "window_avg_pm2_5": 8,
        }
        payload = build_smoke_assessment(
            shooting_point={},
            window_local={},
            models={
                "eccc_firework": {},
                "cams_global": cams,
                "bluesky_canada": {},
            },
        )
        published = payload["smoke_assessment"]["models"]["cams_global"]
        self.assertIsNone(published["reference_time"])
        self.assertEqual(published["cycle_status"], "not_exposed_by_open_meteo")
        self.assertEqual(published["provider_retrieval_time"], "2026-08-24T05:30:00Z")
        self.assertEqual(published["uncertainties"], cams["uncertainties"])
        json.dumps(payload)

    def test_invalid_model_data_is_no_data_and_never_serialized(self):
        for invalid in (True, "8", -1, float("nan"), float("inf"), float("-inf")):
            with self.subTest(invalid=repr(invalid)):
                payload = build_smoke_assessment(
                    shooting_point={},
                    window_local={},
                    models={
                        "eccc_firework": {
                            "valid": True,
                            "status": "source supplied this status",
                            "window_avg_pm2_5": invalid,
                            "window_range": [float("-inf"), "bad"],
                            "neighbor_range": [float("nan"), -2, 30],
                        },
                        "cams_global": {"valid": True, "window_avg_pm2_5": 7},
                        "bluesky_canada": {"valid": True, "window_avg_pm2_5": 8},
                    },
                )
                assessment = payload["smoke_assessment"]
                model = assessment["models"]["eccc_firework"]
                self.assertFalse(model["valid"])
                self.assertIsNone(model["window_avg_pm2_5"])
                self.assertEqual(model["class"], "NO_DATA")
                self.assertEqual(model["status"], "source supplied this status")
                self.assertEqual(model["window_range"], [None, None])
                self.assertEqual(model["neighbor_range"], [None, None, 30])
                self.assertEqual(assessment["consensus"]["coverage"], {"valid": 2, "total": 3})
                self.assertEqual(assessment["consensus"]["status"], "LIKELY_CLEAN")
                json.dumps(payload, allow_nan=False)

    def test_clean_average_with_smoky_window_or_neighbor_is_risky_boundary(self):
        for range_key in ("window_range", "neighbor_range"):
            with self.subTest(range_key=range_key):
                boundary_model = {
                    "valid": True,
                    "window_avg_pm2_5": 8,
                    "window_range": [6, 10],
                    "neighbor_range": [4, 10],
                }
                boundary_model[range_key] = [4, 30]
                payload = build_smoke_assessment(
                    shooting_point={},
                    window_local={},
                    models={
                        "eccc_firework": boundary_model,
                        "cams_global": {"valid": True, "window_avg_pm2_5": 7},
                        "bluesky_canada": {"valid": True, "window_avg_pm2_5": 6},
                    },
                )
                assessment = payload["smoke_assessment"]
                consensus = assessment["consensus"]
                self.assertEqual(consensus["status"], "RISKY_BOUNDARY")
                self.assertLessEqual(consensus["photography_smoke_score"], 55)
                self.assertTrue(consensus["uncertain"])
                self.assertIn("boundary", consensus["reason"].lower())
                self.assertTrue(any("boundary" in note.lower() for note in assessment["uncertainties"]))

    def test_boundary_does_not_downgrade_smoke_risk_or_veto(self):
        boundary_clean = {
            "valid": True,
            "window_avg_pm2_5": 8,
            "window_range": [4, 30],
        }
        for other_values, expected in (([15, 30], "SMOKE_RISK"), ([60, 70], "VETO")):
            with self.subTest(expected=expected):
                payload = build_smoke_assessment(
                    shooting_point={},
                    window_local={},
                    models={
                        "eccc_firework": boundary_clean,
                        "cams_global": {"valid": True, "window_avg_pm2_5": other_values[0]},
                        "bluesky_canada": {"valid": True, "window_avg_pm2_5": other_values[1]},
                    },
                )
                self.assertEqual(payload["smoke_assessment"]["consensus"]["status"], expected)

    def test_ozone_health_context_never_changes_consensus(self):
        arguments = {
            "shooting_point": {},
            "window_local": {},
            "models": {
                "eccc_firework": {"valid": True, "window_avg_pm2_5": 4},
                "cams_global": {"valid": True, "window_avg_pm2_5": 6},
                "bluesky_canada": {"valid": True, "window_avg_pm2_5": 8},
            },
        }
        low_ozone = build_smoke_assessment(
            **arguments, pollutants={"ozone": 1}, health_subindices={"ozone": 1, "pm2_5": 50}
        )["smoke_assessment"]["consensus"]
        high_ozone = build_smoke_assessment(
            **arguments, pollutants={"ozone": 999}, health_subindices={"ozone": 999, "pm2_5": 1}
        )["smoke_assessment"]["consensus"]
        for key in ("status", "photography_smoke_score", "veto"):
            self.assertEqual(low_ozone[key], high_ozone[key])


if __name__ == "__main__":
    unittest.main()
