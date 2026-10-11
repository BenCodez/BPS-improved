"""Sparse detections preserve range and source freshness without inventing fixes."""
import copy
import json

import pytest

from bps.sparse_tracking import solve_sparse_detection


def receiver(slug="porch", x=100, y=200, distance=5, radius=40, observed=995, **kwargs):
    return {"entity_id": slug, "cords": {"x": x, "y": y, "r": radius},
            "distance": distance, "_outdoor_reading": {
                "measured_distance_m": distance, "observed": observed,
                "reading_age_s": 1000 - observed}, **kwargs}


def solve(receivers, *, scale=10, previous=None, now=1000, max_age=30, weights=None, timeout=300):
    floor = {"name": "Property", "scale": scale, "receivers": receivers}
    if weights is None:
        weights = [(r["cords"]["x"], r["cords"]["y"], r["cords"]["r"], 1,
                    r["distance"] * scale) for r in receivers]
    return solve_sparse_detection(floor, weights, scale, max_age, previous,
                                  now=now, position_timeout_s=timeout)


def test_one_receiver_is_a_range_anchor_and_keeps_original_observation():
    rec = receiver(height=3)
    before = copy.deepcopy(rec)
    result = solve([rec])
    assert result["fix"] == (100, 200)
    assert result["weighted"] == [(100, 200, 40, 1, 50)]
    quality = result["outdoor"]
    assert quality["detection_only"] is True and quality["direction_known"] is False
    assert quality["confidence"] == "poor" and quality["receivers_used"] == 1
    assert quality["detection_receiver"] == "porch"
    # Input radius already removes mount/tracker height; do not project twice
    # or replace it with the corrected slant distance.
    assert quality["detection_range_m"] == 4 and quality["detection_range_px"] == 40
    assert quality["estimated_uncertainty_m"] >= quality["detection_range_m"]
    assert quality["observed"] == 995 and quality["position_age_s"] == 5
    assert quality["receiver_diagnostics"][0]["observed"] == 995
    assert quality["receiver_diagnostics"][0]["classification"] == "direction_unknown"
    assert quality["clear_paths"] == 0
    assert rec == before
    json.dumps(result, allow_nan=False)


def test_two_receivers_never_average_or_choose_an_intersection():
    result = solve([receiver(), receiver("garden", 300, 400, 4, 30, 998)])
    assert result["fix"] == (300, 400)
    assert result["fix"] != (200, 300)
    assert result["outdoor"]["receivers_used"] == 2
    assert result["outdoor"]["observed"] == 998
    assert result["outdoor"]["direction_known"] is False
    assert len(result["outdoor"]["receiver_diagnostics"]) == 2


def test_previous_anchor_stays_for_minor_changes_but_cannot_supply_old_xy():
    prior = {"floor": "Property", "cords": [999, 999], "updated": 1,
             "outdoor": {"observed": 1, "detection_receiver": "porch"}}
    a, b = receiver(distance=5.1), receiver("garden", 300, 400, 5, 40)
    assert solve([a, b], previous=prior)["fix"] == (100, 200)
    a["distance"] = 8
    assert solve([a, b], previous=prior)["fix"] == (300, 400)
    assert solve([b], previous=prior)["fix"] == (300, 400)
    prior["floor"] = "Other"
    a["distance"] = 5.1
    assert solve([a, b], previous=prior)["fix"] == (300, 400)


def test_previous_receiver_can_only_be_kept_when_its_current_source_is_fresh():
    prior = {"floor": "Property", "outdoor": {"detection_receiver": "porch"}}
    a = receiver(observed=969)
    result = solve([a, receiver("garden", 300, 400)], previous=prior)
    assert result["fix"] == (300, 400)
    assert result["outdoor"]["receivers_used"] == 1
    assert result["outdoor"]["receiver_diagnostics"][0]["classification"] == "stale"


def test_source_clock_is_checked_again_after_queue_delay():
    rec = receiver(observed=975)
    assert solve([rec], now=1004)["outdoor"]["position_age_s"] == 29
    assert solve([rec], now=1005) is None
    # A reset age or newly published beacon must not rejuvenate source time.
    rec["_outdoor_reading"]["reading_age_s"] = 0
    assert solve([rec], now=1005, previous={"updated": 1005, "cords": [0, 0]}) is None


@pytest.mark.parametrize("observed", [970, 969, 0])
def test_stale_observations_cannot_supply_detection(observed):
    assert solve([receiver(observed=observed)]) is None


def test_disabled_normal_age_gate_still_requires_recent_detection():
    assert solve([receiver(observed=975)], max_age=0) is not None
    assert solve([receiver(observed=970)], max_age=0) is None
    assert solve([receiver(observed=989)], max_age=0, timeout=10) is None
    result = solve([receiver(observed=995)], max_age=0, timeout=10)
    assert result["outdoor"]["use_observation_age"] is True
    assert result["outdoor"]["stale_after_s"] == 10


@pytest.mark.parametrize("reading", [None, {}, {"observed": 995},
    {"observed": 995, "reading_age_s": None, "measured_distance_m": 5},
    {"observed": 1001, "reading_age_s": 0, "measured_distance_m": 5},
    {"observed": 995, "reading_age_s": -1, "measured_distance_m": 5},
    {"observed": float("nan"), "reading_age_s": 5, "measured_distance_m": 5}])
