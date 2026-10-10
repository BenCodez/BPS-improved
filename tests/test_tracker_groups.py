"""Position fusion preserves sources and exposes disagreement honestly."""

import copy
import json
import math

import pytest

from bps import tracker_groups as G

NOW = 1000.0
SCALES = {"Yard": 10.0, "Barn": 40.0}
GROUP = {"id": "rover", "name": "Rover", "enabled": True,
         "beacons": ["beacon_a", "beacon_b"]}


def layout(groups=None, enabled=True):
    return {"outdoor_tracking": {"enabled": enabled},
            "tracker_groups": [copy.deepcopy(GROUP)] if groups is None else groups}


def position(ent="beacon_a", x=100.0, y=100.0, uncertainty=3.0,
             updated=NOW, floor="Yard", receivers=6, **extra):
    return {"ent": ent, "cords": [x, y], "floor": floor, "updated": updated,
            "rms_m": 1.0, "conf": 0.8,
            "outdoor": {"estimated_uncertainty_m": uncertainty,
                        "receivers_used": receivers, "confidence": "good"}, **extra}


def fuse(*positions, now=NOW):
    return G.fuse_group(GROUP, list(positions), SCALES, now)


def test_old_layout_and_disabled_feature_produce_no_groups():
    for data in ({}, None, layout(enabled=False), layout(enabled=1),
                 {"outdoor_tracking": True, "tracker_groups": [GROUP]}):
        assert G.normalize_groups(data) == []
        assert G.fuse_groups(data, [position()], SCALES, NOW) == []


def test_normalization_preserves_missing_members_and_input():
    data = layout()
    before = copy.deepcopy(data)
    assert G.normalize_groups(data, ["beacon_a"]) == [GROUP]
    assert data == before


def test_duplicate_ids_disabled_groups_and_tracker_id_collisions():
    entries = [GROUP, GROUP, {**GROUP, "id": "disabled", "enabled": False},
               {**GROUP, "id": "collision"}]
    assert G.normalize_groups(layout(entries), ["bps_group_collision"]) == [GROUP]


@pytest.mark.parametrize("bad", [None, [], "rover", {},
    {**GROUP, "id": "Rover"}, {**GROUP, "id": "dög"},
    {**GROUP, "id": "a/b"}, {**GROUP, "id": "x" * 65},
    {**GROUP, "beacons": []}, {**GROUP, "beacons": "beacon_a"},
    {**GROUP, "beacons": ["bps_group_rover"]},
    {**GROUP, "enabled": "true"}])
def test_malformed_group_definition_fails_safely(bad):
    assert G.normalize_groups(layout([bad])) == []


def test_bounded_group_and_member_counts():
    groups = [{**GROUP, "id": f"dog_{i}"} for i in range(G.MAX_GROUPS + 20)]
    assert len(G.normalize_groups(layout(groups))) == G.MAX_GROUPS
    too_many = {**GROUP, "beacons": [f"b_{i}" for i in range(G.MAX_BEACONS_PER_GROUP + 1)]}
    assert G.normalize_groups(layout([too_many])) == []


def test_duplicate_beacons_do_not_artificially_improve_confidence():
    group = {**GROUP, "beacons": ["beacon_a", "beacon_a"]}
    result = G.fuse_group(group, [position()], SCALES, NOW)
    assert result["total_beacons"] == result["beacons_reporting"] == 1
    assert result["outdoor"]["estimated_uncertainty_m"] == 3


def test_one_beacon_available_keeps_source_position_and_timestamp():
    result = fuse(position(updated=NOW - 10))
    assert result["ent"] == "bps_group_rover"
    assert result["group"] is True
    assert result["name"] == "Rover"
    assert result["cords"] == [100.0, 100.0]
    assert result["updated"] == NOW - 10
    assert result["beacons_reporting"] == 1 and result["total_beacons"] == 2
    assert result["fusion_confidence"] == "single"
    assert result["outdoor"]["estimated_uncertainty_m"] == pytest.approx(3)


