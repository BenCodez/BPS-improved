"""Real solver/pipeline regressions for the optional outdoor extension."""
import asyncio
import copy
import datetime
import json
import math
from types import SimpleNamespace

import pytest

import bps
from bps.environment import compile_environment
from bps.outdoor_config import validate_outdoor_layout
from bps.outdoor_tracking import solve_outdoor
from bps.storage import load_bps_data, save_bps_data, STORAGE_KEY_LAYOUT


def run(coro):
    return asyncio.run(coro)


def layout(enabled=None):
    floor = {"name": "Property", "scale": 10.0, "zones": [], "receivers": [
        {"entity_id": f"p{i}", "cords": {"x": x, "y": y}, "correction": 1.0}
        for i, (x, y) in enumerate(((0, 0), (100, 0), (0, 100), (100, 100)))
    ]}
    data = {"floor": [floor]}
    if enabled is not None:
        data["outdoor_tracking"] = {"enabled": enabled}
    return data


@pytest.fixture(autouse=True)
def reset_pipeline(monkeypatch):
    monkeypatch.setattr(bps, "apitricords", [])
    monkeypatch.setattr(bps, "tracked_entities", [])
    for mapping in (bps._kf_position_state, bps._floor_probability,
                    bps._floor_challenge, bps._floor_dark_cycles):
        mapping.clear()
    for name in ("last_floor", "last_r_values"):
        monkeypatch.setattr(bps.update_trilateration_and_zone, name, {}, raising=False)


def states(hass, value="7.0710678118654755", age=0):
    state = SimpleNamespace(state=value, attributes={"unit_of_measurement": "m"},
                            last_updated=datetime.datetime.fromtimestamp(
                                bps.time.time() - age, datetime.timezone.utc))
    hass.states = SimpleNamespace(get=lambda _eid: state)


def solve(hass, data):
    item = {"entity": "beacon_a", "data": copy.deepcopy(data)}
    run(bps.process_single_entity(hass, [item], item))
    return copy.deepcopy(bps.apitricords), item


def test_missing_and_disabled_settings_never_enter_outdoor_solver(hass, monkeypatch):
    states(hass)
    def forbidden(*a, **kw):
        raise AssertionError("outdoor work reached legacy solve")
    monkeypatch.setattr(bps, "solve_outdoor", forbidden)
    old, _ = solve(hass, layout())
    # Reset only the filter to compare identical first-sighting payloads.
    bps._kf_position_state.clear()
    disabled, _ = solve(hass, layout(False))
    for payload in (old[0], disabled[0]):
        payload.pop("updated")
    assert old == disabled
    assert "outdoor" not in old[0]
    assert old[0]["cords"] == pytest.approx([50, 50])


def test_enabled_clear_path_equivalent_and_executor_used(hass):
    states(hass)
    calls = []
    async def executor(func, *args):
        calls.append(func)
        return func(*args)
    hass.async_add_executor_job = executor
    result, item = solve(hass, layout(True))
    assert len(calls) == 1
    assert result[0]["cords"] == pytest.approx([50, 50])
    outdoor = result[0]["outdoor"]
    assert outdoor["receivers_used"] == 4
    assert outdoor["clear_paths"] == 4
    assert math.isfinite(outdoor["estimated_uncertainty_m"])
    assert all(r["correction"] == 1.0 for r in item["data"]["floor"][0]["receivers"])
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("bad", ["unavailable", "unknown", "nan", "inf", "-4", None])
def test_outdoor_does_not_reuse_persisted_bad_or_missing_readings(hass, bad):
    data = layout(True)
    for r in data["floor"][0]["receivers"]:
        r["distance"] = 5
        r["cords"]["r"] = 50
    if bad is None:
        hass.states = SimpleNamespace(get=lambda _eid: None)
    else:
        states(hass, bad)
    result, item = solve(hass, data)
    assert result == []
    assert all("distance" not in r for r in item["data"]["floor"][0]["receivers"])


def test_stale_readings_excluded_before_solve(hass):
    states(hass, age=31)
    result, _ = solve(hass, layout(True))
    assert result == []


def points(floor, truth=(50, 50), liar=False):
    weighted = []
    for i, rec in enumerate(floor["receivers"]):
        x, y = rec["cords"]["x"], rec["cords"]["y"]
        radius = math.dist((x, y), truth)
        if liar and i == 3:
            radius *= 0.3
        rec["distance"] = radius / floor["scale"]
        rec["cords"]["r"] = radius
        rec["_outdoor_reading"] = {"measured_distance_m": radius / floor["scale"], "reading_age_s": 0}
        weighted.append((x, y, radius, 1.0, radius))
    return weighted


def test_bounded_refinement_and_no_distance_or_calibration_mutation():
    floor = layout(True)["floor"][0]
    weighted = points(floor, liar=True)
    floor["environment"] = [{"id": "shop", "type": "building", "material": "metal", "points": [
        {"x": 75, "y": 75}, {"x": 95, "y": 75}, {"x": 95, "y": 95}, {"x": 75, "y": 95}]}]
    before = copy.deepcopy(floor)
    calls = []
    def solver(pts, **kw):
        calls.append(pts)
        return bps.trilaterate(pts, **kw)
    result = solve_outdoor(floor, weighted, (-10, -10, 110, 110), 10, 5, 30, None, solver)
    assert result is not None
    assert len(calls) <= 2
    assert floor == before
    assert all(a[2] == b[2] for a, b in zip(result["weighted"], weighted))
    assert compile_environment(floor) is compile_environment(copy.deepcopy(floor))


