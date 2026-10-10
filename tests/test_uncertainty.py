"""Behavioral tests of the estimated-radius heuristic, not statistical claims."""
import math

import pytest

from bps import trilaterate
from bps.uncertainty import MAX_UNCERTAINTY_M, estimate_uncertainty, uncertainty_radius_px
from bps.environment import measurement_reliability


SURROUNDING = [(10, 0), (0, 10), (-10, 0), (0, -10), (7, 7), (-7, -7)]


def estimate(receivers=SURROUNDING, *, residual=0, weight=1, age=0,
             environment=1, classification="clear", scale=1, previous=None, bounds=None,
             reflection=False):
    freshness = measurement_reliability([], (0, 0), (0, 0), age_s=age)["freshness_weight"]
    reliability = weight * environment * freshness * (0.85 if reflection else 1)
    weighted = [(x * scale, y * scale, (math.hypot(x, y) + residual) * scale,
                 reliability, math.hypot(x, y) * scale) for x, y in receivers]
    diagnostics = [{"reading_age_s": age, "environmental_weight": environment,
                    "classification": classification, "reflection_risk": reflection}
                   for _ in receivers]
    return estimate_uncertainty((0, 0), weighted, scale, diagnostics, previous=previous, bounds=bounds)


def radius(**kwargs):
    return estimate(**kwargs)["estimated_uncertainty_m"]


def test_surrounding_receivers_better_than_one_sided_or_collinear():
    assert radius() < radius(receivers=[(10, -8), (10, -4), (10, -1), (10, 1), (10, 4), (10, 8)])
    assert radius() < radius(receivers=[(-10, 0), (-8, 0), (-5, 0), (5, 0), (8, 0), (10, 0)])
    semicircle = [(0, -10), (8.66, -5), (8.66, 5), (0, 10)]
    assert radius(receivers=SURROUNDING[:4]) < radius(receivers=semicircle)


def test_fewer_receivers_and_large_residual_increase_uncertainty():
    assert radius() < radius(receivers=SURROUNDING[:3])
    assert radius() < radius(residual=8)
    assert estimate(residual=8)["residual_m"] == pytest.approx(8)


def test_obstructed_stale_downweighted_and_unknown_age_increase_uncertainty():
    baseline = radius()
    assert baseline < radius(environment=0.2, classification="building")
    assert baseline < radius(age=25)
    assert baseline < radius(weight=0.1)
    assert baseline < radius(age=None)
    assert baseline < radius(reflection=True)


def test_movement_is_not_error_but_clamping_increases_uncertainty():
    assert radius() == radius(previous=(40, 0))
    assert estimate(previous=(40, 0))["motion_m"] == 40
    result = estimate(bounds=(0, -100, 100, 100))
    assert result["near_bounds"] is True
    assert radius() < result["estimated_uncertainty_m"]


def test_pixel_scale_invariance_and_map_conversion():
    assert radius(scale=40) == pytest.approx(radius(scale=1))
    assert uncertainty_radius_px(7.4, 40) == pytest.approx(296)
    assert uncertainty_radius_px(float("nan"), 40) == 0
    assert uncertainty_radius_px(7.4, True) == 0


def test_diagnostics_and_excluded_readings():
    weighted = [(10, 0, 10, 1, 10), (0, 10, 10, 0.2, 10), (-10, 0, 10, 0, 10)]
    diagnostics = [{"classification": "clear", "reading_age_s": 2},
                   {"classification": "dense_trees", "reading_age_s": 4},
                   {"classification": "building", "excluded": True}]
    result = estimate_uncertainty((0, 0), weighted, 1, diagnostics)
    assert result["receivers_used"] == 2
    assert result["clear_paths"] == result["vegetation_paths"] == 1
    assert result["building_paths"] == 0
    assert result["reading_age_s"] == 3
    assert result["downweighted_receivers"] == 1
    assert result["uncertainty_method"] == "robust_solver_weighted_heuristic"


def test_path_and_age_diagnostics_do_not_count_reliability_twice():
    weighted = [(x, y, math.hypot(x, y) + 2, 0.2, math.hypot(x, y)) for x, y in SURROUNDING]
    clear = [{"classification": "clear", "environmental_weight": 1, "reading_age_s": 0} for _ in weighted]
    obstructed = [{"classification": "building", "environmental_weight": 0.2,
                   "reflection_risk": True, "reading_age_s": 29} for _ in weighted]
    first = estimate_uncertainty((0, 0), weighted, 1, clear)
    second = estimate_uncertainty((0, 0), weighted, 1, obstructed)
    assert first["estimated_uncertainty_m"] == pytest.approx(second["estimated_uncertainty_m"])
    assert second["building_paths"] == second["reflection_risk_paths"] == 6
    assert second["reading_age_s"] == 29


def test_far_receiver_outlier_has_the_same_small_influence_as_in_the_solver():
    weighted = [(x, y, math.hypot(x, y) + 3, 0.4, math.hypot(x, y)) for x, y in SURROUNDING]
    diagnostics = [{"reading_age_s": 25, "classification": "building",
                    "environmental_weight": 0.4} for _ in weighted]
    ordinary = estimate_uncertainty((0, 0), weighted, 1, diagnostics)
    weighted.append((300, 0, 600, 0.05, 600))
    diagnostics.append({"reading_age_s": 25, "classification": "building", "environmental_weight": 0.05})
    result = estimate_uncertainty((0, 0), weighted, 1, diagnostics)
    assert result["residual_m"] < 3.2
    assert ordinary["estimated_uncertainty_m"] <= result["estimated_uncertainty_m"] < 5


