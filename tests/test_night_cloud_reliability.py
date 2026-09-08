import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from backend.scripts import night_report


class FakeAstro:
    def __init__(self, *args): pass
    def sun_alt(self, when): return -20.0
    def moon_illum(self, when): return 10.0
    def moon_events(self, start, end): return []
    def moon_alt_az(self, when): return (-5.0, 0.0)
    def gc_alt_az(self, when): return (20.0, 180.0)


def clear_smoke():
    return {"smoke_assessment": {
        "consensus": {"status": "VERIFIED_CLEAN", "photography_smoke_score": 90,
                      "coverage": {"valid": 3, "total": 3}, "veto": False},
        "pollutants": {}, "models": {}, "observed_now": {},
        "source_support": {}, "uncertainties": [],
    }}


def analysis_weather(cloud, count, spread=None):
    start = datetime(2026, 9, 4, 18, tzinfo=night_report.TZ)
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(17)]
    model_row = {
        model: {"total": cloud, "low": cloud, "mid": cloud, "high": cloud,
                "effective": cloud, "available": index < count}
        for index, model in enumerate(night_report.CLOUD_MODELS)
    }
    return {"hourly": {
        "time": times,
        "cloud_cover": [cloud] * 17, "cloud_cover_low": [cloud] * 17,
        "cloud_cover_mid": [cloud] * 17, "cloud_cover_high": [cloud] * 17,
        "temperature_2m": [5] * 17, "relative_humidity_2m": [50] * 17,
        "dew_point_2m": [0] * 17, "wind_speed_10m": [2] * 17,
        "wind_direction_10m": [180] * 17, "wind_gusts_10m": [5] * 17,
        "_cloud_models": [model_row for _ in times],
        "_cloud_available_models": [count] * 17,
        "_cloud_spread": [spread] * 17,
        "_cloud_confidence": ["low" if count < 3 else "high"] * 17,
        "_cloud_source": "open_meteo_explicit_models",
    }}


