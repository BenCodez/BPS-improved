"""Finite, conservative estimated uncertainty, expressed as a radius in metres.

This is an empirical heuristic, NOT a covariance or confidence interval. A
small residual alone can hide poor geometry or correlated RF errors. We add a
range-dependent noise floor, then account for directional coverage, receiver
count, reliability, age, obstruction, map clipping, and recent movement.
Constants are centralized here so real property recordings can tune them.
"""
import math
from statistics import median

from .environment import MAX_COORDINATE_PX, finite_number

MAX_UNCERTAINTY_M = 10_000.0
BASE_NOISE_M = 0.75
RANGE_NOISE_FRACTION = 0.05
REFERENCE_RECEIVERS = 6.0
REFERENCE_AGE_S = 30.0
MOTION_NOISE_FRACTION = 0.15


def _point(value):
    if isinstance(value, dict):
        if "position" in value:
            return _point(value["position"])
        value = (value.get("x"), value.get("y"))
    if not isinstance(value, (list, tuple)) or len(value) < 2:
        return None
    numbers = tuple(finite_number(v, minimum=-MAX_COORDINATE_PX,
                                  maximum=MAX_COORDINATE_PX) for v in value[:2])
    return numbers if all(v is not None for v in numbers) else None


def _geometry(point, samples):
    """Use bearing information plus angular coverage; detect one-sided fixes.

    Opposing but collinear bearings are poor (small information eigenvalue).
    Bearings spanning only one half-plane are also poor even when their
    eigenvalues look reasonable: range errors can all push the fix together.
    """
    xx = yy = xy = total = mean_x = mean_y = 0.0
    angles = []
    for x, y, _radius, weight in samples:
        dx, dy = x - point[0], y - point[1]
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        ux, uy = dx / length, dy / length
        xx += weight * ux * ux
        yy += weight * uy * uy
        xy += weight * ux * uy
        mean_x += weight * ux
        mean_y += weight * uy
        total += weight
        angles.append(math.atan2(dy, dx) % (2 * math.pi))
    if total <= 0 or len(angles) < 2:
        return 8.0, 360.0
    xx, yy, xy = xx / total, yy / total, xy / total
    eigenvalue = max(0.0, (xx + yy - math.hypot(xx - yy, 2 * xy)) / 2)
    factor = min(6.0, math.sqrt(0.5 / max(0.005, eigenvalue)))
    angles.sort()
    gaps = [b - a for a, b in zip(angles, angles[1:])]
    gaps.append(angles[0] + 2 * math.pi - angles[-1])
    maximum_gap = max(gaps)
    factor *= 1.0 + 1.5 * max(0.0, maximum_gap / math.pi - 1.0)
    # A semicircle can have a 180-degree gap and balanced eigenvalues while
    # still placing all useful receivers on one side. The resultant bearing
    # catches that common property-edge geometry without a fragile threshold.
    factor *= 1.0 + 1.25 * min(1.0, math.hypot(mean_x, mean_y) / total)
    return min(8.0, max(1.0, factor)), math.degrees(maximum_gap)


