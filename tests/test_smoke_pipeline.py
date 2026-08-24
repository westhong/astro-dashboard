import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from backend.smoke_pipeline import (
    CachedHttpFetcher,
    aligned_utc_window,
    assess_smoke_window,
    nearest_hotspot_km,
    parse_fire_locations_kml,
)

UTC = timezone.utc
LOCAL = ZoneInfo("America/Edmonton")


def model(value, **extra):
    return {
        "valid": value is not None,
        "window_avg_pm2_5": value,
        "window_range": [value, value] if value is not None else [None, None],
        "neighbor_range": [value, value] if value is not None else [None, None],
        **extra,
    }


class WindowTests(unittest.TestCase):
    def test_aligns_full_local_window_to_overlapping_utc_hour_frames(self):
        start, end = aligned_utc_window(
            datetime(2026, 8, 24, 22, 17, tzinfo=LOCAL),
            datetime(2026, 8, 25, 1, 42, tzinfo=LOCAL),
        )
        self.assertEqual(start, datetime(2026, 8, 25, 4, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 8, 25, 7, tzinfo=UTC))

    def test_rejects_naive_and_reversed_windows(self):
        with self.assertRaises(ValueError):
            aligned_utc_window(datetime(2026, 8, 24, 1), datetime(2026, 8, 24, 2))
        with self.assertRaises(ValueError):
            aligned_utc_window(
                datetime(2026, 8, 24, 2, tzinfo=LOCAL),
                datetime(2026, 8, 24, 1, tzinfo=LOCAL),
            )