def test_group_exposes_bounded_member_radius_breakdowns_without_changing_sources():
    a, b = position(), position("beacon_b", x=115)
    details = {"residual_m": 2, "unadjusted_residual_m": 5,
               "noise_floor_m": 1.1, "geometry_factor": 3.6,
               "effective_receivers": 2.4, "receiver_count_factor": 1.12,
               "robust_downweighted_receivers": 8,
               "pre_publication_uncertainty_m": 2.5, "publication_displacement_m": 1}
    a["outdoor"].update(details)
    a["outdoor"]["receiver_diagnostics"] = [{"receiver": "kitchen", "used": True}]
    b["outdoor"].update({"residual_m": True, "noise_floor_m": float("nan"),
                         "geometry_factor": -1, "effective_receivers": float("inf")})
    before = copy.deepcopy(a)
    result = fuse(a, b)
    member_a, member_b = result["beacon_positions"]
    assert member_a["uncertainty_details"] == details
    assert "receiver_diagnostics" not in member_a
    assert member_b["uncertainty_details"] == {}
    # Payload remains JSON-safe even when a source's diagnostics are not.
    json.dumps(result, allow_nan=False)
    member_a["uncertainty_details"]["residual_m"] = 100
    assert a == before


def test_agreeing_beacons_improve_uncertainty_modestly():
    result = fuse(position(), position("beacon_b", x=115, uncertainty=4))
    assert 100 < result["cords"][0] < 115
    assert result["beacon_disagreement_m"] == pytest.approx(1.5)
    assert 2.25 <= result["outdoor"]["estimated_uncertainty_m"] < 3
    assert result["beacons_reporting"] == 2
    assert all(p["used"] for p in result["beacon_positions"])


def test_group_retains_environment_fallback_warning_and_poor_confidence():
    a, b = position(), position("beacon_b", x=115)
    a["outdoor"].update(environment_fallback=True, confidence="poor")
    result = fuse(a, b)
    assert result["outdoor"]["environment_fallback"] is True
    assert result["outdoor"]["confidence"] == "poor"
    assert result["beacon_positions"][0]["environment_fallback"] is True
    assert 100 < result["cords"][0] < 115


def test_one_group_radius_combines_both_beacons_with_the_published_position_weights():
    a = position(uncertainty=3, updated=NOW)
    b = position("beacon_b", x=110, uncertainty=10, updated=NOW - 100)
    result = fuse(a, b)
    # Recover the normalized weight from the fused centre. An old/noisy member
    # has small influence but its variance still follows that actual weight.
    fraction_b = (result["cords"][0] - a["cords"][0]) / (b["cords"][0] - a["cords"][0])
    combined = math.hypot((1 - fraction_b) * 3, fraction_b * 10)
    spread = math.sqrt((1 - fraction_b) * fraction_b)  # member separation is 1 m
    outdoor = result["outdoor"]
    assert outdoor["beacons_used"] == 2
    assert outdoor["combined_beacon_uncertainty_m"] == pytest.approx(combined)
    assert outdoor["beacon_scatter_m"] == pytest.approx(spread)
    assert outdoor["estimated_uncertainty_m"] == pytest.approx(math.hypot(combined, spread))
    assert outdoor["uncertainty_method"] == "beacon_fusion_heuristic"


def test_good_and_poor_far_apart_fix_prefers_strong_and_inflates_uncertainty():
    result = fuse(position(), position("beacon_b", x=400, uncertainty=20, receivers=3))
    assert result["cords"] == [100.0, 100.0]
    assert result["beacon_disagreement_m"] == 30
    assert result["outdoor"]["estimated_uncertainty_m"] > 15
    assert result["outdoor"]["confidence"] == "poor"
    assert result["fusion_confidence"] == "disagreement"
    assert result["beacon_positions"][1]["used"] is False


def test_two_strong_disagreeing_beacons_never_publish_a_false_midpoint():
    result = fuse(position(), position("beacon_b", x=600))
    assert result["cords"][0] in (100, 600)
    assert result["outdoor"]["estimated_uncertainty_m"] >= 25
    assert result["beacons_reporting"] == 2