def estimate_uncertainty(fix, weighted, scale, diagnostics, bounds=None, previous=None):
    """Estimate map-scaled uncertainty from (x,y,r,w[,slant_px]) readings.

    Radii/residuals use horizontal pixels, exactly as the existing solver.
    ``scale`` is pixels per metre. ``previous`` may be a pixel (x,y) tuple or
    a dict containing x/y or position. Invalid samples are omitted, zero-weight
    exclusions are omitted, and invalid fix/scale returns a finite poor result.
    Diagnostics are per-reading JSON dictionaries from environment helpers.
    """
    point = _point(fix)
    pixels_per_metre = finite_number(scale, minimum=1e-6, maximum=1e6)
    records = diagnostics if isinstance(diagnostics, (list, tuple)) else []
    records = [d for d in records if isinstance(d, dict) and
               d.get("excluded") is not True and d.get("used") is not False]
    result = {"estimated_uncertainty_m": MAX_UNCERTAINTY_M, "confidence": "poor",
              "receivers_used": 0, "clear_paths": 0, "vegetation_paths": 0,
              "building_paths": 0, "custom_paths": 0, "downweighted_receivers": 0,
              "freshness_unknown": 0, "reading_age_s": None, "residual_m": 0.0,
              "geometry_factor": 8.0, "largest_bearing_gap_deg": 360.0,
              "near_bounds": False, "motion_m": 0.0,
              "reflection_risk_paths": 0,
              "uncertainty_method": "conservative_heuristic"}
    samples = []
    if isinstance(weighted, (list, tuple)):
        for sample in weighted:
            if not isinstance(sample, (list, tuple)) or len(sample) < 4:
                continue
            location = _point(sample)
            radius = finite_number(sample[2], minimum=0.0, maximum=1e9)
            weight = finite_number(sample[3], minimum=0.0, maximum=1.0)
            if location is not None and radius is not None and weight is not None and weight > 0:
                samples.append((*location, radius, weight))
    result["receivers_used"] = len(samples)
    if point is None or pixels_per_metre is None or not samples:
        return result
    total_weight = sum(s[3] for s in samples)
    rms = math.sqrt(sum(w * (math.hypot(x - point[0], y - point[1]) - r) ** 2
                        for x, y, r, w in samples) / total_weight) / pixels_per_metre
    geometry_factor, gap = _geometry(point, samples)
    # Normalize first so tiny but valid weights cannot underflow w*w to zero.
    maximum_weight = max(s[3] for s in samples)
    normalized_weights = [s[3] / maximum_weight for s in samples]
    effective_count = sum(normalized_weights) ** 2 / sum(w * w for w in normalized_weights)
    count_factor = max(1.0, math.sqrt(REFERENCE_RECEIVERS / effective_count))
    weight_factor = min(3.0, math.sqrt(len(samples) / total_weight))
    result["downweighted_receivers"] = sum(s[3] < 0.999 for s in samples)
    ages, environmental_penalties = [], []
    for diagnostic in records:
        classification = diagnostic.get("classification", "unknown")
        if classification == "clear":
            result["clear_paths"] += 1
        elif classification == "building":
            result["building_paths"] += 1
        elif classification in {"dense_trees", "light_vegetation"}:
            result["vegetation_paths"] += 1
        elif classification == "custom":
            result["custom_paths"] += 1
        weight = finite_number(diagnostic.get("environmental_weight"), 1.0,
                               minimum=0.0, maximum=1.0)
        environmental_penalties.append(1.0 - weight)
        if diagnostic.get("reflection_risk") is True:
            result["reflection_risk_paths"] += 1
        age = finite_number(diagnostic.get("reading_age_s"), minimum=0.0, maximum=1e9)
        if age is None:
            result["freshness_unknown"] += 1
        else:
            ages.append(age)
    unknown = max(0, len(samples) - len(records))
    result["freshness_unknown"] += unknown
    mean_age = sum(ages) / len(ages) if ages else None
    freshness_factor = 1.0 + (min(2.0, mean_age / REFERENCE_AGE_S)
                               if mean_age is not None else 0.0)
    freshness_factor += 0.10 * min(1.0, result["freshness_unknown"] / len(samples))
    environment_factor = 1.0 + (sum(environmental_penalties) / len(environmental_penalties)
                                 if environmental_penalties else 0.0)
    environment_factor += 0.15 * min(1.0, result["reflection_risk_paths"] / len(samples))
    baseline = BASE_NOISE_M + RANGE_NOISE_FRACTION * median(s[2] / pixels_per_metre for s in samples)
    uncertainty = math.hypot(baseline, rms) * geometry_factor * count_factor * weight_factor
    uncertainty *= freshness_factor * environment_factor
    motion = 0.0
    previous_point = _point(previous)
    if previous_point is not None:
        motion = math.dist(point, previous_point) / pixels_per_metre
        uncertainty = math.hypot(uncertainty, MOTION_NOISE_FRACTION * min(1000.0, motion))
    near_bounds = False
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        numbers = [finite_number(b, minimum=-MAX_COORDINATE_PX,
                                 maximum=MAX_COORDINATE_PX) for b in bounds]
        if all(b is not None for b in numbers):
            minx, miny, maxx, maxy = numbers
            if minx < maxx and miny < maxy:
                edge_distance = min(point[0] - minx, maxx - point[0],
                                    point[1] - miny, maxy - point[1]) / pixels_per_metre
                near_bounds = edge_distance <= max(1.0, baseline)
                if near_bounds:
                    uncertainty *= 1.4 if edge_distance <= 1e-6 else 1.15
    uncertainty = max(0.0, min(MAX_UNCERTAINTY_M, uncertainty))
    result.update(estimated_uncertainty_m=uncertainty,
                  confidence="good" if uncertainty <= 3 else ("moderate" if uncertainty <= 10 else "poor"),
                  residual_m=rms, geometry_factor=geometry_factor,
                  largest_bearing_gap_deg=gap, effective_receivers=effective_count,
                  reading_age_s=mean_age, motion_m=motion, near_bounds=near_bounds)
    return result


def uncertainty_radius_px(uncertainty_m, scale):
    """Map conversion, useful to clients/tests; never substitute a pixel radius."""
    radius = finite_number(uncertainty_m, 0.0, minimum=0.0, maximum=MAX_UNCERTAINTY_M)
    pixels_per_metre = finite_number(scale, 0.0, minimum=1e-6, maximum=1e6)
    return radius * pixels_per_metre
