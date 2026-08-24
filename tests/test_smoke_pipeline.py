import json
import tempfile
import unittest
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

    def test_errors_and_html_are_never_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            fetcher = CachedHttpFetcher(
                Path(directory), opener=lambda *a, **k: self.Response(b"<html>x</html>", 200, "text/html"),
                sleep=lambda _: None,
            )
            with self.assertRaises(ValueError): fetcher.bytes("https://x.test/a")
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


if __name__ == "__main__":
    unittest.main()