def test_missing_deleted_and_all_stale_members_are_safe():
    assert fuse() is None
    assert fuse(position(updated=NOW - G.DEFAULT_MAX_AGE_S - 1)) is None
    assert fuse(position(), position("beacon_b", updated=NOW - 1000))["beacons_reporting"] == 1


def test_freshness_changes_quality_not_source_observation_time():
    fresh = position()
    older = position("beacon_b", x=130, updated=NOW - 90)
    result = fuse(fresh, older)
    assert result["cords"][0] < 115
    result_later = fuse(fresh, older, now=NOW + 10)
    assert result_later["updated"] == result["updated"] == NOW


def test_future_timestamp_and_missing_timestamp_do_not_create_fresh_fixes():
    assert fuse(position(updated=NOW + 100)) is None
    p = position()
    del p["updated"]
    assert fuse(p) is None


def test_observation_age_prevents_recent_solve_extending_stale_readings():
    p = position(updated=NOW)
    p["outdoor"]["observed"] = NOW - 25
    result = G.fuse_group(GROUP, [p], SCALES, NOW, max_age_s=30)
    assert result["updated"] == NOW - 25
    assert result["beacon_positions"][0]["age_s"] == 25
    assert G.fuse_group(GROUP, [p], SCALES, NOW + 6, max_age_s=30) is None


@pytest.mark.parametrize("observed", [float("nan"), True, NOW + 1])
def test_invalid_observation_time_is_excluded(observed):
    p = position()
    p["outdoor"]["observed"] = observed
    assert fuse(p) is None


def test_disabled_observation_gate_retains_old_readings_without_refreshing_source_solve():
    p = position(updated=NOW - 40)
    p["outdoor"]["observed"] = NOW - 1000
    before = copy.deepcopy(p)
    result = G.fuse_group(GROUP, [p], SCALES, NOW, max_age_s=300, use_observation_age=False)
    assert result["updated"] == NOW - 40
    assert result["beacon_positions"][0]["age_s"] == 40
    assert G.fuse_group(GROUP, [p], SCALES, NOW + 261, max_age_s=300,
                        use_observation_age=False) is None
    assert p == before


def test_different_floor_pixels_are_never_averaged_or_compared_as_metres():
    result = fuse(position(), position("beacon_b", x=10000, floor="Barn", uncertainty=20))
    assert result["floor"] == "Yard" and result["cords"] == [100, 100]
    assert result["beacon_disagreement_m"] is None
    assert result["fusion_confidence"] == "floor_conflict"
    assert result["outdoor"]["estimated_uncertainty_m"] >= 4.5
    assert len(result["beacon_positions"]) == 2


def test_zone_snapping_preserves_fused_floor_conflict_warning():
    import bps
    result = fuse(position(uncertainty=1),
                  position("beacon_b", x=10000, floor="Barn", uncertainty=20))
    assert result["outdoor"]["confidence"] == "poor"
    radius = result["outdoor"]["estimated_uncertainty_m"]
    lookup = [{"entity": result["ent"], "data": {"floor": [{"name": "Yard", "zones": [
        {"entity_id": "Yard zone", "poly": True,
         "cords": [{"x": 0, "y": 0}, {"x": 90, "y": 0},
                   {"x": 90, "y": 200}, {"x": 0, "y": 200}]},
    ]}]}}]
    bps._assign_group_zone(result, lookup, SCALES["Yard"])
    assert result["cords"] == [90, 100]
    assert result["outdoor"]["estimated_uncertainty_m"] > radius
    assert result["outdoor"]["confidence"] == "poor"
    assert result["fusion_confidence"] == "floor_conflict"


def test_scale_conversion_is_in_metres():
    a = position(floor="Barn")
    b = position("beacon_b", x=160, floor="Barn", uncertainty=4)
    assert fuse(a, b)["beacon_disagreement_m"] == 1.5