class PipelineTests(unittest.TestCase):
    def test_calls_models_independently_and_builds_health_context_without_observation(self):
        calls = []
        def fire(**kw):
            calls.append(("fire", kw["lat"], kw["lon"], kw["start"], kw["end"]))
            raise OSError("fire offline")
        def cams(**kw):
            calls.append(("cams", kw["lat"], kw["lon"], kw["start"], kw["end"]))
            return model(7.7, pollutants={
                "pm2_5": 7.7, "pm10": 12.0, "ozone": 94.0,
                "nitrogen_dioxide": 4.0, "us_aqi_health_context": 54.0,
                "dominant_pollutant": "ozone",
            }, health_subindices={"pm2_5": 20.0, "pm10": 10.0, "ozone": 54.0, "nitrogen_dioxide": 4.0})
        def blue(**kw):
            calls.append(("blue", kw["lat"], kw["lon"], kw["start"], kw["end"]))
            return model(8.0, fire_locations_url=None)

        result = assess_smoke_window(
            lat=51.123456, lon=-115.654321,
            start_local=datetime(2026, 8, 24, 22, 17, tzinfo=LOCAL),
            end_local=datetime(2026, 8, 25, 1, 42, tzinfo=LOCAL),
            firework_fetch=fire, cams_fetch=cams, bluesky_fetch=blue,
        )
        assessment = result["smoke_assessment"]
        self.assertEqual([c[0] for c in calls], ["fire", "cams", "blue"])
        self.assertTrue(all(c[1:3] == (51.123456, -115.654321) for c in calls))
        self.assertFalse(assessment["models"]["eccc_firework"]["valid"])
        self.assertEqual(assessment["consensus"]["status"], "LIKELY_CLEAN")
        self.assertEqual(assessment["consensus"]["coverage"]["valid"], 2)
        self.assertTrue(assessment["consensus"]["partial"])
        self.assertEqual(assessment["consensus"]["photography_smoke_score"], 90)
        self.assertEqual(assessment["pollutants"]["us_aqi_health_context"], 54.0)
        self.assertEqual(assessment["pollutants"]["dominant_pollutant"], "ozone")
        self.assertTrue(all(v is None for v in assessment["observed_now"].values()))

    def test_default_bluesky_discovery_uses_validated_html_fetcher_only(self):
        class Http:
            def html(self, url): return "validated index"
            def text(self, url): return "generic text"
            def bytes(self, url): return b"binary"
            def json(self, url): return {}

        http = Http()
        with patch("backend.smoke_pipeline.fetch_bluesky_window", return_value=model(8)) as bluesky:
            assess_smoke_window(
                lat=51, lon=-115,
                start_local=datetime(2026, 8, 24, 22, tzinfo=LOCAL),
                end_local=datetime(2026, 8, 24, 23, tzinfo=LOCAL),
                firework_fetch=lambda **kw: model(8),
                cams_fetch=lambda **kw: model(8),
                http_fetcher=http,
            )

        self.assertEqual(bluesky.call_args.kwargs["fetch_text"], http.html)

    def test_only_reported_bad_firework_binary_is_evicted(self):
        calls = []
        valid_url = "https://geo.weather.gc.ca/valid.tif"
        bad_url = "https://geo.weather.gc.ca/corrupt.tif"
        with tempfile.TemporaryDirectory() as directory:
            def opener(request, timeout=0):
                calls.append(request.full_url)
                body = b"valid" if request.full_url == valid_url else b"truncated"
                return HttpCacheTests.Response(body, 200, "image/tiff")

            http = CachedHttpFetcher(
                Path(directory), opener=opener, sleep=lambda _: None,
                today=lambda: "2026-08-24",
            )

            def invalid_firework(**kwargs):
                kwargs["fetch_bytes"](valid_url)
                kwargs["fetch_bytes"](bad_url)
                return model(
                    None,
                    status="FireWork unavailable: corrupt GeoTIFF",
                    failed_urls=[bad_url],
                )

            common = dict(
                lat=51, lon=-115,
                start_local=datetime(2026, 8, 24, 22, tzinfo=LOCAL),
                end_local=datetime(2026, 8, 24, 23, tzinfo=LOCAL),
                cams_fetch=lambda **kw: model(8),
                bluesky_fetch=lambda **kw: model(8),
                http_fetcher=http,
            )
            with patch("backend.smoke_pipeline.fetch_firework_window", side_effect=invalid_firework):
                assess_smoke_window(**common)
                assess_smoke_window(**common)

            self.assertEqual(calls, [valid_url, bad_url, bad_url])
            self.assertTrue(http._path(valid_url).exists())
            self.assertFalse(http._path(bad_url).exists())

    def test_out_of_range_bluesky_result_keeps_valid_cycle_binary_cached(self):
        calls = []
        dispersion_url = "https://firesmoke.ca/cycle/dispersion.nc"
        with tempfile.TemporaryDirectory() as directory:
            def opener(request, timeout=0):
                calls.append(request.full_url)
                return HttpCacheTests.Response(b"valid-cycle", 200, "application/octet-stream")

            http = CachedHttpFetcher(
                Path(directory), opener=opener, sleep=lambda _: None,
                today=lambda: "2026-08-24",
            )

            def outside_window(**kwargs):
                kwargs["fetch_bytes"](dispersion_url)
                return model(None, status="BlueSky does not cover the full window")

            common = dict(
                lat=51, lon=-115,
                start_local=datetime(2026, 8, 24, 22, tzinfo=LOCAL),
                end_local=datetime(2026, 8, 24, 23, tzinfo=LOCAL),
                firework_fetch=lambda **kw: model(8),
                cams_fetch=lambda **kw: model(8),
                http_fetcher=http,
            )
            with patch("backend.smoke_pipeline.fetch_bluesky_window", side_effect=outside_window):
                assess_smoke_window(**common)
                assess_smoke_window(**common)

            self.assertEqual(calls, [dispersion_url])
            self.assertTrue(http._path(dispersion_url).exists())

    def test_malformed_adapter_results_are_isolated_and_normalized(self):
        calls = []

        def malformed(name, value):
            def fetch(**_kwargs):
                calls.append(name)
                return value
            return fetch

        result = assess_smoke_window(
            lat=51.0, lon=-115.0,
            start_local=datetime(2026, 8, 24, 22, tzinfo=LOCAL),
            end_local=datetime(2026, 8, 24, 23, tzinfo=LOCAL),
            firework_fetch=malformed("fire", None),
            cams_fetch=malformed("cams", []),
            bluesky_fetch=malformed("blue", {"valid": True}),
        )["smoke_assessment"]

        self.assertEqual(calls, ["fire", "cams", "blue"])
        self.assertEqual(result["consensus"]["coverage"]["valid"], 0)
        for source in result["models"].values():
            self.assertFalse(source["valid"])
            self.assertIsNone(source["window_avg_pm2_5"])
            self.assertEqual(source["window_range"], [None, None])
            self.assertEqual(source["neighbor_range"], [None, None])
            self.assertIn("malformed", source["status"].lower())
        self.assertEqual(
            sum("malformed" in item.lower() for item in result["uncertainties"]),
            3,
        )

    def test_all_models_unavailable_is_no_data_not_exception(self):
        def fail(**kw):
            raise OSError("offline")
        result = assess_smoke_window(
            lat=51.0, lon=-115.0,
            start_local=datetime(2026, 8, 24, 22, tzinfo=LOCAL),
            end_local=datetime(2026, 8, 24, 23, tzinfo=LOCAL),
            firework_fetch=fail, cams_fetch=fail, bluesky_fetch=fail,
        )["smoke_assessment"]
        self.assertEqual(result["consensus"]["coverage"]["valid"], 0)
        self.assertEqual(result["consensus"]["photography_smoke_score"], 60)
        self.assertTrue(result["consensus"]["uncertain"])

    def test_dust_rule_is_non_scoring_uncertainty(self):
        cams = model(7.0, pollutants={"pm2_5": 7.0, "pm10": 20.0, "ozone": 20.0,
            "nitrogen_dioxide": 2.0, "us_aqi_health_context": 30.0}, health_subindices={})
        result = assess_smoke_window(
            lat=51, lon=-115,
            start_local=datetime(2026, 8, 24, 22, tzinfo=LOCAL),
            end_local=datetime(2026, 8, 24, 23, tzinfo=LOCAL),
            firework_fetch=lambda **kw: model(7), cams_fetch=lambda **kw: cams,
            bluesky_fetch=lambda **kw: model(7),
        )["smoke_assessment"]
        self.assertEqual(result["consensus"]["photography_smoke_score"], 90)
        self.assertTrue(any("dust" in x.lower() for x in result["uncertainties"]))