def test_height_projection_uses_slant_range_for_noise_and_influence():
    weighted = [(x, y, math.hypot(x, y), 1, math.hypot(x, y)) for x, y in SURROUNDING]
    # Horizontal projection of a 20 m slant can be zero. It must not
    # overwhelm the surrounding, nearer receivers with a 0.5 m weight radius.
    weighted.append((0.1, 0, 0, 1, 20))
    records = [{"reading_age_s": 0} for _ in weighted]
    result = estimate_uncertainty((0, 0), weighted, 1, records)
    assert result["effective_receivers"] > 6
    assert result["estimated_uncertainty_m"] < 2


def test_negligible_opposite_bearing_does_not_fill_one_sided_geometry_gap():
    angles = [-60, -30, 0, 30, 60]
    weighted = [(10 * math.cos(math.radians(a)), 10 * math.sin(math.radians(a)), 10, 1, 10)
                for a in angles]
    records = [{"reading_age_s": 0} for _ in weighted]
    baseline = estimate_uncertainty((0, 0), weighted, 1, records)
    weighted.append((-1000, 0, 1000, 0.05, 1000))
    records.append({"reading_age_s": 0})
    with_outlier = estimate_uncertainty((0, 0), weighted, 1, records)
    assert baseline["largest_bearing_gap_deg"] == pytest.approx(240)
    assert with_outlier["largest_bearing_gap_deg"] == pytest.approx(240)
    assert with_outlier["estimated_uncertainty_m"] >= baseline["estimated_uncertainty_m"] * 0.999


def test_collectively_useful_weak_bearings_still_supply_coverage():
    near = [(10 * math.cos(math.radians(a)), 10 * math.sin(math.radians(a)), 10, 1, 10)
            for a in [-60, -30, 0, 30, 60]]
    far = [(10 * math.cos(math.radians(a)), 10 * math.sin(math.radians(a)), 10, 0.1, 10)
           for a in range(120, 241, 10)]
    result = estimate_uncertainty((0, 0), near + far, 1, [{"reading_age_s": 0}] * (len(near) + len(far)))
    assert result["largest_bearing_gap_deg"] < 240


@pytest.mark.parametrize("scale", [1, 40])
@pytest.mark.parametrize("surrounding", [False, True])
def test_robust_fix_and_radius_agree_when_distant_ble_ranges_conflict(scale, surrounding):
    # Synthetic coordinates, not a replay of any user's property. The truth
    # is (0, 0); nearby ranges agree and twelve distant readings overestimate
    # by 100 m. The real solver still lands near truth, but a plain squared
    # residual produced a 31 m radius with only the two nearby receivers.
    readings = [(4, 0, 4, 1, 4), (0, 5, 5, 1, 5)]
    if surrounding:
        readings += [(-6, 0, 6, 1, 6), (0, -7, 7, 1, 7)]
    for i in range(12):
        angle, distance = 2 * math.pi * i / 12, 20 + i * 5
        readings.append((distance * math.cos(angle), distance * math.sin(angle),
                         distance + 100, 0.8, distance + 100))
    weighted = [(x * scale, y * scale, r * scale, w, slant * scale)
                for x, y, r, w, slant in readings]
    fix = trilaterate(weighted, min_weight_radius=0.5 * scale)
    assert fix is not None
    assert math.hypot(*fix) / scale < 0.2
    result = estimate_uncertainty(fix, weighted, scale,
                                  [{"reading_age_s": 0}] * len(readings))
    assert result["robust_downweighted_receivers"] == 12
    assert result["residual_m"] < 0.75 * result["unadjusted_residual_m"]
    if surrounding:
        assert result["estimated_uncertainty_m"] < 6
    else:
        # Weak geometry still deserves a conservative circle; robust loss
        # must not make this indistinguishable from surrounding coverage.
        assert 15 < result["estimated_uncertainty_m"] < 22
        assert result["geometry_factor"] > 3


def test_uniformly_conflicting_ranges_do_not_become_precise_after_robust_weighting():
    points = SURROUNDING[:4]
    good = estimate(receivers=points)
    bad = estimate(receivers=points, residual=30)
    assert bad["robust_downweighted_receivers"] == 4
    assert bad["residual_m"] == pytest.approx(30)
    assert bad["unadjusted_residual_m"] == pytest.approx(30)
    assert bad["geometry_factor"] == good["geometry_factor"]
    assert bad["effective_receivers"] == good["effective_receivers"]
    assert bad["estimated_uncertainty_m"] > 30
    assert bad["confidence"] == "poor"


@pytest.mark.parametrize("bad", [True, None, "40", float("inf"), float("nan"), -1, 0, 10**1000])
def test_invalid_scale_is_finite_poor(bad):
    result = estimate_uncertainty((0, 0), [(1, 0, 1, 1)], bad, [])
    assert result["estimated_uncertainty_m"] == MAX_UNCERTAINTY_M
    assert result["confidence"] == "poor"


@pytest.mark.parametrize("fix,weighted", [
    ((float("inf"), 0), [(1, 0, 1, 1)]),
    ((0, 0), [(1, 0, float("nan"), 1)]),
    ((0, 0), [(1, 0, 1, True)]), ((0, 0), [(1, 0, 1, -1)]),
    ((0, 0), []), (None, None), ((0, 0), [(1, 0, 1, 1e-300)]),
])
def test_invalid_or_sparse_inputs_always_finite_nonnegative(fix, weighted):
    result = estimate_uncertainty(fix, weighted, 40, [None, {"reading_age_s": float("nan")}])
    assert math.isfinite(result["estimated_uncertainty_m"])
    assert 0 <= result["estimated_uncertainty_m"] <= MAX_UNCERTAINTY_M