def test_receiver_count_and_residual_quality_affect_weights():
    a = position(receivers=6)
    b = position("beacon_b", x=130, receivers=1)
    b["rms_m"] = 10
    assert fuse(a, b)["cords"][0] < 115


def test_environment_is_not_applied_a_second_time():
    a, b = position(), position("beacon_b", x=115, uncertainty=4)
    before = fuse(a, b)
    b["outdoor"].update({"building_paths": 99, "environment_reliability": 0.01})
    assert fuse(a, b) == before


def test_receiver_count_does_not_double_count_the_same_proxies():
    result = fuse(position(receivers=6), position("beacon_b", receivers=6))
    assert result["outdoor"]["receivers_used"] == 6
    assert result["outdoor"]["receiver_observations"] == 12


def test_receiver_diagnostics_names_allow_unique_proxy_count():
    a, b = position(receivers=2), position("beacon_b", receivers=2)
    a["outdoor"]["receiver_diagnostics"] = [{"receiver": "one"}, {"receiver": "two"}]
    b["outdoor"]["receiver_diagnostics"] = [
        {"receiver": "two"}, {"receiver": "three"}, {"receiver": "ignored", "used": False}]
    result = fuse(a, b)
    assert result["outdoor"]["receivers_used"] == 3


@pytest.mark.parametrize("field,value", [
    ("cords", [float("nan"), 2]), ("cords", [float("inf"), 2]),
    ("cords", [True, 2]), ("cords", [1]), ("cords", None),
    ("updated", float("nan")), ("updated", True), ("floor", None)])
def test_invalid_fix_values_are_excluded(field, value):
    assert fuse(position(**{field: value})) is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True])
def test_invalid_uncertainty_is_not_hidden_by_fallback(value):
    assert fuse(position(uncertainty=value)) is None


@pytest.mark.parametrize("scale", [0, -1, True, None, float("nan"), float("inf")])
def test_invalid_scale_cannot_produce_wrong_metric_position(scale):
    assert G.fuse_group(GROUP, [position()], {"Yard": scale}, NOW) is None


def test_ordinary_fix_fallback_remains_conservative():
    p = position()
    del p["outdoor"]
    result = fuse(p)
    assert result["outdoor"]["estimated_uncertainty_m"] >= 5


def test_original_tracker_payloads_are_never_mutated_or_aliased():
    positions = [position(), position("beacon_b", x=115)]
    before = copy.deepcopy(positions)
    result = G.fuse_groups(layout(), positions, SCALES, NOW)[0]
    assert positions == before
    result["beacon_positions"][0]["cords"][0] = 999
    assert positions == before


def test_dictionary_inputs_and_newest_duplicate_observation():
    older = position(x=0, updated=NOW - 30)
    newer = position()
    assert fuse(newer, older)["cords"] == [100, 100]
    p = position()
    del p["ent"]
    result = G.fuse_group(GROUP, {"beacon_a": p}, SCALES, NOW)
    assert result["beacons_reporting"] == 1


def test_group_payload_cannot_be_used_as_a_beacon():
    assert fuse(position(group=True)) is None


def test_finite_json_payload_even_for_extreme_valid_inputs():
    result = G.fuse_group(GROUP, [position(x=1e9, uncertainty=0)], {"Yard": 1e-6}, NOW)
    assert math.isfinite(result["outdoor"]["estimated_uncertainty_m"])
    assert result["outdoor"]["estimated_uncertainty_m"] >= 0
    json.dumps(result, allow_nan=False)


def test_genuine_group_prefixed_beacon_fuses_without_allowing_nested_groups():
    group = {**GROUP, "beacons": ["bps_group_beacon", "beacon_b"]}
    data = layout([group])
    assert G.normalize_groups(data, ["bps_group_beacon"]) == [group]
    original = position("bps_group_beacon")
    result = G.fuse_groups(data, [original], SCALES, NOW)[0]
    assert result["beacon_positions"][0]["ent"] == "bps_group_beacon"
    assert result["beacons_reporting"] == 1 and result["total_beacons"] == 2
    generated = {**original, "group": True}
    for positions in ([generated], {"bps_group_beacon": generated}):
        assert G.fuse_groups(data, positions, SCALES, NOW) == []
        assert G.fuse_group(group, positions, SCALES, NOW) is None


