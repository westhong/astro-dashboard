import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import numpy as np

from backend.smoke_sources import (
    build_cams_url,
    build_firework_wcs_url,
    bluesky_cache_path,
    ensure_cached_bluesky,
    extract_bluesky_netcdf,
    extract_firework_geotiff,
    fetch_bluesky_window,
    fetch_cams_window,
    fetch_firework_window,
    parse_bluesky_index,
    parse_firework_capabilities,
    parse_time_dimension,
)


UTC = timezone.utc


class FireWorkSourceTests(unittest.TestCase):
    def test_parses_hourly_interval_and_default_without_inventing_frames(self):
        times = parse_time_dimension(
            "2026-08-24T06:00:00Z/2026-08-24T09:00:00Z/PT1H"
        )
        self.assertEqual(
            times,
            [
                datetime(2026, 8, 24, hour, tzinfo=UTC)
                for hour in (6, 7, 8, 9)
            ],
        )
        self.assertEqual(
            parse_time_dimension("2026-08-24T06:00:00Z,2026-08-24T08:00:00Z"),
            [
                datetime(2026, 8, 24, 6, tzinfo=UTC),
                datetime(2026, 8, 24, 8, tzinfo=UTC),
            ],
        )

    def test_builds_wcs_201_url_with_repeated_lat_long_subsets_and_cycle(self):
        url = build_firework_wcs_url(
            base_url="https://geo.weather.gc.ca/geomet",
            bbox=(49.0, -117.0, 53.0, -113.0),
            valid_time=datetime(2026, 8, 24, 7, tzinfo=UTC),
            reference_time=datetime(2026, 8, 24, 0, tzinfo=UTC),
        )
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["service"], ["WCS"])
        self.assertEqual(query["version"], ["2.0.1"])
        self.assertEqual(query["request"], ["GetCoverage"])
        self.assertEqual(query["coverageId"], ["RAQDPS.SFC_PM2.5"])
        self.assertEqual(query["subset"], ["lat(49.0,53.0)", "long(-117.0,-113.0)"])
        self.assertEqual(query["format"], ["image/tiff"])
        self.assertEqual(query["time"], ["2026-08-24T07:00:00Z"])
        self.assertEqual(query["reference_time"], ["2026-08-24T00:00:00Z"])

    @staticmethod
    def _geotiff(multiplier=1.0):
        import tifffile

        data = np.arange(1, 26, dtype=np.float32).reshape(5, 5) * 1e-9 * multiplier
        output = io.BytesIO()
        tifffile.imwrite(
            output,
            data,
            extratags=[
                (33550, "d", 3, (1.0, 1.0, 0.0), False),
                (33922, "d", 6, (0.0, 0.0, 0.0, -117.0, 53.0, 0.0), False),
            ],
        )
        return output.getvalue()

    def test_extracts_actual_pixel_and_complete_3x3_and_converts_kg_m3(self):
        sample = extract_firework_geotiff(self._geotiff(), lat=51.0, lon=-115.0)
        self.assertAlmostEqual(sample["point_pm2_5"], 13.0, places=5)
        self.assertEqual(len(sample["neighbors_pm2_5"]), 9)
        self.assertAlmostEqual(min(sample["neighbors_pm2_5"]), 7.0, places=5)
        self.assertAlmostEqual(max(sample["neighbors_pm2_5"]), 19.0, places=5)

        fractional = extract_firework_geotiff(self._geotiff(), lat=51.4, lon=-115.4)
        self.assertAlmostEqual(fractional["point_pm2_5"], 7.0, places=5)

        with self.assertRaises(ValueError):
            extract_firework_geotiff(self._geotiff(), lat=53.0, lon=-117.0)

    def test_fetches_every_hour_and_preserves_publish_gate_metadata(self):
        capabilities = """<WMS_Capabilities><Capability><Layer><Layer>
          <Name>RAQDPS.SFC_PM2.5</Name>
          <Dimension name="time" default="2026-08-24T06:00:00Z">2026-08-24T06:00:00Z/2026-08-24T07:00:00Z/PT1H</Dimension>
          <Dimension name="reference_time" default="2026-08-24T00:00:00Z">2026-08-24T00:00:00Z</Dimension>
        </Layer></Layer></Capability></WMS_Capabilities>"""
        parsed = parse_firework_capabilities(capabilities)
        self.assertEqual(parsed["reference_time"], datetime(2026, 8, 24, 0, tzinfo=UTC))
        self.assertEqual(parsed["valid_times"][-1], datetime(2026, 8, 24, 7, tzinfo=UTC))

        payloads = {6: self._geotiff(), 7: self._geotiff(2)}
        requested = []

        def fetch_bytes(url):
            requested.append(url)
            hour = int(parse_qs(urlparse(url).query)["time"][0][11:13])
            return payloads[hour]

        result = fetch_firework_window(
            lat=51.0,
            lon=-115.0,
            start=datetime(2026, 8, 24, 6, tzinfo=UTC),
            end=datetime(2026, 8, 24, 7, tzinfo=UTC),
            fetch_text=lambda _url: capabilities,
            fetch_bytes=fetch_bytes,
            retrieval_time=datetime(2026, 8, 24, 5, 30, tzinfo=UTC),
        )
        self.assertTrue(result["valid"])
        self.assertEqual(len(requested), 2)
        self.assertAlmostEqual(result["window_avg_pm2_5"], 19.5, places=5)
        self.assertEqual([round(x) for x in result["window_range"]], [13, 26])
        self.assertEqual([round(x) for x in result["neighbor_range"]], [7, 38])
        self.assertEqual(result["source"], "ECCC FireWork RAQDPS WCS")
        self.assertEqual(result["reference_time"], "2026-08-24T00:00:00Z")
        self.assertEqual(result["retrieval_time"], "2026-08-24T05:30:00Z")
        self.assertEqual(result["valid_range"], ["2026-08-24T06:00:00Z", "2026-08-24T07:00:00Z"])

    def test_missing_hour_or_network_failure_is_invalid_without_last_frame_reuse(self):
        capabilities = """<Layer><Name>RAQDPS.SFC_PM2.5</Name>
          <Dimension name="time">2026-08-24T06:00:00Z,2026-08-24T08:00:00Z</Dimension>
          <Dimension name="reference_time" default="2026-08-24T00:00:00Z" />
        </Layer>"""
        result = fetch_firework_window(
            lat=51.0,
            lon=-115.0,
            start=datetime(2026, 8, 24, 6, tzinfo=UTC),
            end=datetime(2026, 8, 24, 7, tzinfo=UTC),
            fetch_text=lambda _url: capabilities,
            fetch_bytes=lambda _url: self._geotiff(),
        )
        self.assertFalse(result["valid"])
        self.assertIsNone(result["window_avg_pm2_5"])
        self.assertIn("full window", result["status"])

        failed = fetch_firework_window(
            lat=51.0,
            lon=-115.0,
            start=datetime(2026, 8, 24, 6, tzinfo=UTC),
            end=datetime(2026, 8, 24, 6, tzinfo=UTC),
            fetch_text=lambda _url: (_ for _ in ()).throw(OSError("offline")),
            fetch_bytes=lambda _url: self._geotiff(),
        )
        self.assertFalse(failed["valid"])
        self.assertIn("offline", failed["status"])


