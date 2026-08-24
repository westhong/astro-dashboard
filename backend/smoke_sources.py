"""Network-injectable smoke forecast data sources.

All public source functions fail closed: incomplete temporal or spatial coverage
returns ``valid=False`` rather than extrapolating or reusing a frame.
"""

from __future__ import annotations

import io
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from statistics import fmean
from urllib.parse import urlencode, urljoin

from backend.smoke_assessment import dominant_pollutant

UTC = timezone.utc
FIREWORK_COVERAGE_ID = "RAQDPS.SFC_PM2.5"


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Model timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _iso_duration(value: str) -> timedelta:
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not match:
        raise ValueError(f"Unsupported ISO duration: {value}")
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    step = timedelta(hours=hours, minutes=minutes, seconds=seconds)
    if step <= timedelta(0):
        raise ValueError("Time interval step must be positive")
    return step


def parse_time_dimension(value: str) -> list[datetime]:
    """Expand an ISO interval or comma-separated WMS time dimension."""
    frames: list[datetime] = []
    for item in value.split(","):
        parts = item.strip().split("/")
        if len(parts) == 1:
            frames.append(_parse_utc(parts[0]))
            continue
        if len(parts) != 3:
            raise ValueError(f"Invalid time dimension item: {item}")
        start, end = _parse_utc(parts[0]), _parse_utc(parts[1])
        step = _iso_duration(parts[2])
        current = start
        while current <= end:
            frames.append(current)
            current += step
    return sorted(set(frames))


