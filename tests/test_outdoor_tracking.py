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
from bps.outdoor_tracking import solve_outdoor, account_for_published_position, receiver_accuracy
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
    assert quality["pre_publication_uncertainty_m"] == 2
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


@pytest.mark.parametrize("count", [1, 2])
def test_sparse_live_tracking_marks_direction_unknown_and_never_records_position(hass, monkeypatch, count):
    now = 1000.0
    monkeypatch.setattr(bps.time, "time", lambda: now)
    data = layout(True)
    data["reading_max_age"] = 0
    data["floor"][0]["receivers"] = data["floor"][0]["receivers"][:count]
    published_states = {}
    monkeypatch.setattr(bps, "update_bps_sensor_state", lambda hass, eid, value, *a, **kw:
                        published_states.update({eid: value}))
    states(hass, "5", age=2)
    result, _ = solve(hass, data)
    assert len(result) == 1
    fix = result[0]
    assert fix["cords"] in [[0, 0], [100, 0]]
    assert fix["outdoor"]["detection_only"] is True
    assert fix["outdoor"]["direction_known"] is False
    assert fix["updated"] == 998 and fix["outdoor"]["stale_after_s"] == 30
    assert fix["rms_m"] is None and fix["conf"] == 0
    assert fix["zone"] == "unknown"
    assert published_states["sensor.beacon_a_bps_nearest_zone"] == "unknown"
    assert "beacon_a" not in bps._kf_position_state
    assert bps.get_position_history(hass).pending_count() == 0
    now += 30
    response = run(bps.BPSCordsAPI(hass).get(None)).json_body[0]
    assert response["outdoor"]["stale"] is True
    assert response["updated"] == 998
    # Re-solving old source data cannot rejuvenate it, even with age gating off.
    result, _ = solve(hass, data)
    assert result[0]["updated"] == 998


def test_legacy_indoor_tracking_still_needs_three_receivers(hass):
    data = layout(False)
    data["floor"][0]["receivers"] = data["floor"][0]["receivers"][:2]
    states(hass, "5")
    result, _ = solve(hass, data)
    assert result == []


@pytest.mark.parametrize("count", [0, 1, 2])
@pytest.mark.parametrize("enabled", [False, True])
def test_background_loop_reaches_sparse_tracking_only_when_outdoor_enabled(hass, monkeypatch, count, enabled):
    now = 1000.0
    monkeypatch.setattr(bps.time, "time", lambda: now)
    data = layout(enabled)
    data["floor"][0]["receivers"] = data["floor"][0]["receivers"][:count]
    run(save_bps_data(hass, data))
    states(hass, "5", age=2)
    ids = [f"sensor.beacon_a_distance_to_p{i}" for i in range(count)]
    monkeypatch.setattr(bps, "_bermuda_distance_sensor_ids", lambda hass: ids)
    for name in ("last_liveness", "last_selftest", "last_history_flush"):
        monkeypatch.setattr(bps.update_tracked_entities, name, now, raising=False)
    delays = []
    async def stop_after_cycle(delay):
        delays.append(delay)
        raise asyncio.CancelledError
    monkeypatch.setattr(bps.asyncio, "sleep", stop_after_cycle)
    with pytest.raises(asyncio.CancelledError):
        run(bps.update_tracked_entities(hass))
    if enabled and count:
        assert len(bps.apitricords) == 1
        assert bps.apitricords[0]["outdoor"]["detection_only"] is True
        assert bps.apitricords[0]["updated"] == 998
        assert delays == [bps.secToUpdate]
    else:
        assert bps.apitricords == []
        assert delays == [10]


def test_outdoor_fix_can_be_outside_receiver_extent_and_zone_union(hass):
    data = layout(True)
    truth = (300.0, 200.0)
    distances = {"sensor.beacon_a_distance_to_" + r["entity_id"]:
        str(math.dist(truth, (r["cords"]["x"], r["cords"]["y"])) / 10)
        for r in data["floor"][0]["receivers"]}
    timestamp = datetime.datetime.fromtimestamp(bps.time.time(), datetime.timezone.utc)
    hass.states = SimpleNamespace(get=lambda eid: SimpleNamespace(
        state=distances[eid], attributes={"unit_of_measurement": "m"}, last_updated=timestamp))
    result, _ = solve(hass, data)
    assert result[0]["cords"] == pytest.approx(truth, abs=0.01)
    assert result[0]["outdoor"].get("detection_only") is not True
    assert result[0]["outdoor"]["display_radius_limit_m"] > 0


