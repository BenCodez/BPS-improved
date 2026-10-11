"""Synthetic metre-based evidence for bounded nearby receiver trust."""
import copy
import json
import math

import pytest

import bps
import bps.outdoor_tracking as outdoor
from bps.sparse_tracking import solve_sparse_detection


def case(positions, distances=None, *, scale=10):
    """Origin target with anonymous scanner placements; no real-world data."""
    distances = distances or [math.hypot(*xy) for xy in positions]
    floor = {"name": "Synthetic", "scale": scale, "receivers": [], "environment": []}
    weighted = []
    for i, ((x, y), distance) in enumerate(zip(positions, distances)):
        pt = (x * scale, y * scale, distance * scale, 1.0, distance * scale)
        floor["receivers"].append({"entity_id": f"station_{i}", "distance": distance,
            "cords": {"x": pt[0], "y": pt[1], "r": pt[2]},
            "_outdoor_reading": {"measured_distance_m": distance, "observed": 1000,
                                 "reading_age_s": 0}})
        weighted.append(pt)
    return floor, weighted


def solve(floor, weighted, *, enabled=True, solver=bps.trilaterate, max_age=30):
    scale = floor["scale"]
    return outdoor.solve_outdoor(floor, weighted, None, scale, 0.5 * scale,
                                 max_age, None, solver, nearby_trust=enabled)


def sparse(floor, weighted, *, enabled=True, previous=None):
    return solve_sparse_detection(floor, weighted, floor["scale"], 30, previous,
                                   now=1000, nearby_trust=enabled)


def diagnostics(result):
    return result["outdoor"]["receiver_diagnostics"]


@pytest.mark.parametrize("far_error", [6, 12, 20])
def test_nearby_accurate_ranges_resist_multiple_inconsistent_far_readings(far_error):
    positions = [(-3, 0), (0, -4), (3, 3), (20, 0), (0, 25), (25, 25)]
    ranges = [math.hypot(*xy) for xy in positions]
    ranges[3:] = [r + far_error for r in ranges[3:]]
    floor, weighted = case(positions, ranges)
    saved = copy.deepcopy((floor, weighted))
    baseline = solve(floor, weighted, enabled=False)
    result = solve(floor, weighted)
    assert math.dist(result["fix"], (0, 0)) < math.dist(baseline["fix"], (0, 0))
    assert math.dist(result["fix"], (0, 0)) / floor["scale"] < 1.0
    assert (floor, weighted) == saved
    assert all(a[:3] == b[:3] and a[4] == b[4] for a, b in zip(result["weighted"], weighted))
    assert all(0 < d["reliability_weight"] < 1 for d in diagnostics(result)[3:])
    assert all(abs(d["horizontal_residual_m"]) > 3 for d in diagnostics(result)[3:])
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("scale", [1, 10, 50])
def test_soft_curve_uses_corrected_slant_metres_and_keeps_far_bearings(scale):
    floor, weighted = case([(0, 0)] * 6, [3, 5, 10, 20, 30, 100], scale=scale)
    # High mount collapses a far horizontal radius to zero; never reward it.
    weighted[4] = (*weighted[4][:2], 0, weighted[4][3], weighted[4][4])
    floor["receivers"][4]["cords"]["r"] = 0
    floor["receivers"][4]["height"] = 30
    floor["receivers"][2]["_outdoor_reading"]["measured_distance_m"] = 32.8084
    result = solve(floor, weighted, solver=lambda *args, **kwargs: (0, 0))
    ds = diagnostics(result)
    assert [d["range_trust_weight"] for d in ds] == pytest.approx([1, 1, .5, .325, .15, .15])
    assert [d["range_trust_distance_m"] for d in ds] == [3, 5, 10, 20, 30, 100]
    assert all(d["nearby_priority_reference_m"] == 3 for d in ds)
    assert ds[2]["measured_distance_m"] == 32.8084
    assert ds[2]["corrected_distance_m"] == 10
    assert result["outdoor"]["range_trust_enabled"] is True
    assert len(result["weighted"]) == 6


@pytest.mark.parametrize("scale", [1, 10, 50])
def test_coherent_all_far_ranges_preserve_fix_confidence_and_global_trust(scale):
    floor, weighted = case([(35, 0), (-40, 0), (0, 45), (0, -50)], scale=scale)
    original = solve(floor, weighted, enabled=False)
    result = solve(floor, weighted)
    assert result["fix"] == pytest.approx(original["fix"])
    assert result["weighted"] == original["weighted"]
    assert result["outdoor"]["confidence"] == original["outdoor"]["confidence"]
    assert result["outdoor"]["estimated_uncertainty_m"] == original["outdoor"]["estimated_uncertainty_m"]
    assert all(d["range_trust_weight"] == 1 for d in diagnostics(result))
    assert all(d["nearby_priority_reference_m"] == 35 for d in diagnostics(result))
    assert result["outdoor"]["estimated_uncertainty_m"] > 3