def _utc_z(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def build_firework_wcs_url(
    *,
    base_url: str,
    bbox: tuple[float, float, float, float],
    valid_time: datetime,
    reference_time: datetime,
) -> str:
    """Build one WCS 2.0.1 Rockies-bbox coverage request."""
    min_lat, min_lon, max_lat, max_lon = bbox
    query = [
        ("service", "WCS"),
        ("version", "2.0.1"),
        ("request", "GetCoverage"),
        ("coverageId", FIREWORK_COVERAGE_ID),
        ("subset", f"lat({min_lat},{max_lat})"),
        ("subset", f"long({min_lon},{max_lon})"),
        ("format", "image/tiff"),
        ("time", _utc_z(valid_time)),
        ("reference_time", _utc_z(reference_time)),
    ]
    return f"{base_url}?{urlencode(query)}"


def parse_firework_capabilities(xml_text: str) -> dict[str, object]:
    """Read FireWork valid frames and cycle from WMS capabilities XML."""
    root = ET.fromstring(xml_text)
    dimensions: dict[str, ET.Element] = {}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "Dimension":
            name = element.attrib.get("name")
            if name in {"time", "reference_time"}:
                dimensions[name] = element
    if "time" not in dimensions or "reference_time" not in dimensions:
        raise ValueError("FireWork capabilities missing time metadata")
    time_text = (dimensions["time"].text or dimensions["time"].attrib.get("default", "")).strip()
    if not time_text:
        raise ValueError("FireWork capabilities contain no valid times")
    reference_text = dimensions["reference_time"].attrib.get("default") or (
        dimensions["reference_time"].text or ""
    ).strip()
    if not reference_text:
        raise ValueError("FireWork capabilities contain no reference time")
    return {
        "valid_times": parse_time_dimension(time_text),
        "default_time": _parse_utc(dimensions["time"].attrib["default"])
        if dimensions["time"].attrib.get("default")
        else None,
        "reference_time": _parse_utc(reference_text.split(",")[0]),
    }


def extract_firework_geotiff(payload: bytes, *, lat: float, lon: float) -> dict[str, object]:
    """Extract the nearest FireWork pixel and its complete 3×3 neighborhood."""
    try:
        import tifffile
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError("tifffile is unavailable") from exc

    with tifffile.TiffFile(io.BytesIO(payload)) as image:
        page = image.pages[0]
        data = page.asarray()
        if data.ndim != 2:
            raise ValueError("FireWork coverage is not a 2-D raster")
        try:
            scale = page.tags[33550].value
            tie = page.tags[33922].value
        except KeyError as exc:
            raise ValueError("FireWork GeoTIFF missing georeferencing tags") from exc
    scale_x, scale_y = float(scale[0]), float(scale[1])
    pixel_x, pixel_y, _, model_x, model_y, _ = (float(value) for value in tie[:6])
    col = math.floor(pixel_x + (lon - model_x) / scale_x)
    row = math.floor(pixel_y + (model_y - lat) / scale_y)
    if row < 1 or col < 1 or row >= data.shape[0] - 1 or col >= data.shape[1] - 1:
        raise ValueError("FireWork point lacks a complete 3x3 neighborhood")
    neighborhood = data[row - 1 : row + 2, col - 1 : col + 2].astype(float) * 1e9
    values = neighborhood.ravel().tolist()
    if len(values) != 9 or not all(math.isfinite(value) for value in values):
        raise ValueError("FireWork 3x3 neighborhood is incomplete")
    return {"point_pm2_5": float(neighborhood[1, 1]), "neighbors_pm2_5": values}


def _hourly_window(start: datetime, end: datetime) -> list[datetime]:
    start_utc, end_utc = _aware_utc(start), _aware_utc(end)
    if start_utc > end_utc:
        raise ValueError("Window start is after end")
    if any((value.minute, value.second, value.microsecond) != (0, 0, 0) for value in (start_utc, end_utc)):
        raise ValueError("Model windows must align exactly to UTC hours")
    count = int((end_utc - start_utc) / timedelta(hours=1))
    return [start_utc + timedelta(hours=offset) for offset in range(count + 1)]


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _invalid_result(source: str, status: str, **metadata: object) -> dict[str, object]:
    return {
        "source": source,
        "retrieval_time": metadata.pop("retrieval_time", None),
        "reference_time": metadata.pop("reference_time", None),
        "valid_range": metadata.pop("valid_range", [None, None]),
        "valid": False,
        "status": status,
        "window_avg_pm2_5": None,
        "window_range": [None, None],
        "neighbor_range": [None, None],
        **metadata,
    }


def fetch_firework_window(
    *,
    lat: float,
    lon: float,
    start: datetime,
    end: datetime,
    fetch_text,
    fetch_bytes,
    retrieval_time: datetime | None = None,
    capabilities_url: str = "https://geo.weather.gc.ca/geomet?service=WMS&version=1.3.0&request=GetCapabilities&layer=RAQDPS.SFC_PM2.5",
    wcs_base_url: str = "https://geo.weather.gc.ca/geomet",
    bbox: tuple[float, float, float, float] = (48.0, -120.0, 55.0, -110.0),
) -> dict[str, object]:
    """Fetch exact aligned FireWork frames; any gap invalidates the model."""
    source = "ECCC FireWork RAQDPS WCS"
    retrieved = _utc_z(retrieval_time or datetime.now(UTC))
    try:
        requested = _hourly_window(start, end)
        capabilities = parse_firework_capabilities(fetch_text(capabilities_url))
        available = capabilities["valid_times"]
        reference = capabilities["reference_time"]
        valid_range = [_utc_z(available[0]), _utc_z(available[-1])]
        metadata = {
            "retrieval_time": retrieved,
            "reference_time": _utc_z(reference),
            "valid_range": valid_range,
        }
        if any(frame not in available for frame in requested):
            return _invalid_result(source, "FireWork does not cover the full window", **metadata)
        points, neighbors = [], []
        for frame in requested:
            url = build_firework_wcs_url(
                base_url=wcs_base_url,
                bbox=bbox,
                valid_time=frame,
                reference_time=reference,
            )
            sample = extract_firework_geotiff(fetch_bytes(url), lat=lat, lon=lon)
            points.append(sample["point_pm2_5"])
            neighbors.extend(sample["neighbors_pm2_5"])
        return {
            "source": source,
            **metadata,
            "valid": True,
            "status": "ok",
            "units": "µg/m³",
            "window_avg_pm2_5": fmean(points),
            "window_range": [min(points), max(points)],
            "neighbor_range": [min(neighbors), max(neighbors)],
        }
    except Exception as exc:
        return _invalid_result(source, f"FireWork unavailable: {exc}", retrieval_time=retrieved)


CAMS_GRID_DEGREES = 0.4
CAMS_CYCLE_STATUS = "not_exposed_by_open_meteo"
CAMS_CYCLE_UNCERTAINTY = (
    "Open-Meteo does not expose the CAMS model cycle/reference time."
)
CAMS_HOURLY_FIELDS = (
    "pm2_5",
    "pm10",
    "ozone",
    "nitrogen_dioxide",
    "us_aqi",
    "us_aqi_pm2_5",
    "us_aqi_pm10",
    "us_aqi_nitrogen_dioxide",
    "us_aqi_ozone",
)


def _cams_grid(lat: float, lon: float) -> list[tuple[float, float]]:
    """Return center plus equivalent 3×3 CAMS cells at documented 0.4° spacing."""
    return [
        (lat + dlat, lon + dlon)
        for dlat in (-CAMS_GRID_DEGREES, 0.0, CAMS_GRID_DEGREES)
        for dlon in (-CAMS_GRID_DEGREES, 0.0, CAMS_GRID_DEGREES)
    ]


def build_cams_url(
    *, lat: float, lon: float, start: datetime, end: datetime,
    base_url: str = "https://air-quality-api.open-meteo.com/v1/air-quality",
) -> str:
    """Build a single Open-Meteo multi-coordinate CAMS-global request."""
    _hourly_window(start, end)  # Validate aware, hour-aligned caller input.
    coordinates = _cams_grid(lat, lon)
    query = {
        "latitude": ",".join(str(value[0]) for value in coordinates),
        "longitude": ",".join(str(value[1]) for value in coordinates),
        "hourly": ",".join(CAMS_HOURLY_FIELDS),
        "domains": "cams_global",
        "timezone": "GMT",
        "forecast_days": "5",
    }
    return f"{base_url}?{urlencode(query)}"


def _cams_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def fetch_cams_window(
    *,
    lat: float,
    lon: float,
    start: datetime,
    end: datetime,
    fetch_json,
    retrieval_time: datetime | None = None,
) -> dict[str, object]:
    """Fetch and aggregate one aligned CAMS center/window and 3×3 grid."""
    source = "CAMS global via Open-Meteo"
    retrieved = _utc_z(retrieval_time or datetime.now(UTC))
    metadata = {
        "retrieval_time": retrieved,
        "provider_retrieval_time": retrieved,
        "reference_time": None,
        "cycle_status": CAMS_CYCLE_STATUS,
        "uncertainties": [CAMS_CYCLE_UNCERTAINTY],
    }
    try:
        requested = _hourly_window(start, end)
        url = build_cams_url(lat=lat, lon=lon, start=start, end=end)
        payload = fetch_json(url)
        responses = payload if isinstance(payload, list) else [payload]
        if len(responses) != 9:
            raise ValueError("CAMS response lacks the complete 9-cell grid")

        per_cell: list[dict[str, list[float | None]]] = []
        complete_pm_times: set[datetime] | None = None
        for response in responses:
            hourly = response.get("hourly", {})
            times = [_cams_time(value) for value in hourly.get("time", [])]
            positions = {value: index for index, value in enumerate(times)}
            if any(frame not in positions for frame in requested):
                raise ValueError("CAMS response lacks the full hourly window")
            pm_raw = hourly.get("pm2_5", [])
            cell_pm_times = {
                frame for frame, index in positions.items()
                if index < len(pm_raw)
                and pm_raw[index] is not None
                and math.isfinite(float(pm_raw[index]))
            }
            complete_pm_times = cell_pm_times if complete_pm_times is None else complete_pm_times & cell_pm_times
            selected: dict[str, list[float | None]] = {}
            for field in CAMS_HOURLY_FIELDS:
                raw_values = hourly.get(field, [])
                values = [raw_values[positions[frame]] if positions[frame] < len(raw_values) else None for frame in requested]
                normalized = [
                    float(value) if value is not None and math.isfinite(float(value)) else None
                    for value in values
                ]
                if field == "pm2_5" and any(value is None for value in normalized):
                    raise ValueError("CAMS response lacks a complete 9-cell hourly PM2.5 value")
                selected[field] = normalized
            per_cell.append(selected)

        center = per_cell[4]
        center_pm = center["pm2_5"]
        all_pm = [value for cell in per_cell for value in cell["pm2_5"]]
        if any(value is None for value in center_pm + all_pm):
            raise ValueError("CAMS response lacks a complete 9-cell hourly PM2.5 value")

        uncertainties = list(metadata["uncertainties"])

        def available_mean(values: list[float | None], field: str) -> float | None:
            available = [value for value in values if value is not None]
            if len(available) != len(values):
                uncertainties.append(f"CAMS center {field} is incomplete for the requested window")
            return fmean(available) if available else None

        pollutant_values = {
            "pm2_5": fmean(center_pm),
            "pm10": available_mean(center["pm10"], "pm10"),
            "ozone": available_mean(center["ozone"], "ozone"),
            "nitrogen_dioxide": available_mean(center["nitrogen_dioxide"], "nitrogen_dioxide"),
            "us_aqi_health_context": available_mean(center["us_aqi"], "us_aqi"),
        }
        subindices = {
            "pm2_5": available_mean(center["us_aqi_pm2_5"], "us_aqi_pm2_5"),
            "pm10": available_mean(center["us_aqi_pm10"], "us_aqi_pm10"),
            "nitrogen_dioxide": available_mean(center["us_aqi_nitrogen_dioxide"], "us_aqi_nitrogen_dioxide"),
            "ozone": available_mean(center["us_aqi_ozone"], "us_aqi_ozone"),
        }
        pollutant_values["dominant_pollutant"] = dominant_pollutant(subindices)
        coverage_times = sorted(complete_pm_times or [])
        if not coverage_times:
            raise ValueError("CAMS has no common PM2.5 valid range")
        return {
            "source": source,
            **metadata,
            "valid_range": [_utc_z(coverage_times[0]), _utc_z(coverage_times[-1])],
            "valid": True,
            "status": "ok" if len(uncertainties) == 1 else "ok; health context incomplete",
            "uncertainties": uncertainties,
            "units": "µg/m³",
            "window_avg_pm2_5": fmean(center_pm),
            "window_range": [min(center_pm), max(center_pm)],
            "neighbor_range": [min(all_pm), max(all_pm)],
            "pollutants": pollutant_values,
            "health_subindices": subindices,
        }
    except Exception as exc:
        return _invalid_result(source, f"CAMS unavailable: {exc}", **metadata)


class _IndexParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []
        self.links: list[str] = []

    def handle_data(self, data: str) -> None:
        self.text.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)


