"""Pure experimental distance bands from detached Bermuda radio measurements.

The band propagates observed RSSI variation through the captured exponential
model. It is neither a confidence interval nor a bound on systematic bias.
Nothing here changes calibration or reads live Home Assistant state.
"""

import math

import numpy as np
from scipy.optimize import least_squares

from .solver_settings import SOLVER_ROBUST_F_SCALE


MAX_PACKET_AGE_S = 3.0
MIN_QUALITY_SAMPLES = 5
NOISE_FLOOR_DB = 0.5
NOISE_MULTIPLIER = 2.0
CENTER_TIE_STRENGTH = 0.05
MAX_NFEV = 200
JAC_MIN_DIST = 1e-9
MEANING = "signal_variation_only; not a confidence interval or systematic-bias bound"


def _finite(value, minimum=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value) and (minimum is None or value >= minimum)
    except OverflowError:
        return False


def _unavailable(reason, **evidence):
    return {"status": "unavailable", "reason": reason, "solver_eligible": False,
            "solver_reason": reason, **evidence}


def signal_interval(ad, now):
    """Build a raw slant-distance variation band from one native pair snapshot.

    The filtered range is checked only for optional solver eligibility. A fresh
    rejected raw packet can remain useful for diagnostics without steering the
    experimental position solve.
    """
    if not isinstance(ad, dict):
        return _unavailable("measurement_unavailable")
    observed = ad.get("observed") if _finite(ad.get("observed")) else None
    stamp = ad.get("stamp") if _finite(ad.get("stamp")) else None
    model_version = ad.get("model_version")
    model_version = model_version[:96] if isinstance(model_version, str) else None
    evidence = {"source_observed": observed, "source_stamp": stamp,
                "model_version": model_version}
    if not _finite(now, 0):
        return _unavailable("invalid_capture_clock", **evidence)
    if ad.get("status") != "current":
        return _unavailable("source_not_current", **evidence)
    clock = ad.get("observed_clock")
    age = ad.get("age")
    if (not isinstance(clock, str) or not clock or clock.lower() in
            ("unknown", "unavailable", "none", "capture", "acceptance") or observed is None or stamp is None or
            not _finite(age, 0) or observed < 0 or observed > now):
        return _unavailable("source_clock_unavailable_or_future", **evidence)
    if max(age, now - observed) > MAX_PACKET_AGE_S:
        return _unavailable("raw_packet_stale", **evidence)
    quality = ad.get("quality") if isinstance(ad.get("quality"), dict) else {}
    samples, noise = quality.get("samples"), quality.get("noise_db")
    if not _finite(samples, MIN_QUALITY_SAMPLES) or samples != int(samples):
        return _unavailable("insufficient_quality_samples", **evidence)
    if not _finite(noise, 0):
        return _unavailable("noise_unavailable", **evidence)
    raw_rssi = ad.get("raw_rssi", ad.get("rssi"))
    if not _finite(raw_rssi) or not -127 <= raw_rssi < 0:
        return _unavailable("raw_rssi_unavailable", **evidence)
    reference = ad.get("reference_power")
    offset = ad.get("rssi_offset")
    attenuation = ad.get("attenuation")
    raw_distance = ad.get("raw_distance")
    if (not _finite(reference) or not _finite(offset) or
            not _finite(attenuation) or attenuation <= 0 or
            not _finite(raw_distance) or raw_distance <= 0):
        return _unavailable("invalid_radio_model", **evidence)
    try:
        calculated = 10 ** ((reference - raw_rssi - offset) / (10 * attenuation))
        delta_db = NOISE_MULTIPLIER * max(noise, NOISE_FLOOR_DB)
        lower = 10 ** ((reference - (raw_rssi + delta_db) - offset) / (10 * attenuation))
        upper = 10 ** ((reference - (raw_rssi - delta_db) - offset) / (10 * attenuation))
    except (OverflowError, ZeroDivisionError):
        return _unavailable("interval_nonfinite", **evidence)
    if not all(_finite(v, 0) for v in (calculated, lower, upper, delta_db)) or \
            lower <= 0 or upper <= 0:
        return _unavailable("interval_nonfinite", **evidence)
    if not math.isclose(calculated, raw_distance, rel_tol=1e-5, abs_tol=1e-6):
        return _unavailable("radio_model_mismatch", **evidence)
    if not lower < raw_distance < upper:
        return _unavailable("interval_degenerate", **evidence)

    filtered = ad.get("filtered_distance")
    filter_age = quality.get("age")
    mode = quality.get("mode")
    last_valid_stamp = quality.get("last_valid_stamp")
    last_valid_observed = quality.get("last_valid_observed")
    solver_reason = None
    if not _finite(filtered) or filtered <= 0:
        solver_reason = "filtered_distance_unavailable"
    elif quality.get("status") != "current":
        solver_reason = "filter_not_current"
    elif not _finite(filter_age, 0) or filter_age > MAX_PACKET_AGE_S:
        solver_reason = "filter_clock_stale_or_unknown"
    elif mode in ("pet", "stationary") and (
            not _finite(last_valid_stamp) or last_valid_stamp != stamp or
            not _finite(last_valid_observed, 0) or last_valid_observed > now or
            now - last_valid_observed > MAX_PACKET_AGE_S):
        solver_reason = "raw_packet_not_accepted_by_filter"
    elif mode not in (None, "legacy", "pet", "stationary"):
        solver_reason = "filter_mode_unknown"
    return {
        "status": "available", "reason": None,
        "lower_m": lower, "center_m": raw_distance, "upper_m": upper,
        "noise_db": noise, "noise_floor_db": NOISE_FLOOR_DB,
        "noise_multiplier": NOISE_MULTIPLIER, "delta_db": delta_db,
        "quality_samples": samples, "filter_mode": mode if isinstance(mode, str) else None,
        "filter_age_s": filter_age if _finite(filter_age, 0) else None,
        "last_valid_stamp": last_valid_stamp if _finite(last_valid_stamp) else None,
        "last_valid_observed": last_valid_observed if _finite(last_valid_observed) else None,
        "solver_eligible": solver_reason is None, "solver_reason": solver_reason,
        "meaning": MEANING, **evidence,
    }


