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
from bps.outdoor_tracking import solve_outdoor, account_for_published_position
from bps.storage import load_bps_data, save_bps_data, STORAGE_KEY_LAYOUT


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("initial, displacement, expected", [
    ("good", 4.0, "moderate"), ("moderate", 0.0, "moderate"),
    ("poor", 1.0, "poor"), ("stale", 1.0, "stale"),
])
def test_published_displacement_can_only_reduce_confidence(initial, displacement, expected):
    quality = {"estimated_uncertainty_m": 2.0, "confidence": initial}
    account_for_published_position(quality, (0, 0), (displacement * 10, 0), 10)
    assert quality["publication_displacement_m"] == displacement
    assert quality["estimated_uncertainty_m"] == pytest.approx(math.hypot(2, displacement))
    assert quality["confidence"] == expected


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


@pytest.mark.parametrize("enabled", [None, True])
def test_recording_records_actual_solver_observations_without_changing_fix(hass, monkeypatch, enabled):
    data = layout(enabled)
    now = 1000.0
    monkeypatch.setattr(bps.time, "time", lambda: now)
    states(hass, age=5)
    baseline, _ = solve(hass, data)
    assert "diagnostic_inputs" not in baseline[0]
    bps.apitricords = []
    bps._kf_position_state.clear()
    bps._floor_probability.clear()
    bps._floor_challenge.clear()
    bps._floor_dark_cycles.clear()
    bps.update_trilateration_and_zone.last_r_values = {}
    bps.update_trilateration_and_zone.last_floor = {}
    recording = SimpleNamespace(active=True, sources=frozenset({"beacon_a"}))
    hass.data.setdefault("bps", {})["_diagnostics"] = recording
    captured, _ = solve(hass, data)
    inputs = captured[0].pop("diagnostic_inputs")
    assert len(inputs) == 4 and all(r["observed"] == 995 for r in inputs)
    assert {r["receiver"] for r in inputs} == {"p0", "p1", "p2", "p3"}
    assert captured == baseline
    recording.active = False
    next_fix, _ = solve(hass, data)
    assert "diagnostic_inputs" not in next_fix[0]


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


@pytest.mark.parametrize("bad", ["unavailable", "unknown", "nan", "inf", "1e308", "-4", None])
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