def parse_bluesky_index(html: str, index_url: str) -> dict[str, object]:
    """Parse the published Forecast ID/cycle and relative model asset links."""
    parser = _IndexParser()
    parser.feed(html)
    text = " ".join(" ".join(parser.text).split())

    def require(pattern: str, label: str) -> str:
        match = re.search(pattern, text, re.IGNORECASE)
        if not match:
            raise ValueError(f"BlueSky index missing {label}")
        return match.group(1)

    forecast_id = require(r"Forecast\s*ID\s*[:\-]?\s*([A-Za-z0-9_-]+)", "Forecast ID")
    run_date = require(r"Run\s*date\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})", "run date")
    run_time = require(r"Run\s*time\s*[:\-]?\s*(\d{1,2}:\d{2})(?:\s*UTC)?", "run time")
    reference_time = datetime.fromisoformat(f"{run_date}T{run_time}:00").replace(tzinfo=UTC)

    def asset(name: str, required: bool = True) -> str | None:
        href = next((link for link in parser.links if link.split("?", 1)[0].endswith(name)), None)
        if href is None and required:
            raise ValueError(f"BlueSky index missing {name}")
        return urljoin(index_url, href) if href else None

    return {
        "forecast_id": forecast_id,
        "reference_time": reference_time,
        "dispersion_url": asset("dispersion.nc"),
        "fire_locations_url": asset("fire_locations.kml", required=False)
        or urljoin(index_url, "fire_locations.kml"),
        "index_url": index_url,
    }