def test_saved_radii_and_unknown_source_times_do_not_count(reading):
    rec = receiver()
    rec["_outdoor_reading"] = reading
    assert solve([rec]) is None


def test_ignore_policy_is_never_counted_in_full_or_filtered_input():
    ignored = receiver("ignored", distance=1, radius=10, outdoor_policy="ignore")
    live = receiver()
    assert solve([ignored]) is None
    for weights in (None, [(100, 200, 40, 1, 50)]):
        result = solve([live, ignored], weights=weights)
        assert result["fix"] == (100, 200)
        assert result["outdoor"]["receivers_used"] == 1
        excluded = next(d for d in result["outdoor"]["receiver_diagnostics"] if d["receiver"] == "ignored")
        assert excluded["used"] is False and excluded["classification"] == "manual_ignore"


def test_reliability_and_slant_range_choose_anchor_instead_of_tiny_projection():
    a = receiver("tall", distance=10, radius=1, height=10)
    b = receiver("garden", 300, 400, 4, 30)
    assert solve([a, b])["outdoor"]["detection_receiver"] == "garden"
    a = receiver("noisy", distance=3, radius=20)
    weights = [(100, 200, 20, 0.05, 30), (300, 400, 30, 1, 40)]
    result = solve([a, b], weights=weights)
    assert result["outdoor"]["detection_receiver"] == "garden"
    assert result["outdoor"]["receiver_diagnostics"][0]["reliability_weight"] == 0.05


def test_duplicates_cannot_turn_two_scanners_into_trilateration():
    a, duplicate = receiver(observed=995), receiver(observed=998)
    result = solve([a, receiver("garden", 300, 400, 8, 60), duplicate])
    assert result["outdoor"]["receivers_used"] == 2
    assert result["outdoor"]["observed"] == 998
    assert sum(d["used"] for d in result["outdoor"]["receiver_diagnostics"]) == 2
    assert result["outdoor"]["receiver_diagnostics"][0]["classification"] == "duplicate_receiver"
    assert solve([receiver(), receiver("b"), receiver("c")]) is None
    assert solve([]) is None


@pytest.mark.parametrize("index,value", [(0, float("nan")), (1, float("inf")),
    (2, -1), (2, float("inf")), (3, 0), (3, 2), (3, True), (4, -1), (4, "50")])
def test_malformed_weighted_inputs_are_excluded(index, value):
    point = [100, 200, 40, 1, 50]
    point[index] = value
    assert solve([receiver()], weights=[point]) is None


@pytest.mark.parametrize("field,value", [("distance", float("nan")), ("distance", -1),
    ("distance", True), ("entity_id", ""), ("entity_id", []), ("cords", {})])
def test_malformed_receiver_inputs_are_excluded(field, value):
    rec = receiver()
    rec[field] = value
    assert solve([rec], weights=[(100, 200, 40, 1, 50)]) is None


def test_malformed_receiver_does_not_hide_other_fresh_detection():
    a, b = receiver(), receiver("garden", 300, 400)
    a["distance"] = float("nan")
    result = solve([a, b], weights=[(100, 200, 40, 1, 50), (300, 400, 40, 1, 50)])
    assert result["fix"] == (300, 400)
    assert result["outdoor"]["receivers_used"] == 1
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("scale", [None, True, 0, -1, float("nan"), float("inf"), "10"])
def test_invalid_scale_cannot_publish_metric_detection(scale):
    assert solve_sparse_detection({"name": "Property", "scale": scale, "receivers": [receiver()]},
                                  [(100, 200, 40, 1, 50)], scale, 30, now=1000) is None


@pytest.mark.parametrize("floor", [None, {}, {"name": "", "scale": 10},
    {"name": "Property", "scale": 20, "receivers": [receiver()]},
    {"name": "Property", "scale": 10, "receivers": None}])
def test_invalid_floor_is_not_a_publishable_map_anchor(floor):
    assert solve_sparse_detection(floor, [(100, 200, 40, 1, 50)], 10, 30, now=1000) is None


def test_input_order_mismatch_and_horizontal_range_above_slant_fail_closed():
    assert solve([receiver()], weights=[]) is None
    assert solve([receiver()], weights=[(101, 200, 40, 1, 50)]) is None
    assert solve([receiver()], weights=[(100, 200, 40, 1, 60)]) is None
    assert solve([receiver(radius=60)]) is None


def test_zero_horizontal_range_has_nonzero_coarse_uncertainty():
    result = solve([receiver(distance=0, radius=0)])
    assert result["fix"] == (100, 200)
    assert result["outdoor"]["detection_range_m"] == 0
    assert result["outdoor"]["estimated_uncertainty_m"] >= 3
    assert result["outdoor"]["confidence"] == "poor"


@pytest.mark.parametrize("slug", [[], {}, 3, True])
def test_malformed_previous_receiver_does_not_hide_current_detection(slug):
    prior = {"floor": "Property", "outdoor": {"detection_receiver": slug}}
    assert solve([receiver()], previous=prior)["fix"] == (100, 200)


def test_extreme_finite_input_cannot_overflow_metric_payload():
    assert solve([receiver(distance=1e308, radius=1e308)], scale=1e-6) is None
    assert solve([receiver(x=1e308)]) is None