def test_inventory_retains_missing_prefixed_member_in_group_reporting():
    group = {**GROUP, "beacons": ["bps_group_beacon", "beacon_b"]}
    result = G.fuse_groups(layout([group]), [position("beacon_b")], SCALES, NOW,
                           known_trackers=["bps_group_beacon"])[0]
    assert result["total_beacons"] == 2 and result["beacons_reporting"] == 1
    assert G.normalize_groups(layout([group]), []) == [group]


def test_configured_group_outputs_are_rejected_even_when_disabled():
    other = {**GROUP, "id": "other", "enabled": False}
    nested = {**GROUP, "beacons": ["bps_group_other"]}
    assert G.normalize_groups(layout([nested, other])) == []
    # A genuine beacon with the same name takes precedence; the conflicting
    # generated output is retired instead of preventing the beacon's use.
    assert G.normalize_groups(layout([nested, other]), ["bps_group_other"]) == [nested]


def test_runtime_holds_a_distant_quality_spike_until_distinct_observations():
    state = {}
    a, b = position(x=0, uncertainty=1), position('beacon_b', x=600, uncertainty=2)
    previous = G.fuse_group(GROUP, [a, b], SCALES, NOW, stability=state)
    assert previous['anchor_beacon'] == 'beacon_a'
    b['outdoor'].update(estimated_uncertainty_m=.5, observed=NOW + 1)
    b['updated'] = NOW + 1
    held = G.fuse_group(GROUP, [a, b], SCALES, NOW + 1, previous=previous, stability=state)
    assert held['cords'] == [0, 100]
    assert held['anchor_switch_pending'] is True
    # A new solve timestamp of the same measurements does not confirm motion.
    for i in range(2, 5):
        b['updated'] = NOW + i
        held = G.fuse_group(GROUP, [a, b], SCALES, NOW + i, previous=held,
                            stability=state, use_observation_age=False)
        assert held['anchor_beacon'] == 'beacon_a'
    b['outdoor']['observed'] = NOW + 5
    b['updated'] = NOW + 5
    moved = G.fuse_group(GROUP, [a, b], SCALES, NOW + 5, previous=held, stability=state)
    assert moved['anchor_beacon'] == 'beacon_b'
    assert moved['anchor_switch_pending'] is False
    assert moved['cords'] == [600, 100]
    assert moved['outdoor']['confidence'] == 'poor'


def test_runtime_stale_incumbent_hands_off_without_waiting():
    state = {}
    a, b = position(x=0, uncertainty=1), position('beacon_b', x=600, uncertainty=2)
    previous = G.fuse_group(GROUP, [a, b], SCALES, NOW, stability=state)
    b['updated'] = NOW + 120
    result = G.fuse_group(GROUP, [a, b], SCALES, NOW + 121, previous=previous, stability=state)
    assert result['anchor_beacon'] == 'beacon_b'
    assert not result['anchor_switch_pending']


def test_runtime_membership_changes_and_disable_clear_challenges():
    state = {}
    a, b = position(x=0, uncertainty=1), position('beacon_b', x=600, uncertainty=2)
    prev = G.fuse_groups(layout(), [a, b], SCALES, NOW, stability=state)
    b['outdoor']['estimated_uncertainty_m'] = .5
    held = G.fuse_groups(layout(), [a, b], SCALES, NOW, previous={prev[0]['ent']: prev[0]}, stability=state)
    assert held[0]['anchor_switch_pending']
    changed = layout([{**GROUP, 'beacons': ['beacon_b']}])
    result = G.fuse_groups(changed, [a, b], SCALES, NOW, previous={held[0]['ent']: held[0]}, stability=state)
    assert result[0]['anchor_beacon'] == 'beacon_b'
    G.fuse_groups(layout([]), [a, b], SCALES, NOW, stability=state)
    assert state == {}
