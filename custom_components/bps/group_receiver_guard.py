"""Near-receiver consistency checks for unpublished group positions."""

import math
from collections.abc import Mapping

from .environment import finite_number
from .tracker_groups import GROUP_MAX_SPEED_MS, GROUP_MOTION_SLACK_M

SILENT_RECEIVER_RADIUS_M = 3.0
HEALTH_MAX_AGE_S = 30.0


def _point(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    point = [finite_number(v) for v in value]
    return point if all(v is not None for v in point) else None


def _age(state, now):
    stamp = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)
    try:
        observed = finite_number(stamp.timestamp(), minimum=0)
        return now - observed if observed is not None and observed <= now else None
    except (AttributeError, OSError, OverflowError, TypeError, ValueError):
        return None


def _range(value):
    if isinstance(value, bool):
        return None
    try:
        return finite_number(float(value), minimum=0)
    except (TypeError, ValueError, OverflowError):
        return None


def _direct_absence_known(direct, info, now, limit):
    """Missing rows prove silence only in a complete, unambiguous export."""
    owner = info.get("entry_id")
    snapshot = direct.snapshots.get(owner, {})
    for field in ("omitted", "bridge_omitted"):
        counts = snapshot.get(field)
        if not isinstance(counts, Mapping) or any(
                finite_number(counts.get(key), minimum=0) != 0
                for key in ("devices", "scanners", "observations")):
            return False
    rows = direct.pairs.get((owner, info.get("device_id"), info.get("scanner_mac")), [])
    if len(rows) > 1:
        return False
    if not rows:
        return info.get("reason") == "measurement_missing"
    age = finite_number(info.get("source_age_s"), minimum=0)
    return age is not None and age <= direct.now and age + now - direct.now >= limit


def receiver_constraints(floor, members, sources, health, now, max_age_s):
    """Use proven online identities and supported pairs; unknown evidence is neutral."""
    limit = min(HEALTH_MAX_AGE_S, max_age_s)
    scale = finite_number(floor.get("scale"), minimum=1e-6, maximum=1e6)
    if scale is None or not members:
        return []
    constraints = []
    for rec, binding in zip(floor.get("receivers", []), sources.floor_bindings(floor)):
        if not isinstance(rec, Mapping) or rec.get("outdoor_policy") == "ignore" or sources.blocked(binding):
            continue
        key = binding.get("identity")
        if not key:
            continue
        cords = rec.get("cords")
        point = _point([cords.get("x"), cords.get("y")]) if isinstance(cords, Mapping) else None
        if point is None:
            continue
        cached = health.get(rec.get("entity_id"), {})
        captured = finite_number(cached.get("captured"), minimum=0)
        same_identity = list(key) == cached.get("identity")
        evidence_age = finite_number(cached.get("source_age_s", 0.0), minimum=0)
        current_health = same_identity and captured is not None and 0 <= now - captured < limit \
            and evidence_age is not None and evidence_age + now - captured < limit
        scanner_age = sources.direct.scanner_age(key)
        if key in sources.direct.scanners:
            age = finite_number(scanner_age, minimum=0)
            captured_at = finite_number(sources.direct.now, minimum=0)
            online = age is not None and captured_at is not None and 0 <= now - captured_at < limit \
                and age + now - captured_at < limit
            if current_health and cached.get("online") is False \
                    and (not online or captured_at - age <= captured):
                online = False
        else:
            online = current_health and cached.get("online") is True
        if not online:
            continue
        missing = []
        for member in members:
            _eid, state = sources.source(member, rec, binding, now=now)
            if state is None:
                break  # missing/disabled entities and ambiguous tracker proofs are unknown
            value = _range(getattr(state, "state", None))
            age = _age(state, now)
            if value is not None and (age is None or age < limit):
                break
            info = getattr(state, "attributes", {}).get("bps_source", {})
            raw_age = finite_number(info.get("source_age_s"), minimum=0)
            if info.get("path") == "direct" and info.get("source_status") == "current" \
                    and raw_age is not None and raw_age + now - sources.direct.now < limit:
                break  # a pending/rejected filter does not mean the radio hears nothing
            absent = str(getattr(state, "state", "")).lower() in ("unknown", "unavailable")
            if value is None and not absent:
                break
            if info.get("path") == "direct" and info.get("reason") in (
                    "invalid_observation_clock", "accepted_stamp_unavailable", "filtered_measurement_unavailable"):
                break
            if info.get("path") == "direct":
                if not _direct_absence_known(sources.direct, info, now, limit):
                    break
            elif age is None:
                break  # unclocked legacy unavailability cannot prove absence
            missing.append(member)
        if len(missing) == len(members):
            constraints.append({"receiver": rec["entity_id"], "cords": point,
                "radius_m": SILENT_RECEIVER_RADIUS_M, "missing_beacons": missing,
                "online_source": "bermuda_direct" if key in sources.direct.scanners else "receiver_liveness"})
    return constraints


