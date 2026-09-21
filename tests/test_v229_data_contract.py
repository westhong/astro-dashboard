import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_LOCATIONS = Path.home() / "AppData/Local/hermes/profiles/astro-assistant/skills/photography/rockies-milkyway-scout/references/locations.json"
BACKEND_LOCATIONS = ROOT / "backend/references/locations.json"
SPOTS = ROOT / "backend/spots.json"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_authoritative_and_backend_locations_are_semantically_identical():
    assert load(SKILL_LOCATIONS) == load(BACKEND_LOCATIONS)


def test_night_and_daylight_use_the_same_unique_canonical_set():
    locations = load(SKILL_LOCATIONS)
    daylight = [point for point in load(SPOTS)["points"] if point.get("daylight_events")]

    assert len(locations) == len(daylight) == 16
    assert len(set(locations)) == 16
    assert len({(item["lat"], item["lon"]) for item in locations.values()}) == 16
    assert len({item["location_id"] for item in daylight}) == 16
    assert len({(item["lat"], item["lon"]) for item in daylight}) == 16
    assert set(locations) == {item["location_id"] for item in daylight}
    assert {
        location_id: (item["lat"], item["lon"])
        for location_id, item in locations.items()
    } == {
        item["location_id"]: (item["lat"], item["lon"])
        for item in daylight
    }


def test_castle_and_wedge_are_present_once_with_verified_coordinates():
    locations = load(SKILL_LOCATIONS)
    daylight = [point for point in load(SPOTS)["points"] if point.get("daylight_events")]

    expected = {
        "castle_mountain": (51.266607, -115.927466),
        "wedge_pond": (50.873890, -115.146438),
    }
    for location_id, coordinate in expected.items():
        assert (locations[location_id]["lat"], locations[location_id]["lon"]) == coordinate
        matches = [point for point in daylight if point["location_id"] == location_id]
        assert len(matches) == 1
        assert (matches[0]["lat"], matches[0]["lon"]) == coordinate

    wedge = locations["wedge_pond"]
    assert wedge["elev_m"] == 1534
    assert "Alberta Parks" in wedge["coord_source"]
    assert "West" not in wedge["coord_source"]
    assert "composition_az" not in wedge


def test_all_daylight_ui_copy_is_formal_traditional_chinese_for_new_points():
    points = {point.get("location_id"): point for point in load(SPOTS)["points"]}
    wedge = points["wedge_pond"]
    assert wedge["daylight_events"] == ["sunrise"]
    assert "保證" in wedge["caveat"]
    assert "保育通行證" in wedge["access"]


def test_daylight_builder_uses_explicit_canonical_location_id():
    source = (ROOT / "backend/daylight_report.py").read_text(encoding="utf-8")
    assert '"id": point["location_id"]' in source
