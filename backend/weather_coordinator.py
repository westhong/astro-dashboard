"""Deterministic, globally deduplicated weather request planning.

The coordinator records HTTP requests, coordinate counts, fields, and forecast
horizon separately.  It deliberately makes no claim about provider quota units.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
AIR_QUALITY_URL = "https://air-quality-api.open-meteo.com/v1/air-quality"
MODELS = ("ecmwf_ifs025", "gem_seamless", "icon_seamless", "gfs_seamless")
SITE_BEST_FIELDS = (
    "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
    "precipitation_probability", "visibility", "temperature_2m",
    "relative_humidity_2m", "dew_point_2m", "wind_speed_10m",
    "wind_direction_10m", "wind_gusts_10m", "freezing_level_height",
)
SITE_MODEL_FIELDS = (
    "cloud_cover", "cloud_cover_low", "cloud_cover_mid", "cloud_cover_high",
    "temperature_2m", "relative_humidity_2m", "dew_point_2m",
    "wind_speed_10m", "wind_direction_10m", "wind_gusts_10m",
    "visibility", "freezing_level_height",
)
CAMS_FIELDS = (
    "pm2_5", "pm10", "ozone", "nitrogen_dioxide", "us_aqi",
    "us_aqi_pm2_5", "us_aqi_pm10", "us_aqi_nitrogen_dioxide", "us_aqi_ozone",
)


def _coordinate(lat: Any, lon: Any) -> tuple[float, float]:
    return round(float(lat), 6), round(float(lon), 6)


def _cams_grid(coord: tuple[float, float]) -> tuple[tuple[float, float], ...]:
    lat, lon = coord
    return tuple(
        _coordinate(lat + dlat, lon + dlon)
        for dlat in (-0.4, 0.0, 0.4)
        for dlon in (-0.4, 0.0, 0.4)
    )


@dataclass(frozen=True)
class RequestPlan:
    site_coords: tuple[tuple[float, float], ...]
    site_index: dict[str, int]
    horizon_coords: tuple[tuple[float, float], ...]
    horizon_index: dict[tuple[str, str, str], int]
    cams_coords: tuple[tuple[float, float], ...]
    cams_index: dict[str, tuple[int, ...]]
    fallback_coords: tuple[tuple[float, float], ...]


def build_request_plan(
    locations: Mapping[str, Mapping[str, Any]],
    horizon_points: Mapping[tuple[str, str, str], tuple[float, float]],
) -> RequestPlan:
    site_coords: list[tuple[float, float]] = []
    site_index: dict[str, int] = {}
    seen_sites: dict[tuple[float, float], str] = {}
    for location_id in sorted(locations):
        item = locations[location_id]
        coord = _coordinate(item["lat"], item["lon"])
        if coord in seen_sites:
            raise ValueError(
                f"duplicate canonical coordinate: {seen_sites[coord]} and {location_id}"
            )
        seen_sites[coord] = location_id
        site_index[location_id] = len(site_coords)
        site_coords.append(coord)

    horizon_coords: list[tuple[float, float]] = []
    horizon_coord_index: dict[tuple[float, float], int] = {}
    horizon_index: dict[tuple[str, str, str], int] = {}
    for key in sorted(horizon_points):
        coord = _coordinate(*horizon_points[key])
        index = horizon_coord_index.setdefault(coord, len(horizon_coords))
        if index == len(horizon_coords):
            horizon_coords.append(coord)
        horizon_index[key] = index

    cams_coords: list[tuple[float, float]] = []
    cams_coord_index: dict[tuple[float, float], int] = {}
    cams_index: dict[str, tuple[int, ...]] = {}
    for location_id in sorted(site_index):
        indices: list[int] = []
        for coord in _cams_grid(site_coords[site_index[location_id]]):
            index = cams_coord_index.setdefault(coord, len(cams_coords))
            if index == len(cams_coords):
                cams_coords.append(coord)
            indices.append(index)
        cams_index[location_id] = tuple(indices)

    sites = tuple(site_coords)
    return RequestPlan(
        site_coords=sites,
        site_index=site_index,
        horizon_coords=tuple(horizon_coords),
        horizon_index=horizon_index,
        cams_coords=tuple(cams_coords),
        cams_index=cams_index,
        fallback_coords=sites,
    )


def _decimal_text(value: Any) -> str:
    try:
        decimal = Decimal(str(value))
    except InvalidOperation:
        return str(value)
    text = format(decimal.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def _normalize_param(key: str, value: Any) -> str:
    text = str(value)
    if key in {"latitude", "longitude"}:
        return ",".join(_decimal_text(part) for part in text.split(","))
    if isinstance(value, (int, float, Decimal)) or text.replace(".", "", 1).isdigit():
        return _decimal_text(value)
    return text


def stable_request_key(url: str, params: Mapping[str, Any]) -> str:
    normalized = [(key, _normalize_param(key, params[key])) for key in sorted(params)]
    raw = json.dumps([url, normalized], ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _params(coords: tuple[tuple[float, float], ...], *, hourly: tuple[str, ...]) -> dict[str, str]:
    return {
        "latitude": ",".join(_decimal_text(lat) for lat, _ in coords),
        "longitude": ",".join(_decimal_text(lon) for _, lon in coords),
        "timezone": "America/Edmonton",
        "forecast_days": "5",
        "hourly": ",".join(hourly),
    }


def _as_list(payload: Any) -> list[dict[str, Any]]:
    return payload if isinstance(payload, list) else [payload]


class UnifiedWeatherCoordinator:
    """Lazily fetch and reuse canonical site weather batches.

    The small public surface is intentional: callers ask for a canonical site's
    best-match payload or one model view, while the coordinator guarantees that
    each underlying all-site batch is executed at most once.
    """

    def __init__(
        self,
        locations: Mapping[str, Mapping[str, Any]],
        *,
        fetch_json: Callable[[str], Any],
        fallback_fetch: Callable[[tuple[tuple[float, float], ...], int], list[dict[str, Any]]] | None = None,
        cams_chunk_size: int = 90,
    ) -> None:
        self.plan = build_request_plan(locations, {})
        self.fetch_json = fetch_json
        self.fallback_fetch = fallback_fetch
        self.cams_chunk_size = cams_chunk_size
        self._best: list[dict[str, Any]] | None = None
        self._models: list[dict[str, Any]] | None = None
        self._cams: list[dict[str, Any] | None] | None = None
        self._horizon_plan: RequestPlan | None = None
        self._horizons: list[dict[str, Any]] | None = None
        self._best_loaded = False
        self._models_loaded = False
        self._errors = {location_id: [] for location_id in self.plan.site_index}

    def _fetch_sites(
        self,
        *,
        kind: str,
        hourly: tuple[str, ...],
        models: bool = False,
    ) -> list[dict[str, Any]] | None:
        params = _params(self.plan.site_coords, hourly=hourly)
        if kind == "best_match":
            params["daily"] = "sunrise,sunset"
        if models:
            params["models"] = ",".join(MODELS)
        try:
            payloads = _as_list(self.fetch_json(f"{FORECAST_URL}?{urlencode(params)}"))
            expected = len(self.plan.site_coords)
            if len(payloads) != expected:
                raise ValueError(f"{kind} expected {expected} responses, received {len(payloads)}")
            return payloads
        except Exception as exc:
            for errors in self._errors.values():
                errors.append(str(exc))
            return None

    def _load_best(self) -> None:
        if not self._best_loaded:
            self._best_loaded = True
            self._best = self._fetch_sites(
                kind="best_match", hourly=SITE_BEST_FIELDS
            )
            if self._best is None and self.fallback_fetch is not None:
                try:
                    fallback = list(self.fallback_fetch(self.plan.fallback_coords, 5))
                    if len(fallback) != len(self.plan.site_coords):
                        raise ValueError(
                            f"MET fallback expected {len(self.plan.site_coords)} responses, received {len(fallback)}"
                        )
                    self._best = fallback
                except Exception as exc:
                    for errors in self._errors.values():
                        errors.append(str(exc))

    def _load_models(self) -> None:
        if not self._models_loaded:
            self._models_loaded = True
            self._models = self._fetch_sites(
                kind="four_models", hourly=SITE_MODEL_FIELDS, models=True
            )

    def best_match(self, location_id: str) -> dict[str, Any] | None:
        self._load_best()
        if self._best is None:
            return None
        return self._best[self.plan.site_index[location_id]]

    def model(self, location_id: str, model: str) -> dict[str, Any] | None:
        if model not in MODELS:
            raise KeyError(model)
        self._load_models()
        if self._models is None:
            return None
        payload = self._models[self.plan.site_index[location_id]]
        hourly = payload.get("hourly") or {}
        suffix = f"_{model}"
        selected = {
            (key[: -len(suffix)] if key.endswith(suffix) else key): value
            for key, value in hourly.items()
            if key == "time" or key.endswith(suffix)
        }
        result = dict(payload)
        result["hourly"] = selected
        return result

    def four_models(self, location_id: str) -> dict[str, Any] | None:
        """Return the untouched explicit-model payload for night consensus."""
        self._load_models()
        if self._models is None:
            return None
        return self._models[self.plan.site_index[location_id]]

    def _load_cams(self) -> None:
        if self._cams is not None:
            return
        if self.cams_chunk_size < 1:
            raise ValueError("cams_chunk_size must be positive")
        self._cams = [None] * len(self.plan.cams_coords)
        for start in range(0, len(self.plan.cams_coords), self.cams_chunk_size):
            chunk = self.plan.cams_coords[start : start + self.cams_chunk_size]
            params = _params(chunk, hourly=CAMS_FIELDS)
            params["timezone"] = "GMT"
            params["domains"] = "cams_global"
            try:
                responses = _as_list(
                    self.fetch_json(f"{AIR_QUALITY_URL}?{urlencode(params)}")
                )
                if len(responses) != len(chunk):
                    raise ValueError(
                        f"cams expected {len(chunk)} responses, received {len(responses)}"
                    )
                self._cams[start : start + len(chunk)] = responses
            except Exception as exc:
                for location_id, indices in self.plan.cams_index.items():
                    if any(start <= index < start + len(chunk) for index in indices):
                        self._errors[location_id].append(str(exc))

    def cams_grid(self, location_id: str) -> list[dict[str, Any] | None]:
        self._load_cams()
        assert self._cams is not None
        return [self._cams[index] for index in self.plan.cams_index[location_id]]

    def cams_window(self, *, lat, lon, start, end) -> dict[str, object]:
        """Aggregate a site's prefetched 3×3 CAMS grid without another HTTP call."""
        from backend.smoke_sources import fetch_cams_window

        coord = _coordinate(lat, lon)
        location_id = next(
            location_id
            for location_id, index in self.plan.site_index.items()
            if self.plan.site_coords[index] == coord
        )
        return fetch_cams_window(
            lat=lat,
            lon=lon,
            start=start,
            end=end,
            fetch_json=lambda _url: self.cams_grid(location_id),
        )

    def configure_horizons(
        self,
        horizon_points: Mapping[tuple[str, str, str], tuple[float, float]],
    ) -> None:
        """Register the complete runtime horizon set before its one batch fetch."""
        if self._horizons is not None:
            raise RuntimeError("horizons already fetched")
        self._horizon_plan = build_request_plan(
            {
                location_id: {
                    "lat": self.plan.site_coords[index][0],
                    "lon": self.plan.site_coords[index][1],
                }
                for location_id, index in self.plan.site_index.items()
            },
            horizon_points,
        )

    def horizon(self, key: tuple[str, str, str]) -> dict[str, Any] | None:
        if self._horizon_plan is None:
            return None
        if self._horizons is None:
            coords = self._horizon_plan.horizon_coords
            params = _params(coords, hourly=("cloud_cover_low", "cloud_cover_mid"))
            try:
                responses = _as_list(
                    self.fetch_json(f"{FORECAST_URL}?{urlencode(params)}")
                )
                if len(responses) != len(coords):
                    raise ValueError(
                        f"horizon expected {len(coords)} responses, received {len(responses)}"
                    )
                self._horizons = responses
            except Exception:
                self._horizons = []
        index = self._horizon_plan.horizon_index[key]
        return self._horizons[index] if index < len(self._horizons) else None

    def site_error(self, location_id: str) -> str | None:
        errors = self._errors[location_id]
        return "; ".join(errors) if errors else None


