"""Read-only, bounded paired raw-position replay at measured stationary points.

Uses the live solver and obstruction weighting. Replay deliberately omits
Kalman history, temporal jump weights, floor election and zone snapping.
"""
import copy
import json
import math

from .outdoor_tracking import solve_outdoor
from .uncertainty import estimate_uncertainty

MAX_REPLAYS = 128


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def geometry(floor):
    return {"scale": floor.get("scale"), "zones": floor.get("zones", []),
            "environment": floor.get("environment", []),
            "receivers": [{k: r.get(k) for k in ("entity_id", "height", "outdoor_policy")} |
                {"cords": {k: r.get("cords", {}).get(k) for k in ("x", "y")}}
                for r in floor.get("receivers", [])]}


def truth_for(annotations, target, members, now):
    active = {}
    for mark in annotations:
        if mark["time"] > now:
            break
        if mark.get("kind") == "known_position":
            active[mark.get("target")] = mark
        elif mark.get("kind") == "clear_position":
            active.pop(mark.get("target"), None)
    candidates = [m for key, m in active.items() if key == target or target in members.get(key, [])]
    return max(candidates, key=lambda m: m["time"]) if candidates else None


def metrics(values):
    def percentile(p):
        if not values:
            return None
        ordered = sorted(values)
        at = (len(ordered) - 1) * p
        lo, hi = math.floor(at), math.ceil(at)
        return round(ordered[lo] + (ordered[hi] - ordered[lo]) * (at - lo), 4)
    return {"samples": len(values), "median_m": percentile(.5), "p95_m": percentile(.95)}


def replay(floor, readings, corrections, scale, layout, now, solver):
    candidate = copy.deepcopy(floor)
    rows = {r["receiver"]: r for r in readings}
    weighted, selected = [], []
    for receiver in candidate.get("receivers", []):
        row = rows.get(receiver.get("entity_id"))
        if row is None:
            continue
        factor = corrections.get(receiver["entity_id"], row.get("receiver_correction", 1.0))
        slant = row["measured_distance_m"] * factor * row.get("tracker_distance_factor", 1.0)
        horizontal = slant
        height = receiver.get("height")
        if number(height) and 0 <= height <= 10:
            horizontal = math.sqrt(max(slant ** 2 - (height - row.get("tracker_height_m", .8)) ** 2,
                                       min(slant ** 2, .5 ** 2)))
        xy = receiver["cords"]
        weighted.append((xy["x"], xy["y"], horizontal * scale, 1.0, slant * scale))
        receiver.update(distance=slant, cords={**xy, "r": horizontal * scale},
            _outdoor_reading={"measured_distance_m": row["measured_distance_m"],
                "observed": row["observed"], "reading_age_s": now - row["observed"]})
        selected.append(receiver)
    candidate["receivers"] = selected
    if len(weighted) < 3:
        return None
    xs, ys = [p[0] for p in weighted], [p[1] for p in weighted]
    for zone in floor.get("zones", []):
        xs.extend(c["x"] for c in zone.get("cords", []))
        ys.extend(c["y"] for c in zone.get("cords", []))
    margin = .1 * max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    bounds = (min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin)
    if layout.get("outdoor_tracking", {}).get("enabled") is True:
        result = solve_outdoor(candidate, weighted, bounds, scale, .5 * scale,
            layout.get("reading_max_age", 30), None, solver)
        if result is None:
            return None
        fix, quality = result["fix"], result["outdoor"]
    else:
        fix = solver(weighted, bounds=bounds, min_weight_radius=.5 * scale)
        if fix is None:
            return None
        quality = estimate_uncertainty(fix, weighted, scale, [])
    if not all(number(float(v)) for v in fix):
        return None
    return {"cords": list(fix), "floor": floor["name"], "updated": now,
            "outdoor": {**quality, "observed": max(r["observed"] for r in readings)}}


