import asyncio
import inspect
import json
import multiprocessing
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import app
from backend import build_report as static_builder
from backend.process_isolation import (
    IsolatedProcessError,
    IsolatedProcessTimeout,
    run_in_spawned_process,
)


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
        payload = app._build_report_sync("2026-09-20")

    prepare.assert_called_once_with(["2026-09-20"], coordinator)
    run_all.assert_called_once_with(ids, "2026-09-20", coordinator)
    daylight.assert_called_once_with("2026-09-20", coordinator)
    subprocess_call.assert_not_called()
    assert [item["location_id"] for item in payload["locations"]] == ids


def test_static_builder_does_not_refetch_individual_missing_sites():
    source = inspect.getsource(static_builder._build_static_reports)
    assert "run_one(" not in source


def test_lan_report_keeps_event_loop_responsive_during_complete_sync_build():
    def slow_isolated_call(*_args, **_kwargs):
        time.sleep(0.15)
        return {"locations": [], "failed_count": 0}

    async def exercise():
        started = time.perf_counter()
        report_task = asyncio.create_task(app.build_report("2026-09-20"))
        await asyncio.sleep(0.02)
        tick_elapsed = time.perf_counter() - started
        await report_task
        return tick_elapsed

    with patch.object(app, "run_in_spawned_process", side_effect=slow_isolated_call):
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

    app._reset_report_state_for_tests()
    with patch.object(app, "_edmonton_today", return_value=date(2026, 9, 20)), \
         patch.object(app, "build_report", side_effect=slow_build):
        responses = asyncio.run(exercise())

    assert calls == 1
    assert [json.loads(response.body) for response in responses] == [
        {"night_date": "2026-09-20", "locations": [], "failed_count": 0,
         "cache_age_seconds": 0},
    ] * 2


def test_cancelled_same_date_waiter_does_not_cancel_shared_build():
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
        cancelled_waiter = asyncio.create_task(app.report("2026-09-20"))
        await started.wait()
        surviving_waiter = asyncio.create_task(app.report("2026-09-20"))
        await asyncio.sleep(0)
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter
        assert app._inflight["2026-09-20"].done() is False
        release.set()
        return json.loads((await surviving_waiter).body)

    app._reset_report_state_for_tests()
    with patch.object(app, "_edmonton_today", return_value=date(2026, 9, 20)), \
         patch.object(app, "build_report", side_effect=slow_build):
        payload = asyncio.run(exercise())

    assert calls == 1
    assert payload["night_date"] == "2026-09-20"
    assert payload["cache_age_seconds"] == 0
    assert not app._inflight


def test_lan_report_timeout_returns_honest_error_payload_promptly():
    started = time.perf_counter()
    with patch.object(
        app, "location_ids", return_value=["vermilion_lakes", "two_jack_lake"]
    ), patch.object(
        app, "run_in_spawned_process", side_effect=IsolatedProcessTimeout(0.05)
    ), patch.object(app, "LAN_REPORT_TIMEOUT", 0.05):
        payload = asyncio.run(app.build_report("2026-09-20"))
    elapsed = time.perf_counter() - started

    assert elapsed < 0.2
    assert payload["failed_count"] == 2
    assert all(item["error"] for item in payload["locations"])
    assert all("超時" in item["message"] for item in payload["locations"])


@pytest.mark.parametrize("stage", ["coordinator_prep", "spots", "daylight"])
def test_complete_report_timeout_terminates_worker_without_late_mutation(stage):
    with tempfile.TemporaryDirectory() as directory:
        before = {child.pid for child in multiprocessing.active_children()}
        started = time.perf_counter()
        with pytest.raises(IsolatedProcessTimeout):
            run_in_spawned_process(
                "tests.process_timeout_fixtures",
                "blocking_complete_report",
                args=(stage, directory, 2.0),
                timeout=0.15,
            )
        elapsed = time.perf_counter() - started
        time.sleep(0.25)
        after = {child.pid for child in multiprocessing.active_children()}

        assert elapsed < 1.0
        assert (Path(directory) / f"{stage}.entered").exists()
        assert not (Path(directory) / f"{stage}.late").exists()
        assert after <= before


def test_cancelled_build_waits_for_spawned_worker_cleanup_before_releasing_slot():
    app._reset_report_state_for_tests()
    entered = threading.Event()
    child_stopped = threading.Event()
    allow_runner_return = threading.Event()

    def redirected_runner(*_args, **kwargs):
        entered.set()
        return run_in_spawned_process(
            "tests.process_timeout_fixtures",
            "blocking_complete_report",
            args=("daylight", kwargs.pop("marker_dir"), 5.0),
            timeout=10,
            cancellation_event=kwargs["cancellation_event"],
        )

    async def exercise(marker_dir):
        real_stop = __import__("backend.process_isolation", fromlist=["_stop_process"])._stop_process

        def delayed_cleanup(process):
            real_stop(process)
            child_stopped.set()
            assert allow_runner_return.wait(2)

        def runner(*args, **kwargs):
            kwargs["marker_dir"] = marker_dir
            return redirected_runner(*args, **kwargs)

        with patch.object(app, "MAX_CONCURRENT_REPORT_BUILDS", 1), \
             patch.object(app, "run_in_spawned_process", side_effect=runner), \
             patch("backend.process_isolation._stop_process", side_effect=delayed_cleanup):
            first = asyncio.create_task(app._build_and_cache("2026-09-20", None))
            assert await asyncio.to_thread(entered.wait, 2)
            marker = Path(marker_dir) / "daylight.entered"
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert marker.exists()
            first.cancel()
            assert await asyncio.to_thread(child_stopped.wait, 2)
            await asyncio.sleep(0.05)
            assert not first.done()
            assert app._report_build_semaphore().locked()
            allow_runner_return.set()
            with pytest.raises(asyncio.CancelledError):
                await first

    with tempfile.TemporaryDirectory() as directory:
        before = {child.pid for child in multiprocessing.active_children()}
        asyncio.run(exercise(directory))
        time.sleep(0.1)
        assert not (Path(directory) / "daylight.late").exists()
        assert {child.pid for child in multiprocessing.active_children()} <= before


