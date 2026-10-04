"""Optional position-level fusion for logical animals with several BLE beacons.

Coordinates stay in BPS map pixels; distances and uncertainty are metres. This
module neither reads Bermuda measurements nor changes the individual fixes.
The uncertainty circle is a conservative heuristic, not a confidence interval:
beacons on one animal share RF conditions, so agreement earns only a modest
improvement. Obstruction is already represented in each beacon's uncertainty;
applying its path penalties again here would count the same effect twice.
"""

import math
import re
from collections.abc import Mapping

MAX_GROUPS = 32
MAX_BEACONS_PER_GROUP = 16
DEFAULT_MAX_AGE_S = 120.0
MIN_UNCERTAINTY_M = 0.5
MAX_UNCERTAINTY_M = 1000.0
AGREEMENT_FLOOR_M = 5.0
AGREEMENT_UNCERTAINTY_FACTOR = 2.5
# Shared receiver geometry means two agreeing beacons are not independent.
MIN_AGREEMENT_UNCERTAINTY_RATIO = 0.75
_SLUG = re.compile(r"[a-z0-9_]{1,64}\Z", re.ASCII)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _slug(value):
    return isinstance(value, str) and bool(_SLUG.fullmatch(value))


def normalize_groups(layout, known_trackers=None):
    """Return bounded, safe group definitions without altering stored layout.

    All groups require Outdoor Tracking. Missing beacon references are retained
    for truthful ``reporting/total`` diagnostics and recovery when they return.
    ``known_trackers`` prevents generated IDs colliding with real trackers.
    Duplicate IDs and malformed entries are skipped; enabled defaults to true.
    """
    if not isinstance(layout, Mapping):
        return []
    outdoor = layout.get("outdoor_tracking")
    if not isinstance(outdoor, Mapping) or outdoor.get("enabled") is not True:
        return []
    entries = layout.get("tracker_groups")
    if not isinstance(entries, list):
        return []
    known = set(known_trackers or ())
    seen = set()
    result = []
    for entry in entries[:MAX_GROUPS]:
        if not isinstance(entry, Mapping):
            continue
        group_id = entry.get("id")
        if not _slug(group_id) or group_id in seen or f"bps_group_{group_id}" in known:
            continue
        if entry.get("enabled", True) is not True:
            continue
        members = entry.get("beacons")
        if not isinstance(members, list) or len(members) > MAX_BEACONS_PER_GROUP:
            continue
        beacons = list(dict.fromkeys(b for b in members if _slug(b)))
        # Groups cannot refer to other group outputs (including themselves).
        if not beacons or any(b.startswith("bps_group_") for b in beacons):
            continue
        name = entry.get("name", group_id)
        if not isinstance(name, str) or not name.strip():
            name = group_id
        seen.add(group_id)
        result.append({"id": group_id, "name": name.strip()[:128],
                       "enabled": True, "beacons": beacons})
    return result


def _position_index(positions):
    if isinstance(positions, Mapping):
        values = positions.values()
        result = {key: value for key, value in positions.items()
                  if isinstance(key, str) and isinstance(value, Mapping)}
    elif isinstance(positions, (list, tuple)):
        values = positions
        result = {}
    else:
        return {}
    for value in values:
        if not isinstance(value, Mapping) or value.get("group"):
            continue
        ent = value.get("ent")
        if isinstance(ent, str):
            previous = result.get(ent)
            ts = _number(value.get("updated"))
            prev_ts = _number(previous.get("updated")) if previous else None
            if previous is None or (ts is not None and (prev_ts is None or ts > prev_ts)):
                result[ent] = value
    return result