def constrain_group(position, constraints, previous, scale, now, max_age_s, *, motion_budget_m=None):
    """Select an existing supported fix or hold it briefly; never invent a repelled point."""
    if not constraints or position.get("outdoor", {}).get("detection_only"):
        return position, None

    def conflicts(point):
        return [r for r in constraints if math.dist(point, r["cords"]) / scale < r["radius_m"]]

    original = _point(position.get("cords"))
    blocked = conflicts(original) if original is not None else []
    if not blocked:
        return position, None
    report = {"state": "unavailable", "rejected_cords": original,
              "radius_m": SILENT_RECEIVER_RADIUS_M, "receivers": blocked,
              "beacons_reporting": position["beacons_reporting"]}
    prior = previous if isinstance(previous, Mapping) and previous.get("floor") == position["floor"] else {}
    prior_point = _point(prior.get("cords"))
    prior_time = finite_number(prior.get("updated"), minimum=0)
    if prior_point is None or prior_time is None or not 0 <= now - prior_time < max_age_s:
        prior_point = None
    allowed = max(GROUP_MOTION_SLACK_M, GROUP_MAX_SPEED_MS * max(0,
        position["updated"] - prior_time)) if prior_point is not None else math.inf
    budget = finite_number(motion_budget_m, minimum=0)
    if prior_point is not None and budget is not None:
        allowed = min(allowed, max(GROUP_MOTION_SLACK_M, budget))
    motion_step = finite_number(position.get("motion_gate", {}).get("allowed_step_m"), minimum=0)
    if prior_point is not None and motion_step is not None:
        allowed = min(allowed, max(GROUP_MOTION_SLACK_M, motion_step))
    candidates = []
    for member in position.get("beacon_positions", []):
        point = _point(member.get("cords"))
        stamp = finite_number(member.get("updated"), minimum=0)
        if not member.get("used") or member.get("detection_only") or member.get("floor") != position["floor"] \
                or point is None or stamp is None or not 0 <= now - stamp < max_age_s or conflicts(point):
            continue
        if prior_point is not None and math.dist(point, prior_point) / scale > allowed:
            continue
        candidates.append((member, point))
    if candidates:
        member, point = min(candidates, key=lambda pair: math.dist(pair[1], prior_point)
                           if prior_point is not None else -pair[0]["weight"])
        report.update(state="member_fix", selected_beacon=member["ent"])
        position["cords"] = point
        position["updated"] = member["updated"]
    elif prior_point is not None and not conflicts(prior_point) and not prior.get("outdoor", {}).get("detection_only"):
        report["state"] = "held"
        position["cords"] = prior_point
        position["updated"] = prior_time  # cached coordinates do not gain a new observation timestamp
    else:
        return None, report
    displacement = math.dist(original, position["cords"]) / scale
    report["displacement_m"] = displacement
    quality = position["outdoor"]
    quality["estimated_uncertainty_m"] = math.hypot(quality["estimated_uncertainty_m"], displacement)
    quality["confidence"] = "poor"
    quality["receiver_guard"] = report
    position["fusion_confidence"] = "disagreement"
    return position, report