def bluesky_cache_path(cache_dir: Path, metadata: dict[str, object]) -> Path:
    """Key the large file by both published forecast ID and model cycle."""
    forecast_id = re.sub(r"[^A-Za-z0-9_.-]", "_", str(metadata["forecast_id"]))
    reference = metadata["reference_time"]
    if not isinstance(reference, datetime):
        raise ValueError("BlueSky cache metadata lacks a datetime reference_time")
    cycle = _aware_utc(reference).strftime("%Y%m%dT%H%M%SZ")
    return Path(cache_dir) / f"{forecast_id}_{cycle}_dispersion.nc"


def _valid_netcdf_header(payload: bytes) -> bool:
    return len(payload) >= 128 and payload[:4] in {b"CDF\x01", b"CDF\x02", b"CDF\x05"}


def _validate_bluesky_netcdf(path: Path) -> None:
    try:
        from scipy.io import netcdf_file
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError("scipy is unavailable") from exc
    try:
        with netcdf_file(str(path), "r", mmap=False) as dataset:
            if "PM25" not in dataset.variables or "TFLAG" not in dataset.variables:
                raise ValueError("NetCDF lacks PM25 or TFLAG")
    except Exception as exc:
        raise ValueError(f"BlueSky NetCDF structure is invalid: {exc}") from exc