def test_sparse_group_uses_source_expiry_without_false_history_or_zone(hass, monkeypatch):
    now = 1000.0
    monkeypatch.setattr(bps.time, "time", lambda: now)
    data = layout(True)
    data["reading_max_age"] = 0
    data["tracker_groups"] = [{"id": "rover", "beacons": ["beacon_a"]}]
    data["floor"][0]["receivers"] = data["floor"][0]["receivers"][:1]
    states(hass, "5", age=2)
    run(save_bps_data(hass, data))
    solve(hass, data)
    run(bps.update_tracker_groups(hass))
    group = bps.apitricords[-1]
    assert group["group"] and group["outdoor"]["detection_only"]
    assert group["outdoor"]["stale_after_s"] == 30
    assert group["zone"] == group["nearest_zone"] == "unknown"
    assert bps.get_position_history(hass).pending_count() == 0
    now += 30
    run(bps.update_tracker_groups(hass))
    assert all(not row.get("group") for row in bps.apitricords)


@pytest.mark.parametrize("timeout,expected", [(None, 300), (120, 120), (0, 300),
    (-1, 300), (float("inf"), 300)])
def test_disabled_reading_gate_ordinary_fixes_age_by_last_solve(hass, monkeypatch, timeout, expected):
    now = 2000.0
    monkeypatch.setattr(bps.time, "time", lambda: now)
    data = layout(True)
    data["reading_max_age"] = 0
    if timeout is not None:
        data["position_timeout"] = timeout
    states(hass, age=1000)
    result, _ = solve(hass, data)
    assert len(result) == 1
    source = result[0]
    assert source["outdoor"]["observed"] == 1000, 'measurement clock remains truthful'
    assert source["outdoor"]["use_observation_age"] is False
    assert source["outdoor"]["stale_after_s"] == expected
    for elapsed, stale in [(0, False), (expected - 1, False), (expected, True)]:
        now = 2000 + elapsed
        response = run(bps.BPSCordsAPI(hass).get(None)).json_body[0]
        assert response["outdoor"]["stale"] is stale
        assert response["outdoor"]["position_age_s"] == elapsed
        assert response["outdoor"]["observed"] == 1000
        assert response["updated"] == 2000, 'polling cannot refresh the solve clock'


def test_enabled_reading_gate_ordinary_fix_ages_by_measurement(hass, monkeypatch):
    now = 2000.0
    monkeypatch.setattr(bps.time, "time", lambda: now)
    data = layout(True)
    data.update(reading_max_age=90, position_timeout=300)
    states(hass, age=45)
    result, _ = solve(hass, data)
    assert result[0]["outdoor"]["use_observation_age"] is True
    now += 45
    response = run(bps.BPSCordsAPI(hass).get(None)).json_body[0]
    assert response["outdoor"]["position_age_s"] == 90
    assert response["outdoor"]["stale"] is True
    assert response["outdoor"]["stale_after_s"] == 90


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


@pytest.mark.parametrize("accuracy", [None, True, False, "2", {}, [], -1,
                                      float("nan"), float("inf"), 1001, 10 ** 1000])
def test_absent_or_malformed_receiver_accuracy_preserves_weights_and_fix(accuracy):
    floor = layout(True)["floor"][0]
    weighted = points(floor, liar=True)
    baseline = solve_indoor(floor, weighted)
    floor["receivers"][3]["distance_accuracy_m"] = accuracy
    calls = []
    def solver(pts, **kwargs):
        calls.append(pts)
        return bps.trilaterate(pts, **kwargs)
    result = solve_indoor(floor, weighted, solver)
    assert len(calls) == 1
    assert result["fix"] == baseline["fix"]
    assert result["weighted"] == baseline["weighted"]
    diag = result["outdoor"]["receiver_diagnostics"][3]
    assert diag["receiver_accuracy_m"] is None
    assert diag["accuracy_weight"] == 1
    assert diag["base_jump_weight"] == 1


@pytest.mark.parametrize("slant,accuracy,expected", [(2, 0, 1), (2, 2, 0.5),
    (4, 2, 0.8), (0.1, 0.5, 0.5), (0, 0.5, 0.5), (2, 1000, 0.05)])
def test_accuracy_factor_matches_additive_range_error_variance(slant, accuracy, expected):
    result = receiver_accuracy({"distance_accuracy_m": accuracy}, slant)
    assert result["receiver_accuracy_m"] == accuracy
    assert result["accuracy_weight"] == pytest.approx(expected)
    json.dumps(result, allow_nan=False)


def test_noisy_receiver_has_less_influence_than_accurate_receivers():
    floor = layout(True)["floor"][0]
    weighted = points(floor, liar=True)
    baseline = solve_indoor(floor, weighted)
    for receiver in floor["receivers"]:
        receiver["distance_accuracy_m"] = 0
    floor["receivers"][3]["distance_accuracy_m"] = 10
    saved = copy.deepcopy(floor)
    result = solve_indoor(floor, weighted)
    assert floor == saved
    assert math.dist(result["fix"], (50, 50)) < math.dist(baseline["fix"], (50, 50)) / 2
    assert all(a[:3] == b[:3] and a[4] == b[4] for a, b in zip(result["weighted"], weighted))
    diagnostic = result["outdoor"]["receiver_diagnostics"][3]
    assert diagnostic["receiver_accuracy_m"] == 10
    assert diagnostic["accuracy_weight"] == 0.05
    assert diagnostic["reliability_weight"] == 0.05
    assert abs(diagnostic["horizontal_residual_m"]) > 2, 'the conflicting range stays visible'
    assert diagnostic["corrected_distance_m"] == floor["receivers"][3]["distance"]


