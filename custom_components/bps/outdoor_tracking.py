"""Bounded, executor-side extension of the existing BPS weighted solver.

No calibration state or measurements are changed here. Optional accuracy and
relative slant-range trust form the base weight. Environmental trust is computed
from the preliminary fix and applied once to that base. At most two solves are
performed; a failed refinement retains the first.
"""
import math

from .environment import (CompiledEnvironment, compile_environment,
                          measurement_reliability, finite_number, report_geometry_failure,
                          MIN_ENVIRONMENT_WEIGHT, MIN_RELIABILITY)
from .environment_geometry import point_relation
from .solver_settings import MIN_WEIGHT_RADIUS_M
from .uncertainty import estimate_uncertainty


# These are trust heuristics, not a claim that a short RSSI range proves a
# tracker is indoors. Use slant ranges: a configured height can collapse the
# horizontal radius even for a distant or through-floor observation.
INDOOR_NEAR_RANGE_M = 3.0
INDOOR_FRESH_AGE_S = 15.0
INDOOR_CLEARANCE_SLACK_M = 0.5
INDOOR_FAR_RANGE_M = 6.0
MIN_INDOOR_RANGE_WEIGHT = 0.25
MIN_ACCURACY_WEIGHT = 0.05
NEARBY_PRIORITY_RANGE_M = 5.0
LOWER_TRUST_RANGE_M = 10.0
MIN_RANGE_TRUST = 0.15
MIN_RANGE_TRUST_DISTANCE_M = 30.0


def receiver_accuracy(receiver, slant_m):
    """Return optional expected range-error metadata and relative trust.

    The solver already uses reliability / range². Multiplying reliability by
    range² / (range² + expected_error²) adds the configured error variance once.
    The safety floor keeps every selected observation visible to the solver.
    Missing or malformed configuration preserves the existing range weights.
    """
    accuracy = finite_number(receiver.get("distance_accuracy_m"), minimum=0.0, maximum=1000.0)
    slant = finite_number(slant_m, minimum=0.0)
    factor = 1.0
    if accuracy is not None and slant is not None:
        radius = max(slant, MIN_WEIGHT_RADIUS_M)
        factor = max(MIN_ACCURACY_WEIGHT, 1.0 / (1.0 + (accuracy / radius) ** 2))
    return {"receiver_accuracy_m": accuracy, "accuracy_weight": factor}


def nearby_range_trust(observations, max_age_s, *, enabled=False):
    """Return bounded relative trust for corrected slant ranges in metres.

    The original inverse-variance weighting already favors short ranges. This
    additional soft factor never boosts a reading or removes a distant bearing.
    Normalize by the strongest fresh, reliable station so uniformly distant
    readings do not lose global trust. A stale, weak, inaccurate or untimed
    close reading cannot establish that reference. Measurements stay unchanged.
    """
    def factor(distance):
        if distance is None or distance <= NEARBY_PRIORITY_RANGE_M:
            return 1.0
        if distance >= MIN_RANGE_TRUST_DISTANCE_M:
            return MIN_RANGE_TRUST
        start, end, high, low = (
            (NEARBY_PRIORITY_RANGE_M, LOWER_TRUST_RANGE_M, 1.0, 0.5)
            if distance <= LOWER_TRUST_RANGE_M else
            (LOWER_TRUST_RANGE_M, MIN_RANGE_TRUST_DISTANCE_M, 0.5, MIN_RANGE_TRUST))
        t = (distance - start) / (end - start)
        return high + (low - high) * t * t * (3.0 - 2.0 * t)

    maximum = finite_number(max_age_s, 30.0, minimum=0.0)
    fresh_limit = min(maximum / 2 if maximum > 0 else 15.0, 15.0)
    distances = [finite_number(d.get("range_trust_distance_m"), minimum=0.0)
                 for d in observations]
    candidates = []
    if enabled:
        for index, diagnostic in enumerate(observations):
            age = finite_number(diagnostic.get("reading_age_s"), minimum=0.0)
            observed = finite_number(diagnostic.get("observed"), minimum=0.0)
            jump = finite_number(diagnostic.get("base_jump_weight"), 0.0, minimum=0.0)
            accuracy = finite_number(diagnostic.get("accuracy_weight"), 0.0, minimum=0.0)
            policy = diagnostic.get("policy", "auto")
            if (distances[index] is None or age is None or observed is None
                    or age > fresh_limit or jump < 0.5 or accuracy < 0.5
                    or policy not in {"auto", "normal", "prefer"}):
                continue
            # Combined jump and accuracy guards also reject two individually
            # borderline factors whose product is already unreliable.
            neutral = measurement_reliability(CompiledEnvironment(()), (0, 0), (0, 0),
                policy=policy, age_s=age, max_age_s=maximum, base_weight=jump * accuracy)
            if neutral["reliability_weight"] >= 0.5:
                candidates.append(index)
    reference = min((distances[i] for i in candidates), default=None)
    strongest = factor(reference) if reference is not None else 1.0
    return [{"range_trust_weight": min(1.0, factor(distance) / strongest)
             if reference is not None else 1.0,
             "range_trust_distance_m": distance,
             "nearby_priority_reference_m": reference}
            for distance in distances]