def _fix(beacon, position, scales, now, max_age_s):
    if not isinstance(position, Mapping) or position.get("group"):
        return None
    updated = _number(position.get("updated"))
    if updated is None or updated > now + 5:
        return None
    floor = position.get("floor")
    if not isinstance(floor, str) or not floor:
        return None
    scale = _number(scales.get(floor))
    if scale is None or not 1e-6 <= scale <= 1e6:
        return None
    cords = position.get("cords")
    if not isinstance(cords, (list, tuple)) or len(cords) != 2:
        return None
    x, y = map(_number, cords)
    if x is None or y is None or max(abs(x), abs(y)) > 1e9:
        return None
    outdoor = position.get("outdoor")
    outdoor = outdoor if isinstance(outdoor, Mapping) else {}
    # A solve may reuse a reading shortly before it expires. Its solve time
    # must not extend the observation's lifetime by another group timeout.
    if "observed" in outdoor:
        observed = _number(outdoor["observed"])
        if observed is None or observed > updated:
            return None
        updated = observed
    if now - updated > max_age_s:
        return None
    residual = _number(position.get("rms_m"))
    residual = max(0.0, min(residual, 1000.0)) if residual is not None else 5.0
    raw_uncertainty = outdoor.get("estimated_uncertainty_m")
    uncertainty = _number(raw_uncertainty)
    if raw_uncertainty is not None and (uncertainty is None or uncertainty < 0):
        return None
    if uncertainty is None:
        # Conservative fallback for a previously published ordinary BPS fix.
        uncertainty = max(5.0, residual * 2.0)
    uncertainty = min(MAX_UNCERTAINTY_M, max(MIN_UNCERTAINTY_M, uncertainty))
    count = _number(outdoor.get("receivers_used"))
    count = int(max(0, min(256, count))) if count is not None else 0
    age = max(0.0, now - updated)
    freshness = max(0.1, 1.0 - 0.9 * age / max_age_s)
    # Mild bounded tie-breakers, rather than another full uncertainty model.
    receiver_quality = 0.6 + 0.4 * min(count / 6.0, 1.0)
    residual_quality = 1.0 / (1.0 + min(residual / uncertainty, 4.0) * 0.25)
    reliability = max(0.05, min(1.0, freshness * receiver_quality * residual_quality))
    receiver_ids = set()
    receiver_diagnostics = outdoor.get("receiver_diagnostics")
    if isinstance(receiver_diagnostics, Mapping):
        receiver_ids = {str(k) for k, v in receiver_diagnostics.items()
                        if isinstance(v, Mapping) and v.get("used", True)}
    elif isinstance(receiver_diagnostics, list):
        for entry in receiver_diagnostics:
            if not isinstance(entry, Mapping) or entry.get("used", True) is False:
                continue
            receiver_id = next((entry.get(key) for key in ("receiver", "entity_id", "name")
                                if isinstance(entry.get(key), str) and entry.get(key)), None)
            if receiver_id:
                receiver_ids.add(receiver_id)
    return {"ent": beacon, "cords": [x, y], "floor": floor,
            "updated": updated, "estimated_uncertainty_m": uncertainty,
            "receivers_used": count, "age_s": age,
            "weight": reliability / uncertainty ** 2, "scale": scale,
            "receiver_ids": receiver_ids}


def _distance(first, second):
    return math.hypot(first["cords"][0] - second["cords"][0],
                      first["cords"][1] - second["cords"][1]) / first["scale"]


