from urllib.parse import parse_qs, urlparse
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from backend import daylight_report
from backend.weather_coordinator import UnifiedWeatherCoordinator
from backend.scripts import night_report


def test_unified_coordinator_fetches_site_batches_once_and_reuses_models():
    locations = {
        "b": {"lat": 51.0, "lon": -115.0},
        "a": {"lat": 50.0, "lon": -116.0},
    }
    calls = []

    def fetch(url):
        calls.append(url)
        query = parse_qs(urlparse(url).query)
        count = len(query["latitude"][0].split(","))
        if "models" not in query:
            return [{"hourly": {"cloud_cover": [10]}} for _ in range(count)]
        return [
            {"hourly": {
                "cloud_cover_ecmwf_ifs025": [20],
                "wind_speed_10m_ecmwf_ifs025": [5],
                "cloud_cover_gfs_seamless": [30],
            }}
            for _ in range(count)
        ]

    coordinator = UnifiedWeatherCoordinator(locations, fetch_json=fetch)
    assert coordinator.best_match("a")["hourly"]["cloud_cover"] == [10]
    assert coordinator.best_match("b")["hourly"]["cloud_cover"] == [10]
    assert coordinator.model("a", "ecmwf_ifs025")["hourly"]["cloud_cover"] == [20]
    assert coordinator.model("a", "ecmwf_ifs025")["hourly"]["wind_speed_10m"] == [5]
    assert coordinator.model("b", "gfs_seamless")["hourly"]["cloud_cover"] == [30]
    assert len(calls) == 2


def test_unified_coordinator_exposes_honest_individual_site_error():
    locations = {
        "a": {"lat": 50.0, "lon": -116.0},
        "b": {"lat": 51.0, "lon": -115.0},
    }
    calls = []

    def fetch(url):
        calls.append(url)
        return [{"hourly": {}}]

    coordinator = UnifiedWeatherCoordinator(locations, fetch_json=fetch)
    assert coordinator.best_match("a") is None
    assert coordinator.best_match("b") is None
    assert "expected 2" in coordinator.site_error("a")
    assert len(calls) == 1


def test_night_runtime_consumes_coordinator_without_legacy_refetch():
    locations = {
        "a": {"lat": 50.0, "lon": -116.0},
        "b": {"lat": 51.0, "lon": -115.0},
    }

    class Coordinator:
        def four_models(self, location_id):
            return {"hourly": {"time": [], "cloud_cover_ecmwf_ifs025": []}}

        def cams_grid(self, location_id):
            return [{"hourly": {"pm2_5": [], "us_aqi": []}}] * 9

        def cams_window(self, **kwargs):
            return {}

        def site_error(self, location_id):
            return None

    seen = []

    def analyze(location_id, _loc, _date, *, wx, aq, aq_error, cams_fetch):
        seen.append((location_id, wx, aq, aq_error, cams_fetch))
        return {"location_id": location_id}

    with patch.object(night_report, "fetch_weather_batch") as weather_fetch, \
         patch.object(night_report, "fetch_air_quality_batch") as aq_fetch, \
         patch.object(night_report, "analyze", side_effect=analyze):
        results = night_report.run_all_locations(
            locations, "2026-09-20", coordinator=Coordinator()
        )

    weather_fetch.assert_not_called()
    aq_fetch.assert_not_called()
    assert [item["location_id"] for item in results] == ["a", "b"]
    assert [entry[0] for entry in seen] == ["a", "b"]
    assert all(callable(entry[4]) for entry in seen)


def test_night_runtime_preserves_met_fallback_from_coordinator():
    locations = {"a": {"lat": 50.0, "lon": -116.0}}
    fallback = {
        "_source": "met_norway",
        "hourly": {
            "time": [], "cloud_cover": [], "cloud_cover_low": [],
            "cloud_cover_mid": [], "cloud_cover_high": [],
        },
    }

    class Coordinator:
        def four_models(self, location_id): return None
        def best_match(self, location_id): return fallback
        def cams_grid(self, location_id): return [None] * 9
        def cams_window(self, **kwargs): return {}
        def site_error(self, location_id): return "Open-Meteo unavailable"

    with patch.object(night_report, "analyze", return_value={"location_id": "a"}) as analyze:
        results = night_report.run_all_locations(
            locations, "2026-09-20", coordinator=Coordinator()
        )

    assert results == [{"location_id": "a"}]
    assert analyze.call_args.kwargs["wx"]["hourly"]["_cloud_source"] == "met_norway"