def test_accuracy_is_applied_once_in_both_solves_with_path_freshness_and_jump_trust():
    floor = layout(True)["floor"][0]
    weighted = points(floor)
    weighted[0] = (*weighted[0][:3], 0.8, weighted[0][4])
    floor["receivers"][0]["distance_accuracy_m"] = weighted[0][4] / 10
    floor["receivers"][0]["_outdoor_reading"]["reading_age_s"] = 20
    floor["environment"] = [{"id": "shop", "type": "building", "material": "metal", "points": [
        {"x": x, "y": y} for x, y in [(10, 10), (40, 10), (40, 40), (10, 40)]]}]
    calls = []
    def solver(pts, **kwargs):
        calls.append(copy.deepcopy(pts))
        return (50, 50)
    result = solve_indoor(floor, weighted, solver)
    expected = 0.8 * 0.5 * 0.5 ** 2 * (1.5 - 20 / 30)
    assert len(calls) == 2
    assert calls[0][0][3] == 0.8 * 0.5
    assert calls[1][0][3] == pytest.approx(expected)
    assert result["weighted"][0][3] == pytest.approx(expected)
    diagnostic = result["outdoor"]["receiver_diagnostics"][0]
    assert diagnostic["base_jump_weight"] == 0.8
    assert diagnostic["accuracy_weight"] == 0.5
    assert diagnostic["environmental_weight"] == 0.25


def test_accuracy_uses_measured_slant_not_height_projected_horizontal_range():
    floor, weighted = indoor_case()
    floor["receivers"][0]["distance_accuracy_m"] = 2
    floor["receivers"][0]["height"] = 6
    weighted[0] = (*weighted[0][:2], 5, 0.5, 20)
    result = solve_indoor(floor, weighted)
    diagnostic = result["outdoor"]["receiver_diagnostics"][0]
    assert diagnostic["accuracy_weight"] == 0.5
    assert diagnostic["base_jump_weight"] == 0.5
    assert diagnostic["indoor_support_strength"] == 0.5


@pytest.mark.parametrize("accuracy,expected_support", [(0.1, True), (3, False), (1000, False)])
def test_noisy_close_reading_cannot_supply_authoritative_indoor_support(accuracy, expected_support):
    floor, weighted = indoor_case()
    floor["receivers"][0]["distance_accuracy_m"] = accuracy
    result = solve_indoor(floor, weighted)
    assert bool(result["outdoor"]["indoor_support_receivers"]) is expected_support
    if not expected_support:
        assert all(d["indoor_range_weight"] == 1 for d in result["outdoor"]["receiver_diagnostics"])


def test_weak_jump_weights_are_not_inflated_by_accuracy_floor():
    floor = layout(True)["floor"][0]
    weighted = points(floor)
    weighted[0] = (*weighted[0][:3], 0.001, weighted[0][4])
    floor["receivers"][0]["distance_accuracy_m"] = 1000
    result = solve_indoor(floor, weighted)
    assert result["weighted"][0][3] == pytest.approx(0.001 * 0.05)


@pytest.mark.parametrize("failure", ["geometry", "refinement"])
def test_accuracy_weight_survives_geometry_fallback_and_refinement_failure(monkeypatch, failure):
    import bps.outdoor_tracking as O
    floor = layout(True)["floor"][0]
    weighted = points(floor)
    floor["receivers"][0]["distance_accuracy_m"] = weighted[0][4] / 10
    floor["environment"] = [{"id": "shop", "type": "building", "material": "metal", "points": [
        {"x": x, "y": y} for x, y in [(10, 10), (40, 10), (40, 40), (10, 40)]]}]
    calls = []
    def solver(pts, **kwargs):
        calls.append(pts)
        return (50, 50) if len(calls) == 1 else None
    if failure == "geometry":
        def fail(*args):
            raise ArithmeticError("injected compilation failure")
        monkeypatch.setattr(O, "compile_environment", fail)
    result = solve_indoor(floor, weighted, solver)
    assert result["weighted"][0][3] == 0.5
    diagnostic = result["outdoor"]["receiver_diagnostics"][0]
    assert diagnostic["accuracy_weight"] == 0.5
    assert diagnostic["reliability_weight"] == 0.5
    if failure == "geometry":
        assert result["outdoor"]["environment_fallback"] is True
    else:
        assert diagnostic["status"] == "refinement_failed"


