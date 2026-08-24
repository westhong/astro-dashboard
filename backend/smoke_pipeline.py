"""Three-model smoke integration pipeline for photography reports."""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from backend.smoke_assessment import build_smoke_assessment
from backend.smoke_sources import (
    fetch_bluesky_window,
    fetch_cams_window,
    fetch_firework_window,
)

UTC = timezone.utc
HTTP_CACHE_DIR = Path.home() / ".cache" / "astro-smoke-http"
BLUESKY_CACHE_DIR = Path.home() / ".cache" / "astro-smoke-bluesky"
HTTP_CACHE_TTL = 55 * 60


def aligned_utc_window(start_local: datetime, end_local: datetime) -> tuple[datetime, datetime]:
    """Floor an aware local shooting window to overlapping UTC hourly frames."""
    if start_local.tzinfo is None or end_local.tzinfo is None:
        raise ValueError("Smoke shooting windows must be timezone-aware")
    start = start_local.astimezone(UTC)
    end = end_local.astimezone(UTC)
    if start > end:
        raise ValueError("Smoke shooting window start is after end")
    floor = lambda value: value.replace(minute=0, second=0, microsecond=0)
    return floor(start), floor(end)


class CachedHttpFetcher:
    """Retrying 55-minute atomic cache keyed by full URL and local date."""
    def __init__(
        self,
        cache_dir: Path = HTTP_CACHE_DIR,
        *,
        opener=urlopen,
        sleep=time.sleep,
        now=time.time,
        today=None,
        attempts: int = 4,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.opener = opener
        self.sleep = sleep
        self.now = now
        self.today = today or (lambda: datetime.now().astimezone().date().isoformat())
        self.attempts = attempts

    def _path(self, url: str) -> Path:
        key = hashlib.sha256(f"{url}|{self.today()}".encode()).hexdigest()
        return self.cache_dir / f"{key}.cache"

    def bytes(self, url: str) -> bytes:
        path = self._path(url)
        try:
            if path.exists() and self.now() - path.stat().st_mtime < HTTP_CACHE_TTL:
                return path.read_bytes()
        except OSError:
            pass
        last: Exception | None = None
        for attempt in range(self.attempts):
            try:
                request = Request(url, headers={"User-Agent": "astro-dashboard/1 smoke-assessment"})
                with self.opener(request, timeout=45) as response:
                    status = int(getattr(response, "status", 200))
                    content_type = str(response.headers.get("Content-Type", "")).lower()
                    payload = response.read()
                if status == 429 or status >= 500:
                    raise HTTPError(url, status, "transient upstream response", None, None)
                if status >= 400:
                    raise ValueError(f"HTTP {status} from {url}")
                if "text/html" in content_type or payload.lstrip().lower().startswith((b"<html", b"<!doctype html")):
                    raise ValueError(f"HTML response rejected from {url}")
                self.cache_dir.mkdir(parents=True, exist_ok=True)
                temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
                try:
                    temporary.write_bytes(payload)
                    os.replace(temporary, path)
                finally:
                    if temporary.exists():
                        temporary.unlink()
                return payload
            except (HTTPError, URLError, OSError) as exc:
                last = exc
                if isinstance(exc, HTTPError):
                    transient = exc.code == 429 or exc.code >= 500
                else:
                    transient = isinstance(exc, (URLError, OSError))
                if not transient or attempt == self.attempts - 1:
                    raise
                self.sleep(min(2 ** attempt, 4))
        raise last or RuntimeError("HTTP fetch failed")

    def text(self, url: str) -> str:
        return self.bytes(url).decode("utf-8")

    def json(self, url: str):
        payload = self.bytes(url)
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # A malformed success must not survive in cache.
            try:
                self._path(url).unlink()
            except OSError:
                pass
            raise ValueError(f"Invalid JSON from {url}") from exc


def parse_fire_locations_kml(text: str) -> list[tuple[float, float]]:
    root = ET.fromstring(text)
    points: list[tuple[float, float]] = []
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "coordinates" or not element.text:
            continue
        for coordinate in element.text.split():
            parts = coordinate.split(",")
            if len(parts) >= 2:
                lon, lat = float(parts[0]), float(parts[1])
                if math.isfinite(lat) and math.isfinite(lon):
                    points.append((lat, lon))
    return points


def nearest_hotspot_km(lat: float, lon: float, hotspots: list[tuple[float, float]]) -> float | None:
    if not hotspots:
        return None
    radius = 6371.0088
    lat1 = math.radians(lat)
    def distance(point: tuple[float, float]) -> float:
        lat2 = math.radians(point[0])
        dlat = lat2 - lat1
        dlon = math.radians(point[1] - lon)
        a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
        return radius * 2 * math.asin(min(1.0, math.sqrt(a)))
    return min(distance(point) for point in hotspots)


def _failed_model(name: str, exc: Exception) -> dict[str, object]:
    return {
        "source": name,
        "valid": False,
        "status": f"{name} unavailable: {exc}",
        "window_avg_pm2_5": None,
        "window_range": [None, None],
        "neighbor_range": [None, None],
    }


def assess_smoke_window(
    *,
    lat: float,
    lon: float,
    start_local: datetime,
    end_local: datetime,
    observed_now: dict[str, object] | None = None,
    firework_fetch=None,
    cams_fetch=None,
    bluesky_fetch=None,
    fetch_text=None,
    http_fetcher: CachedHttpFetcher | None = None,
    bluesky_cache_dir: Path = BLUESKY_CACHE_DIR,
) -> dict[str, object]:
    """Fetch each model independently and build the formal assessment contract."""
    start_utc, end_utc = aligned_utc_window(start_local, end_local)
    http = http_fetcher or CachedHttpFetcher()
    text_fetch = fetch_text or http.text
    firework_fetch = firework_fetch or (
        lambda **kw: fetch_firework_window(**kw, fetch_text=http.text, fetch_bytes=http.bytes)
    )
    cams_fetch = cams_fetch or (
        lambda **kw: fetch_cams_window(**kw, fetch_json=http.json)
    )
    bluesky_fetch = bluesky_fetch or (
        lambda **kw: fetch_bluesky_window(
            **kw, cache_dir=bluesky_cache_dir, fetch_text=http.text, fetch_bytes=http.bytes
        )
    )
    common = {"lat": lat, "lon": lon, "start": start_utc, "end": end_utc}
    models: dict[str, dict[str, object]] = {}
    for key, label, function in (
        ("eccc_firework", "ECCC FireWork", firework_fetch),
        ("cams_global", "CAMS global", cams_fetch),
        ("bluesky_canada", "BlueSky Canada", bluesky_fetch),
    ):
        try:
            models[key] = function(**common)
        except Exception as exc:
            models[key] = _failed_model(label, exc)

    cams = models["cams_global"]
    pollutants = dict(cams.get("pollutants") or {})
    health_subindices = dict(cams.get("health_subindices") or {})
    uncertainties: list[str] = []
    for result in models.values():
        if not result.get("valid"):
            uncertainties.append(str(result.get("status") or "Smoke model unavailable"))

    pm25 = pollutants.get("pm2_5")
    pm10 = pollutants.get("pm10")
    if isinstance(pm25, (int, float)) and isinstance(pm10, (int, float)) and pm10 >= pm25 + 10 and pm10 >= 1.5 * pm25:
        uncertainties.append(
            "Possible dust/haze uncertainty: CAMS PM10 is at least PM2.5 + 10 and 1.5× PM2.5; this does not change the photography smoke score."
        )

    support = {
        "classification": "NO_IDENTIFIED_SOURCE",
        "nearest_confirmed_fire_km": None,
        "nearest_satellite_hotspot_km": None,
        "transport_supported": None,
        "notes": [],
    }
    kml_url = models["bluesky_canada"].get("fire_locations_url")
    if kml_url:
        try:
            hotspots = parse_fire_locations_kml(text_fetch(str(kml_url)))
            distance = nearest_hotspot_km(lat, lon, hotspots)
            support["nearest_satellite_hotspot_km"] = round(distance, 1) if distance is not None else None
            if hotspots:
                support["classification"] = "SATELLITE_HOTSPOT_ONLY"
                support["notes"].append("BlueSky cycle includes satellite hotspots; these do not confirm a wildfire incident or transport.")
            else:
                support["notes"].append("BlueSky fire_locations.kml contained no parseable hotspots.")
        except Exception as exc:
            uncertainties.append(f"BlueSky fire_locations KML unavailable: {exc}")
            support["notes"].append("Hotspot support unavailable; model forecast remains intact.")

    return build_smoke_assessment(
        shooting_point={"lat": lat, "lon": lon},
        window_local={
            "start": start_local.isoformat(),
            "end": end_local.isoformat(),
            "timezone": getattr(start_local.tzinfo, "key", str(start_local.tzinfo)),
        },
        models=models,
        observed_now=observed_now,
        pollutants=pollutants,
        health_subindices=health_subindices,
        source_support=support,
        uncertainties=uncertainties,
    )