@pytest.mark.parametrize("bad", ["old", "missing_age", "missing_clock", "jump", "accuracy", "policy", "ignore", "stale"])
def test_unreliable_close_station_cannot_downweight_healthy_far_stations(bad):
    floor, weighted = case([(1, 0), (35, 0), (-40, 0), (0, 45), (0, -50)])
    rec = floor["receivers"][0]
    if bad == "old":
        rec["_outdoor_reading"]["reading_age_s"] = 16
    elif bad == "missing_age":
        rec["_outdoor_reading"].pop("reading_age_s")
    elif bad == "missing_clock":
        rec["_outdoor_reading"].pop("observed")
    elif bad == "jump":
        weighted[0] = (*weighted[0][:3], .1, weighted[0][4])
    elif bad == "accuracy":
        rec["distance_accuracy_m"] = 1000
    elif bad == "policy":
        rec["outdoor_policy"] = "deprioritize"
    elif bad == "ignore":
        rec["outdoor_policy"] = "ignore"
    else:
        rec["_outdoor_reading"]["reading_age_s"] = 31
    result = solve(floor, weighted)
    far = [d for d in diagnostics(result) if d["receiver"] != "station_0"]
    assert all(d["range_trust_weight"] == 1 and d["reliability_weight"] == 1 for d in far)
    assert all(d["nearby_priority_reference_m"] == 35 for d in far)
    near = next(d for d in diagnostics(result) if d["receiver"] == "station_0")
    if bad in {"ignore", "stale"}:
        assert near["excluded"] is True and near["reliability_weight"] == 0
    elif bad in {"jump", "accuracy"}:
        assert near["reliability_weight"] <= .1


def test_no_fresh_reference_leaves_range_trust_neutral_and_age_guards_authoritative():
    floor, weighted = case([(2, 0), (10, 0), (0, 30)])
    for rec in floor["receivers"]:
        rec["_outdoor_reading"]["reading_age_s"] = 20
    result = solve(floor, weighted)
    assert all(d["range_trust_weight"] == 1 and d["nearby_priority_reference_m"] is None
               for d in diagnostics(result))
    assert all(d["reliability_weight"] < 1 for d in diagnostics(result))


@pytest.mark.parametrize("failure", [None, "compile", "association", "refinement"])
def test_range_accuracy_jump_and_freshness_apply_once_through_refinement_and_fallback(monkeypatch, failure):
    floor, weighted = case([(3, 0), (10, 0), (0, 15)])
    weighted[1] = (*weighted[1][:3], .8, weighted[1][4])
    floor["receivers"][1]["distance_accuracy_m"] = 5
    floor["receivers"][1]["_outdoor_reading"]["reading_age_s"] = 20
    floor["environment"] = [{"id": "synthetic_trees", "type": "light_vegetation",
        "points": [{"x": x, "y": y} for x, y in [(50, -10), (60, -10), (60, 10), (50, 10)]]}]
    calls = []

    def solver(pts, **kwargs):
        calls.append(copy.deepcopy(pts))
        return None if failure == "refinement" and len(calls) > 1 else (0, 0)

    def fail(*args, **kwargs):
        raise ArithmeticError("synthetic geometry failure")

    if failure == "compile":
        monkeypatch.setattr(outdoor, "compile_environment", fail)
    elif failure == "association":
        monkeypatch.setattr(outdoor, "_indoor_support", fail)
    result = solve(floor, weighted, solver=solver)
    base = .8 * .8 * .5
    assert len(calls) == 2
    assert calls[0][1][3] == pytest.approx(base)
    geometry = .9 if failure in {None, "refinement"} else 1.0
    assert calls[1][1][3] == pytest.approx(base * (1.5 - 20 / 30) * geometry)
    actual = base if failure == "refinement" else calls[1][1][3]
    assert result["weighted"][1][3] == pytest.approx(actual)
    assert diagnostics(result)[1]["reliability_weight"] == pytest.approx(actual)
    assert diagnostics(result)[1]["range_trust_weight"] == .5
    if failure in {"compile", "association"}:
        assert result["outdoor"]["environment_fallback"] is True
    if failure == "refinement":
        assert diagnostics(result)[1]["status"] == "refinement_failed"