class KmlTests(unittest.TestCase):
    KML = '''<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
      <Placemark><Point><coordinates>-115.0,51.0,0</coordinates></Point></Placemark>
      <Placemark><Point><coordinates>-114.0,52.0</coordinates></Point></Placemark>
    </Document></kml>'''

    def test_parses_hotspots_and_calculates_nearest_haversine_distance(self):
        points = parse_fire_locations_kml(self.KML)
        self.assertEqual(points, [(51.0, -115.0), (52.0, -114.0)])
        self.assertAlmostEqual(nearest_hotspot_km(51.0, -115.0, points), 0.0)

    def test_kml_failure_only_adds_uncertainty(self):
        blue = model(8, fire_locations_url="https://x/fire_locations.kml")
        result = assess_smoke_window(
            lat=51, lon=-115,
            start_local=datetime(2026, 8, 24, 22, tzinfo=LOCAL),
            end_local=datetime(2026, 8, 24, 23, tzinfo=LOCAL),
            firework_fetch=lambda **kw: model(8), cams_fetch=lambda **kw: model(8),
            bluesky_fetch=lambda **kw: blue,
            fetch_text=lambda url: (_ for _ in ()).throw(OSError("kml offline")),
        )["smoke_assessment"]
        self.assertEqual(result["consensus"]["status"], "VERIFIED_CLEAN")
        self.assertEqual(result["source_support"]["classification"], "NO_IDENTIFIED_SOURCE")
        self.assertTrue(any("KML" in x for x in result["uncertainties"]))


