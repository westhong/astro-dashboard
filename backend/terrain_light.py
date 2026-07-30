#!/usr/bin/env python3
"""Terrain-aware direct-sunlight windows for the daylight dashboard.

DEM source: Mapzen/Terrain Tiles on AWS (SRTM HGT, one arc-second).  The
calculation traces the Sun azimuth against the visible terrain horizon from the
stored camera coordinate.  It is a terrain model, not a substitute for the
exact tripod position, trees, buildings, atmospheric refraction, or a field
observation.
"""
from __future__ import annotations

import gzip
import math
import mmap
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from skyfield.api import load, wgs84

TZ = ZoneInfo("America/Edmonton")
EARTH_RADIUS_M = 6_371_000.0
# Standard atmospheric refraction increases the effective Earth radius. This
# keeps the terrain profile conservative enough for direct-Sun modelling.
EFFECTIVE_RADIUS_M = EARTH_RADIUS_M * 7.0 / 6.0
HGT_SAMPLES = 3601
HGT_BYTES = HGT_SAMPLES * HGT_SAMPLES * 2
CACHE_DIR = Path(os.environ.get("ASTRO_DEM_CACHE", "/tmp/astro-dashboard-dem"))


def _tile_name(lat: float, lon: float) -> tuple[str, int, int]:
    south, west = math.floor(lat), math.floor(lon)
    ns = f"N{south:02d}" if south >= 0 else f"S{abs(south):02d}"
    ew = f"E{west:03d}" if west >= 0 else f"W{abs(west):03d}"
    return f"{ns}{ew}", south, west


class HgtTiles:
    def __init__(self) -> None:
        self._maps: dict[str, tuple[mmap.mmap, object, int, int]] = {}

    def _load(self, lat: float, lon: float) -> tuple[mmap.mmap, int, int]:
        name, south, west = _tile_name(lat, lon)
        if name in self._maps:
            mapped, _handle, tile_south, tile_west = self._maps[name]
            return mapped, tile_south, tile_west
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        path = CACHE_DIR / f"{name}.hgt"
        if not path.exists() or path.stat().st_size != HGT_BYTES:
            url = f"https://s3.amazonaws.com/elevation-tiles-prod/skadi/{name[:3]}/{name}.hgt.gz"
            request = urllib.request.Request(url, headers={"User-Agent": "astro-dashboard/2.5 terrain-light"})
            with urllib.request.urlopen(request, timeout=120) as response:
                raw = gzip.decompress(response.read())
            if len(raw) != HGT_BYTES:
                raise RuntimeError(f"DEM tile {name} size invalid: {len(raw)}")
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(raw)
            tmp.replace(path)
        handle = path.open("rb")
        mapped = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        self._maps[name] = (mapped, handle, south, west)
        return mapped, south, west

    def elevation(self, lat: float, lon: float) -> float:
        mapped, south, west = self._load(lat, lon)
        col = max(0, min(3600, round((lon - west) * 3600)))
        row = max(0, min(3600, round((south + 1 - lat) * 3600)))
        value = int.from_bytes(mapped[(row * HGT_SAMPLES + col) * 2:(row * HGT_SAMPLES + col) * 2 + 2], "big", signed=True)
        if value <= -10_000:
            raise RuntimeError("DEM contains no elevation at this terrain sample")
        return float(value)