def compare_tracking(bundle, floor_name, corrections, current_floor, solver):
    if not corrections or any(not number(v) or not .2 <= v <= 5 for v in corrections.values()):
        raise ValueError("No valid calibration corrections for this floor")
    if bundle.get("format") != "bps-diagnostics-v1":
        raise ValueError("No diagnostic recording available")
    before, after, failures, locations, seen = [], [], 0, set(), set()
    reports, samples = {}, []
    context_id, context_since = None, bundle["started"]
    annotations = sorted(bundle.get("annotations", []), key=lambda m: m["time"])
    for frame in bundle.get("frames", []):
        context = bundle["contexts"][frame["context_id"]]
        if context_id != frame["context_id"]:
            context_id, context_since = frame["context_id"], frame["time"]
        layout = context["layout"]
        floor = next((f for f in layout.get("floor", []) if f.get("name") == floor_name), None)
        if floor is None or geometry(floor) != geometry(current_floor):
            continue
        scale = floor.get("scale")
        if not number(scale) or not 1e-6 <= scale <= 1e6:
            continue
        members = context.get("target_members", {})
        sources = {s for target in bundle.get("targets", []) for s in members.get(target, [target])}
        for target in sorted(sources):
            truth = truth_for(annotations, target, members, frame["time"])
            if not truth or truth.get("floor") != floor_name:
                continue
            gate = layout.get("reading_max_age", context.get("defaults", {}).get("reading_max_age_s", 30))
            rows = [r for r in frame.get("readings", []) if r.get("tracker") == target
                and r.get("floor") == floor_name and r.get("status") == "current"
                and not (layout.get("outdoor_tracking", {}).get("enabled") is True and r.get("ignored"))
                and not r.get("assumed_unit_m")
                and number(r.get("measured_distance_m")) and r["measured_distance_m"] >= 0
                and number(r.get("observed")) and max(truth["time"], context_since) <= r["observed"] <= frame["time"]
                and (not gate or frame["time"] - r["observed"] < gate)]
            signature = (target, frame["context_id"], tuple(sorted((r["receiver"], r["observed"]) for r in rows)))
            if len(rows) < 3 or signature in seen or not any(r["receiver"] in corrections for r in rows):
                continue
            seen.add(signature)
            samples.append((target, truth, frame["time"], floor, rows, layout, scale))
    # Spread the bounded work over the recording instead of testing only its
    # first location. Report truncation; additional polls do not add samples.
    eligible = len(samples)
    if eligible > MAX_REPLAYS:
        samples = [samples[round(i * (eligible - 1) / (MAX_REPLAYS - 1))] for i in range(MAX_REPLAYS)]
    for target, truth, now, floor, rows, layout, scale in samples:
        baseline = replay(floor, rows, {}, scale, layout, now, solver)
        proposed = replay(floor, rows, corrections, scale, layout, now, solver)
        stats = reports.setdefault(target, {"baseline": [], "candidate": [], "candidate_failures": 0})
        if baseline is None:
            continue
        if proposed is None:
            failures += 1
            stats["candidate_failures"] += 1
            continue
        point = (truth["x_m"], truth["y_m"])
        b = math.dist([v / scale for v in baseline["cords"]], point)
        c = math.dist([v / scale for v in proposed["cords"]], point)
        before.append(b); after.append(c)
        stats["baseline"].append(b); stats["candidate"].append(c)
        locations.add(point)
    b, c = metrics(before), metrics(after)
    enough = len(before) >= 5 and len(locations) >= 2
    improves = enough and all(b[k] - c[k] >= max(.25, b[k] * .1) for k in ("median_m", "p95_m"))
    tracker_regression = any(len(s["baseline"]) >= 5 and any(
        metrics(s["candidate"])[k] - metrics(s["baseline"])[k] >= max(.25, metrics(s["baseline"])[k] * .1)
        for k in ("median_m", "p95_m")) for s in reports.values())
    worse = failures or tracker_regression or (enough and any(c[k] - b[k] >= max(.25, b[k] * .1) for k in ("median_m", "p95_m")))
    return {"floor": floor_name, "verdict": "worse" if worse else "improves" if improves else "inconclusive",
        "baseline": b, "candidate": c, "candidate_failures": failures, "locations": len(locations),
        "eligible_samples": eligible, "replayed_samples": len(samples), "truncated": eligible > MAX_REPLAYS,
        "trackers": {key: {"baseline": metrics(s["baseline"]), "candidate": metrics(s["candidate"]),
            "candidate_failures": s["candidate_failures"]} for key, s in reports.items()},
        "scope": "Paired raw beacon fixes on the marked floor, using recorded corrections as baseline. "
            "Group recordings test their member beacons. Includes obstruction weights and height projection; "
            "excludes temporal jump weights, Kalman smoothing, group fusion, floor election and zone snapping. "
            "At least 5 distinct observations at 2 measured locations are needed. No calibration changed."}


def compare_tracking_export(payload, floor_name, corrections, current_floor, solver):
    """Decode and compare a potentially 16 MiB recording in the executor."""
    return compare_tracking(json.loads(payload), floor_name, corrections, current_floor, solver)