def project_interval(interval, correction_factor, dz_m, scale):
    """Monotonically project a slant band to horizontal metres and map pixels."""
    if not isinstance(interval, dict) or interval.get("status") != "available":
        return {"status": "unavailable", "reason": "source_interval_unavailable"}
    lower, center, upper = (interval.get(key) for key in ("lower_m", "center_m", "upper_m"))
    if (not all(_finite(v, 0) for v in (lower, center, upper)) or
            lower <= 0 or not lower <= center <= upper):
        return {"status": "unavailable", "reason": "invalid_slant_interval"}
    if (not _finite(correction_factor) or correction_factor <= 0 or
            not _finite(dz_m) or not _finite(scale) or scale <= 0):
        return {"status": "unavailable", "reason": "invalid_projection_parameters"}

    def horizontal(slant):
        corrected = slant * correction_factor
        if not _finite(corrected, 0):
            return None
        if corrected <= abs(dz_m):
            return 0.0
        # This form avoids squaring large finite slant ranges before the root.
        return corrected * math.sqrt(max(0.0, 1 - (dz_m / corrected) ** 2))

    metres = [horizontal(slant) for slant in (lower, center, upper)]
    if any(value is None or not _finite(value, 0) for value in metres):
        return {"status": "unavailable", "reason": "projected_interval_nonfinite"}
    pixels = [value * scale for value in metres]
    if any(not _finite(value, 0) for value in pixels):
        return {"status": "unavailable", "reason": "projected_interval_nonfinite"}
    return {"status": "available", "reason": None,
            "lower_m": metres[0], "center_m": metres[1], "upper_m": metres[2],
            "lower_px": pixels[0], "center_px": pixels[1], "upper_px": pixels[2]}


def _bounds_for_row(value, point_radius, min_weight_radius):
    if isinstance(value, dict):
        if value.get("status") != "available":
            return None
        lower, upper = value.get("lower_px"), value.get("upper_px")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        lower, upper = value
    else:
        return None
    if not _finite(lower, 0) or not _finite(upper, 0) or upper - lower <= 1e-9:
        return None
    tolerance = max(1e-3, 1e-6 * max(1.0, point_radius))
    floor_exception = lower <= tolerance and point_radius <= min_weight_radius + tolerance
    if point_radius < lower - tolerance or (point_radius > upper + tolerance and not floor_exception):
        return None
    return lower, upper