@pytest.mark.parametrize("function_name", ["raise_worker_error", "exit_without_result"])
def test_spawned_worker_failure_is_reported_as_isolation_error(function_name):
    with pytest.raises(IsolatedProcessError):
        run_in_spawned_process(
            "tests.process_timeout_fixtures", function_name, timeout=2,
        )


def test_lan_worker_error_returns_same_honest_schema_to_same_date_waiters():
    ids = [f"location-{index}" for index in range(16)]

    async def exercise():
        first = asyncio.create_task(app.report("2026-09-20"))
        second = asyncio.create_task(app.report("2026-09-20"))
        responses = await asyncio.gather(first, second)
        return [json.loads(response.body) for response in responses]

    app._reset_report_state_for_tests()
    with patch.object(app, "_edmonton_today", return_value=date(2026, 9, 20)), \
         patch.object(app, "location_ids", return_value=ids), \
         patch.object(app, "run_in_spawned_process", side_effect=IsolatedProcessError("lost result")):
        payloads = asyncio.run(exercise())

    assert payloads[0] == payloads[1]
    payload = payloads[0]
    assert payload["version"] == app.VERSION
    assert payload["night_date"] == "2026-09-20"
    assert payload["generated_utc"]
    assert payload["failed_count"] == 16
    assert [item["location_id"] for item in payload["locations"]] == ids
    assert all(item["error"] and "工作程序失敗" in item["message"] for item in payload["locations"])
    assert payload["best_location_id"] is None
    assert payload["daylight"]["error"] is True
    assert not app._inflight


def test_static_worker_error_fails_build_instead_of_publishing_fabricated_report():
    with patch.object(
        static_builder, "run_in_spawned_process", side_effect=IsolatedProcessError("lost result")
    ):
        with pytest.raises(IsolatedProcessError):
            static_builder.main()


def test_static_main_isolates_one_complete_build_not_each_site():
    with patch.object(static_builder, "run_in_spawned_process") as isolated:
        static_builder.main()

    isolated.assert_called_once_with(
        "backend.build_report",
        "_build_static_reports",
        timeout=static_builder.STATIC_BUILD_TIMEOUT,
    )


@pytest.mark.parametrize("requested", ["2026-09-20", "2026-09-26", "not-a-date"])
def test_lan_rejects_dates_outside_frontend_five_day_horizon(requested):
    with patch.object(app, "_edmonton_today", return_value=date(2026, 9, 21)):
        with pytest.raises(HTTPException) as exc:
            app._validate_report_date(requested)

    assert exc.value.status_code == 422


@pytest.mark.parametrize("requested", ["2026-09-21", "2026-09-25"])
def test_lan_accepts_frontend_five_day_horizon_boundaries(requested):
    with patch.object(app, "_edmonton_today", return_value=date(2026, 9, 21)):
        assert app._validate_report_date(requested) == requested


def test_lan_cache_and_inflight_bookkeeping_stay_bounded():
    async def immediate_build(date_str, prior_payload=None):
        return {"night_date": date_str, "locations": [], "failed_count": 0}

    async def exercise():
        for offset in range(20):
            app._cache[f"2000-01-{offset + 1:02d}"] = (0, {})
        await app.report("2026-09-21")
        await asyncio.sleep(0)

    app._reset_report_state_for_tests()
    with patch.object(app, "_edmonton_today", return_value=date(2026, 9, 21)), \
         patch.object(app, "build_report", side_effect=immediate_build):
        asyncio.run(exercise())

    assert len(app._cache) <= app.REPORT_CACHE_MAX
    assert len(app._inflight) == 0


def test_distinct_date_builds_respect_global_concurrency_cap():
    active = 0
    peak = 0
    release = asyncio.Event()

    async def blocked_build(date_str, prior_payload=None):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await release.wait()
            return {"night_date": date_str, "locations": [], "failed_count": 0}
        finally:
            active -= 1

    async def exercise():
        tasks = [
            asyncio.create_task(app.report(f"2026-09-{day:02d}"))
            for day in (21, 22, 23, 24, 25)
        ]
        for _ in range(20):
            if peak == app.MAX_CONCURRENT_REPORT_BUILDS:
                break
            await asyncio.sleep(0.01)
        assert peak == app.MAX_CONCURRENT_REPORT_BUILDS
        release.set()
        await asyncio.gather(*tasks)

    app._reset_report_state_for_tests()
    with patch.object(app, "_edmonton_today", return_value=date(2026, 9, 21)), \
         patch.object(app, "build_report", side_effect=blocked_build):
        asyncio.run(exercise())

    assert peak == app.MAX_CONCURRENT_REPORT_BUILDS
    assert not app._inflight