def indoor_case(near_range=2.0, near_xy=(50, 50), near_weight=0.5):
    """A short indoor range and three incompatible distant scanner ranges."""
    floor = {"name": "Ground", "scale": 10, "receivers": [], "zones": [], "environment": [
        {"id": "shop", "type": "building", "material": "metal", "points": [
            {"x": x, "y": y} for x, y in [(0, 0), (100, 0), (100, 100), (0, 100)]]}]}
    weighted = []
    for i, (xy, distance) in enumerate(zip(
            [near_xy, (400, 0), (400, 150), (300, 300)], [near_range, 7, 7, 7])):
        x, y = xy
        radius = distance * floor["scale"]
        floor["receivers"].append({"entity_id": f"p{i}", "distance": distance,
            "cords": {"x": x, "y": y, "r": radius}, "correction": 1.0,
            "_outdoor_reading": {"measured_distance_m": distance, "reading_age_s": 0}})
        weighted.append((x, y, radius, near_weight if i == 0 else 1.0, radius))
    return floor, weighted


def solve_indoor(floor, weighted, solver=bps.trilaterate, max_age=30):
    return solve_outdoor(floor, weighted, (-100, -100, 500, 500), 10, 5,
                         max_age, None, solver)


@pytest.mark.parametrize("distance", [1.0, 2.0, 3.0])
def test_fresh_close_indoor_receiver_softly_resists_far_inconsistent_ranges(distance):
    floor, weighted = indoor_case(near_range=distance)
    saved = copy.deepcopy(floor)
    original = bps.trilaterate(weighted, bounds=(-100, -100, 500, 500), min_weight_radius=5)
    calls = []
    def solver(pts, **kwargs):
        calls.append(pts)
        return bps.trilaterate(pts, **kwargs)
    result = solve_indoor(floor, weighted, solver)
    info = result["outdoor"]
    assert floor == saved
    assert len(calls) == 2
    assert info["indoor_support_building"] == "shop"
    assert info["indoor_support_receivers"] == 1
    assert info["indoor_support_strength"] == 1
    assert info["indoor_support_applied"] is True
    assert info["indoor_support_conflict"] is False
    assert result["weighted"][0][3] == weighted[0][3], 'support preserves original jump trust'
    assert all(a[:3] == b[:3] and a[4] == b[4] for a, b in zip(result["weighted"], weighted))
    assert all(0.25 <= d["indoor_range_weight"] < 1 for d in info["receiver_diagnostics"][1:])
    assert all(d["indoor_range_disagreement_m"] > 15 for d in info["receiver_diagnostics"][1:])
    assert all(abs(d["horizontal_residual_m"]) > 15 for d in info["receiver_diagnostics"][1:])
    assert math.dist(result["fix"], (50, 50)) <= math.dist(original, (50, 50))
    if distance >= 2:
        assert 0 < result["fix"][0] < 100 and 0 < result["fix"][1] < 100
    assert result["fix"] != (50, 50), 'the beacon is never snapped onto its receiver'
    json.dumps(result, allow_nan=False)


def test_indoor_support_restores_only_own_building_and_preserves_path_evidence():
    floor, weighted = indoor_case()
    floor["environment"].extend([
        {"id": "other", "type": "building", "material": "wood", "points": [
            {"x": x, "y": y} for x, y in [(180, 0), (200, 0), (200, 100), (180, 100)]]},
        {"id": "trees", "type": "dense_trees", "points": [
            {"x": x, "y": y} for x, y in [(110, 0), (150, 0), (150, 100), (110, 100)]]},
    ])
    result = solve_indoor(floor, weighted, lambda *a, **kw: (350, 50))
    diag = result["outdoor"]["receiver_diagnostics"][0]
    assert diag["building_crossings"] == 3
    assert diag["reflection_risk"] is True
    assert diag["environmental_weight"] == pytest.approx(0.9 ** 2 * 0.75)
    assert diag["reliability_weight"] == pytest.approx(0.5 * 0.9 ** 2 * 0.75)
    assert diag["indoor_boundary_restoration"] > 0
    assert diag["intersections"][0]["weight"] == 0.5, 'raw obstruction evidence is retained'
    assert result["fix"] == (350, 50), 'mapped evidence cannot force a fix indoors'


def test_indoor_restoration_preserves_fence_penalty_and_crossing_summary():
    floor, weighted = indoor_case()
    floor["environment"].append({"id": "fence", "type": "fence", "material": "metal",
        "thickness": "thin", "points": [{"x": 150, "y": 0}, {"x": 150, "y": 100}]})
    result = solve_indoor(floor, weighted, lambda *a, **kw: (180, 80))
    diagnostic = result["outdoor"]["receiver_diagnostics"][0]
    assert diagnostic["indoor_support_strength"] == 1
    assert diagnostic["environmental_weight"] == 0.875
    assert diagnostic["reliability_weight"] == 0.5 * 0.875
    assert diagnostic["fence_crossings"] == 1
    assert result["outdoor"]["fence_paths"] >= 1
    assert result["outdoor"]["fence_crossings"] >= 1


