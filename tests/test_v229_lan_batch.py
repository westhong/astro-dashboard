import asyncio
import inspect
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
    run_all.assert_called_once_with(ids, "2026-09-20", coordinator)
    daylight.assert_called_once_with("2026-09-20", coordinator)
    subprocess_call.assert_not_called()
    assert [item["location_id"] for item in payload["locations"]] == ids


def test_static_builder_does_not_refetch_individual_missing_sites():
    source = inspect.getsource(static_builder.main)
    assert "run_one(" not in source
