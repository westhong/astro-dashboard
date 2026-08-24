"""Pure domain logic for photography smoke assessment."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping, Sequence


MODEL_CLASSES = ("CLEAN", "HAZE", "SMOKY", "HEAVY", "NO_DATA")


def dominant_pollutant(health_subindices: Mapping[str, float | None]) -> str | None:
    """Return the largest comparable health sub-index, not a smoke verdict."""
    available = {name: value for name, value in health_subindices.items() if value is not None}
    if not available:
        return None
    return max(available, key=available.__getitem__)


def classify_pm25(pm2_5: float | None) -> str:
    """Classify a model's aligned window-average PM2.5 value."""
    if pm2_5 is None:
        return "NO_DATA"
    if pm2_5 <= 10:
        return "CLEAN"
    if pm2_5 <= 25:
        return "HAZE"
    if pm2_5 <= 55:
        return "SMOKY"
    return "HEAVY"


def evaluate_model(pm2_5: float | None) -> dict[str, object]:
    """Return the per-model photography classification and uncertainty."""
    if pm2_5 is not None and pm2_5 > 55:
        vote = "VETO"
    elif pm2_5 is not None and pm2_5 > 35:
        vote = "RISKY_CAP"
    else:
        vote = None
    return {
        "class": classify_pm25(pm2_5),
        "score": pm25_score(pm2_5),
        "vote": vote,
        "uncertain": pm2_5 is None,
    }


def evaluate_consensus(values: Sequence[float | None]) -> dict[str, object]:
    """Derive a photography consensus without averaging model PM2.5."""
    if len(values) != 3:
        raise ValueError("Three model slots are required")

    valid = [value for value in values if value is not None]
    classes = [classify_pm25(value) for value in valid]
    counts = Counter(classes)
    coverage = len(valid)
    partial = coverage < 3
    uncertainties = []
    if partial:
        uncertainties.append(f"Partial model coverage: {coverage}/3 valid models.")

    if coverage <= 1:
        status, confidence = "SINGLE_MODEL_ONLY", "low"
    elif counts["HEAVY"] >= 2:
        status = "VETO"
        confidence = "high" if coverage == 3 else "medium"
    elif coverage == 2:
        if len(counts) == 1:
            agreed_class = classes[0]
            status = "VERIFIED_CLEAN" if agreed_class == "CLEAN" else "SMOKE_RISK"
            confidence = "medium"
        else:
            status, confidence = "MODEL_SPLIT", "low"
    elif counts["CLEAN"] == 3:
        status, confidence = "VERIFIED_CLEAN", "high"
    elif counts["CLEAN"] == 2 and counts["HAZE"] == 1:
        status, confidence = "LIKELY_CLEAN", "medium"
    elif counts["CLEAN"] == 2:
        status, confidence = "RISKY_BOUNDARY", "medium"
    elif len(counts) == 3:
        status, confidence = "MODEL_SPLIT", "low"
    elif max(counts.values(), default=0) >= 2:
        status, confidence = "SMOKE_RISK", "medium"
    else:
        status, confidence = "MODEL_SPLIT", "low"

    if coverage == 3:
        consensus_pm2_5 = sorted(valid)[-2]
    elif valid:
        consensus_pm2_5 = max(valid)
    else:
        consensus_pm2_5 = None

    score = pm25_score(consensus_pm2_5)
    if status == "VETO":
        score = 5
    elif valid and max(valid) > 55 and max(valid) - min(valid) > 30:
        status = "RISKY_BOUNDARY"
        score = min(score, 55)

    return {
        "status": status,
        "confidence": confidence,
        "consensus_pm2_5": consensus_pm2_5,
        "photography_smoke_score": score,
        "veto": status == "VETO",
        "reason": f"Model classes: {', '.join(classes) or 'NO_DATA'}.",
        "partial": partial,
        "uncertain": partial,
        "uncertainties": uncertainties,
    }


def build_smoke_assessment(
    *,
    shooting_point: Mapping[str, object],
    window_local: Mapping[str, object],
    models: Mapping[str, Mapping[str, object]],
    observed_now: Mapping[str, object] | None = None,
    pollutants: Mapping[str, object] | None = None,
    health_subindices: Mapping[str, float | None] | None = None,
    source_support: Mapping[str, object] | None = None,
    uncertainties: Sequence[str] = (),
) -> dict[str, object]:
    """Build the serializable smoke-assessment contract from caller data only."""
    observed_input = observed_now or {}
    pollutant_input = pollutants or {}
    support_input = source_support or {}

    observed = {
        key: observed_input.get(key)
        for key in ("aqhi", "station", "observation_time_utc", "visual_visibility")
    }
    pollutant_values = {
        key: pollutant_input.get(key)
        for key in (
            "pm2_5",
            "pm10",
            "ozone",
            "nitrogen_dioxide",
            "us_aqi_health_context",
        )
    }
    pollutant_values["dominant_pollutant"] = dominant_pollutant(health_subindices or {})

    normalized_models: dict[str, dict[str, object]] = {}
    consensus_values: list[float | None] = []
    for name in ("eccc_firework", "cams_global", "bluesky_canada"):
        raw = models.get(name, {})
        value = raw.get("window_avg_pm2_5")
        valid = bool(raw.get("valid", value is not None)) and value is not None
        effective_value = value if valid else None
        model = {
            "reference_time": raw.get("reference_time"),
            "valid": valid,
            "window_avg_pm2_5": effective_value,
            "window_range": raw.get("window_range", [None, None]),
            "neighbor_range": raw.get("neighbor_range", [None, None]),
            "class": classify_pm25(effective_value),
        }
        if name == "bluesky_canada":
            model = {"forecast_id": raw.get("forecast_id"), **model}
        normalized_models[name] = model
        consensus_values.append(effective_value)  # type: ignore[arg-type]

    consensus = evaluate_consensus(consensus_values)
    all_uncertainties = [*uncertainties, *consensus["uncertainties"]]
    assessment = {
        "shooting_point": {
            "lat": shooting_point.get("lat"),
            "lon": shooting_point.get("lon"),
        },
        "window_local": {
            "start": window_local.get("start"),
            "end": window_local.get("end"),
            "timezone": window_local.get("timezone"),
        },
        "observed_now": observed,
        "pollutants": pollutant_values,
        "models": normalized_models,
        "consensus": consensus,
        "source_support": {
            "classification": support_input.get("classification"),
            "nearest_confirmed_fire_km": support_input.get("nearest_confirmed_fire_km"),
            "nearest_satellite_hotspot_km": support_input.get("nearest_satellite_hotspot_km"),
            "transport_supported": support_input.get("transport_supported"),
            "notes": list(support_input.get("notes", [])),
        },
        "uncertainties": all_uncertainties,
    }
    payload = {"smoke_assessment": assessment}
    json.dumps(payload)
    return payload


def pm25_score(pm2_5: float | None) -> int:
    """Map PM2.5 µg/m³ to West's photography smoke score ladder."""
    if pm2_5 is None:
        return 60
    if pm2_5 <= 5:
        return 100
    if pm2_5 <= 10:
        return 90
    if pm2_5 <= 15:
        return 75
    if pm2_5 <= 25:
        return 55
    if pm2_5 <= 35:
        return 35
    if pm2_5 <= 55:
        return 18
    return 5