def test_uncertainty_receives_actual_range_adjusted_solver_weights(monkeypatch):
    floor, weighted = case([(3, 0), (10, 0), (0, 30)])
    original = outdoor.estimate_uncertainty
    seen = []

    def inspect(fix, weights, scale, ds, **kwargs):
        seen.append(copy.deepcopy(weights))
        assert [p[3] for p in weights] == [d["reliability_weight"] for d in ds]
        return original(fix, weights, scale, ds, **kwargs)

    monkeypatch.setattr(outdoor, "estimate_uncertainty", inspect)
    result = solve(floor, weighted)
    assert seen == [result["weighted"]]
    assert [p[3] for p in seen[0]] == pytest.approx([1, .5, .15])
    assert math.isfinite(result["outdoor"]["estimated_uncertainty_m"])


@pytest.mark.parametrize("count", [1, 2])
def test_sparse_detection_keeps_direction_unknown_outside_image_and_range_metadata(count):
    floor, weighted = case([(-20, -30), (200, 200)][:count], [4, 12][:count])
    saved = copy.deepcopy((floor, weighted))
    result = sparse(floor, weighted)
    assert result["fix"] == weighted[0][:2]
    quality = result["outdoor"]
    assert quality["direction_known"] is False and quality["detection_only"] is True
    assert quality["confidence"] == "poor"
    assert quality["observations_used"] == count == quality["receivers_used"]
    assert quality["range_trust_enabled"] is True
    assert quality["detection_range_m"] == 4
    assert quality["estimated_uncertainty_m"] >= 4
    assert diagnostics(result)[0]["range_trust_weight"] == 1
    if count == 2:
        d = diagnostics(result)[1]
        assert 0 < d["range_trust_weight"] < .5
        assert result["weighted"][1][3] == d["reliability_weight"] == d["range_trust_weight"]
    assert (floor, weighted) == saved


def test_sparse_range_trust_changes_relative_ranking_once_and_preserves_current_anchor_stability():
    floor, weighted = case([(0, 0), (20, 20)], [5, 6.8])
    weighted[0] = (*weighted[0][:3], .5, weighted[0][4])
    assert sparse(floor, weighted, enabled=False)["outdoor"]["detection_receiver"] == "station_1"
    assert sparse(floor, weighted)["outdoor"]["detection_receiver"] == "station_0"
    floor, weighted = case([(0, 0), (20, 20)], [5, 10])
    previous = {"floor": floor["name"], "outdoor": {"detection_receiver": "station_1"}}
    result = sparse(floor, weighted, previous=previous)
    assert result["outdoor"]["detection_receiver"] == "station_0"
    assert diagnostics(result)[1]["reliability_weight"] == .5
    assert result["weighted"][1][3] == .5
    # Small changes keep the previous receiver without inventing an old range.
    floor, weighted = case([(0, 0), (20, 20)], [5.1, 5])
    previous["outdoor"]["detection_receiver"] = "station_0"
    assert sparse(floor, weighted, previous=previous)["outdoor"]["detection_receiver"] == "station_0"


def test_sparse_untimed_or_noisy_close_station_cannot_suppress_healthy_far_anchor():
    floor, weighted = case([(0, 0), (20, 20)], [2, 35])
    floor["receivers"][0]["_outdoor_reading"].pop("observed")
    result = sparse(floor, weighted)
    assert result["outdoor"]["detection_receiver"] == "station_1"
    assert diagnostics(result)[1]["range_trust_weight"] == 1
    floor, weighted = case([(0, 0), (20, 20)], [2, 35])
    floor["receivers"][0]["distance_accuracy_m"] = 1000
    result = sparse(floor, weighted)
    assert diagnostics(result)[0]["reliability_weight"] == .05
    assert diagnostics(result)[1]["range_trust_weight"] == 1


def test_pure_function_defaults_preserve_existing_weights():
    floor, weighted = case([(3, 0), (10, 0), (0, 30)])
    result = outdoor.solve_outdoor(floor, weighted, None, 10, 5, 30, None, bps.trilaterate)
    assert result["weighted"] == weighted
    assert result["outdoor"]["range_trust_enabled"] is False
    floor["receivers"] = floor["receivers"][:2]
    result = solve_sparse_detection(floor, weighted[:2], 10, 30, now=1000)
    assert result["weighted"] == weighted[:2]
    assert result["outdoor"]["range_trust_enabled"] is False
