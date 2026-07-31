#!/usr/bin/env python3
"""Validate and merge Hermes photographic analysis into a generated report."""

import argparse
import json
from pathlib import Path

REQUIRED_TEXT = ("source_generated_utc", "analyzed_at", "analyst", "status", "headline", "summary", "confidence")
VALID_STATUS = {"verified", "partial", "failed"}
VALID_CONFIDENCE = {"high", "medium", "low"}


def _load(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def validate_analysis(report: dict, analysis: dict) -> None:
    if analysis.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")
    for key in REQUIRED_TEXT:
        if not isinstance(analysis.get(key), str) or not analysis[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if analysis["source_generated_utc"] != report.get("generated_utc"):
        raise ValueError("source_generated_utc does not match report generated_utc")
    if analysis["status"] not in VALID_STATUS:
        raise ValueError("status must be verified, partial, or failed")
    if analysis["confidence"] not in VALID_CONFIDENCE:
        raise ValueError("confidence must be high, medium, or low")

    location_ids = {item.get("location_id") for item in report.get("locations", []) if item.get("location_id")}
    notes = analysis.get("location_notes")
    if not isinstance(notes, dict) or set(notes) != location_ids:
        raise ValueError("location_notes must contain exactly every report location_id")
    for location_id, note in notes.items():
        if not isinstance(note, dict):
            raise ValueError(f"location_notes.{location_id} must be an object")
        for key in ("verdict", "photographic_meaning"):
            if not isinstance(note.get(key), str) or not note[key].strip():
                raise ValueError(f"location_notes.{location_id}.{key} must be a non-empty string")

    best_id = analysis.get("best_location_id")
    if best_id is not None and best_id not in location_ids:
        raise ValueError("best_location_id must be null or a valid location_id")
    for key in ("key_factors", "uncertainties"):
        if not isinstance(analysis.get(key), list) or not all(isinstance(x, str) for x in analysis[key]):
            raise ValueError(f"{key} must be an array of strings")
    verification = analysis.get("verification")
    required_checks = ("score_recalculated", "best_window_recalculated", "source_fields_checked")
    if not isinstance(verification, dict) or not all(isinstance(verification.get(k), bool) for k in required_checks):
        raise ValueError("verification must contain the three boolean checks")


def merge_analysis(report_path: Path, analysis_path: Path) -> None:
    report_path = Path(report_path)
    analysis_path = Path(analysis_path)
    report = _load(report_path)
    analysis = _load(analysis_path)
    validate_analysis(report, analysis)
    report["ai_analysis"] = analysis
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--analysis", required=True, type=Path)
    args = parser.parse_args()
    merge_analysis(args.report, args.analysis)
    print(f"Merged verified analysis into {args.report}")


if __name__ == "__main__":
    main()
