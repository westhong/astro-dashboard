import asyncio
import inspect
import json
import threading
import time
from unittest.mock import patch

import app
from backend import build_report as static_builder


def test_lan_uses_one_all_locations_batch_and_no_per_site_subprocess():
    ids = ["a", "b", "c"]
    batch = [
        {"location_id": location_id, "night": {"grade_code": "MARGINAL", "score": 55}}
        for location_id in ids
    ]
    coordinator = object()
    with patch.object(app, "location_ids", return_value=ids), \
         patch("backend.build_report.create_weather_coordinator", return_value=coordinator), \
         patch("backend.daylight_report.prepare_coordinator_horizons") as prepare, \
         patch("backend.build_report.run_all_with_coordinator", return_value=batch) as run_all, \
         patch("backend.build_report.select_best_location", return_value=batch[0]), \
         patch.object(app, "build_spots", return_value=[]), \
         patch.object(app, "build_daylight_report", return_value={"points": []}) as daylight, \
         patch.object(app.asyncio, "create_subprocess_exec") as subprocess_call:
        payload = asyncio.run(app.build_report("2026-09-20"))

    prepare.assert_called_once_with(["2026-09-20"], coordinator)
    run_all.assert_called_once_with(
        ids, "2026-09-20", coordinator, timeout=app.LAN_BATCH_TIMEOUT
    )
    daylight.assert_called_once_with("2026-09-20", coordinator)
    subprocess_call.assert_not_called()
    assert [item["location_id"] for item in payload["locations"]] == ids


def test_static_builder_does_not_refetch_individual_missing_sites():
    source = inspect.getsource(static_builder.main)
    assert "run_one(" not in source


def test_lan_report_keeps_event_loop_responsive_during_complete_sync_build():
    batch = [{"location_id": "a", "night": {"grade_code": "MARGINAL", "score": 55}}]

    def slow_spots(_date_str):
        time.sleep(0.15)
        return []

    async def exercise():
        started = time.perf_counter()
        report_task = asyncio.create_task(app.build_report("2026-09-20"))
        await asyncio.sleep(0.02)
        tick_elapsed = time.perf_counter() - started
        await report_task
        return tick_elapsed

    with patch.object(app, "location_ids", return_value=["a"]), \
         patch("backend.build_report.create_weather_coordinator", return_value=object()), \
         patch("backend.daylight_report.prepare_coordinator_horizons"), \
         patch("backend.build_report.run_all_with_coordinator", return_value=batch), \
         patch("backend.build_report.select_best_location", return_value=batch[0]), \
         patch.object(app, "build_spots", side_effect=slow_spots), \
         patch.object(app, "build_daylight_report", return_value={"points": []}):
        tick_elapsed = asyncio.run(exercise())

    assert tick_elapsed < 0.1


def test_same_date_concurrent_lan_cache_misses_share_one_build():
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def slow_build(date_str, prior_payload=None):
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"night_date": date_str, "locations": [], "failed_count": 0}

    async def exercise():
        first = asyncio.create_task(app.report("2026-09-20"))
        await started.wait()
        second = asyncio.create_task(app.report("2026-09-20"))
        await asyncio.sleep(0)
        release.set()
        return await asyncio.gather(first, second)

    app._cache.clear()
    with patch.object(app, "build_report", side_effect=slow_build):
        responses = asyncio.run(exercise())

    assert calls == 1
    assert [json.loads(response.body) for response in responses] == [
        {"night_date": "2026-09-20", "locations": [], "failed_count": 0,
         "cache_age_seconds": 0},
    ] * 2


def test_lan_report_timeout_returns_honest_error_payload_promptly():
    blocked = threading.Event()

    def never_finishes(*_args, **_kwargs):
        blocked.wait(5)

    started = time.perf_counter()
    with patch.object(
        app, "location_ids", return_value=["vermilion_lakes", "two_jack_lake"]
    ), patch(
        "backend.build_report.create_weather_coordinator", return_value=object()
    ), patch(
        "backend.daylight_report.prepare_coordinator_horizons"
    ), patch(
        "backend.scripts.night_report.run_all_locations", side_effect=never_finishes
    ), patch.object(
        app, "build_spots", return_value=[]
    ), patch.object(
        app, "build_daylight_report", return_value={"points": []}
    ), patch.object(app, "LAN_BATCH_TIMEOUT", 0.05):
        payload = asyncio.run(app.build_report("2026-09-20"))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.2
    assert payload["failed_count"] == 2
    assert all(item["error"] for item in payload["locations"])
    assert all("超時" in item["message"] for item in payload["locations"])


def test_static_batch_timeout_returns_per_location_errors_promptly():
    blocked = threading.Event()

    def never_finishes(*_args, **_kwargs):
        blocked.wait(5)

    started = time.perf_counter()
    with patch("backend.scripts.night_report.run_all_locations", side_effect=never_finishes):
        results = static_builder.run_all_with_coordinator(
            ["vermilion_lakes", "two_jack_lake"],
            "2026-09-20",
            coordinator=object(),
            timeout=0.05,
        )
    elapsed = time.perf_counter() - started

    assert elapsed < 0.2
    assert [item["location_id"] for item in results] == [
        "vermilion_lakes", "two_jack_lake",
    ]
    assert all(item["error"] for item in results)
    assert all("超時" in item["message"] for item in results)
