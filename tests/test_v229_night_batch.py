from unittest.mock import patch

from backend.scripts import night_report


def test_night_run_all_keeps_individual_failure_without_refetching_batch():
    locs = {
        "a": {"lat": 50.0, "lon": -116.0},
        "b": {"lat": 51.0, "lon": -115.0},
    }
    weather = [{"hourly": {}}, {"hourly": {}}]
    air = [{"hourly": {}}, {"hourly": {}}]

    def analyze(location_id, *_args, **_kwargs):
        if location_id == "b":
            raise ValueError("missing site payload")
        return {"location_id": location_id, "night": {"score": 50}}

    with patch.object(night_report, "fetch_weather_batch", return_value=weather) as weather_fetch, \
         patch.object(night_report, "fetch_air_quality_batch", return_value=air) as aq_fetch, \
         patch.object(night_report, "analyze", side_effect=analyze):
        results = night_report.run_all_locations(locs, "2026-09-20")

    weather_fetch.assert_called_once()
    aq_fetch.assert_called_once()
    assert results[0]["location_id"] == "a"
    assert results[1]["location_id"] == "b"
    assert results[1]["error"] is True
    assert "missing site payload" in results[1]["message"]


def test_night_run_all_fails_all_sites_honestly_on_mismatched_batch_count():
    locs = {
        "a": {"lat": 50.0, "lon": -116.0},
        "b": {"lat": 51.0, "lon": -115.0},
    }
    with patch.object(night_report, "fetch_weather_batch", return_value=[{"hourly": {}}]), \
         patch.object(night_report, "fetch_air_quality_batch") as aq_fetch:
        results = night_report.run_all_locations(locs, "2026-09-20")

    aq_fetch.assert_not_called()
    assert len(results) == 2
    assert all(item["error"] for item in results)
    assert all("預期 2" in item["message"] for item in results)