class NightCloudFetchTests(unittest.TestCase):
    def test_conservative_aggregation_uses_max_for_two_and_sole_for_one(self):
        self.assertEqual(night_report._conservative_cloud([15, 60]), 60)
        self.assertEqual(night_report._conservative_cloud([35]), 35)
        self.assertIsNone(night_report._conservative_cloud([]))

    def test_forty_point_model_spread_is_low_confidence(self):
        hourly = {"time": ["2026-09-04T22:00"]}
        totals = [0, 20, 30, 40]
        for model, total in zip(night_report.CLOUD_MODELS, totals):
            hourly[f"cloud_cover_{model}"] = [total]
            hourly[f"cloud_cover_low_{model}"] = [total]
            hourly[f"cloud_cover_mid_{model}"] = [total]
            hourly[f"cloud_cover_high_{model}"] = [total]

        normalized = night_report._normalize_cloud_models({"hourly": hourly})["hourly"]

        self.assertEqual(normalized["_cloud_spread"], [40])
        self.assertEqual(normalized["_cloud_confidence"], ["low"])

    def test_fetch_requests_four_explicit_cloud_models_in_one_batch(self):
        coords = [(51.0, -115.0), (51.1, -115.1)]
        raw = [
            {"hourly": {"time": []}},
            {"hourly": {"time": []}},
        ]

        with patch.object(night_report, "_get_with_retry", return_value=raw) as get:
            result = night_report.fetch_weather_batch(coords)

        self.assertEqual(len(result), 2)
        params = get.call_args.args[1]
        self.assertEqual(
            params["models"],
            "ecmwf_ifs025,gem_seamless,icon_seamless,gfs_seamless",
        )
        self.assertEqual(get.call_count, 1)

    def test_suffixed_cloud_fields_use_layer_max_and_second_highest_consensus(self):
        hourly = {"time": ["2026-09-04T22:00"]}
        values = {
            "ecmwf_ifs025": (20, 5, 8, 12),
            "gem_seamless": (0, 4, 52, 3),
            "icon_seamless": (40, 25, 7, 10),
            "gfs_seamless": (10, 2, 5, 9),
        }
        for model, (total, low, mid, high) in values.items():
            hourly[f"cloud_cover_{model}"] = [total]
            hourly[f"cloud_cover_low_{model}"] = [low]
            hourly[f"cloud_cover_mid_{model}"] = [mid]
            hourly[f"cloud_cover_high_{model}"] = [high]

        with patch.object(
            night_report, "_get_with_retry", return_value={"hourly": hourly}
        ):
            result = night_report.fetch_weather_batch([(51.0, -115.0)])[0]["hourly"]

        self.assertEqual(result["cloud_cover"], [40])
        self.assertEqual(result["cloud_cover_low"], [5])
        self.assertEqual(result["cloud_cover_mid"], [8])
        self.assertEqual(result["cloud_cover_high"], [10])
        self.assertEqual(result["_cloud_available_models"], [4])
        self.assertEqual(result["_cloud_spread"], [42])
        self.assertEqual(result["_cloud_confidence"], ["low"])
        self.assertEqual(result["_cloud_models"][0]["gem_seamless"]["effective"], 52)

    def test_incomplete_cloud_model_is_unavailable_and_never_becomes_clear(self):
        hourly = {"time": ["2026-09-04T22:00"]}
        for model in night_report.CLOUD_MODELS:
            hourly[f"cloud_cover_{model}"] = [None]
            hourly[f"cloud_cover_low_{model}"] = [None]
            hourly[f"cloud_cover_mid_{model}"] = [52 if model == "gem_seamless" else None]
            hourly[f"cloud_cover_high_{model}"] = [None]

        normalized = night_report._normalize_cloud_models({"hourly": hourly})["hourly"]

        self.assertEqual(normalized["cloud_cover"][0], 52)
        self.assertIsNone(normalized["cloud_cover_low"][0])
        self.assertEqual(normalized["cloud_cover_mid"][0], 52)
        self.assertEqual(normalized["_cloud_available_models"][0], 0)
        self.assertEqual(normalized["_cloud_confidence"][0], "low")
        self.assertFalse(normalized["_cloud_models"][0]["gem_seamless"]["available"])
        self.assertEqual(normalized["_cloud_models"][0]["gem_seamless"]["effective"], 52)

    def test_partial_models_still_contribute_material_cloud_evidence(self):
        hourly = {"time": ["2026-09-04T22:00"]}
        for model in night_report.CLOUD_MODELS:
            hourly[f"cloud_cover_{model}"] = [0]
            hourly[f"cloud_cover_low_{model}"] = [0]
            hourly[f"cloud_cover_mid_{model}"] = [0]
            hourly[f"cloud_cover_high_{model}"] = [0]
        for model, mid in (("icon_seamless", 80), ("gfs_seamless", 90)):
            hourly[f"cloud_cover_{model}"] = [None]
            hourly[f"cloud_cover_low_{model}"] = [None]
            hourly[f"cloud_cover_mid_{model}"] = [mid]
            hourly[f"cloud_cover_high_{model}"] = [None]

        normalized = night_report._normalize_cloud_models({"hourly": hourly})["hourly"]

        self.assertEqual(normalized["cloud_cover"], [80])
        self.assertEqual(normalized["cloud_cover_mid"], [80])
        self.assertEqual(normalized["_cloud_available_models"], [2])
        self.assertEqual(normalized["_cloud_spread"], [90])
        self.assertEqual(normalized["_cloud_confidence"], ["low"])

    def test_suffixed_weather_fields_are_exposed_without_fabricating_missing_values(self):
        hourly = {"time": ["2026-09-04T22:00"]}
        for model in night_report.CLOUD_MODELS:
            for field in ("cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"):
                hourly[f"{field}_{model}"] = [10]
        hourly["temperature_2m_ecmwf_ifs025"] = [4.5]
        hourly["wind_speed_10m_ecmwf_ifs025"] = [None]
        hourly["wind_speed_10m_gem_seamless"] = [7]
        hourly["wind_gusts_10m_ecmwf_ifs025"] = [None]

        normalized = night_report._normalize_cloud_models({"hourly": hourly})["hourly"]

        self.assertEqual(normalized["temperature_2m"], [4.5])
        self.assertEqual(normalized["wind_speed_10m"], [7])
        self.assertEqual(normalized["wind_gusts_10m"], [None])

    def test_met_norway_fallback_is_one_honestly_labeled_cloud_model(self):
        fallback = {
            "_source": "met_norway",
            "hourly": {
                "time": ["2026-09-04T22:00"],
                "cloud_cover": [0], "cloud_cover_low": [5],
                "cloud_cover_mid": [52], "cloud_cover_high": [10],
            },
        }
        with patch.object(night_report, "_get_with_retry", side_effect=OSError("offline")), patch.object(
            night_report, "_met_norway_fetch_batch", return_value=[fallback]
        ):
            forecast = night_report.fetch_weather_batch([(51.0, -115.0)])[0]

        hourly = forecast["hourly"]
        self.assertEqual(hourly["cloud_cover"], [52])
        self.assertEqual(hourly["_cloud_available_models"], [1])
        self.assertEqual(hourly["_cloud_source"], "met_norway")
        self.assertEqual(hourly["_cloud_models"][0]["met_norway"]["effective"], 52)