def _cached_bluesky_is_valid(path: Path) -> bool:
    try:
        with path.open("rb") as cached:
            if not _valid_netcdf_header(cached.read(128)):
                return False
        _validate_bluesky_netcdf(path)
        return True
    except (OSError, ValueError):
        return False


def _acquire_cache_lock(
    lock_path: Path, *, timeout: float = 300, stale_after: float = 900
) -> None:
    """Acquire an atomic directory lock, recovering abandoned stale locks."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            lock_path.mkdir()
            return
        except FileExistsError:
            try:
                stale = time.time() - lock_path.stat().st_mtime > stale_after
            except OSError:
                continue
            if stale:
                try:
                    lock_path.rmdir()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(f"Timed out waiting for BlueSky cache lock {lock_path}")
            time.sleep(0.05)


def ensure_cached_bluesky(metadata: dict[str, object], cache_dir: Path, fetch_bytes) -> Path:
    """Download dispersion.nc once per cycle with a stale-safe process lock."""
    path = bluesky_cache_path(Path(cache_dir), metadata)
    path.parent.mkdir(parents=True, exist_ok=True)
    if _cached_bluesky_is_valid(path):
        return path
    lock_path = path.with_name(f"{path.name}.lock")
    _acquire_cache_lock(lock_path)
    try:
        if _cached_bluesky_is_valid(path):
            return path
        try:
            path.unlink()
        except OSError:
            pass
        payload = fetch_bytes(metadata["dispersion_url"])
        if not isinstance(payload, bytes) or not _valid_netcdf_header(payload):
            raise ValueError("BlueSky download is not a valid NetCDF classic file")
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes(payload)
            _validate_bluesky_netcdf(temporary)
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return path
    finally:
        try:
            lock_path.rmdir()
        except OSError:
            pass


def _tflag_datetime(date_code: int, time_code: int) -> datetime:
    date = datetime.strptime(str(int(date_code)), "%Y%j")
    clock = f"{int(time_code):06d}"
    return date.replace(
        hour=int(clock[:2]), minute=int(clock[2:4]), second=int(clock[4:]), tzinfo=UTC
    )


_BLUESKY_DECODED_CACHE: OrderedDict[tuple[str, int, int], dict[str, object]] = OrderedDict()
_BLUESKY_DECODED_CACHE_MAX = 2


def clear_bluesky_decoded_cache() -> None:
    _BLUESKY_DECODED_CACHE.clear()


def load_bluesky_decoded(path: Path) -> dict[str, object]:
    """Decode a cycle once per process, invalidating replaced files by stat key."""
    resolved = Path(path).resolve()
    stat = resolved.stat()
    key = (str(resolved), stat.st_size, stat.st_mtime_ns)
    cached = _BLUESKY_DECODED_CACHE.get(key)
    if cached is not None:
        _BLUESKY_DECODED_CACHE.move_to_end(key)
        return cached
    try:
        from scipy.io import netcdf_file
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("scipy is unavailable") from exc
    with netcdf_file(str(resolved), "r", mmap=False) as dataset:
        for attribute in ("XORIG", "YORIG", "XCELL", "YCELL"):
            if not hasattr(dataset, attribute):
                raise ValueError(f"BlueSky NetCDF missing {attribute}")
        if "PM25" not in dataset.variables or "TFLAG" not in dataset.variables:
            raise ValueError("BlueSky NetCDF missing PM25 or TFLAG")
        decoded = {
            "pm25": dataset.variables["PM25"].data.copy(),
            "tflag": dataset.variables["TFLAG"].data.copy(),
            "units": getattr(dataset.variables["PM25"], "units", b"ug/m^3"),
            "xorig": float(dataset.XORIG), "yorig": float(dataset.YORIG),
            "xcell": float(dataset.XCELL), "ycell": float(dataset.YCELL),
        }
    for old_key in [item for item in _BLUESKY_DECODED_CACHE if item[0] == key[0]]:
        del _BLUESKY_DECODED_CACHE[old_key]
    _BLUESKY_DECODED_CACHE[key] = decoded
    while len(_BLUESKY_DECODED_CACHE) > _BLUESKY_DECODED_CACHE_MAX:
        _BLUESKY_DECODED_CACHE.popitem(last=False)
    return decoded


def extract_bluesky_netcdf(
    path: Path, *, lat: float, lon: float, start: datetime, end: datetime,
) -> dict[str, object]:
    """Extract aligned BlueSky interval-start frames and a complete 3×3 grid."""
    source = "BlueSky Canada HYSPLIT dispersion.nc"
    try:
        requested = _hourly_window(start, end)
        decoded = load_bluesky_decoded(path)
        pm25, tflag, units = decoded["pm25"], decoded["tflag"], decoded["units"]
        xorig, yorig = decoded["xorig"], decoded["yorig"]
        xcell, ycell = decoded["xcell"], decoded["ycell"]
        if pm25.ndim != 4 or pm25.shape[1] < 1:
            raise ValueError("BlueSky PM25 has an unexpected shape")
        raw_ends = [_tflag_datetime(row[0][0], row[0][1]) for row in tflag]
        valid_starts = [value - timedelta(hours=1) for value in raw_ends]
        positions = {value: index for index, value in enumerate(valid_starts)}
        valid_range = [_utc_z(valid_starts[0]), _utc_z(valid_starts[-1])]
        raw_range = [_utc_z(raw_ends[0]), _utc_z(raw_ends[-1])]
        metadata = {
            "valid_range": valid_range,
            "raw_tflag_range": raw_range,
            "tflag_semantics": "interval_end; valid_time = TFLAG - PT1H",
        }
        if any(frame not in positions for frame in requested):
            return _invalid_result(source, "BlueSky does not cover the full window", **metadata)
        col = math.floor((lon - xorig) / xcell + 1e-9)
        row = math.floor((lat - yorig) / ycell + 1e-9)
        if row < 1 or col < 1 or row >= pm25.shape[2] - 1 or col >= pm25.shape[3] - 1:
            return _invalid_result(source, "BlueSky point lacks a complete 3x3 neighborhood", **metadata)
        points: list[float] = []
        neighbors: list[float] = []
        for frame in requested:
            raster = pm25[positions[frame], 0]
            neighborhood = raster[row - 1 : row + 2, col - 1 : col + 2].astype(float)
            values = neighborhood.ravel().tolist()
            if len(values) != 9 or not all(math.isfinite(value) for value in values):
                return _invalid_result(source, "BlueSky 3x3 window contains missing values", **metadata)
            points.append(float(neighborhood[1, 1]))
            neighbors.extend(values)
        if isinstance(units, bytes):
            units = units.decode("ascii", errors="replace")
        return {
            "source": source,
            **metadata,
            "valid": True,
            "status": "ok",
            "units": str(units).strip(),
            "window_avg_pm2_5": fmean(points),
            "window_range": [min(points), max(points)],
            "neighbor_range": [min(neighbors), max(neighbors)],
        }
    except Exception as exc:
        return _invalid_result(source, f"BlueSky unavailable: {exc}")


def fetch_bluesky_window(
    *,
    lat: float,
    lon: float,
    start: datetime,
    end: datetime,
    cache_dir: Path,
    fetch_text,
    fetch_bytes,
    retrieval_time: datetime | None = None,
    index_url: str = "https://firesmoke.ca/forecasts/current/",
) -> dict[str, object]:
    """Resolve current BlueSky metadata, cache its NetCDF, and extract a window."""
    source = "BlueSky Canada HYSPLIT dispersion.nc"
    retrieved = _utc_z(retrieval_time or datetime.now(UTC))
    try:
        metadata = parse_bluesky_index(fetch_text(index_url), index_url)
        path = ensure_cached_bluesky(metadata, Path(cache_dir), fetch_bytes)
        result = extract_bluesky_netcdf(path, lat=lat, lon=lon, start=start, end=end)
        result.update(
            {
                "source": source,
                "retrieval_time": retrieved,
                "reference_time": _utc_z(metadata["reference_time"]),
                "forecast_id": metadata["forecast_id"],
                "dispersion_url": metadata["dispersion_url"],
                "fire_locations_url": metadata["fire_locations_url"],
            }
        )
        return result
    except Exception as exc:
        return _invalid_result(source, f"BlueSky unavailable: {exc}", retrieval_time=retrieved)