def test_fence_region_never_supplies_indoor_receiver_association():
    floor, weighted = indoor_case()
    floor["environment"][0].update(type="fence", thickness="solid")
    result = solve_indoor(floor, weighted)
    assert result["outdoor"]["indoor_support_receivers"] == 0
    assert result["outdoor"]["receiver_diagnostics"][0]["indoor_building"] is None


@pytest.mark.parametrize("xy,expected_strength", [((0, 50), 0), ((-1, 50), 0), ((1, 50), 0.04)])
def test_boundary_receiver_is_not_strong_indoor_evidence(xy, expected_strength):
    floor, weighted = indoor_case(near_xy=xy)
    result = solve_indoor(floor, weighted, lambda *a, **kw: (180, 80))
    diag = result["outdoor"]["receiver_diagnostics"][0]
    assert diag["indoor_support_strength"] == pytest.approx(expected_strength)
    if expected_strength == 0:
        assert diag["indoor_building"] is None
        assert all(d["indoor_range_weight"] == 1 for d in result["outdoor"]["receiver_diagnostics"])
    else:
        assert diag["indoor_boundary_restoration"] == pytest.approx(0.5 * expected_strength)


@pytest.mark.parametrize("age,max_age", [(None, 30), (16, 30), (10, 10), (100, 0)])
def test_stale_or_unknown_age_does_not_strengthen_indoor_association(age, max_age):
    floor, weighted = indoor_case()
    floor["receivers"][0]["_outdoor_reading"]["reading_age_s"] = age
    result = solve_indoor(floor, weighted, max_age=max_age)
    assert result["outdoor"]["indoor_support_receivers"] == 0
    assert all(d.get("indoor_range_weight", 1) == 1 for d in result["outdoor"]["receiver_diagnostics"])


@pytest.mark.parametrize("policy", ["normal", "deprioritize", "ignore"])
def test_indoor_evidence_respects_manual_receiver_policy(policy):
    floor, weighted = indoor_case()
    floor["receivers"][0]["outdoor_policy"] = policy
    result = solve_indoor(floor, weighted)
    assert result["outdoor"]["indoor_support_receivers"] == 0


@pytest.mark.parametrize("raw,slant,weight", [(9, 2, 1), (2, 9, 1), (2, 2, 0.01)])
def test_height_projection_calibration_or_weak_jump_cannot_create_close_indoor_evidence(raw, slant, weight):
    floor, weighted = indoor_case()
    floor["receivers"][0]["height"] = 6  # Deliberately wrong units; never infer a vertical leg here.
    floor["receivers"][0]["distance"] = slant
    floor["receivers"][0]["_outdoor_reading"]["measured_distance_m"] = raw
    weighted[0] = (*weighted[0][:2], 5, weight, slant * 10)
    result = solve_indoor(floor, weighted)
    assert result["outdoor"]["indoor_support_receivers"] == 0
    assert all(d["indoor_range_weight"] == 1 for d in result["outdoor"]["receiver_diagnostics"])


def test_other_floor_building_cannot_strengthen_this_floor(hass):
    floor, _weighted = indoor_case()
    upstairs = copy.deepcopy(floor)
    upstairs["name"] = "Upstairs"
    floor["environment"] = []
    for rec in upstairs["receivers"]:
        rec["entity_id"] = "upper_" + rec["entity_id"]
    def state(eid):
        receiver = next(rec for rec in floor["receivers"] if rec["entity_id"] in eid)
        xy = receiver["cords"]["x"], receiver["cords"]["y"]
        reading = 90 if "upper_" in eid else math.dist(xy, (70, 50)) / 10
        return SimpleNamespace(state=str(reading), attributes={"unit_of_measurement": "m"},
            last_updated=datetime.datetime.now(datetime.timezone.utc))
    hass.states = SimpleNamespace(get=state)
    result, _item = solve(hass, {"floor": [floor, upstairs], "outdoor_tracking": {"enabled": True}})
    assert result[0]["floor"] == "Ground"
    assert result[0]["outdoor"]["indoor_support_receivers"] == 0
    assert all(d["indoor_building"] is None for d in result[0]["outdoor"]["receiver_diagnostics"])


