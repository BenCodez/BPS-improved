"""Receiver-centred detection when fresh ranges cannot determine direction.

The centre is a display anchor, never a trilaterated position. In particular,
two range circles do not resolve their mirror ambiguity. Callers must publish
this anchor without position smoothing, zone snapping or assigning a zone.
"""
import math

from .environment import (MAX_COORDINATE_PX, CompiledEnvironment, finite_number,
                          measurement_reliability, MAX_OBSTRUCTION_STRENGTH)
from .outdoor_tracking import nearby_range_trust, receiver_accuracy


def solve_sparse_detection(floor, weighted, scale, max_age_s, previous=None, *,
                           now, position_timeout_s=300.0, nearby_trust=False,
                           obstruction_strength=1.0):
    """Return a solver-shaped detection candidate for 1–2 distinct receivers.

    Inputs have the same ordering/units as ``solve_outdoor``: each weighted
    point is (x_px, y_px, horizontal_radius_px, jump_weight, slant_radius_px).
    ``previous`` is a complete prior published payload and affects only which
    CURRENT receiver anchors the display. Source timestamps are required; an
    old position or a persisted radius never supplies a missing observation.

    Disabling the normal reading-age gate cannot make sparse detection depend
    on unlimited historical ranges: it uses a 30-second freshness limit, or
    the configured position timeout when that is shorter.
    """
    if not isinstance(floor, dict) or not isinstance(floor.get("name"), str) or not floor["name"].strip():
        return None
    scale = finite_number(scale, minimum=1e-6, maximum=1e6)
    floor_scale = finite_number(floor.get("scale"), minimum=1e-6, maximum=1e6)
    now = finite_number(now, minimum=0.0)
    if scale is None or floor_scale != scale or now is None:
        return None
    obstruction_strength = finite_number(obstruction_strength, 1.0,
        minimum=1.0, maximum=MAX_OBSTRUCTION_STRENGTH)
    placements = floor.get("receivers")
    if not isinstance(placements, list) or not isinstance(weighted, (list, tuple)):
        return None
    receivers = [r for r in placements if isinstance(r, dict)
                 and r.get("distance") is not None and isinstance(r.get("cords"), dict)
                 and "r" in r["cords"]]
    usable = [r for r in receivers if r.get("outdoor_policy") != "ignore"]
    matching = receivers if len(receivers) == len(weighted) else usable
    if len(matching) != len(weighted):
        return None
    limit = finite_number(max_age_s, 30.0, minimum=0.0, maximum=1e6)
    if not limit:
        timeout = finite_number(position_timeout_s, 300.0, minimum=0.0) or 300.0
        limit = min(30.0, timeout)
    diagnostics, selected = [], {}

    for rec, pt in zip(matching, weighted):
        reading = rec.get("_outdoor_reading")
        reading = reading if isinstance(reading, dict) else {}
        slug = rec.get("entity_id")
        distance = finite_number(rec.get("distance"), minimum=0.0)
        measured = finite_number(reading.get("measured_distance_m"), minimum=0.0)
        observed = finite_number(reading.get("observed"), minimum=0.0)
        age = finite_number(reading.get("reading_age_s"), minimum=0.0)
        diag = {"receiver": slug if isinstance(slug, str) else "", "used": False,
                "excluded": True, "status": "excluded", "reliability_weight": 0.0,
                "observed": observed, "reading_age_s": age,
                "measured_distance_m": measured, "corrected_distance_m": distance,
                "range_trust_weight": 1.0, "range_trust_distance_m": distance,
                "nearby_priority_reference_m": None,
                "classification": "malformed"}
        diagnostics.append(diag)
        if rec.get("outdoor_policy") == "ignore":
            diag["classification"] = "manual_ignore"
            continue
        values = ([finite_number(v) for v in pt] if isinstance(pt, (list, tuple))
                  and len(pt) == 5 else [])
        cords = rec["cords"]
        placement = [finite_number(cords.get(k)) for k in ("x", "y", "r")]
        if (not isinstance(slug, str) or not slug.strip() or not values
                or any(v is None for v in values + placement)
                or distance is None or measured is None
                or max(abs(values[0]), abs(values[1])) > MAX_COORDINATE_PX
                or max(values[2], values[4]) > 1e9
                or values[2] < 0 or not 0 < values[3] <= 1 or values[4] < 0
                or any(not math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
                       for a, b in zip(values[:3], placement))
                or not math.isclose(values[4], distance * scale, rel_tol=1e-9, abs_tol=1e-9)
                or values[2] > values[4] + 1e-9):
            continue
        if age is None or observed is None or observed > now:
            diag["classification"] = "unknown_observation_time"
            continue
        age = max(age, now - observed)
        diag["reading_age_s"] = age
        if age >= limit:
            diag["classification"] = "stale"
            continue
        pt = tuple(values)
        accuracy = receiver_accuracy(rec, pt[4] / scale)
        reliability = measurement_reliability(
            CompiledEnvironment(()), pt[:2], pt[:2], policy=rec.get("outdoor_policy", "auto"),
            age_s=age, max_age_s=limit, base_weight=pt[3] * accuracy["accuracy_weight"],
            scale_px_per_m=scale, obstruction_strength=obstruction_strength)
        diag.update(reliability, **accuracy, classification="direction_unknown", path_assessed=False,
                    base_jump_weight=pt[3], range_trust_distance_m=pt[4] / scale,
                    horizontal_distance_m=pt[2] / scale, radius_px=pt[2])
        prior = selected.get(slug)
        # A repeated placement cannot count as a second scanner. Prefer the
        # newest source reading, retaining deterministic order for exact ties.
        if prior and prior[1]["observed"] >= observed:
            diag.update(used=False, excluded=True, status="excluded", classification="duplicate_receiver")
            continue
        if prior:
            prior[1].update(used=False, excluded=True, status="excluded", classification="duplicate_receiver")
        selected[slug] = (pt, diag)

    # Ignored receivers may have been removed by live candidate extraction;
    # retain their diagnostics without reintroducing them as solver inputs.
    for rec in receivers:
        if rec.get("outdoor_policy") == "ignore" and rec not in matching:
            reading = rec.get("_outdoor_reading") or {}
            diagnostics.append({"receiver": rec.get("entity_id", ""), "used": False,
                "excluded": True, "status": "excluded", "classification": "manual_ignore",
                "reliability_weight": 0.0,
                "range_trust_weight": 1.0,
                "range_trust_distance_m": finite_number(rec.get("distance"), minimum=0.0),
                "nearby_priority_reference_m": None,
                "observed": finite_number(reading.get("observed"), minimum=0.0) if isinstance(reading, dict) else None})
    if not 1 <= len(selected) <= 2:
        return None

    trusts = nearby_range_trust([d for _pt, d in selected.values()], limit, enabled=nearby_trust)
    for (pt, diagnostic), trust in zip(selected.values(), trusts):
        diagnostic["floor_election_weight"] = diagnostic["reliability_weight"]
        diagnostic.update(trust)
        diagnostic["reliability_weight"] *= trust["range_trust_weight"]
        if diagnostic["reliability_weight"] < 0.999:
            diagnostic["status"] = "downweighted"

    def rank(item, weight_key):
        _pt, diag = item
        # Slant ranges avoid rewarding a high-mounted receiver whose horizontal
        # projection collapsed. Weak jump/freshness/policy trust costs range.
        return diag["corrected_distance_m"] / math.sqrt(diag[weight_key])

    def choose_anchor(weight_key, previous_key):
        anchor = min(selected.values(), key=lambda item: (rank(item, weight_key), item[1]["receiver"]))
        if isinstance(previous, dict) and previous.get("floor") == floor["name"]:
            quality = previous.get("outdoor")
            previous_slug = quality.get(previous_key, quality.get("detection_receiver")) \
                if isinstance(quality, dict) else None
            prior = selected.get(previous_slug) if isinstance(previous_slug, str) else None
            if prior and rank(prior, weight_key) <= rank(anchor, weight_key) + max(1.0, rank(anchor, weight_key) * 0.20):
                anchor = prior
        return anchor

    anchor = choose_anchor("reliability_weight", "detection_receiver")
    # Reception-floor confidence uses its own range-neutral current anchor.
    # Nearby priority may move the DISPLAY anchor without changing this score.
    election_anchor = choose_anchor("floor_election_weight", "floor_election_receiver")
    pt, diag = anchor
    radius_m = pt[2] / scale
    quality = {"detection_only": True, "direction_known": False,
               "detection_receiver": diag["receiver"], "receiver": diag["receiver"],
               "floor_election_receiver": election_anchor[1]["receiver"],
               "floor_election_range_m": election_anchor[0][2] / scale,
               "detection_range_m": radius_m,
               "detection_range_px": pt[2], "confidence": "poor",
               "estimated_uncertainty_m": max(3.0, radius_m * 1.5),
               "uncertainty_method": "sparse_receiver_detection",
               "receivers_used": len(selected), "observations_used": len(selected),
               "range_trust_enabled": bool(nearby_trust), "clear_paths": 0,
               "obstruction_strength": obstruction_strength,
               "building_crossings": 0, "receiver_diagnostics": diagnostics,
               "observed": diag["observed"], "newest_reading_age_s": min(d["reading_age_s"] for _, d in selected.values()),
               "position_age_s": diag["reading_age_s"], "stale_after_s": limit,
               "use_observation_age": True, "stale": False,
               "geometry_degenerate": True, "boundary_hit": False}
    weights = [(*p[:3], d["reliability_weight"], p[4]) if nearby_trust else p
               for p, d in selected.values()]
    return {"fix": pt[:2], "weighted": weights, "outdoor": quality}