class NightCloudAnalysisTests(unittest.TestCase):
    LOC = {"lat": 51.0, "lon": -115.0, "elev_m": 1500,
           "name_zh": "測試", "mountain": "山", "coord_source": "fixture"}

    def test_unavailable_cloud_hours_are_not_scored_as_clear(self):
        with patch.object(night_report, "Astro", FakeAstro):
            result = night_report.analyze(
                "test", self.LOC, "2026-09-04",
                wx=analysis_weather(None, 0), aq=None,
                smoke_assessment=clear_smoke(),
            )

        self.assertEqual(result["hourly"], [])
        self.assertEqual(result["night"]["grade_code"], "NO_DATA")
        self.assertEqual(result["night"]["score"], 0)

    def test_missing_cloud_hour_is_not_bridged_into_false_consecutive_window(self):
        weather = analysis_weather(None, 4, 5)
        # Only 22:00, 00:00 and 01:00 have cloud data. 23:00 is a real gap.
        for index in (4, 6, 7):
            for field in ("cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"):
                weather["hourly"][field][index] = 0

        with patch.object(night_report, "Astro", FakeAstro):
            result = night_report.analyze(
                "test", self.LOC, "2026-09-04",
                wx=weather, aq=None, smoke_assessment=clear_smoke(),
            )

        hourly_times = [row["time"] for row in result["hourly"]]
        self.assertNotIn("23:00", hourly_times)
        self.assertEqual(result["best_window"]["hours"], 2)
        self.assertEqual(result["night"]["grade_code"], "MARGINAL")
        self.assertTrue(any("不足 3 小時" in cap for cap in result["night"]["caps"]))
        start = datetime.strptime(result["best_window"]["start"], "%H:%M")
        end = datetime.strptime(result["best_window"]["end"], "%H:%M")
        duration_hours = ((end - start).total_seconds() / 3600) % 24
        self.assertEqual(duration_hours, 2)

    def test_single_available_cloud_hour_caps_grade_at_risky(self):
        weather = analysis_weather(None, 4, 5)
        for field in ("cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high"):
            weather["hourly"][field][4] = 0

        with patch.object(night_report, "Astro", FakeAstro):
            result = night_report.analyze(
                "test", self.LOC, "2026-09-04",
                wx=weather, aq=None, smoke_assessment=clear_smoke(),
            )

        self.assertEqual(result["best_window"]["hours"], 1)
        self.assertEqual(result["night"]["grade_code"], "RISKY")
        self.assertTrue(any("只有 1 小時" in cap for cap in result["night"]["caps"]))

    def test_two_model_coverage_is_stored_and_caps_grade_without_changing_score(self):
        with patch.object(night_report, "Astro", FakeAstro):
            result = night_report.analyze(
                "test", self.LOC, "2026-09-04",
                wx=analysis_weather(0, 2, 12), aq=None,
                smoke_assessment=clear_smoke(),
            )

        self.assertEqual(result["night"]["score"], 98.0)
        self.assertEqual(result["night"]["grade_code"], "MARGINAL")
        self.assertTrue(any("雲模型覆蓋不足" in cap for cap in result["night"]["caps"]))
        hour = result["hourly"][0]
        self.assertEqual(hour["cloud_available_models"], 2)
        self.assertEqual(hour["cloud_spread_pct"], 12)
        self.assertEqual(hour["cloud_confidence"], "low")
        self.assertEqual(hour["cloud_source"], "open_meteo_explicit_models")
        self.assertIn("ecmwf_ifs025", hour["cloud_models"])

    def test_single_cloud_model_caps_final_grade_at_risky(self):
        with patch.object(night_report, "Astro", FakeAstro):
            result = night_report.analyze(
                "test", self.LOC, "2026-09-04",
                wx=analysis_weather(0, 1), aq=None,
                smoke_assessment=clear_smoke(),
            )

        self.assertEqual(result["night"]["score"], 98.0)
        self.assertEqual(result["night"]["grade_code"], "RISKY")

    def test_large_best_window_cloud_spread_caps_final_grade_at_marginal(self):
        with patch.object(night_report, "Astro", FakeAstro):
            result = night_report.analyze(
                "test", self.LOC, "2026-09-04",
                wx=analysis_weather(0, 4, 40), aq=None,
                smoke_assessment=clear_smoke(),
            )

        self.assertEqual(result["night"]["score"], 98.0)
        self.assertEqual(result["night"]["grade_code"], "MARGINAL")
        self.assertTrue(any("雲模型分歧" in cap for cap in result["night"]["caps"]))

    def test_weather_source_names_actual_explicit_models(self):
        with patch.object(night_report, "Astro", FakeAstro):
            result = night_report.analyze(
                "test", self.LOC, "2026-09-04",
                wx=analysis_weather(10, 4, 5), aq=None,
                smoke_assessment=clear_smoke(),
            )

        source = result["sources"]["weather"]
        for name in ("ECMWF IFS 0.25°", "GEM", "ICON", "GFS"):
            self.assertIn(name, source)
        self.assertNotIn("GEM 系", source)


if __name__ == "__main__":
    unittest.main()