def test_conflicting_close_receivers_in_nearby_buildings_disable_indoor_support():
    from bps.environment import measurement_reliability
    floor, weighted = indoor_case()
    floor["environment"][0]["points"] = [{"x": x, "y": y} for x, y in [(0, 0), (60, 0), (60, 100), (0, 100)]]
    floor["environment"].append({"id": "next_door", "type": "building", "points": [
        {"x": x, "y": y} for x, y in [(60, 0), (120, 0), (120, 100), (60, 100)]]})
    floor["receivers"][1].update(distance=2, cords={"x": 70, "y": 50, "r": 20},
                                _outdoor_reading={"measured_distance_m": 2, "reading_age_s": 0})
    weighted[1] = (70, 50, 20, 1, 20)
    fix = (180, 80)
    result = solve_indoor(floor, weighted, lambda *a, **kw: fix)
    quality = result["outdoor"]
    assert quality["indoor_support_conflict"] is True
    assert quality["confidence"] == "poor"
    assert quality["indoor_support_receivers"] == 0
    for index, diag in enumerate(quality["receiver_diagnostics"]):
        ordinary = measurement_reliability(compile_environment(floor), weighted[index][:2], fix,
            base_weight=weighted[index][3], age_s=0)
        assert diag["reliability_weight"] == ordinary["reliability_weight"]
        assert diag["indoor_range_weight"] == 1
        assert diag["indoor_support_conflict"] is (index < 2)


def test_incompatible_close_ranges_within_same_building_disable_support():
    floor, weighted = indoor_case(near_range=1)
    floor["receivers"][1].update(distance=1, cords={"x": 90, "y": 50, "r": 10},
                                _outdoor_reading={"measured_distance_m": 1, "reading_age_s": 0})
    weighted[1] = (90, 50, 10, 1, 10)
    assert solve_indoor(floor, weighted)["outdoor"]["indoor_support_conflict"] is True


def test_consistent_far_ranges_are_not_suppressed_by_indoor_support():
    floor, weighted = indoor_case()
    truth = (70, 50)
    for index in range(1, len(weighted)):
        pt = weighted[index]
        radius = math.dist(pt[:2], truth)
        weighted[index] = (*pt[:2], radius, pt[3], radius)
        floor["receivers"][index]["distance"] = radius / 10
        floor["receivers"][index]["_outdoor_reading"]["measured_distance_m"] = radius / 10
    result = solve_indoor(floor, weighted)
    assert all(d["indoor_range_weight"] == 1 for d in result["outdoor"]["receiver_diagnostics"])
    assert result["fix"] == pytest.approx(truth, abs=1e-5)


def test_indoor_refinement_failure_reports_original_weights():
    floor, weighted = indoor_case()
    calls = []
    def solver(pts, **kwargs):
        calls.append(pts)
        return (180, 80) if len(calls) == 1 else None
    result = solve_indoor(floor, weighted, solver)
    assert result["fix"] == (180, 80)
    assert result["weighted"] == weighted
    assert result["outdoor"]["indoor_support_applied"] is False
    assert all(d["reliability_weight"] == pt[3] and d["status"] == "refinement_failed"
               for pt, d in zip(weighted, result["outdoor"]["receiver_diagnostics"]))


def test_indoor_association_geometry_failure_is_neutral_and_poor(monkeypatch):
    import bps.outdoor_tracking as O
    floor, weighted = indoor_case()
    def fail(*args):
        raise ArithmeticError("injected clearance failure")
    monkeypatch.setattr(O, "_boundary_clearance", fail)
    result = solve_indoor(floor, weighted)
    assert result["weighted"] == weighted
    assert result["outdoor"]["confidence"] == "poor"
    assert result["outdoor"]["environment_fallback"] is True
    assert result["outdoor"]["indoor_support_receivers"] == 0
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("failure", ["compilation", "path"])
def test_geometry_failure_keeps_range_fix_policies_and_measurements(hass, monkeypatch, failure):
    from bps import environment as E, outdoor_tracking as O
    floor = layout(True)["floor"][0]
    weighted = points(floor)
    before = copy.deepcopy(floor)
    def fail(*args):
        raise ArithmeticError("injected geometry failure")
    if failure == "compilation":
        monkeypatch.setattr(O, "compile_environment", fail)
    else:
        monkeypatch.setattr(E, "interior_path", fail)
        floor["environment"] = [{"id": "shop", "type": "building", "points": [
            {"x": 20, "y": 20}, {"x": 80, "y": 20}, {"x": 80, "y": 80}, {"x": 20, "y": 80}]}]
        before = copy.deepcopy(floor)
    result = solve_outdoor(floor, weighted, (-10, -10, 110, 110), 10, 5, 30, None, bps.trilaterate)
    assert result["fix"] == pytest.approx((50, 50))
    assert result["weighted"] == weighted
    assert result["outdoor"]["confidence"] == "poor"
    assert result["outdoor"]["environment_fallback"] is True
    assert result["outdoor"]["environment_error_receivers"] == 4
    assert all(d["environmental_weight"] == 1 for d in result["outdoor"]["receiver_diagnostics"])
    assert floor == before
    assert math.isfinite(result["outdoor"]["estimated_uncertainty_m"])
    # Failed geometry does not revive explicit stale observations.
    for rec in floor["receivers"]:
        rec["_outdoor_reading"]["reading_age_s"] = 31
    assert solve_outdoor(floor, weighted, (-10, -10, 110, 110), 10, 5, 30, None, bps.trilaterate) is None


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