class CamsSourceTests(unittest.TestCase):
    @staticmethod
    def _payload():
        times = ["2026-08-24T06:00", "2026-08-24T07:00"]
        responses = []
        for cell in range(9):
            hourly = {
                "time": times,
                "pm2_5": [cell + 1.0, cell + 3.0],
                "pm10": [10.0 + cell, 12.0 + cell],
                "ozone": [40.0, 42.0],
                "nitrogen_dioxide": [5.0, 6.0],
                "us_aqi": [50.0, 60.0],
                "us_aqi_pm2_5": [20.0, 30.0],
                "us_aqi_pm10": [10.0, 11.0],
                "us_aqi_nitrogen_dioxide": [4.0, 5.0],
                "us_aqi_ozone": [50.0, 60.0],
            }
            responses.append({"timezone": "GMT", "hourly_units": {"pm2_5": "μg/m³"}, "hourly": hourly})
        return responses

    def test_builds_one_batch_query_for_documented_cams_grid(self):
        url = build_cams_url(
            lat=51.0,
            lon=-115.0,
            start=datetime(2026, 8, 24, 6, tzinfo=UTC),
            end=datetime(2026, 8, 24, 7, tzinfo=UTC),
        )
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["domains"], ["cams_global"])
        self.assertEqual(query["timezone"], ["GMT"])
        self.assertEqual(len(query["latitude"][0].split(",")), 9)
        self.assertEqual(len(query["longitude"][0].split(",")), 9)
        self.assertIn("us_aqi_pm2_5", query["hourly"][0])
        self.assertEqual(query["forecast_days"], ["5"])
        self.assertNotIn("start_date", query)
        self.assertNotIn("end_date", query)
        other_window = build_cams_url(
            lat=51.0,
            lon=-115.0,
            start=datetime(2026, 8, 25, 1, tzinfo=UTC),
            end=datetime(2026, 8, 25, 4, tzinfo=UTC),
        )
        self.assertEqual(url, other_window)

    def test_aggregates_center_window_and_all_nine_cells_without_model_averaging(self):
        seen = []
        result = fetch_cams_window(
            lat=51.0,
            lon=-115.0,
            start=datetime(2026, 8, 24, 6, tzinfo=UTC),
            end=datetime(2026, 8, 24, 7, tzinfo=UTC),
            fetch_json=lambda url: seen.append(url) or self._payload(),
            retrieval_time=datetime(2026, 8, 24, 5, 30, tzinfo=UTC),
        )
        self.assertTrue(result["valid"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(result["window_avg_pm2_5"], 6.0)
        self.assertEqual(result["window_range"], [5.0, 7.0])
        self.assertEqual(result["neighbor_range"], [1.0, 11.0])
        self.assertEqual(result["pollutants"]["dominant_pollutant"], "ozone")
        self.assertEqual(result["pollutants"]["pm2_5"], 6.0)
        self.assertEqual(result["source"], "CAMS global via Open-Meteo")
        self.assertEqual(result["valid_range"], ["2026-08-24T06:00:00Z", "2026-08-24T07:00:00Z"])

    def test_exposes_retrieval_time_but_never_invents_cams_cycle(self):
        retrieved = datetime(2026, 8, 24, 5, 30, tzinfo=UTC)
        result = fetch_cams_window(
            lat=51.0,
            lon=-115.0,
            start=datetime(2026, 8, 24, 6, tzinfo=UTC),
            end=datetime(2026, 8, 24, 7, tzinfo=UTC),
            fetch_json=lambda _url: self._payload(),
            retrieval_time=retrieved,
        )
        self.assertIsNone(result["reference_time"])
        self.assertEqual(result["cycle_status"], "not_exposed_by_open_meteo")
        self.assertEqual(result["provider_retrieval_time"], "2026-08-24T05:30:00Z")
        self.assertTrue(any("cycle" in note.lower() for note in result["uncertainties"]))

        failed = fetch_cams_window(
            lat=51.0,
            lon=-115.0,
            start=datetime(2026, 8, 24, 6, tzinfo=UTC),
            end=datetime(2026, 8, 24, 7, tzinfo=UTC),
            fetch_json=lambda _url: (_ for _ in ()).throw(OSError("offline")),
            retrieval_time=retrieved,
        )
        self.assertIsNone(failed["reference_time"])
        self.assertEqual(failed["cycle_status"], "not_exposed_by_open_meteo")
        self.assertEqual(failed["provider_retrieval_time"], "2026-08-24T05:30:00Z")
        self.assertTrue(any("cycle" in note.lower() for note in failed["uncertainties"]))

    def test_missing_health_fields_do_not_invalidate_complete_pm25(self):
        payload = self._payload()
        for response in payload:
            response["hourly"]["ozone"] = [None, None]
            response["hourly"]["us_aqi_ozone"] = [None, None]
        result = fetch_cams_window(
            lat=51.0,
            lon=-115.0,
            start=datetime(2026, 8, 24, 6, tzinfo=UTC),
            end=datetime(2026, 8, 24, 7, tzinfo=UTC),
            fetch_json=lambda _url: payload,
        )
        self.assertTrue(result["valid"])
        self.assertIsNone(result["pollutants"]["ozone"])
        self.assertEqual(result["pollutants"]["dominant_pollutant"], "pm2_5")
        self.assertTrue(result["uncertainties"])

    def test_dst_local_window_aligns_to_two_distinct_utc_hours(self):
        local = ZoneInfo("America/Edmonton")
        start = datetime(2025, 11, 2, 1, 0, tzinfo=local, fold=0)
        end = datetime(2025, 11, 2, 1, 0, tzinfo=local, fold=1)
        captured = []
        payload = self._payload()
        for response in payload:
            response["hourly"]["time"] = ["2025-11-02T07:00", "2025-11-02T08:00"]
        result = fetch_cams_window(
            lat=51.0,
            lon=-115.0,
            start=start,
            end=end,
            fetch_json=lambda url: captured.append(url) or payload,
        )
        self.assertTrue(result["valid"])
        query = parse_qs(urlparse(captured[0]).query)
        self.assertNotIn("start_date", query)
        self.assertEqual(query["forecast_days"], ["5"])
        self.assertEqual(result["valid_range"], ["2025-11-02T07:00:00Z", "2025-11-02T08:00:00Z"])

    def test_missing_cell_hour_and_network_failure_are_honestly_invalid(self):
        payload = self._payload()
        payload[8]["hourly"]["pm2_5"][1] = None
        missing = fetch_cams_window(
            lat=51.0,
            lon=-115.0,
            start=datetime(2026, 8, 24, 6, tzinfo=UTC),
            end=datetime(2026, 8, 24, 7, tzinfo=UTC),
            fetch_json=lambda _url: payload,
        )
        self.assertFalse(missing["valid"])
        self.assertIsNone(missing["window_avg_pm2_5"])
        self.assertIn("complete 9-cell", missing["status"])

        failed = fetch_cams_window(
            lat=51.0,
            lon=-115.0,
            start=datetime(2026, 8, 24, 6, tzinfo=UTC),
            end=datetime(2026, 8, 24, 7, tzinfo=UTC),
            fetch_json=lambda _url: (_ for _ in ()).throw(OSError("CAMS offline")),
        )
        self.assertFalse(failed["valid"])
        self.assertIn("CAMS offline", failed["status"])


class BlueSkySourceTests(unittest.TestCase):
    INDEX_HTML = """<html><body>
      <dl><dt>Forecast ID</dt><dd>BSC18CA12-07</dd>
      <dt>Run date</dt><dd>2026-08-24</dd><dt>Run time</dt><dd>02:00 UTC</dd></dl>
      <a href="BSC18CA12-07/dispersion.nc">dispersion.nc</a>
      <a href="BSC18CA12-07/fire_locations.kml">fire_locations.kml</a>
    </body></html>"""

    @staticmethod
    def _netcdf(path):
        from scipy.io import netcdf_file

        with netcdf_file(path, "w") as dataset:
            dataset.createDimension("TSTEP", 2)
            dataset.createDimension("LAY", 1)
            dataset.createDimension("ROW", 5)
            dataset.createDimension("COL", 5)
            dataset.createDimension("VAR", 1)
            dataset.createDimension("DATE-TIME", 2)
            dataset.XORIG = -117.0
            dataset.YORIG = 50.0
            dataset.XCELL = 0.1
            dataset.YCELL = 0.1
            pm25 = dataset.createVariable("PM25", "f", ("TSTEP", "LAY", "ROW", "COL"))
            pm25.units = "ug/m^3"
            base = np.arange(1, 26, dtype=np.float32).reshape(5, 5)
            pm25[:] = np.stack([base, base * 2])[:, None, :, :]
            tflag = dataset.createVariable("TFLAG", "i", ("TSTEP", "VAR", "DATE-TIME"))
            tflag[:] = np.array([[[2026236, 70000]], [[2026236, 80000]]], dtype=np.int32)

    def test_parses_current_index_and_builds_cycle_specific_cache_key(self):
        metadata = parse_bluesky_index(
            self.INDEX_HTML, "https://firesmoke.ca/forecasts/current/"
        )
        self.assertEqual(metadata["forecast_id"], "BSC18CA12-07")
        self.assertEqual(metadata["reference_time"], datetime(2026, 8, 24, 2, tzinfo=UTC))
        self.assertEqual(
            metadata["dispersion_url"],
            "https://firesmoke.ca/forecasts/current/BSC18CA12-07/dispersion.nc",
        )
        self.assertTrue(metadata["fire_locations_url"].endswith("fire_locations.kml"))
        no_kml_link = self.INDEX_HTML.replace(
            '<a href="BSC18CA12-07/fire_locations.kml">fire_locations.kml</a>', ""
        )
        fallback = parse_bluesky_index(no_kml_link, "https://firesmoke.ca/forecasts/current/")
        self.assertEqual(
            fallback["fire_locations_url"],
            "https://firesmoke.ca/forecasts/current/fire_locations.kml",
        )
        with tempfile.TemporaryDirectory() as directory:
            first = bluesky_cache_path(Path(directory), metadata)
            changed_id = bluesky_cache_path(Path(directory), {**metadata, "forecast_id": "OTHER"})
            changed_cycle = bluesky_cache_path(
                Path(directory),
                {**metadata, "reference_time": datetime(2026, 8, 24, 8, tzinfo=UTC)},
            )
            self.assertNotEqual(first, changed_id)
            self.assertNotEqual(first, changed_cycle)

    def test_downloads_once_atomically_and_rejects_non_netcdf_content(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.nc"
            self._netcdf(source)
            payload = source.read_bytes()
            metadata = parse_bluesky_index(self.INDEX_HTML, "https://firesmoke.ca/forecasts/current/")
            calls = []
            first = ensure_cached_bluesky(
                metadata, Path(directory) / "cache", lambda url: calls.append(url) or payload
            )
            second = ensure_cached_bluesky(
                metadata, Path(directory) / "cache", lambda url: calls.append(url) or payload
            )
            self.assertEqual(first, second)
            self.assertEqual(len(calls), 1)
            self.assertTrue(first.read_bytes().startswith(b"CDF\x01"))
            self.assertFalse(any(first.parent.glob("*.tmp")))

            bad = {**metadata, "forecast_id": "BAD"}
            with self.assertRaises(ValueError):
                ensure_cached_bluesky(bad, Path(directory) / "cache", lambda _url: b"<html>no</html>")
            truncated = {**metadata, "forecast_id": "TRUNCATED"}
            with self.assertRaises(ValueError):
                ensure_cached_bluesky(
                    truncated,
                    Path(directory) / "cache",
                    lambda _url: b"CDF\x01" + b"\0" * 200,
                )

    def test_extracts_tflag_interval_starts_point_and_complete_neighbors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dispersion.nc"
            self._netcdf(path)
            result = extract_bluesky_netcdf(
                path,
                lat=50.25,
                lon=-116.75,
                start=datetime(2026, 8, 24, 6, tzinfo=UTC),
                end=datetime(2026, 8, 24, 7, tzinfo=UTC),
            )
        self.assertTrue(result["valid"])
        self.assertEqual(result["window_avg_pm2_5"], 19.5)
        self.assertEqual(result["window_range"], [13.0, 26.0])
        self.assertEqual(result["neighbor_range"], [7.0, 38.0])
        self.assertEqual(result["valid_range"], ["2026-08-24T06:00:00Z", "2026-08-24T07:00:00Z"])
        self.assertEqual(result["raw_tflag_range"], ["2026-08-24T07:00:00Z", "2026-08-24T08:00:00Z"])
        self.assertEqual(result["tflag_semantics"], "interval_end; valid_time = TFLAG - PT1H")

    def test_strips_padded_netcdf_units(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dispersion.nc"
            self._netcdf(path)
            from scipy.io import netcdf_file

            with netcdf_file(path, "a") as dataset:
                dataset.variables["PM25"].units = "  ug/m^3  "
            result = extract_bluesky_netcdf(
                path,
                lat=50.25,
                lon=-116.75,
                start=datetime(2026, 8, 24, 6, tzinfo=UTC),
                end=datetime(2026, 8, 24, 7, tzinfo=UTC),
            )
        self.assertEqual(result["units"], "ug/m^3")

    def test_out_of_range_or_incomplete_spatial_window_is_invalid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dispersion.nc"
            self._netcdf(path)
            outside_time = extract_bluesky_netcdf(
                path,
                lat=50.25,
                lon=-116.75,
                start=datetime(2026, 8, 24, 5, tzinfo=UTC),
                end=datetime(2026, 8, 24, 6, tzinfo=UTC),
            )
            outside_grid = extract_bluesky_netcdf(
                path,
                lat=50.0,
                lon=-117.0,
                start=datetime(2026, 8, 24, 6, tzinfo=UTC),
                end=datetime(2026, 8, 24, 7, tzinfo=UTC),
            )
        self.assertFalse(outside_time["valid"])
        self.assertIn("full window", outside_time["status"])
        self.assertFalse(outside_grid["valid"])
        self.assertIn("3x3", outside_grid["status"])

    def test_end_to_end_source_metadata_and_network_failure_are_isolated(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "fixture.nc"
            self._netcdf(source)
            payload = source.read_bytes()
            result = fetch_bluesky_window(
                lat=50.25,
                lon=-116.75,
                start=datetime(2026, 8, 24, 6, tzinfo=UTC),
                end=datetime(2026, 8, 24, 7, tzinfo=UTC),
                cache_dir=Path(directory) / "cache",
                fetch_text=lambda _url: self.INDEX_HTML,
                fetch_bytes=lambda _url: payload,
                retrieval_time=datetime(2026, 8, 24, 5, 30, tzinfo=UTC),
            )
            self.assertTrue(result["valid"])
            self.assertEqual(result["source"], "BlueSky Canada HYSPLIT dispersion.nc")
            self.assertEqual(result["forecast_id"], "BSC18CA12-07")
            self.assertEqual(result["reference_time"], "2026-08-24T02:00:00Z")
            self.assertEqual(result["retrieval_time"], "2026-08-24T05:30:00Z")
            self.assertTrue(result["fire_locations_url"].endswith("fire_locations.kml"))

            failed = fetch_bluesky_window(
                lat=50.25,
                lon=-116.75,
                start=datetime(2026, 8, 24, 6, tzinfo=UTC),
                end=datetime(2026, 8, 24, 7, tzinfo=UTC),
                cache_dir=Path(directory) / "other-cache",
                fetch_text=lambda _url: (_ for _ in ()).throw(OSError("index offline")),
                fetch_bytes=lambda _url: payload,
            )
            self.assertFalse(failed["valid"])
            self.assertIsNone(failed["window_avg_pm2_5"])
            self.assertIn("index offline", failed["status"])


if __name__ == "__main__":
    unittest.main()