def test_daylight_runtime_reuses_coordinator_site_model_horizon_and_cams_data():
    hourly = {
        "time": ["2026-09-20T18:00", "2026-09-20T19:00", "2026-09-20T20:00"],
        "cloud_cover": [20, 20, 20], "cloud_cover_low": [5, 5, 5],
        "cloud_cover_mid": [10, 10, 10], "cloud_cover_high": [20, 20, 20],
        "precipitation_probability": [0, 0, 0], "visibility": [30000] * 3,
        "wind_speed_10m": [4, 4, 4], "wind_gusts_10m": [6, 6, 6],
    }
    forecast = {
        "daily": {"time": ["2026-09-20"], "sunset": ["2026-09-20T19:00"]},
        "hourly": hourly,
    }

    class Coordinator:
        def best_match(self, location_id): return forecast
        def model(self, location_id, model): return {"hourly": hourly}
        def horizon(self, key): return {"hourly": hourly}
        def cams_grid(self, location_id):
            return [{"hourly": {"time": hourly["time"], "pm2_5": [5] * 3, "us_aqi": [20] * 3}}] * 9
        def cams_window(self, **kwargs): return {}

    class Calculator:
        def _sun(self, *args): return 0, 250
        def direct_light_time(self, *args): return {"time": "18:50", "basis": "fixture"}

    smoke = {
        "smoke_assessment": {
            "consensus": {"status": "VERIFIED_CLEAN", "photography_smoke_score": 90,
                          "consensus_pm2_5": 5, "coverage": {"valid": 3, "total": 3}, "veto": False},
            "pollutants": {"pm2_5": 5, "pm10": 8, "ozone": 20, "nitrogen_dioxide": 2,
                           "us_aqi_health_context": 20, "dominant_pollutant": "pm2_5"},
            "models": {}, "observed_now": {}, "source_support": {}, "uncertainties": [],
        }
    }
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "spots.json").write_text(json.dumps({"points": [{
            "id": "spot-a", "location_id": "a", "name": "A", "lat": 50.0,
            "lon": -116.0, "daylight_events": ["sunset"],
        }]}), encoding="utf-8")
        with patch.object(daylight_report, "HERE", Path(directory)), \
             patch.object(daylight_report, "DirectLightCalculator", Calculator), \
             patch.object(daylight_report, "_fetch") as best_fetch, \
             patch.object(daylight_report, "_fetch_air_quality") as aq_fetch, \
             patch.object(daylight_report, "_fetch_ecmwf") as ecmwf_fetch, \
             patch.object(daylight_report, "_fetch_model") as model_fetch, \
             patch.object(daylight_report, "assess_smoke_window", return_value=smoke):
            built = daylight_report.build_daylight(
                "2026-09-20", coordinator=Coordinator()
            )

    best_fetch.assert_not_called()
    aq_fetch.assert_not_called()
    ecmwf_fetch.assert_not_called()
    model_fetch.assert_not_called()
    assert built["points"][0]["id"] == "a"
    assert "sunset" in built["points"][0]["events"]


def _build_daylight_with_missing_shared_data(*, cams_grid, four_models):
    hourly = {
        "time": ["2026-09-20T18:00", "2026-09-20T19:00", "2026-09-20T20:00"],
        "cloud_cover": [20, 20, 20], "cloud_cover_low": [5, 5, 5],
        "cloud_cover_mid": [10, 10, 10], "cloud_cover_high": [20, 20, 20],
        "precipitation_probability": [0, 0, 0], "visibility": [30000] * 3,
        "wind_speed_10m": [4, 4, 4], "wind_gusts_10m": [6, 6, 6],
    }
    forecast = {
        "daily": {"time": ["2026-09-20"], "sunset": ["2026-09-20T19:00"]},
        "hourly": hourly,
    }

    class Coordinator:
        def best_match(self, location_id): return forecast
        def four_models(self, location_id): return four_models
        def model(self, location_id, model): return None
        def horizon(self, key): return {"hourly": hourly}
        def cams_grid(self, location_id): return cams_grid
        def cams_window(self, **kwargs): return {}

    class Calculator:
        def _sun(self, *args): return 0, 250
        def direct_light_time(self, *args): return {"time": "18:50", "basis": "fixture"}

    uncertain_smoke = {
        "smoke_assessment": {
            "consensus": {
                "status": "SINGLE_MODEL_ONLY", "photography_smoke_score": 50,
                "consensus_pm2_5": None, "coverage": {"valid": 0, "total": 3},
                "veto": False,
            },
            "pollutants": {"us_aqi_health_context": None},
            "models": {}, "observed_now": {}, "source_support": {},
            "uncertainties": ["CAMS 資料暫缺"],
        }
    }
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, "spots.json").write_text(json.dumps({"points": [{
            "id": "spot-a", "location_id": "a", "name": "A", "lat": 50.0,
            "lon": -116.0, "daylight_events": ["sunset"],
        }]}), encoding="utf-8")
        with patch.object(daylight_report, "HERE", Path(directory)), \
             patch.object(daylight_report, "DirectLightCalculator", Calculator), \
             patch.object(daylight_report, "_fetch_air_quality") as aq_fetch, \
             patch.object(daylight_report, "_fetch_ecmwf") as ecmwf_fetch, \
             patch.object(daylight_report, "_fetch_model") as model_fetch, \
             patch.object(daylight_report, "assess_smoke_window", return_value=uncertain_smoke):
            built = daylight_report.build_daylight(
                "2026-09-20", coordinator=Coordinator()
            )

    aq_fetch.assert_not_called()
    ecmwf_fetch.assert_not_called()
    model_fetch.assert_not_called()
    return built


def test_daylight_runtime_keeps_point_when_shared_cams_grid_is_none():
    built = _build_daylight_with_missing_shared_data(
        cams_grid=None, four_models={"hourly": {}},
    )

    point = built["points"][0]
    event = point["events"]["sunset"]
    assert not built.get("error")
    assert not point.get("error")
    assert event["weather"]["pm2_5"] is None
    assert event["smoke_assessment"]["consensus"]["status"] == "SINGLE_MODEL_ONLY"
    assert "煙塵資料暫缺，煙分以不確定值計算" in event["notes"]


def test_daylight_runtime_keeps_best_match_when_shared_four_models_is_none():
    cams = {"hourly": {
        "time": ["2026-09-20T18:00", "2026-09-20T19:00", "2026-09-20T20:00"],
        "pm2_5": [5, 5, 5], "us_aqi": [20, 20, 20],
    }}
    built = _build_daylight_with_missing_shared_data(
        cams_grid=[cams] * 9, four_models=None,
    )

    point = built["points"][0]
    event = point["events"]["sunset"]
    assert not built.get("error")
    assert not point.get("error")
    assert event["wind_detail"]["ecmwf_missing"] is True
    assert event["confidence"]["models"] == {"best_match": 20}
