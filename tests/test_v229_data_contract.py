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

    assert len(locations) == len(daylight) == 18
    assert len(set(locations)) == 18
    assert len({(item["lat"], item["lon"]) for item in locations.values()}) == 18
    assert len({item["location_id"] for item in daylight}) == 18
    assert len({(item["lat"], item["lon"]) for item in daylight}) == 18
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


def test_existing_location_elevations_match_pre_v229_contract_everywhere():
    locations = load(SKILL_LOCATIONS)
    backend_locations = load(BACKEND_LOCATIONS)
    daylight = {point.get("location_id"): point for point in load(SPOTS)["points"]}
    expected = {
        "herbert_lake": 1570,
        "lake_minnewanka": 1515,
        "lake_louise": 1750,
        "bow_lake": 1935,
    }

    for location_id, elevation in expected.items():
        assert locations[location_id]["elev_m"] == elevation
        assert backend_locations[location_id]["elev_m"] == elevation
        assert daylight[location_id]["elev_m"] == elevation


def test_all_daylight_ui_copy_is_formal_traditional_chinese_for_new_points():
    points = {point.get("location_id"): point for point in load(SPOTS)["points"]}
    wedge = points["wedge_pond"]
    assert wedge["daylight_events"] == ["sunrise"]
    assert "保證" in wedge["caveat"]
    assert "保育通行證" in wedge["access"]


def test_quarry_lake_uses_official_park_anchor_not_lake_centroid():
    locations = load(SKILL_LOCATIONS)
    point = next(point for point in load(SPOTS)["points"] if point["id"] == "quarry_haling")
    location = locations["quarry_lake"]
    assert (location["lat"], location["lon"]) == (51.0780551, -115.3736271)
    assert (point["lat"], point["lon"]) == (location["lat"], location["lon"])
    assert point["daylight_events"] == ["sunrise", "sunset"]
    assert point["cat"] == "sunset"
    assert point["est_coords"] is True
    assert "非湖岸腳架點" in location["coord_source"]
    assert "停車收費" in point["access"]


def test_daylight_builder_uses_explicit_canonical_location_id():
    source = (ROOT / "backend/daylight_report.py").read_text(encoding="utf-8")
    assert '"id": point["location_id"]' in source
