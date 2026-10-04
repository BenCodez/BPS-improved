"""Behavioral tests of the estimated-radius heuristic, not statistical claims."""
import math

import pytest

from bps.uncertainty import MAX_UNCERTAINTY_M, estimate_uncertainty, uncertainty_radius_px


SURROUNDING = [(10, 0), (0, 10), (-10, 0), (0, -10), (7, 7), (-7, -7)]


def estimate(receivers=SURROUNDING, *, residual=0, weight=1, age=0,
             environment=1, classification="clear", scale=1, previous=None, bounds=None,
             reflection=False):
    weighted = [(x * scale, y * scale, (math.hypot(x, y) + residual) * scale,
                 weight, math.hypot(x, y) * scale) for x, y in receivers]
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


def test_motion_and_clamping_increase_uncertainty():
    assert radius() < radius(previous=(40, 0))
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
    assert result["uncertainty_method"] == "conservative_heuristic"


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
