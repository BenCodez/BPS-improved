"""Bounded, executor-side extension of the existing BPS weighted solver.

No calibration state or measurements are changed here. Environmental trust is
computed from the preliminary fix and multiplied into the original jump weight
once. At most two solves are performed; a failed refinement retains the first.
"""
import math

from .environment import compile_environment, measurement_reliability, finite_number
from .uncertainty import estimate_uncertainty


def solve_outdoor(floor, weighted, bounds, scale, min_weight_radius, max_age_s,
                  previous, solver):
    """Return an outdoor candidate, or None with fewer than three usable inputs.

    ``weighted`` and ``floor.receivers`` share the ordering of the live candidate
    extraction. All work is on a per-tracker snapshot in HA's executor.
    """
    receivers = [r for r in floor.get("receivers", [])
                 if r.get("distance") is not None and "r" in r.get("cords", {})]
    if finite_number(scale, minimum=1e-6, maximum=1e6) is None:
        return None
    selected = [(pt, rec) for pt, rec in zip(weighted, receivers)
                if rec.get("outdoor_policy") != "ignore"]
    if len(selected) < 3:
        return None
    base = [pt for pt, _rec in selected]
    fix = solver(base, bounds=bounds, min_weight_radius=min_weight_radius)
    if fix is None:
        return None
    environment = compile_environment(floor)
    diagnostics = []
    adjusted = []
    for pt, rec in selected:
        reading = rec.get("_outdoor_reading", {})
        diagnostic = measurement_reliability(
            environment, pt[:2], fix, policy=rec.get("outdoor_policy", "auto"),
            age_s=reading.get("reading_age_s"), max_age_s=max_age_s,
            base_weight=pt[3],
        )
        diagnostic.update({
            "receiver": rec.get("entity_id", ""),
            "measured_distance_m": reading.get("measured_distance_m"),
            "corrected_distance_m": rec["distance"],
        })
        # The environment helper clamps the final product, not a distance.
        weight = diagnostic["reliability_weight"]
        adjusted.append((*pt[:3], weight, pt[4]))
        diagnostics.append(diagnostic)
    if any(a[3] != b[3] for a, b in zip(adjusted, base)):
        refined = solver(adjusted, bounds=bounds, min_weight_radius=min_weight_radius)
        if refined is not None:
            fix, base = refined, adjusted
        else:
            # Telemetry must describe the weights that actually produced the fix.
            for diagnostic, pt in zip(diagnostics, base):
                diagnostic["reliability_weight"] = pt[3]
                diagnostic["status"] = "refinement_failed"
    quality = estimate_uncertainty(
        fix, base, scale, diagnostics, bounds=bounds, previous=previous,
    )
    quality["receiver_diagnostics"] = diagnostics
    quality["stale_after_s"] = max_age_s or 30.0
    quality["stale"] = False
    quality["position_age_s"] = 0.0
    ages = [d["reading_age_s"] for d in diagnostics if d.get("reading_age_s") is not None]
    quality["newest_reading_age_s"] = min(ages) if ages else None
    # Preserve explicit exclusions in diagnostics, but never feed zero weights
    # into the normal solver or claim excluded receivers corroborate the fix.
    for rec in receivers:
        if rec.get("outdoor_policy") == "ignore":
            quality["receiver_diagnostics"].append({
                "receiver": rec.get("entity_id", ""), "status": "excluded",
                "classification": "manual_ignore", "reliability_weight": 0.0,
                "environmental_weight": 1.0, "building_crossings": 0,
                **rec.get("_outdoor_reading", {}),
                "corrected_distance_m": rec["distance"],
            })
    return {"fix": fix, "weighted": base, "outdoor": quality}


def account_for_published_position(quality, raw, published, scale):
    """Include Kalman lag and map/zone clamping in the displayed heuristic."""
    displacement = math.dist(raw, published) / scale
    quality["publication_displacement_m"] = displacement
    quality["estimated_uncertainty_m"] = math.hypot(
        quality["estimated_uncertainty_m"], displacement,
    )
    radius = quality["estimated_uncertainty_m"]
    quality["confidence"] = "good" if radius <= 3 else "moderate" if radius <= 10 else "poor"