def test_no_environment_keeps_weak_jump_weight_and_single_solve():
    floor = layout(True)["floor"][0]
    weighted = points(floor)
    weighted[0] = (*weighted[0][:3], 0.001, weighted[0][4])
    calls = []
    def solver(pts, **kw):
        calls.append(pts)
        return bps.trilaterate(pts, **kw)
    result = solve_outdoor(floor, weighted, (-10, -10, 110, 110), 10, 5, 30, None, solver)
    assert len(calls) == 1
    assert result["weighted"] == weighted


def test_obstructed_lying_receiver_has_less_influence():
    floor = layout(True)["floor"][0]
    weighted = points(floor, liar=True)
    floor["environment"] = [{"id": "shop", "type": "building", "material": "metal", "points": [
        {"x": 90, "y": 90}, {"x": 110, "y": 90}, {"x": 110, "y": 110}, {"x": 90, "y": 110}]}]
    raw = bps.trilaterate(weighted, bounds=(-10, -10, 110, 110), min_weight_radius=5)
    result = solve_outdoor(floor, weighted, (-10, -10, 110, 110), 10, 5, 30, None, bps.trilaterate)
    assert result["weighted"][3][3] < weighted[3][3]
    assert math.dist(result["fix"], (50, 50)) < math.dist(raw, (50, 50))
    assert result["outdoor"]["receiver_diagnostics"][3]["reflection_risk"]


def test_ignore_cannot_remove_all_receivers_into_solver():
    floor = layout(True)["floor"][0]
    weighted = points(floor)
    for rec in floor["receivers"]:
        rec["outdoor_policy"] = "ignore"
    assert solve_outdoor(floor, weighted, (-10, -10, 110, 110), 10, 5, 30, None,
                         lambda *a, **k: pytest.fail("insufficient solve")) is None


def test_disabling_removes_optional_payload(hass):
    states(hass)
    assert "outdoor" in solve(hass, layout(True))[0][0]
    assert "outdoor" not in solve(hass, layout(False))[0][0]


def test_old_layout_store_roundtrip_unchanged(hass):
    old = layout()
    run(save_bps_data(hass, old))
    hass.data["bps"].pop("layout")
    assert run(load_bps_data(hass)) == old
    assert hass._store_backing[STORAGE_KEY_LAYOUT] == old


def test_malformed_optional_fields_rejected_before_store_write(hass, tmp_path):
    old = layout()
    run(save_bps_data(hass, old))
    bad = {**old, "outdoor_tracking": {"enabled": "true"}}
    response = run(bps.BPSSaveAPIText()._write_save(hass, str(tmp_path), {}, bad))
    assert response.status == 400
    assert hass._store_backing[STORAGE_KEY_LAYOUT] == old
    assert len(hass._store_saves) == 1


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, -1, "3"])
def test_invalid_uncertainty_settings_fail_safe(bad):
    assert validate_outdoor_layout({"outdoor_tracking": {"hide_uncertainty_below_m": bad}})


def test_group_runtime_preserves_originals_and_deletes_disabled_group(hass):
    data = layout(True)
    data["tracker_groups"] = [{"id": "rover", "name": "Rover", "beacons": ["beacon_a", "beacon_b"]}]
    run(save_bps_data(hass, data))
    now = bps.time.time()
    originals = [{"ent": name, "floor": "Property", "zone": "unknown", "cords": xy,
                  "updated": now, "rms_m": 1, "outdoor": {"estimated_uncertainty_m": 3, "receivers_used": 4}}
                 for name, xy in (("beacon_a", [50, 50]), ("beacon_b", [55, 50]))]
    bps.apitricords = copy.deepcopy(originals)
    run(bps.update_tracker_groups(hass))
    assert bps.apitricords[:2] == originals
    assert bps.apitricords[2]["ent"] == "bps_group_rover"
    assert 50 <= bps.apitricords[2]["cords"][0] <= 55
    data["outdoor_tracking"]["enabled"] = False
    run(bps.update_tracker_groups(hass))
    assert bps.apitricords == originals


def test_group_collision_with_unsolved_discovered_beacon(hass):
    data = layout(True)
    data["tracker_groups"] = [{"id": "rover", "beacons": ["beacon_a"]}]
    run(save_bps_data(hass, data))
    bps.tracked_entities = ["sensor.bps_group_rover_distance_to_p0"]
    bps.apitricords = [{"ent": "beacon_a", "floor": "Property", "cords": [50, 50],
                       "updated": bps.time.time(), "outdoor": {"estimated_uncertainty_m": 3}}]
    run(bps.update_tracker_groups(hass))
    assert len(bps.apitricords) == 1


def test_existing_location_endpoints_remain_authenticated():
    for cls in (bps.BPSCordsAPI, bps.BPSHistoryAPI, bps.BPSReadAPIText, bps.BPSSaveAPIText):
        assert cls.requires_auth is True


def test_api_polling_does_not_refresh_old_fix_freshness(hass, monkeypatch):
    monkeypatch.setattr(bps.time, "time", lambda: 1000.0)
    position = {"ent": "beacon_a", "updated": 980, "outdoor": {"observed": 960, "estimated_uncertainty_m": 3}}
    hass.data["bps"] = {"apitricords": [position]}
    response = run(bps.BPSCordsAPI(hass).get(None))
    assert response.json_body[0]["outdoor"]["stale"] is True
    assert response.json_body[0]["outdoor"]["position_age_s"] == 40
    assert "stale" not in position["outdoor"]  # projection leaves stored fix intact