def _boundary_clearance(point, region):
    distances = []
    for start, end, _index in region.edges:
        dx, dy = end[0] - start[0], end[1] - start[1]
        t = max(0.0, min(1.0, ((point[0] - start[0]) * dx
                              + (point[1] - start[1]) * dy) / (dx * dx + dy * dy)))
        distances.append(math.dist(point, (start[0] + t * dx, start[1] + t * dy)))
    return min(distances)


def _indoor_support(environment, selected, diagnostics, scale, max_age_s):
    """Adjust trust from close same-floor indoor receivers, without moving a fix.

    The map associates the RECEIVER with a building. Clearance gradually
    strengthens short, current slant observations, while near-wall receivers
    contribute little additional evidence. Conflicting buildings (or impossible
    near-range pairs) disable this heuristic rather than choosing a winner.
    """
    buildings = [r for r in environment.regions if r.kind == "building"]
    evidence = []
    fresh_limit = min(max_age_s / 2 if max_age_s > 0 else INDOOR_FRESH_AGE_S,
                      INDOOR_FRESH_AGE_S)
    for index, ((pt, rec), diagnostic) in enumerate(zip(selected, diagnostics)):
        diagnostic.update(indoor_building=None, indoor_clearance_m=None,
                          indoor_support_strength=0.0, indoor_support_conflict=False,
                          indoor_range_weight=1.0, indoor_range_disagreement_m=0.0,
                          indoor_boundary_restoration=0.0)
        matches = [r for r in buildings if point_relation(pt[:2], r.edges, r.bounds) == 1]
        if len(matches) != 1:
            continue
        region = matches[0]
        # Duplicate ids cannot identify which intersection should be restored.
        if sum(r.identifier == region.identifier for r in buildings) != 1:
            continue
        clearance = _boundary_clearance(pt[:2], region) / scale
        diagnostic.update(indoor_building=region.identifier, indoor_clearance_m=clearance)
        measured = finite_number(diagnostic.get("measured_distance_m"), minimum=0.0)
        slant = finite_number(pt[4] / scale, minimum=0.0)
        age = diagnostic.get("reading_age_s")
        if (measured is None or slant is None or max(measured, slant) > INDOOR_NEAR_RANGE_M
                or age is None or age > fresh_limit or diagnostic["base_jump_weight"] < 0.5
                or diagnostic["accuracy_weight"] < 0.5
                or diagnostic["policy"] not in {"auto", "prefer"}):
            continue
        radius = max(measured, slant)
        strength = min(1.0, clearance / (radius + INDOOR_CLEARANCE_SLACK_M)) * diagnostic["accuracy_weight"]
        evidence.append((index, region.identifier, pt[:2], radius, strength))
    conflict = len({e[1] for e in evidence}) > 1
    for i, a in enumerate(evidence):
        for b in evidence[i + 1:]:
            # Full slant ranges bound horizontal separation conservatively.
            if math.dist(a[2], b[2]) / scale > a[3] + b[3] + 1.0:
                conflict = True
    summary = {"indoor_support_receivers": 0, "indoor_support_building": None,
               "indoor_support_strength": 0.0, "indoor_support_conflict": conflict}
    # A geometry error removes this optional trust adjustment as well.
    if conflict or any("environment_error" in d for d in diagnostics):
        for index, *_rest in evidence:
            diagnostics[index]["indoor_support_conflict"] = conflict
        return summary
    if not evidence:
        return summary
    summary.update(indoor_support_receivers=len(evidence), indoor_support_building=evidence[0][1],
                   indoor_support_strength=max(e[4] for e in evidence))
    for index, building, _xy, _radius, strength in evidence:
        diagnostic = diagnostics[index]
        diagnostic["indoor_support_strength"] = strength
        # Recompute the product before the shared floor, retaining every other
        # building/vegetation penalty. Raw crossing diagnostics stay intact.
        product = math.prod(
            item["weight"] + strength * (1.0 - item["weight"])
            if item["type"] == "building" and item["id"] == building else item["weight"]
            for item in diagnostic["intersections"])
        environment_weight = max(MIN_ENVIRONMENT_WEIGHT, product)
        diagnostic["indoor_boundary_restoration"] = environment_weight - diagnostic["environmental_weight"]
        diagnostic["environmental_weight"] = environment_weight
    for (pt, _rec), diagnostic in zip(selected, diagnostics):
        slant = pt[4] / scale
        if slant > INDOOR_FAR_RANGE_M and diagnostic["policy"] != "normal":
            for _index, _building, xy, near_range, strength in evidence:
                separation = math.dist(pt[:2], xy) / scale
                # Range circles may disagree even when the preliminary fix
                # favors the far receiver. Allow substantial RSSI noise first.
                slack = max(1.0, 0.15 * slant)
                disagreement = max(0.0, separation - near_range - slant,
                                   slant - separation - near_range)
                diagnostic["indoor_range_disagreement_m"] = max(
                    diagnostic["indoor_range_disagreement_m"], disagreement)
                gap = disagreement - slack
                if gap > 0:
                    factor = 1.0 - (1.0 - MIN_INDOOR_RANGE_WEIGHT) * strength * gap / (gap + slack)
                    diagnostic["indoor_range_weight"] = min(diagnostic["indoor_range_weight"], factor)
        # Same policy/freshness/floor rules as measurement_reliability; only
        # relative trust changes. Weak original jump weights are never raised.
        weight = pt[3] * diagnostic["environmental_weight"] * diagnostic["freshness_weight"]
        weight *= diagnostic["policy_weight"] * diagnostic["indoor_range_weight"]
        diagnostic["reliability_weight"] = min(1.0, max(min(pt[3], MIN_RELIABILITY), weight))
        diagnostic["status"] = "downweighted" if diagnostic["reliability_weight"] < 0.999 else "used"
    return summary