def fuse_group(group, positions, scales, now, max_age_s=DEFAULT_MAX_AGE_S):
    """Fuse one normalized enabled group, returning a fresh payload or None.

    Different floors are never averaged. A distant fix cannot drag a better
    fix into a midpoint: the best-quality anchor accepts only nearby members.
    Discarded fixes still increase uncertainty and remain in diagnostics.
    ``updated`` is a source observation timestamp, never this refresh's time.
    Callers recompute zones using the resulting point and the existing rules.
    """
    if not isinstance(group, Mapping) or group.get("enabled", True) is not True:
        return None
    if not _slug(group.get("id")) or not isinstance(scales, Mapping):
        return None
    members = group.get("beacons")
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_BEACONS_PER_GROUP:
        return None
    if any(not _slug(b) or b.startswith("bps_group_") for b in members):
        return None
    now, max_age_s = _number(now), _number(max_age_s)
    if now is None or max_age_s is None or max_age_s <= 0:
        return None
    index = _position_index(positions)
    fixes = []
    for beacon in dict.fromkeys(members):
        fix = _fix(beacon, index.get(beacon), scales, now, max_age_s)
        if fix is not None:
            fixes.append(fix)
    if not fixes:
        return None
    by_floor = {}
    for fix in fixes:
        by_floor.setdefault(fix["floor"], []).append(fix)
    floor = max(by_floor, key=lambda f: sum(p["weight"] for p in by_floor[f]))
    candidates = by_floor[floor]
    anchor = max(candidates, key=lambda p: p["weight"])
    accepted = [p for p in candidates if _distance(anchor, p) <= max(
        AGREEMENT_FLOOR_M, AGREEMENT_UNCERTAINTY_FACTOR * min(
            anchor["estimated_uncertainty_m"], p["estimated_uncertainty_m"]))]
    weight = sum(p["weight"] for p in accepted)
    if len(accepted) == 1:
        x, y = accepted[0]["cords"]
    else:
        x = sum(p["weight"] * p["cords"][0] for p in accepted) / weight
        y = sum(p["weight"] * p["cords"][1] for p in accepted) / weight
    disagreement = max((_distance(a, b) for i, a in enumerate(candidates)
                        for b in candidates[i + 1:]), default=0.0)
    scatter = math.sqrt(sum(p["weight"] * (
        math.hypot(p["cords"][0] - x, p["cords"][1] - y) / p["scale"]) ** 2
        for p in accepted) / weight)
    best_u = min(p["estimated_uncertainty_m"] for p in accepted)
    # Reliability should never make one beacon artificially more certain.
    uncertainty = math.hypot(max(best_u * MIN_AGREEMENT_UNCERTAINTY_RATIO,
                                math.sqrt(1.0 / sum(1.0 / p["estimated_uncertainty_m"] ** 2
                                                    for p in accepted))), scatter)
    conflict = len(accepted) < len(candidates)
    floor_conflict = len(by_floor) > 1
    if conflict:
        uncertainty = math.hypot(uncertainty, disagreement * 0.5)
    if floor_conflict:
        uncertainty = max(uncertainty, anchor["estimated_uncertainty_m"] * 1.5)
    if len(fixes) == 1:
        fusion_confidence = "single"
    elif floor_conflict:
        fusion_confidence = "floor_conflict"
    elif conflict:
        fusion_confidence = "disagreement"
    else:
        fusion_confidence = "good" if uncertainty <= 5 else "moderate"
    confidence = "good" if uncertainty <= 5 else "moderate" if uncertainty <= 15 else "poor"
    if conflict or floor_conflict:
        confidence = "poor"
    diagnostics = [{k: v for k, v in p.items() if k not in ("weight", "scale", "receiver_ids")}
                   for p in fixes]
    accepted_ids = {p["ent"] for p in accepted}
    for entry in diagnostics:
        entry["used"] = entry["ent"] in accepted_ids
    # The same proxy often sees both beacons. Counts alone cannot identify
    # overlap; use a conservative maximum unless all sources provide names.
    if all(p["receiver_ids"] for p in accepted):
        receivers_used = len(set().union(*(p["receiver_ids"] for p in accepted)))
    else:
        receivers_used = max(p["receivers_used"] for p in accepted)
    return {"ent": f"bps_group_{group['id']}", "group": True,
            "name": group.get("name", group["id"]), "cords": [x, y],
            "floor": floor, "zone": None,
            "updated": max(p["updated"] for p in accepted),
            "beacons_reporting": len(fixes), "total_beacons": len(set(members)),
            "beacon_disagreement_m": disagreement if not floor_conflict else None,
            "fusion_confidence": fusion_confidence, "beacon_positions": diagnostics,
            "outdoor": {"estimated_uncertainty_m": uncertainty,
                        "confidence": confidence,
                        "receivers_used": receivers_used,
                        "receiver_observations": sum(p["receivers_used"] for p in accepted)}}


def fuse_groups(layout, positions, scales, now, max_age_s=DEFAULT_MAX_AGE_S):
    """Normalize and fuse configured groups; disabled layouts do no work."""
    if not normalize_groups(layout):
        return []
    index = _position_index(positions)
    groups = normalize_groups(layout, known_trackers=(
        ent for ent, p in index.items() if not p.get("group")))
    return [payload for group in groups
            if (payload := fuse_group(group, index, scales, now, max_age_s)) is not None]
