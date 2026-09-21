"""Spawn-safe fixtures for process-isolation regressions."""
from pathlib import Path
import time


def blocking_complete_report(stage: str, marker_dir: str, delay: float = 2.0):
    """Model coordinator prep/night/spots/daylight and block at one phase."""
    marker = Path(marker_dir)
    marker.mkdir(parents=True, exist_ok=True)
    for current in ("coordinator_prep", "night", "spots", "daylight"):
        (marker / f"{current}.entered").write_text("entered", encoding="utf-8")
        if current == stage:
            time.sleep(delay)
            (marker / f"{current}.late").write_text("late", encoding="utf-8")
    return {"ok": True}