def solve_outdoor(floor, weighted, bounds, scale, min_weight_radius, max_age_s,
                  previous, solver, *, position_timeout_s=300.0, nearby_trust=False):
    """Return an outdoor candidate, or None with fewer than three usable inputs.

    ``weighted`` and ``floor.receivers`` share the ordering of the live candidate
    extraction. All work is on a per-tracker snapshot in HA's executor.
    """
    receivers = [r for r in floor.get("receivers", [])
                 if r.get("distance") is not None and "r" in r.get("cords", {})]
    if finite_number(scale, minimum=1e-6, maximum=1e6) is None:
        return None
    usable = [rec for rec in receivers if rec.get("outdoor_policy") != "ignore"]
    # Live floor extraction already excludes ignored receivers so they cannot
    # consume candidate-floor slots. Also accept unfiltered inputs for callers
    # exercising the pure wrapper directly.
    matching = receivers if len(weighted) == len(receivers) else usable
    if len(weighted) != len(matching):
        return None
    max_age_s = finite_number(max_age_s, 30.0, minimum=0.0, maximum=1e6)

    def excluded(rec):
        age = finite_number(rec.get("_outdoor_reading", {}).get("reading_age_s"), minimum=0.0)
        return rec.get("outdoor_policy") == "ignore" or (max_age_s > 0 and age is not None and age >= max_age_s)

    selected = [(pt, rec) for pt, rec in zip(weighted, matching)
                if not excluded(rec)]
    if len(selected) < 3:
        return None
    accuracies = [receiver_accuracy(rec, pt[4] / scale) for pt, rec in selected]
    jump_weights = [pt[3] for pt, _rec in selected]
    ranges = nearby_range_trust([
        {"range_trust_distance_m": pt[4] / scale, "base_jump_weight": pt[3],
         "policy": rec.get("outdoor_policy", "auto"), **rec.get("_outdoor_reading", {}),
         **accuracy}
        for (pt, rec), accuracy in zip(selected, accuracies)], max_age_s, enabled=nearby_trust)
    # Accuracy and relative range trust are base terms included in BOTH solves
    # exactly once; refinement and geometry fallback retain these same inputs.
    # Preserve the original tuples when unset, including weak jump weights.
    selected = [(pt if accuracy["accuracy_weight"] * trust["range_trust_weight"] == 1 else
                 (*pt[:3], pt[3] * accuracy["accuracy_weight"] * trust["range_trust_weight"], pt[4]), rec)
                for (pt, rec), accuracy, trust in zip(selected, accuracies, ranges)]
    base = [pt for pt, _rec in selected]
    fix = solver(base, bounds=bounds, min_weight_radius=min_weight_radius)
    if fix is None:
        return None
    compilation_error = None
    try:
        environment = compile_environment(floor)
    except Exception as error:
        # Pure Python exceptions are recoverable. A failed environment must
        # not prevent the existing range solver or freshness/policy handling.
        report_geometry_failure(error)
        compilation_error = type(error).__name__
        environment = CompiledEnvironment(())
    diagnostics = []
    for (pt, rec), accuracy, trust, jump_weight in zip(selected, accuracies, ranges, jump_weights):
        reading = rec.get("_outdoor_reading", {})
        diagnostic = measurement_reliability(
            environment, pt[:2], fix, policy=rec.get("outdoor_policy", "auto"),
            age_s=reading.get("reading_age_s"), max_age_s=max_age_s,
            base_weight=pt[3],
        )
        if compilation_error:
            diagnostic.update(environment_error=compilation_error, classification="unknown")
        diagnostic.update({
            "receiver": rec.get("entity_id", ""),
            "measured_distance_m": reading.get("measured_distance_m"),
            "corrected_distance_m": rec["distance"],
            "observed": reading.get("observed"),
            "base_jump_weight": jump_weight,
            **accuracy,
            **trust,
        })
        diagnostics.append(diagnostic)
    try:
        indoor = _indoor_support(environment, selected, diagnostics, scale, max_age_s)
    except Exception as error:
        report_geometry_failure(error)
        indoor = {"indoor_support_receivers": 0, "indoor_support_building": None,
                  "indoor_support_strength": 0.0, "indoor_support_conflict": False}
        # Restore neutral geometry trust, retaining policy and freshness. A
        # partial association failure must not publish partially applied trust.
        for pt, diagnostic in zip(base, diagnostics):
            neutral = measurement_reliability(CompiledEnvironment(()), pt[:2], fix,
                policy=diagnostic["policy"], age_s=diagnostic["reading_age_s"],
                max_age_s=max_age_s, base_weight=pt[3])
            diagnostic.update(neutral, environment_error=type(error).__name__,
                              classification="unknown", indoor_support_strength=0.0,
                              indoor_range_weight=1.0, indoor_range_disagreement_m=0.0,
                              indoor_boundary_restoration=0.0)
    adjusted = [(*pt[:3], d["reliability_weight"], pt[4]) for pt, d in zip(base, diagnostics)]
    if any(a[3] != b[3] for a, b in zip(adjusted, base)):
        refined = solver(adjusted, bounds=bounds, min_weight_radius=min_weight_radius)
        if refined is not None:
            fix, base = refined, adjusted
        else:
            # Telemetry must describe the weights that actually produced the fix.
            for diagnostic, pt in zip(diagnostics, base):
                diagnostic["reliability_weight"] = pt[3]
                diagnostic["status"] = "refinement_failed"
            indoor["indoor_support_applied"] = False
    indoor.setdefault("indoor_support_applied", indoor["indoor_support_receivers"] > 0)
    for pt, diagnostic in zip(base, diagnostics):
        diagnostic["horizontal_residual_m"] = (math.dist(pt[:2], fix) - pt[2]) / scale
    quality = estimate_uncertainty(
        fix, base, scale, diagnostics, bounds=bounds, previous=previous,
    )
    quality["receiver_diagnostics"] = diagnostics
    quality["range_trust_enabled"] = bool(nearby_trust)
    quality["fence_paths"] = sum(d.get("fence_paths", 0) > 0 for d in diagnostics)
    quality["fence_crossings"] = sum(d.get("fence_crossings", 0) for d in diagnostics)
    quality.update(indoor)
    if indoor["indoor_support_conflict"]:
        quality["confidence"] = "poor"
    failures = sum("environment_error" in d for d in diagnostics)
    if failures:
        quality.update(environment_fallback=True, environment_error_receivers=failures,
                       confidence="poor")
    quality["stale_after_s"] = max_age_s or finite_number(
        position_timeout_s, 300.0, minimum=0.0) or 300.0
    quality["use_observation_age"] = max_age_s > 0
    quality["stale"] = False
    quality["position_age_s"] = 0.0
    ages = [d["reading_age_s"] for d in diagnostics if d.get("reading_age_s") is not None]
    quality["newest_reading_age_s"] = min(ages) if ages else None
    observations = [finite_number(d.get("observed"), minimum=0.0) for d in diagnostics]
    quality["observed"] = max((ts for ts in observations if ts is not None), default=None)
    # Preserve explicit exclusions in diagnostics, but never feed zero weights
    # into the normal solver or claim excluded receivers corroborate the fix.
    for rec in receivers:
        if excluded(rec):
            quality["receiver_diagnostics"].append({
                "receiver": rec.get("entity_id", ""), "status": "excluded",
                "used": False, "excluded": True,
                "classification": "manual_ignore" if rec.get("outdoor_policy") == "ignore" else "stale",
                "reliability_weight": 0.0,
                "environmental_weight": 1.0, "building_crossings": 0,
                **rec.get("_outdoor_reading", {}),
                "corrected_distance_m": rec["distance"],
                **receiver_accuracy(rec, rec["distance"]),
                "range_trust_weight": 1.0,
                "range_trust_distance_m": finite_number(rec["distance"], minimum=0.0),
                "nearby_priority_reference_m": ranges[0]["nearby_priority_reference_m"],
            })
    return {"fix": fix, "weighted": base, "outdoor": quality}


def account_for_published_position(quality, raw, published, scale):
    """Include Kalman lag and map/zone clamping in the displayed heuristic."""
    displacement = math.dist(raw, published) / scale
    quality["pre_publication_uncertainty_m"] = quality["estimated_uncertainty_m"]
    quality["publication_displacement_m"] = displacement
    quality["estimated_uncertainty_m"] = math.hypot(
        quality["estimated_uncertainty_m"], displacement,
    )
    radius = quality["estimated_uncertainty_m"]
    confidence = "good" if radius <= 3 else "moderate" if radius <= 10 else "poor"
    # Moving the published point adds uncertainty; it cannot resolve a source
    # conflict or make stale evidence current. Preserve any worse assessment.
    levels = {"good": 0, "moderate": 1, "poor": 2, "stale": 3}
    if levels.get(quality.get("confidence"), 0) <= levels[confidence]:
        quality["confidence"] = confidence