@pytest.mark.parametrize("edit", [
    lambda d: d.update(tracker_hardware={"beacon_a": {"notes": "x" * 257}}),
    lambda d: d.update(tracker_hardware={"beacon_a": {"rssi_offset": -9}}),
    lambda d: d["floor"][0]["receivers"][0].update(height_unit="yards"),
    lambda d: d["floor"][0]["receivers"][0].update(distance_accuracy_m=True),
    lambda d: d["floor"][0]["receivers"][0].update(distance_accuracy_m=float("nan")),
    lambda d: d["floor"][0].update(environment=[{"id": "f", "name": "Fence",
        "type": "fence", "material": "metal", "thickness": "wide",
        "points": [{"x": 0, "y": 0}, {"x": 100, "y": 100}]}]),
])
def test_new_optional_fields_reject_atomically(hass, tmp_path, edit):
    old = layout()
    run(save_bps_data(hass, old))
    bad = copy.deepcopy(old)
    edit(bad)
    response = run(bps.BPSSaveAPIText()._write_save(hass, str(tmp_path), {}, bad))
    assert response.status == 400
    assert hass._store_backing[STORAGE_KEY_LAYOUT] == old
    assert len(hass._store_saves) == 1


def test_two_point_fence_and_hardware_profiles_save_without_unit_migration(hass, tmp_path):
    data = layout(True)
    data["floor"][0]["environment"] = [{"id": "f", "name": "Fence",
        "type": "fence", "material": "metal", "thickness": "thin",
        "points": [{"x": 0, "y": 0}, {"x": 100, "y": 100}]}]
    data["floor"][0]["receivers"][0].update(height=1.524, height_unit="ft",
        distance_accuracy_m=2, hardware_model="Example scanner", antenna_notes="External omni")
    data["floor"][0]["receivers"][1]["height"] = 5
    data["tracker_hardware"] = {"beacon_a": {"hardware_model": "Example tag", "notes": "Collar"}}
    response = run(bps.BPSSaveAPIText()._write_save(hass, str(tmp_path), {}, data))
    assert response is None  # The write helper returns only an error response.
    hass.data["bps"].pop("layout")
    assert run(load_bps_data(hass)) == data


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


def test_save_and_runtime_accept_genuine_prefixed_beacon_only(hass, monkeypatch, tmp_path):
    data = layout(True)
    data["tracker_groups"] = [{"id": "rover", "beacons": ["bps_group_beacon", "beacon_b"]}]
    monkeypatch.setattr(bps, "_bermuda_distance_sensor_ids", lambda _hass: [
        "sensor.bps_group_beacon_distance_to_p0", "sensor.beacon_b_distance_to_p0"])
    response = run(bps.BPSSaveAPIText()._write_save(hass, str(tmp_path), {}, data))
    assert response is None
    now = bps.time.time()
    original = {"ent": "bps_group_beacon", "floor": "Property", "zone": "unknown",
                "cords": [50, 50], "updated": now, "rms_m": 1}
    bps.apitricords = [copy.deepcopy(original)]
    bps.tracked_entities = ["sensor.bps_group_beacon_distance_to_p0"]
    run(bps.update_tracker_groups(hass))
    assert bps.apitricords[0] == original
    assert bps.apitricords[1]["beacon_positions"][0]["ent"] == "bps_group_beacon"
    bad = copy.deepcopy(data)
    bad["tracker_groups"][0]["beacons"] = ["bps_group_rover"]
    response = run(bps.BPSSaveAPIText()._write_save(hass, str(tmp_path), {}, bad))
    assert response.status == 400
    assert hass._store_backing[STORAGE_KEY_LAYOUT] == data


@pytest.mark.parametrize("missing,name", [(True, None), (False, None), (False, False),
    (False, 3), (False, []), (False, {}), (False, ""), (False, " \t"), (False, "x" * 257)])
