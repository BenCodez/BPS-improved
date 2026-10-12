"""Synthetic group recovery keeps source-time and single-beacon jump bounds."""

import copy
import math

import pytest

from bps import tracker_groups as G


NOW = 1000.0
SCALES = {"Yard": 10.0}
GROUP = {"id": "animal", "beacons": ["tag_a", "tag_b"]}


def sources(offset, *, opposite=False):
    # Each source moves at 3 m/s. Quality reversals switch the selected point
    # between sources 30 m apart, without making their physical motion faster.
    return [{"ent": ent, "floor": "Yard", "cords": [x * 10, 0],
             "updated": NOW + offset, "rms_m": 1,
             "outdoor": {"observed": NOW + offset,
                         "estimated_uncertainty_m": uncertainty,
                         "receivers_used": 6}}
            for ent, x, uncertainty in (
                ("tag_a", 60 + 3 * offset, 20 if (offset // 2) % 2 else 40),
                ("tag_b", (-90 if opposite else 90) + 3 * offset,
                 40 if (offset // 2) % 2 else 20))]


def start():
    state = {}
    ps = sources(0)
    for p in ps:
        p["cords"] = [0, 0]
    previous = G.fuse_group(GROUP, ps, SCALES, NOW, stability=state)
    return previous, state


def test_two_forward_sources_recover_despite_repeated_quality_and_anchor_changes():
    previous, state = start()
    for offset in range(1, 15):
        ps = sources(offset)
        result = G.fuse_group(GROUP, ps, SCALES, NOW + offset,
                              previous=previous, stability=state)
        step = math.dist(result["cords"], previous["cords"]) / 10
        assert step <= G.GROUP_MAX_SPEED_MS + 1e-9
        if offset <= 3:
            assert result["cords"] == [0, 0]
        if 4 <= offset <= 9:
            assert step == pytest.approx(10)
            assert result["motion_gate"]["state"] == "limited"
            assert result["motion_gate"]["corroborating_beacons"] == ["tag_a", "tag_b"]
        # Spatially disagreeing beacons still never form a false midpoint.
        assert result["outdoor"]["beacons_used"] == 1
        previous = result
    assert min(math.dist(previous["cords"], p["cords"]) / 10 for p in ps) <= 10


@pytest.mark.parametrize("failure", ["opposite", "cached", "aged", "fallback", "sparse", "uncertain"])
def test_untrusted_second_source_cannot_preserve_a_changing_motion_challenge(failure):
    previous, state = start()
    for offset in range(1, 10):
        ps = sources(offset, opposite=failure == "opposite")
        if failure == "cached":
            ps[1]["outdoor"]["observed"] = NOW
        elif failure == "aged":
            for p in ps:
                p["outdoor"]["observed"] -= G.GROUP_MOTION_SUPPORT_MAX_AGE_S + 1
        elif failure == "fallback":
            ps[1]["outdoor"]["environment_fallback"] = True
        elif failure == "sparse":
            ps[1]["outdoor"]["receivers_used"] = 2
        elif failure == "uncertain":
            for p in ps:
                p["outdoor"]["estimated_uncertainty_m"] *= 5
        result = G.fuse_group(GROUP, ps, SCALES, NOW + offset,
                              previous=previous, stability=state,
                              use_observation_age=False)
        assert math.dist(result["cords"], previous["cords"]) / 10 <= G.GROUP_MAX_SPEED_MS + 1e-9
        if offset <= 4:
            assert result["cords"] == [0, 0]
            assert result["motion_gate"]["state"] == "held"
        if offset == 3:
            assert result["motion_gate"]["confirmation_observations"] <= 1
        assert "corroborating_beacons" not in result["motion_gate"]
        previous = result


def test_both_sources_reversing_direction_restart_the_confirmation_clock():
    previous, state = start()
    for offset in range(1, 6):
        ps = sources(offset)
        if offset >= 3:
            for p in ps:
                p["cords"][0] *= -1
        result = G.fuse_group(GROUP, ps, SCALES, NOW + offset,
                              previous=previous, stability=state)
        assert result["cords"] == [0, 0]
        if offset == 3:
            assert result["motion_gate"]["confirmation_observations"] == 1
            assert result["motion_gate"]["confirmation_span_s"] == 0
            assert "corroborating_beacons" not in result["motion_gate"]
        previous = result


def test_corroborated_recovery_cannot_spend_motion_budget_on_cached_solves():
    previous, state = start()
    for offset in range(1, 5):
        ps = sources(offset)
        previous = G.fuse_group(GROUP, ps, SCALES, NOW + offset,
                                previous=previous, stability=state)
    held_point = copy.deepcopy(previous["cords"])
    for poll in range(5, 10):
        for p in ps:
            p["updated"] = NOW + poll
        result = G.fuse_group(GROUP, ps, SCALES, NOW + poll,
                              previous=previous, stability=state,
                              use_observation_age=False)
        assert result["cords"] == held_point
        assert result["motion_gate"]["allowed_step_m"] == 0
        assert result["motion_gate"]["confirmation_observations"] == 4
        previous = result


def test_returning_member_establishes_baseline_without_consuming_other_member_epochs():
    previous, state = start()
    # A missing member is pruned, while the other member's new observation
    # waits for a jointly advancing pair instead of consuming its baseline.
    previous = G.fuse_group(GROUP, sources(1)[:1], SCALES, NOW + 1,
                            previous=previous, stability=state)
    assert state["motion_support_epochs"] == {"tag_a": NOW}
    ps = sources(2)
    previous = G.fuse_group(GROUP, ps, SCALES, NOW + 2,
                            previous=previous, stability=state)
    assert state["motion_support_epochs"] == {"tag_a": NOW, "tag_b": NOW + 2}
    assert "corroborating_beacons" not in previous["motion_gate"]
    # Recent solves and another observation from A cannot advance B's baseline.
    ps = sources(3)
    ps[1]["outdoor"]["observed"] = NOW + 2
    previous = G.fuse_group(GROUP, ps, SCALES, NOW + 3,
                            previous=previous, stability=state,
                            use_observation_age=False)
    assert state["motion_support_epochs"] == {"tag_a": NOW, "tag_b": NOW + 2}
    assert "corroborating_beacons" not in previous["motion_gate"]
    # B's next distinct observation completes the pair with A's waiting one.
    ps = sources(4)
    ps[0]["outdoor"]["observed"] = NOW + 3
    result = G.fuse_group(GROUP, ps, SCALES, NOW + 4,
                          previous=previous, stability=state,
                          use_observation_age=False)
    assert state["motion_support_epochs"] == {"tag_a": NOW + 3, "tag_b": NOW + 4}
    assert result["motion_gate"]["corroborating_beacons"] == ["tag_a", "tag_b"]