def test_group_history_records_expiry_driven_transition_without_refreshing_sources(hass, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(bps.time, "time", lambda: now)
    data = layout(True)
    data["tracker_groups"] = [{"id": "rover", "beacons": ["beacon_a", "beacon_b"]}]
    data["floor"][0]["zones"] = [
        {"entity_id": name, "poly": True,
         "cords": [{"x": left, "y": 0}, {"x": right, "y": 0},
                   {"x": right, "y": 1000}, {"x": left, "y": 1000}]}
        for name, left, right in (("Left", 0, 498), ("Right", 498, 1000))
    ]
    run(save_bps_data(hass, data))
    originals = [{"ent": name, "floor": "Property", "cords": [x, 50],
                  "updated": updated, "rms_m": 1,
                  "outdoor": {"observed": updated, "estimated_uncertainty_m": 3,
                              "receivers_used": 4}}
                 for name, x, updated in (("beacon_a", 500, now), ("beacon_b", 450, now - 29))]
    bps.apitricords = copy.deepcopy(originals)
    run(bps.update_tracker_groups(hass))
    first = copy.deepcopy(bps.apitricords[-1])
    assert first["zone"] == "Left"
    assert first["beacons_reporting"] == 2

    now += 3  # Older beacon expires; the newest observation is unchanged.
    run(bps.update_tracker_groups(hass))
    second = bps.apitricords[-1]
    assert second["zone"] == "Right"
    assert second["beacons_reporting"] == 1
    assert second["cords"] == [500, 50]
    assert second["updated"] == first["updated"] == 1000
    assert second["outdoor"]["observed"] == 1000
    assert second["outdoor"]["position_age_s"] == 3
    assert bps.apitricords[:2] == originals
    history = bps.get_position_history(hass)
    rows = [json.loads(line) for lines in history.drain_pending().values() for line in lines]
    assert [(row["t"], row["z"]) for row in rows] == [(1000, "Left"), (1003, "Right")]
    assert rows[-1]["x"] == 50
    assert history.query("bps_group_rover", 0, 2000, 100)["count"] == 2

    # Unchanged polling stays gated, and an expired group cannot add history.
    now += 3
    run(bps.update_tracker_groups(hass))
    assert history.pending_count() == 0
    now = 1031
    run(bps.update_tracker_groups(hass))
    assert not any(p.get("group") for p in bps.apitricords)
    assert history.pending_count() == 0


def test_generic_timeout_preserves_valid_groups_and_fusion_expires_them_once(hass, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(bps.time, "time", lambda: now)
    data = layout(True)
    data["position_timeout"] = 10
    data["reading_max_age"] = 30
    data["tracker_groups"] = [{"id": "rover", "beacons": ["beacon_a"]}]
    run(save_bps_data(hass, data))
    source = {"ent": "beacon_a", "floor": "Property", "cords": [50, 50],
              "updated": now, "outdoor": {"observed": now - 20, "estimated_uncertainty_m": 3}}
    bps.apitricords = [source]
    states_written = []
    sensor = SimpleNamespace(_state="unknown")
    sensor.async_write_ha_state = lambda: states_written.append(sensor._state)
    hass.data["bps_sensors"] = {"sensor.bps_group_rover_bps_floor": sensor}
    run(bps.update_tracker_groups(hass))
    group = copy.deepcopy(bps.apitricords[-1])
    assert group["updated"] == 980
    assert states_written == ["Property"]
    history = bps.get_position_history(hass)
    assert history.tracks["bps_group_rover"].force_gap is False

    # Ordinary trackers, including genuine group-prefixed beacons, still prune.
    bps.apitricords.append({"ent": "bps_group_real", "updated": 980})
    now += 3
    run(bps.prune_stale_positions(hass))
    assert bps.apitricords == [source, group]
    assert states_written == ["Property"]
    assert history.tracks["bps_group_rover"].force_gap is False
    run(bps.update_tracker_groups(hass))
    assert states_written == ["Property"]
    assert history.tracks["bps_group_rover"].force_gap is False
    assert history.query("bps_group_rover", 0, 2000, 100)["count"] == 1

    # A recent solve cannot extend the group's positive observation-age gate.
    now = 1011
    source["updated"] = now
    run(bps.prune_stale_positions(hass))
    assert any(p.get("group") for p in bps.apitricords)
    run(bps.update_tracker_groups(hass))
    assert bps.apitricords == [source]
    assert states_written == ["Property", "unknown"]
    assert history.tracks["bps_group_rover"].force_gap is True
    assert history.query("bps_group_rover", 0, 2000, 100)["count"] == 1


@pytest.mark.parametrize("members", [1, 2])
def test_group_entities_publish_changes_without_ticking_age_attributes(hass, monkeypatch, members):
    now = 1000.0
    monkeypatch.setattr(bps.time, "time", lambda: now)
    data = layout(True)
    names = ["beacon_a", "beacon_b"][:members]
    data["tracker_groups"] = [{"id": "rover", "beacons": names}]
    run(save_bps_data(hass, data))
    bps.apitricords = [{"ent": name, "floor": "Property", "cords": [0, 0],
                       "updated": now - i, "outdoor": {"observed": now - i,
                       "estimated_uncertainty_m": 3}}
                      for i, name in enumerate(names)]
    sensors = []
    for kind in ("zone", "floor"):
        sensor = SimpleNamespace(_state="unknown", _attrs={}, writes=0)
        def write(sensor=sensor):
            sensor.writes += 1
        sensor.async_write_ha_state = write
        hass.data.setdefault("bps_sensors", {})[f"sensor.bps_group_rover_bps_{kind}"] = sensor
        sensors.append(sensor)
    run(bps.update_tracker_groups(hass))
    assert all(sensor.writes == 1 for sensor in sensors)
    attrs = copy.deepcopy(sensors[0]._attrs)
    assert attrs["tracker_key"] == "bps_group_rover"
    assert attrs["zone"] == "unknown"
    assert "last_update_age_s" not in attrs
    assert "position_age_s" not in attrs["outdoor"]
    assert all("age_s" not in beacon for beacon in attrs["beacon_positions"])
    for now in range(1001, 1011):
        run(bps.update_tracker_groups(hass))
    assert all(sensor.writes == 1 and sensor._attrs == attrs for sensor in sensors)
    response = run(bps.BPSCordsAPI(hass).get(None))
    group = response.json_body[-1]
    assert group["outdoor"]["position_age_s"] == 10
    assert group["beacon_positions"][0]["age_s"] == 10

    # A new observation at the same coordinates still updates the entities.
    now = 1011
    bps.apitricords[0]["updated"] = now
    bps.apitricords[0]["outdoor"]["observed"] = now
    run(bps.update_tracker_groups(hass))
    assert all(sensor.writes == 2 and sensor._attrs["updated"] == now for sensor in sensors)
    if members == 2:
        now = 1030  # The older member expires without a new observation.
        run(bps.update_tracker_groups(hass))
        assert all(sensor.writes == 3 and sensor._attrs["beacons_reporting"] == 1 for sensor in sensors)
    now = 1042
    run(bps.update_tracker_groups(hass))
    writes = [sensor.writes for sensor in sensors]
    assert all(sensor._state == "unknown" and sensor._attrs == {} for sensor in sensors)
    now += 1
    run(bps.update_tracker_groups(hass))
    assert [sensor.writes for sensor in sensors] == writes


@pytest.mark.parametrize("delay", [0, 5])
def test_solver_delay_preserves_used_reading_time_and_group_freshness(hass, monkeypatch, delay):
    now = 1000.0
    monkeypatch.setattr(bps.time, "time", lambda: now)
    data = layout(True)
    data["tracker_groups"] = [{"id": "rover", "beacons": ["beacon_a"]}]
    data["floor"][0]["receivers"].append({"entity_id": "ignored", "cords": {"x": 200, "y": 200},
                                         "correction": 1.0, "outdoor_policy": "ignore"})
    run(save_bps_data(hass, data))
    def state(eid):
        timestamp = 999 if eid.endswith("_ignored") else 971
        return SimpleNamespace(state="7.0710678118654755", attributes={"unit_of_measurement": "m"},
                               last_updated=datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc))
    hass.states = SimpleNamespace(get=state)
    async def executor(func, *args):
        nonlocal now
        now += delay
        return func(*args)
    hass.async_add_executor_job = executor
    result, _ = solve(hass, data)
    assert result[0]["updated"] == 1000 + delay
    assert result[0]["outdoor"]["observed"] == 971
    run(bps.update_tracker_groups(hass))
    assert bool([p for p in bps.apitricords if p.get("group")]) is (delay == 0)
    response = run(bps.BPSCordsAPI(hass).get(None))
    quality = response.json_body[0]["outdoor"]
    assert quality["position_age_s"] == 29 + delay
    assert quality["stale"] is (delay == 5)


def test_first_real_beacon_solve_replaces_published_colliding_group(hass):
    data = layout(True)
    data["tracker_groups"] = [{"id": "rover", "beacons": ["beacon_a"]}]
    run(save_bps_data(hass, data))
    states(hass)
    solve(hass, data)
    run(bps.update_tracker_groups(hass))
    assert bps.apitricords[-1]["ent"] == "bps_group_rover"
    assert bps.apitricords[-1]["group"] is True
    bps.tracked_entities = ["sensor.bps_group_rover_distance_to_p0"]
    item = {"entity": "bps_group_rover", "data": copy.deepcopy(data)}
    run(bps.process_single_entity(hass, [item], item))
    real = copy.deepcopy(bps.apitricords[-1])
    assert real["ent"] == "bps_group_rover"
    assert not real.get("group")
    for field in ("name", "beacon_positions", "beacons_reporting", "fusion_confidence"):
        assert field not in real
    assert "radii" in real and "raw" in real
    run(bps.update_tracker_groups(hass))
    assert bps.apitricords[-1] == real
    assert [p["ent"] for p in bps.apitricords] == ["beacon_a", "bps_group_rover"]
    response = run(bps.BPSCordsAPI(hass).get(None))
    assert response.json_body[-1]["cords"] == real["cords"]
    assert not response.json_body[-1].get("group")


def test_group_collision_with_unsolved_discovered_beacon(hass):
    data = layout(True)
    data["tracker_groups"] = [{"id": "rover", "beacons": ["beacon_a"]}]
    run(save_bps_data(hass, data))
    bps.tracked_entities = ["sensor.bps_group_rover_distance_to_p0"]
    bps.apitricords = [{"ent": "beacon_a", "floor": "Property", "cords": [50, 50],
                       "updated": bps.time.time(), "outdoor": {"estimated_uncertainty_m": 3}}]
    run(bps.update_tracker_groups(hass))
    assert len(bps.apitricords) == 1


@pytest.mark.parametrize("cutoff, age, expected", [(5, 10, False), (90, 40, True)])
def test_group_runtime_uses_configured_measurement_freshness(hass, cutoff, age, expected):
    data = layout(True)
    data["reading_max_age"] = cutoff
    data["tracker_groups"] = [{"id": "rover", "beacons": ["beacon_a"]}]
    run(save_bps_data(hass, data))
    now = bps.time.time()
    bps.apitricords = [{"ent": "beacon_a", "floor": "Property", "cords": [50, 50],
                       "updated": now, "outdoor": {"observed": now - age,
                       "stale_after_s": cutoff, "estimated_uncertainty_m": 3}}]
    run(bps.update_tracker_groups(hass))
    groups = [p for p in bps.apitricords if p.get("group")]
    assert bool(groups) is expected
    if groups:
        assert groups[0]["outdoor"]["stale_after_s"] == cutoff
        response = run(bps.BPSCordsAPI(hass).get(None))
        assert response.json_body[-1]["outdoor"]["stale"] is False


@pytest.mark.parametrize("timeout, solve_age, observation_age, expected", [
    (None, 45, 45, True), (None, 301, 301, False),
    (120, 45, 45, True), (120, 121, 121, False), (120, 10, 1000, True),
])
def test_disabled_reading_gate_groups_follow_source_position_timeout(
        hass, monkeypatch, timeout, solve_age, observation_age, expected):
    now = 2000.0
    monkeypatch.setattr(bps.time, "time", lambda: now)
    data = layout(True)
    data["reading_max_age"] = 0
    if timeout is not None:
        data["position_timeout"] = timeout
    data["tracker_groups"] = [{"id": "rover", "beacons": ["beacon_a"]}]
    run(save_bps_data(hass, data))
    source = {"ent": "beacon_a", "floor": "Property", "cords": [50, 50],
              "updated": now - solve_age, "outdoor": {"observed": now - observation_age,
              "estimated_uncertainty_m": 3}}
    bps.apitricords = [copy.deepcopy(source)]
    sensor = SimpleNamespace(_state="Property", async_write_ha_state=lambda: None)
    hass.data["bps_sensors"] = {"sensor.bps_group_rover_bps_floor": sensor}
    run(bps.update_tracker_groups(hass))
    groups = [p for p in bps.apitricords if p.get("group")]
    assert bool(groups) is expected
    assert bps.apitricords[0] == source
    assert sensor._state == ("Property" if expected else "unknown")
    if groups:
        assert groups[0]["updated"] == source["updated"]
        assert groups[0]["outdoor"]["stale_after_s"] == (timeout or bps.STALE_POSITION_SECS)
        response = run(bps.BPSCordsAPI(hass).get(None))
        assert response.json_body[-1]["outdoor"]["stale"] is False


def test_ignored_receivers_do_not_consume_candidate_floor_slots(hass):
    states(hass)
    data = layout(True)
    good = copy.deepcopy(data["floor"][0])
    good["name"] = "Good"
    bad = []
    for i in range(bps.FLOOR_CANDIDATES):
        floor = copy.deepcopy(good)
        floor["name"] = f"Bad{i}"
        floor["receivers"] = floor["receivers"][:3]
        floor["receivers"][0]["outdoor_policy"] = "ignore"
        bad.append(floor)
    data["floor"] = bad + [good]
    result, _ = solve(hass, data)
    assert result[0]["floor"] == "Good"


def test_ignored_receiver_not_counted_in_fused_diagnostics(hass):
    states(hass)
    data = layout(True)
    data["tracker_groups"] = [{"id": "rover", "beacons": ["beacon_a"]}]
    data["floor"][0]["receivers"][0]["outdoor_policy"] = "ignore"
    run(save_bps_data(hass, data))
    source = solve(hass, data)[0][0]
    assert source["outdoor"]["receivers_used"] == 3
    excluded = [d for d in source["outdoor"]["receiver_diagnostics"] if d["status"] == "excluded"]
    assert excluded and all(d["used"] is False for d in excluded)
    run(bps.update_tracker_groups(hass))
    assert bps.apitricords[-1]["outdoor"]["receivers_used"] == 3


def test_nonfinite_age_configuration_does_not_enter_json(hass):
    states(hass)
    data = layout(True)
    data["reading_max_age"] = float("inf")
    result, _ = solve(hass, data)
    json.dumps(result, allow_nan=False)


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