@dataclass
class TerrainHorizon:
    lat: float
    lon: float
    tiles: HgtTiles
    observer_elevation_m: float
    _azimuth_cache: dict[float, float]

    @classmethod
    def at(cls, lat: float, lon: float, tiles: HgtTiles) -> "TerrainHorizon":
        return cls(lat, lon, tiles, tiles.elevation(lat, lon), {})

    def altitude(self, azimuth_deg: float) -> float:
        """Maximum visible terrain angle at a bearing, in degrees."""
        key = round(azimuth_deg * 4) / 4  # 0.25° cache; finer than 1-min Sun motion.
        if key in self._azimuth_cache:
            return self._azimuth_cache[key]
        bearing = math.radians(key)
        phi1, lam1 = math.radians(self.lat), math.radians(self.lon)
        max_angle = -90.0
        # Dense nearby sampling catches ridges that dominate the apparent horizon;
        # logarithmic spacing retains distant mountain ranges to 50 km.
        for i in range(180):
            distance = 80.0 * (50_000.0 / 80.0) ** (i / 179)
            delta = distance / EARTH_RADIUS_M
            phi2 = math.asin(math.sin(phi1) * math.cos(delta) + math.cos(phi1) * math.sin(delta) * math.cos(bearing))
            lam2 = lam1 + math.atan2(math.sin(bearing) * math.sin(delta) * math.cos(phi1), math.cos(delta) - math.sin(phi1) * math.sin(phi2))
            elev = self.tiles.elevation(math.degrees(phi2), math.degrees(lam2))
            curvature = distance * distance / (2 * EFFECTIVE_RADIUS_M)
            angle = math.degrees(math.atan2(elev - self.observer_elevation_m - curvature, distance))
            max_angle = max(max_angle, angle)
        self._azimuth_cache[key] = max_angle
        return max_angle


class DirectLightCalculator:
    def __init__(self) -> None:
        self.tiles = HgtTiles()
        self.ts = load.timescale()
        self.eph = load("de421.bsp")
        self.earth = self.eph["earth"]
        self.sun = self.eph["sun"]
        self._horizons: dict[tuple[float, float], TerrainHorizon] = {}

    def _horizon(self, lat: float, lon: float) -> TerrainHorizon:
        key = (round(lat, 6), round(lon, 6))
        if key not in self._horizons:
            self._horizons[key] = TerrainHorizon.at(lat, lon, self.tiles)
        return self._horizons[key]

    def _sun(self, lat: float, lon: float, elevation_m: float, when: datetime) -> tuple[float, float]:
        loc = wgs84.latlon(lat, lon, elevation_m=elevation_m)
        observation = (self.earth + loc).at(self.ts.from_datetime(when)).observe(self.sun).apparent()
        alt, az, _ = observation.altaz()
        return alt.degrees, az.degrees

    def direct_light_time(self, lat: float, lon: float, date_str: str, event: str, geometric_time: str) -> dict:
        """Return first (sunrise) / last (sunset) direct terrain-cleared sunlight."""
        if event not in {"sunrise", "sunset"}:
            raise ValueError(f"unsupported event {event}")
        horizon = self._horizon(lat, lon)
        anchor = datetime.fromisoformat(geometric_time).replace(tzinfo=TZ)
        if event == "sunrise":
            start, end, direction = anchor - timedelta(minutes=20), anchor + timedelta(minutes=300), 1
        else:
            start, end, direction = anchor - timedelta(minutes=300), anchor + timedelta(minutes=20), 1
        visible: list[tuple[datetime, float, float, float]] = []
        current = start
        while current <= end:
            sun_alt, sun_az = self._sun(lat, lon, horizon.observer_elevation_m, current)
            terrain_alt = horizon.altitude(sun_az)
            if sun_alt >= terrain_alt:
                visible.append((current, sun_alt, sun_az, terrain_alt))
            current += timedelta(minutes=1)
        if not visible:
            raise RuntimeError("terrain model found no direct sunlight in the search interval")
        chosen = visible[0] if event == "sunrise" else visible[-1]
        when, sun_alt, sun_az, terrain_alt = chosen
        return {
            "time": when.strftime("%H:%M"),
            "sun_altitude_deg": round(float(sun_alt), 1),
            "sun_azimuth_deg": round(float(sun_az), 1),
            "terrain_horizon_deg": round(terrain_alt, 1),
            "observer_elevation_m": round(horizon.observer_elevation_m),
            "basis": "SRTM 1 arc-second DEM terrain model（須以精確腳架點與現場實測校準）",
            "source": "AWS Terrain Tiles / SRTM 1 arc-second",
        }