def test_malformed_polygon_names_rejected_before_any_store_or_map_write(hass, tmp_path, missing, name):
    from io import BytesIO
    old = layout()
    run(save_bps_data(hass, old))
    data = layout(True)
    region = {"id": "shop", "type": "building", "material": "metal", "name": name,
              "points": [{"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 0, "y": 100}]}
    if missing:
        region.pop("name")
    data["floor"][0]["environment"] = [region]
    upload = SimpleNamespace(filename="new.png", file=BytesIO(b"PNGDATA"))
    response = run(bps.BPSSaveAPIText()._write_save(
        hass, str(tmp_path), {"new_floor": "true", "file": upload}, data))
    assert response.status == 400
    assert validate_outdoor_layout(data) == "Environment polygon names must be nonempty text of at most 256 characters"
    assert hass._store_backing[STORAGE_KEY_LAYOUT] == old
    assert len(hass._store_saves) == 1 and not (tmp_path / "new.png").exists()


@pytest.mark.parametrize("name", ["S", "S" * 256])
def test_bounded_polygon_names_save_and_reload(hass, tmp_path, name):
    data = layout(True)
    data["floor"][0]["environment"] = [{"id": "shop", "name": name,
        "type": "building", "material": "metal", "points": [
            {"x": 0, "y": 0}, {"x": 100, "y": 0}, {"x": 0, "y": 100}]}]
    assert run(bps.BPSSaveAPIText()._write_save(hass, str(tmp_path), {}, data)) is None
    hass.data["bps"].pop("layout")
    assert run(load_bps_data(hass)) == data


@pytest.mark.parametrize("explicit_enabled", [False, True])
def test_save_rejects_output_collision_even_with_only_ordinary_members(hass, monkeypatch, tmp_path, explicit_enabled):
    from io import BytesIO
    data = layout(True)
    group = {"id": "rover", "name": "Rover", "beacons": ["beacon_a", "beacon_b"]}
    if explicit_enabled:
        group["enabled"] = True
    data["tracker_groups"] = [group]
    inventory = ["sensor.beacon_a_distance_to_p0", "sensor.beacon_b_distance_to_p0"]
    monkeypatch.setattr(bps, "_bermuda_distance_sensor_ids", lambda _hass: list(inventory))
    assert run(bps.BPSSaveAPIText()._write_save(hass, str(tmp_path), {}, data)) is None
    before = copy.deepcopy(hass._store_backing[STORAGE_KEY_LAYOUT])
    # A real beacon may appear after the editor loaded the earlier inventory.
    inventory.append("sensor.bps_group_rover_distance_to_p0")
    edited = copy.deepcopy(data)
    edited["tracker_groups"][0]["name"] = "Rover renamed"
    upload = SimpleNamespace(filename="new.png", file=BytesIO(b"PNGDATA"))
    response = run(bps.BPSSaveAPIText()._write_save(
        hass, str(tmp_path), {"new_floor": "true", "file": upload}, edited))
    assert response.status == 400 and "conflicts with a real tracker" in response.body_text
    assert hass._store_backing[STORAGE_KEY_LAYOUT] == before
    assert len(hass._store_saves) == 1 and not (tmp_path / "new.png").exists()
    # Retiring the saved conflict is still possible without touching the beacon.
    edited["tracker_groups"][0]["enabled"] = False
    assert run(bps.BPSSaveAPIText()._write_save(hass, str(tmp_path), {}, edited)) is None
    assert hass._store_backing[STORAGE_KEY_LAYOUT] == edited
    edited["tracker_groups"][0]["enabled"] = True
    assert run(bps.BPSSaveAPIText()._write_save(hass, str(tmp_path), {}, edited)).status == 400


@pytest.mark.parametrize('walls', [['metal'], ['metal', 'wood', 'metal', 5], 'metal'])
def test_reject_invalid_wall_materials_before_layout_save(walls):
    points = [{'x': 0, 'y': 0}, {'x': 10, 'y': 0}, {'x': 10, 'y': 10}, {'x': 0, 'y': 10}]
    data = {'floor': [{'environment': [{'id': 'shop', 'name': 'Shop', 'type': 'building',
        'material': 'metal', 'points': points, 'wall_materials': walls}]}]}
    assert validate_outdoor_layout(data)
    data['floor'][0]['environment'][0]['wall_materials'] = ['wood', None, 'metal', None]
    assert validate_outdoor_layout(data) is None


def test_primary_position_remains_usable_without_zones_then_expires(hass, monkeypatch):
    now = 1000.
    monkeypatch.setattr(bps.time, 'time', lambda: now)
    data = layout(True)
    data['tracker_groups'] = [{'id': 'rover', 'beacons': ['beacon_a']}]
    run(save_bps_data(hass, data))
    bps.apitricords = [{'ent': 'beacon_a', 'floor': 'Property', 'cords': [50, 60],
        'updated': now, 'outdoor': {'observed': now, 'estimated_uncertainty_m': 3}}]
    primary = SimpleNamespace(_state='unknown', _attrs={}, async_write_ha_state=lambda: None)
    hass.data['bps_sensors'] = {'sensor.bps_group_rover_bps_position': primary}
    run(bps.update_tracker_groups(hass))
    assert primary._state == 'tracking'
    assert primary._attrs['zone'] == 'unknown'
    assert primary._attrs['floor'] == 'Property'
    assert primary._attrs['cords'] == [50, 60]
    now += 31
    run(bps.update_tracker_groups(hass))
    assert primary._state == 'unknown'
    assert primary._attrs['tracker_key'] == 'bps_group_rover'
    assert 'cords' not in primary._attrs
