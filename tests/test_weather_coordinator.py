from urllib.parse import parse_qs, urlparse

import pytest

from backend.weather_coordinator import (
    RequestPlan,
    build_request_plan,
    fetch_request_plan,
    stable_request_key,
)


def locations():
    return {
        "zeta": {"lat": 51.0, "lon": -115.0},
        "alpha": {"lat": 50.0, "lon": -116.0},
        "same_alpha_is_invalid": {"lat": 50.0, "lon": -116.0},
    }


def test_request_plan_dedupes_in_deterministic_id_order_with_reverse_mapping():
    plan = build_request_plan(
        {
            "zeta": {"lat": 50.4, "lon": -116.0},
            "alpha": {"lat": 50.0, "lon": -116.0},
        },
        {
            ("zeta", "2026-09-20", "sunset"): (52.0, -114.0),
            ("alpha", "2026-09-20", "sunrise"): (52.0, -114.0),
            ("alpha", "2026-09-21", "sunrise"): (53.0, -113.0),
        },
    )

    assert isinstance(plan, RequestPlan)
    assert plan.site_coords == ((50.0, -116.0), (50.4, -116.0))
    assert plan.site_index == {"alpha": 0, "zeta": 1}
    assert plan.horizon_coords == ((52.0, -114.0), (53.0, -113.0))
    assert plan.horizon_index[("alpha", "2026-09-20", "sunrise")] == 0
    assert plan.horizon_index[("zeta", "2026-09-20", "sunset")] == 0
    assert plan.horizon_index[("alpha", "2026-09-21", "sunrise")] == 1
    assert plan.fallback_coords == plan.site_coords
    assert len(plan.cams_coords) < len(plan.site_coords) * 9
    assert len(plan.cams_index["alpha"]) == 9


def test_request_plan_rejects_duplicate_canonical_site_coordinates():
    with pytest.raises(ValueError, match="duplicate canonical coordinate"):
        build_request_plan(locations(), {})


def test_stable_request_key_normalizes_param_order_and_coordinate_format():
    left = stable_request_key("https://example.test", {"b": 2, "latitude": "50.000000,51.0", "a": 1})
    right = stable_request_key("https://example.test", {"a": "1", "latitude": "50,51.000000", "b": "2"})
    assert left == right


def _responses(url):
    query = parse_qs(urlparse(url).query)
    count = len(query["latitude"][0].split(","))
    return [{"hourly": {}} for _ in range(count)]


def test_fetch_plan_batches_best_models_horizon_once_and_cams_by_chunks():
    plan = build_request_plan(
        {"b": {"lat": 51.0, "lon": -115.0}, "a": {"lat": 50.0, "lon": -116.0}},
        {("a", "2026-09-20", "sunrise"): (52.0, -114.0)},
    )
    calls = []

    def fetch(url):
        calls.append(url)
        return _responses(url)

    result = fetch_request_plan(plan, fetch, cams_chunk_size=5)
    kinds = [item[0] for item in result["http_requests"]]
    assert kinds.count("site_best_match") == 1
    assert kinds.count("site_four_models") == 1
    assert kinds.count("horizon") == 1
    assert kinds.count("cams") == 4
    assert all(item[1]["forecast_days"] == "5" for item in result["http_requests"])
    assert result["site"]["a"]["best_match"] is not None
    assert result["site"]["a"]["four_models"] is not None


def test_mismatched_site_response_count_fails_each_site_without_refetching():
    plan = build_request_plan({"a": {"lat": 50.0, "lon": -116.0}, "b": {"lat": 51.0, "lon": -115.0}}, {})
    calls = []

    def fetch(url):
        calls.append(url)
        if len(calls) == 1:
            return [{"hourly": {}}]
        return _responses(url)

    result = fetch_request_plan(plan, fetch)
    assert len(calls) == 3  # best, four-model, CAMS; no horizon and no retry
    assert all(result["site"][key]["best_match"] is None for key in ("a", "b"))
    assert all("expected 2" in result["site"][key]["errors"][0] for key in ("a", "b"))


def test_met_fallback_is_called_once_with_unique_site_coordinates():
    plan = build_request_plan({"a": {"lat": 50.0, "lon": -116.0}, "b": {"lat": 51.0, "lon": -115.0}}, {})
    seen = []

    def fetch(url):
        if "air-quality" in url:
            return _responses(url)
        raise OSError("forecast unavailable")

    def fallback(coords, forecast_days):
        seen.append((coords, forecast_days))
        return [{"_source": "met_norway", "hourly": {}} for _ in coords]

    result = fetch_request_plan(plan, fetch, fallback_fetch=fallback)
    assert seen == [(plan.fallback_coords, 5)]
    assert all(result["site"][key]["best_match"]["_source"] == "met_norway" for key in ("a", "b"))