class HttpCacheTests(unittest.TestCase):
    class Response:
        def __init__(self, body=b'{"ok": true}', status=200, content_type="application/json"):
            self.body, self.status = body, status
            self.headers = {"Content-Type": content_type}
        def read(self): return self.body
        def __enter__(self): return self
        def __exit__(self, *args): pass

    def test_retries_transient_and_atomically_caches_by_full_url_and_date(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            responses = [OSError("network"), self.Response(status=429), self.Response()]
            def opener(request, timeout=0):
                calls.append(request.full_url)
                item = responses.pop(0)
                if isinstance(item, Exception): raise item
                return item
            fetcher = CachedHttpFetcher(Path(directory), opener=opener, sleep=lambda _: None, today=lambda: "2026-08-24")
            self.assertEqual(fetcher.json("https://x.test/a?q=1"), {"ok": True})
            self.assertEqual(fetcher.json("https://x.test/a?q=1"), {"ok": True})
            self.assertEqual(len(calls), 3)
            self.assertEqual(len(list(Path(directory).glob("*.cache"))), 1)
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_concurrent_same_url_fetches_do_not_race_on_temporary_file(self):
        url = "https://x.test/concurrent"
        fetch_barrier = threading.Barrier(2)
        replace_sources = []
        source_lock = threading.Lock()
        with tempfile.TemporaryDirectory() as directory:
            def opener(*_args, **_kwargs):
                fetch_barrier.wait(timeout=2)
                return self.Response(b"complete", 200, "application/octet-stream")

            fetcher = CachedHttpFetcher(
                Path(directory), opener=opener, sleep=lambda _: None,
                today=lambda: "2026-08-24", attempts=1,
            )
            real_replace = __import__("os").replace

            def tracked_replace(source, destination):
                with source_lock:
                    replace_sources.append(Path(source))
                return real_replace(source, destination)

            with patch("backend.smoke_pipeline.os.replace", side_effect=tracked_replace):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [executor.submit(fetcher.bytes, url) for _ in range(2)]
                    results = [future.result(timeout=3) for future in futures]

            self.assertEqual(results, [b"complete", b"complete"])
            self.assertEqual(len(set(replace_sources)), 2)
            self.assertEqual(fetcher._path(url).read_bytes(), b"complete")
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_validated_bluesky_index_html_is_cached_in_explicit_html_mode(self):
        calls = []
        payload = b'''<!doctype html><html><body>
          Forecast ID: BSC00CA12-01
          <a href="dispersion.nc">dispersion.nc</a>
        </body></html>'''
        with tempfile.TemporaryDirectory() as directory:
            def opener(request, timeout=0):
                calls.append(request.full_url)
                return self.Response(payload, 200, "text/html; charset=UTF-8")
            fetcher = CachedHttpFetcher(
                Path(directory), opener=opener, sleep=lambda _: None,
                today=lambda: "2026-08-24",
            )

            self.assertEqual(fetcher.html("https://firesmoke.ca/forecasts/current/"), payload.decode())
            self.assertEqual(fetcher.html("https://firesmoke.ca/forecasts/current/"), payload.decode())
            self.assertEqual(calls, ["https://firesmoke.ca/forecasts/current/"])
            self.assertEqual(len(list(Path(directory).glob("*.cache"))), 1)
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_bluesky_html_mode_rejects_unmarked_error_page_without_caching(self):
        with tempfile.TemporaryDirectory() as directory:
            fetcher = CachedHttpFetcher(
                Path(directory),
                opener=lambda *a, **k: self.Response(
                    b"<!doctype html><html>Access denied</html>", 200, "text/html"
                ),
                sleep=lambda _: None,
            )

            with self.assertRaisesRegex(ValueError, "Invalid BlueSky index HTML"):
                fetcher.html("https://firesmoke.ca/forecasts/current/")
            self.assertFalse(list(Path(directory).glob("*.cache")))

    def test_errors_and_html_are_never_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            fetcher = CachedHttpFetcher(
                Path(directory), opener=lambda *a, **k: self.Response(b"<html>x</html>", 200, "text/html"),
                sleep=lambda _: None,
            )
            with self.assertRaises(ValueError): fetcher.bytes("https://x.test/a")
            self.assertFalse(list(Path(directory).glob("*.cache")))

    def test_json_api_html_error_is_rejected_without_caching(self):
        with tempfile.TemporaryDirectory() as directory:
            fetcher = CachedHttpFetcher(
                Path(directory),
                opener=lambda *a, **k: self.Response(
                    b"<!doctype html><html>upstream error</html>", 200, "text/html"
                ),
                sleep=lambda _: None,
            )

            with self.assertRaisesRegex(ValueError, "HTML response rejected"):
                fetcher.json("https://api.test/data.json")
            self.assertFalse(list(Path(directory).glob("*.cache")))

    def test_non_transient_4xx_is_not_retried_or_cached(self):
        calls = []
        with tempfile.TemporaryDirectory() as directory:
            def opener(request, timeout=0):
                calls.append(request.full_url)
                raise HTTPError(request.full_url, 404, "missing", None, None)
            fetcher = CachedHttpFetcher(Path(directory), opener=opener, sleep=lambda _: None)
            with self.assertRaises(HTTPError):
                fetcher.bytes("https://x.test/missing")
            self.assertEqual(calls, ["https://x.test/missing"])
            self.assertFalse(list(Path(directory).glob("*.cache")))


class WorkflowTests(unittest.TestCase):
    def test_fallback_installs_and_preflights_smoke_runtime_dependencies(self):
        workflow = (Path(__file__).parents[1] / ".github" / "workflows" / "update.yml").read_text(
            encoding="utf-8"
        )
        install = "pip install skyfield requests -r requirements-smoke.txt"
        preflight = 'python -c "import numpy, scipy, tifffile"'
        self.assertIn(install, workflow)
        self.assertIn(preflight, workflow)
        self.assertLess(workflow.index(install), workflow.index(preflight))
        self.assertLess(workflow.index(preflight), workflow.index("python backend/build_report.py"))


if __name__ == "__main__":
    unittest.main()