def fetch_request_plan(
    plan: RequestPlan,
    fetch_json: Callable[[str], Any],
    *,
    fallback_fetch: Callable[[tuple[tuple[float, float], ...], int], list[dict[str, Any]]] | None = None,
    cams_chunk_size: int = 90,
) -> dict[str, Any]:
    """Execute each planned batch once and preserve honest per-site failures."""
    if cams_chunk_size < 1:
        raise ValueError("cams_chunk_size must be positive")
    requests: list[tuple[str, dict[str, str]]] = []
    site = {
        location_id: {"best_match": None, "four_models": None, "errors": []}
        for location_id in plan.site_index
    }

    def request(kind: str, base: str, params: dict[str, str]) -> Any:
        requests.append((kind, dict(params)))
        return fetch_json(f"{base}?{urlencode(params)}")

    best_params = _params(plan.site_coords, hourly=SITE_BEST_FIELDS)
    best_params["daily"] = "sunrise,sunset"
    try:
        best = _as_list(request("site_best_match", FORECAST_URL, best_params))
        if len(best) != len(plan.site_coords):
            raise ValueError(
                f"site_best_match expected {len(plan.site_coords)} responses, received {len(best)}"
            )
    except Exception as exc:
        if fallback_fetch is not None:
            try:
                best = list(fallback_fetch(plan.fallback_coords, 5))
                if len(best) != len(plan.site_coords):
                    raise ValueError(
                        f"MET fallback expected {len(plan.site_coords)} responses, received {len(best)}"
                    )
            except Exception as fallback_exc:
                best = []
                exc = fallback_exc
        else:
            best = []
        if not best:
            for entry in site.values():
                entry["errors"].append(str(exc))
    if len(best) == len(plan.site_coords):
        for location_id, index in plan.site_index.items():
            site[location_id]["best_match"] = best[index]

    model_params = _params(plan.site_coords, hourly=SITE_MODEL_FIELDS)
    model_params["models"] = ",".join(MODELS)
    try:
        models = _as_list(request("site_four_models", FORECAST_URL, model_params))
        if len(models) != len(plan.site_coords):
            raise ValueError(
                f"site_four_models expected {len(plan.site_coords)} responses, received {len(models)}"
            )
        for location_id, index in plan.site_index.items():
            site[location_id]["four_models"] = models[index]
    except Exception as exc:
        for entry in site.values():
            entry["errors"].append(str(exc))

    horizons: list[dict[str, Any]] = []
    if plan.horizon_coords:
        horizon_params = _params(
            plan.horizon_coords, hourly=("cloud_cover_low", "cloud_cover_mid")
        )
        try:
            horizons = _as_list(request("horizon", FORECAST_URL, horizon_params))
            if len(horizons) != len(plan.horizon_coords):
                raise ValueError(
                    f"horizon expected {len(plan.horizon_coords)} responses, received {len(horizons)}"
                )
        except Exception:
            horizons = []

    cams: list[dict[str, Any] | None] = [None] * len(plan.cams_coords)
    for start in range(0, len(plan.cams_coords), cams_chunk_size):
        chunk = plan.cams_coords[start : start + cams_chunk_size]
        cams_params = _params(chunk, hourly=CAMS_FIELDS)
        cams_params["timezone"] = "GMT"
        cams_params["domains"] = "cams_global"
        try:
            responses = _as_list(request("cams", AIR_QUALITY_URL, cams_params))
            if len(responses) != len(chunk):
                raise ValueError(
                    f"cams expected {len(chunk)} responses, received {len(responses)}"
                )
            cams[start : start + len(chunk)] = responses
        except Exception:
            continue

    return {
        "plan": plan,
        "site": site,
        "horizon": horizons,
        "cams": cams,
        "http_requests": requests,
        "http_request_count": len(requests),
        "coordinate_counts": {
            "site": len(plan.site_coords),
            "horizon": len(plan.horizon_coords),
            "cams": len(plan.cams_coords),
            "fallback": len(plan.fallback_coords),
        },
    }