def solve_with_intervals(points, intervals, *, bounds=None, min_weight_radius=1e-3,
                         point_solver, telemetry=None):
    """Refine an ordinary point solve with soft RSSI bands when valid.

    Invalid bands retain exact point residuals. The ordinary solver is called
    once for the warm start and returned unchanged whenever the refinement is
    unavailable or fails.
    """
    def note(applied, reason):
        if isinstance(telemetry, dict):
            telemetry.update(applied=applied, reason=reason)

    note(False, "no_usable_intervals")
    original = point_solver(points, bounds=bounds, min_weight_radius=min_weight_radius)
    if original is None:
        note(False, "point_seed_failed")
        return original
    if not isinstance(intervals, (list, tuple)) or len(intervals) != len(points):
        return original
    if not _finite(min_weight_radius) or min_weight_radius <= 0:
        return original
    try:
        rows = [tuple(point) for point in points]
        if len(rows) < 3 or any(len(point) != 5 for point in rows):
            return original
        px, py, radius, reliability, slant = [np.array([point[index] for point in rows],
                                                       dtype=float) for index in range(5)]
        if (not all(np.isfinite(array).all() for array in (px, py, radius, reliability, slant)) or
                np.any(radius < 0) or np.any(slant < 0) or np.any(reliability < 0) or
                np.any(reliability > 1)):
            return original
        sqrt_w = np.sqrt(reliability) / np.maximum(slant, min_weight_radius)
        low, high, band = radius.copy(), radius.copy(), np.zeros(len(rows), dtype=bool)
        for index, value in enumerate(intervals):
            valid = _bounds_for_row(value, float(radius[index]), min_weight_radius)
            if valid is not None and reliability[index] > 0:
                low[index], high[index] = valid
                band[index] = True
        if not np.any(band):
            return original
        x0 = np.asarray(original, dtype=float)
        if x0.shape != (2,) or not np.isfinite(x0).all():
            return original
        if bounds is None:
            lo, hi = [-np.inf, -np.inf], [np.inf, np.inf]
        elif (isinstance(bounds, (list, tuple)) and len(bounds) == 4 and
              all(_finite(value) for value in bounds) and
              bounds[0] < bounds[2] and bounds[1] < bounds[3]):
            lo, hi = [bounds[0], bounds[1]], [bounds[2], bounds[3]]
            x0 = np.clip(x0, lo, hi)
        else:
            return original

        def residual(position):
            distance = np.hypot(position[0] - px, position[1] - py)
            primary = np.where(band, np.where(distance < low, distance - low,
                                               np.where(distance > high, distance - high, 0.0)),
                               distance - radius)
            tie = CENTER_TIE_STRENGTH * sqrt_w[band] * (distance[band] - radius[band])
            return np.concatenate((sqrt_w * primary, tie))

        def jacobian(position):
            dx, dy = position[0] - px, position[1] - py
            distance = np.maximum(np.hypot(dx, dy), JAC_MIN_DIST)
            active = np.where(band, (np.hypot(dx, dy) < low) |
                              (np.hypot(dx, dy) > high), True)
            first = np.column_stack((sqrt_w * active * dx / distance,
                                     sqrt_w * active * dy / distance))
            tie = np.column_stack((CENTER_TIE_STRENGTH * sqrt_w[band] * dx[band] / distance[band],
                                   CENTER_TIE_STRENGTH * sqrt_w[band] * dy[band] / distance[band]))
            return np.vstack((first, tie))

        result = least_squares(residual, x0, jac=jacobian, bounds=(lo, hi),
                               method="trf", loss="soft_l1",
                               f_scale=SOLVER_ROBUST_F_SCALE, max_nfev=MAX_NFEV)
        if result.success and np.shape(result.x) == (2,) and np.isfinite(result.x).all():
            note(True, "applied")
            return float(result.x[0]), float(result.x[1])
    except (ArithmeticError, TypeError, ValueError):
        pass
    note(False, "interval_solve_failed")
    return original
