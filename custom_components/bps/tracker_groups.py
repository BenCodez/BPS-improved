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
# Radio uncertainty must not turn two beacons on one animal into agreement
# tens of metres apart. Motion confirmation uses source observations, not polls.
MAX_AGREEMENT_DISTANCE_M = 10.0
GROUP_MAX_SPEED_MS = 10.0
GROUP_MOTION_SLACK_M = 5.0
GROUP_JUMP_CONFIRMATION_S = 3.0
# Corroboration can preserve a pending challenge through noisy target/anchor
# changes; it never raises the speed limit or widens spatial agreement.
GROUP_MOTION_SUPPORT_MAX_AGE_S = 5.0
GROUP_MOTION_SUPPORT_MAX_UNCERTAINTY_M = 50.0
GROUP_MOTION_DIRECTION_COSINE = 0.8
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
    ``known_trackers`` prevents generated IDs colliding with real trackers and
    identifies genuine beacons that conflict with a configured group output.
    Unknown members remain valid unless they reference a configured group.
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
    outputs = {f"bps_group_{e['id']}" for e in entries[:MAX_GROUPS]
               if isinstance(e, Mapping) and _slug(e.get("id"))} - known
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
        if not beacons or any(b in outputs for b in beacons):
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
                  if isinstance(key, str) and isinstance(value, Mapping) and not value.get("group")}
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


def _fix(beacon, position, scales, now, max_age_s, use_observation_age):
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
    # With the reading gate enabled, a recent solve must not extend an old
    # observation's lifetime. Validate observations even when the gate is off.
    if "observed" in outdoor:
        observed = _number(outdoor["observed"])
        if observed is None or observed > updated:
            return None
        if use_observation_age or outdoor.get("detection_only") is True:
            updated = observed
    if outdoor.get("detection_only") is True:
        max_age_s = min(max_age_s, _number(outdoor.get("stale_after_s")) or 30.0)
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
            "updated": updated, "observed": outdoor.get("observed", updated),
            "estimated_uncertainty_m": uncertainty,
            "detection_only": outdoor.get("detection_only") is True,
            "detection_receiver": outdoor.get("detection_receiver") if isinstance(outdoor.get("detection_receiver"), str) else None,
            "detection_range_m": _number(outdoor.get("detection_range_m")),
            "environment_fallback": outdoor.get("environment_fallback") is True,
            "uncertainty_details": {key: value for key in (
                "residual_m", "unadjusted_residual_m", "noise_floor_m",
                "geometry_factor", "effective_receivers", "receiver_count_factor",
                "robust_downweighted_receivers", "pre_publication_uncertainty_m",
                "publication_displacement_m")
                if (value := _number(outdoor.get(key))) is not None and value >= 0},
            "receivers_used": count, "age_s": age,
            "weight": reliability / uncertainty ** 2, "scale": scale,
            "receiver_ids": receiver_ids}


def _distance(first, second):
    return math.hypot(first["cords"][0] - second["cords"][0],
                      first["cords"][1] - second["cords"][1]) / first["scale"]


def _agreement_limit(first, second):
    return min(MAX_AGREEMENT_DISTANCE_M, max(AGREEMENT_FLOOR_M,
        AGREEMENT_UNCERTAINTY_FACTOR * min(first["estimated_uncertainty_m"],
                                         second["estimated_uncertainty_m"])))


def _motion_previous(previous, floor, scale, now, max_age_s):
    if not isinstance(previous, Mapping) or previous.get("floor") != floor:
        return None
    if isinstance(previous.get("outdoor"), Mapping) and previous["outdoor"].get("detection_only"):
        return None
    cords = previous.get("cords")
    updated = _number(previous.get("updated"))
    if not isinstance(cords, (list, tuple)) or len(cords) != 2 or updated is None:
        return None
    xy = list(map(_number, cords))
    if any(v is None or abs(v) > 1e9 for v in xy) or not 0 <= now - updated <= max_age_s:
        return None
    return {"cords": xy, "floor": floor, "scale": scale, "updated": updated}


def _same_motion_direction(first, second):
    lengths = math.hypot(*first) * math.hypot(*second)
    return lengths > 0 and sum(a * b for a, b in zip(first, second)) >= (
        GROUP_MOTION_DIRECTION_COSINE * lengths)


def _motion_support(candidates, previous, stability, now):
    """Require a new observation from every directional member on this floor.

    Sources need not be spatially agreeing to corroborate the direction away
    from a held group. Their positions still use the normal anchor selection;
    this evidence only prevents restarting the motion confirmation clock.
    """
    if not isinstance(stability, dict):
        return []
    epochs = stability.get("motion_support_epochs")
    if epochs is None:
        epochs = {p["ent"]: p.get("observed", p.get("updated"))
                  for p in (previous or {}).get("beacon_positions", [])}
    if previous is None or not epochs:
        stability["motion_support_epochs"] = {p["ent"]: p["observed"] for p in candidates}
        return []
    # A newly reporting member establishes its baseline once, rather than
    # comparing against its current observation forever. Keep unconsumed
    # epochs for remaining members and drop absent members to bound state.
    epochs = {p["ent"]: epochs.get(p["ent"], p["observed"]) for p in candidates}
    stability["motion_support_epochs"] = epochs
    if len(candidates) < 2 or any(p["observed"] <= epochs[p["ent"]]
                                 for p in candidates):
        return []
    # Consume the joint observation even when its quality/direction fails.
    # Alternating member updates can complete a pair, but re-solving/polling
    # the same readings cannot provide another corroboration.
    stability["motion_support_epochs"] = {p["ent"]: p["observed"] for p in candidates}
    if any(p["detection_only"] or p["environment_fallback"] or p["receivers_used"] < 3
           or p["estimated_uncertainty_m"] > GROUP_MOTION_SUPPORT_MAX_UNCERTAINTY_M
           or not 0 <= now - p["observed"] <= GROUP_MOTION_SUPPORT_MAX_AGE_S
           or _distance(p, previous) <= GROUP_MOTION_SLACK_M for p in candidates):
        return []
    vectors = [(p["cords"][0] - previous["cords"][0],
                p["cords"][1] - previous["cords"][1]) for p in candidates]
    if not all(_same_motion_direction(a, b) for i, a in enumerate(vectors) for b in vectors[i + 1:]):
        return []
    return candidates


def _motion_point(x, y, fixes, previous, stability, support=()):
    """Hold a sudden excursion, then approach persistent evidence gradually."""
    if previous is None or not isinstance(stability, dict):
        return x, y, None
    epoch = max(p["observed"] for p in (*fixes, *support))
    old_epoch = _number(stability.get("motion_epoch"))
    if old_epoch is None:
        old_epoch = previous["updated"]
    dt = max(0.0, epoch - old_epoch)
    stability["motion_epoch"] = max(epoch, old_epoch)
    scale = previous["scale"]
    displacement = math.hypot(x - previous["cords"][0], y - previous["cords"][1]) / scale
    allowed = GROUP_MAX_SPEED_MS * dt
    if displacement <= max(GROUP_MOTION_SLACK_M, allowed):
        stability.pop("motion_challenge", None)
        return x, y, None
    challenge = stability.get("motion_challenge")
    changed_target = challenge is not None and math.hypot(x - challenge["x"], y - challenge["y"]) / scale > max(
        GROUP_MOTION_SLACK_M, GROUP_MAX_SPEED_MS * max(0, epoch - challenge["epoch"]))
    corroborated = bool(support) and challenge is not None and _same_motion_direction(
        (x - previous["cords"][0], y - previous["cords"][1]),
        (challenge["x"] - previous["cords"][0], challenge["y"] - previous["cords"][1]))
    if challenge is None or (changed_target and not corroborated):
        challenge = {"x": x, "y": y, "first": epoch, "epoch": epoch,
                     "count": int(epoch > old_epoch)}
    elif epoch > challenge["epoch"]:
        challenge = {**challenge, "x": x, "y": y, "epoch": epoch,
                     "count": challenge["count"] + 1}
    stability["motion_challenge"] = challenge
    confirmed = challenge["count"] >= 3 and epoch - challenge["first"] >= GROUP_JUMP_CONFIRMATION_S
    fraction = min(1.0, allowed / displacement) if confirmed else 0.0
    nx = previous["cords"][0] + fraction * (x - previous["cords"][0])
    ny = previous["cords"][1] + fraction * (y - previous["cords"][1])
    return nx, ny, {"state": "limited" if fraction else "held",
                   "displacement_m": displacement, "allowed_step_m": allowed,
                   "confirmation_observations": challenge["count"],
                   "confirmation_span_s": max(0.0, epoch - challenge["first"]),
                   **({"corroborating_beacons": [p["ent"] for p in support]} if corroborated else {}),
                   "max_speed_m_s": GROUP_MAX_SPEED_MS}


def fuse_group(group, positions, scales, now, max_age_s=DEFAULT_MAX_AGE_S, *,
               use_observation_age=True, previous=None, stability=None):
    """Fuse one normalized enabled group, returning a fresh payload or None.

    Different floors are never averaged. A distant fix cannot drag a better
    fix into a midpoint: the best-quality anchor accepts only nearby members.
    Discarded fixes still increase uncertainty and remain in diagnostics.
    ``updated`` is a source timestamp, never this refresh's time. When the
    caller disables the reading-age gate, expire by source solve time instead
    of observation time using ``use_observation_age=False``.
    Callers recompute zones using the resulting point and the existing rules.
    """
    if not isinstance(group, Mapping) or group.get("enabled", True) is not True:
        return None
    if not _slug(group.get("id")) or not isinstance(scales, Mapping):
        return None
    members = group.get("beacons")
    if not isinstance(members, list) or not 1 <= len(members) <= MAX_BEACONS_PER_GROUP:
        return None
    if any(not _slug(b) for b in members):
        return None
    now, max_age_s = _number(now), _number(max_age_s)
    if now is None or max_age_s is None or max_age_s <= 0:
        return None
    index = _position_index(positions)
    fixes = []
    for beacon in dict.fromkeys(members):
        fix = _fix(beacon, index.get(beacon), scales, now, max_age_s, use_observation_age)
        if fix is not None:
            fixes.append(fix)
    if not fixes:
        if isinstance(stability, dict):
            stability.clear()
        return None
    by_floor = {}
    for fix in fixes:
        by_floor.setdefault(fix["floor"], []).append(fix)
    directional = [p for p in fixes if not p["detection_only"]]
    detection_only = not directional
    floor_votes = {f: [p for p in ps if not p["detection_only"]] for f, ps in by_floor.items()} if directional else by_floor
    floor = max(floor_votes, key=lambda f: sum(p["weight"] for p in floor_votes[f]))
    candidates = floor_votes[floor]
    anchor = max(candidates, key=lambda p: p["weight"])
    pending = False
    if isinstance(stability, dict):
        signature = (tuple(members), floor, anchor["scale"], detection_only)
        if stability.get("signature") != signature:
            stability.clear()
            previous = None
        stability["signature"] = signature
        incumbent = next((p for p in candidates if isinstance(previous, Mapping)
                          and p["ent"] == previous.get("anchor_beacon")), None)
        if incumbent is not None and incumbent["ent"] != anchor["ent"] and _distance(anchor, incumbent) > _agreement_limit(anchor, incumbent):
            # Re-solving cached readings is not another observation, including
            # when the reading-age gate is off. A brief quality reversal must
            # not immediately hand the animal's icon to a distant BLE spike.
            epoch = anchor["observed"]
            challenge = stability.get("challenge")
            if challenge is None or challenge["ent"] != anchor["ent"]:
                previous_epoch = next((p.get("observed", p.get("updated"))
                                       for p in previous.get("beacon_positions", [])
                                       if p.get("ent") == anchor["ent"]), None)
                challenge = {"ent": anchor["ent"], "observed": epoch,
                             "count": int(previous_epoch is None or epoch > previous_epoch)}
            elif epoch > challenge["observed"]:
                challenge = {**challenge, "observed": epoch, "count": challenge["count"] + 1}
            if challenge["count"] < 2:
                stability["challenge"] = challenge
                anchor = incumbent
                pending = True
            else:
                stability.pop("challenge", None)
        else:
            # Agreement, a stale/missing incumbent or a new floor needs no hold.
            stability.pop("challenge", None)
    prior = _motion_previous(previous, floor, anchor["scale"], now, max_age_s) if isinstance(stability, dict) and not detection_only else None
    support = _motion_support(candidates, previous if prior is not None else None, stability, now)
    rejected_motion = []
    if prior is not None:
        nearby = [p for p in candidates if _distance(prior, p) <= min(
            MAX_AGREEMENT_DISTANCE_M, max(GROUP_MOTION_SLACK_M,
                GROUP_MAX_SPEED_MS * max(0.0, p["observed"] - prior["updated"])))]
        if nearby and anchor not in nearby:
            # This also catches the incumbent beacon itself jumping: retaining
            # its ID is not sufficient when its coordinates have just changed.
            rejected_motion = [p["ent"] for p in candidates if p not in nearby]
            anchor = min(nearby, key=lambda p: _distance(prior, p))
            pending = True
    accepted = [anchor] if detection_only else [p for p in candidates if _distance(anchor, p) <= _agreement_limit(anchor, p)]
    weight = sum(p["weight"] for p in accepted)
    if len(accepted) == 1:
        x, y = accepted[0]["cords"]
    else:
        x = sum(p["weight"] * p["cords"][0] for p in accepted) / weight
        y = sum(p["weight"] * p["cords"][1] for p in accepted) / weight
    x, y, motion = _motion_point(x, y, accepted, prior, stability, support)
    if motion is None and rejected_motion:
        motion = {"state": "beacon_jump_rejected", "rejected_beacons": rejected_motion,
                  "max_speed_m_s": GROUP_MAX_SPEED_MS}
    disagreement = max((_distance(a, b) for i, a in enumerate(candidates)
                        for b in candidates[i + 1:]), default=0.0)
    scatter = math.sqrt(sum(p["weight"] * (
        math.hypot(p["cords"][0] - x, p["cords"][1] - y) / p["scale"]) ** 2
        for p in accepted) / weight)
    best_u = min(p["estimated_uncertainty_m"] for p in accepted)
    # Reliability should never make one beacon artificially more certain.
    # Propagate uncertainty using the same normalized weights as the published
    # group centre; age/quality tie-breakers can differ from inverse variance.
    combined = max(best_u * MIN_AGREEMENT_UNCERTAINTY_RATIO, math.sqrt(sum(
        (p["weight"] / weight * p["estimated_uncertainty_m"]) ** 2 for p in accepted)))
    uncertainty = math.hypot(combined, scatter)
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
    environment_fallback = any(p["environment_fallback"] for p in accepted)
    if conflict or floor_conflict or environment_fallback or motion or detection_only:
        confidence = "poor"
    diagnostics = [{k: v for k, v in p.items() if k not in ("scale", "receiver_ids")}
                   for p in fixes]
    accepted_ids = {p["ent"] for p in accepted}
    for entry in diagnostics:
        entry["used"] = entry["ent"] in accepted_ids
        entry["normalized_weight"] = entry["weight"] / weight if entry["used"] else 0.0
        entry["selection_reason"] = ("anchor" if entry["ent"] == anchor["ent"] else
            "agrees_with_anchor" if entry["used"] else
            "different_floor" if entry["floor"] != floor else
            "detection_only_with_directional_fix" if entry["detection_only"] and not detection_only else
            "outside_anchor_agreement")
        if entry["ent"] in rejected_motion:
            entry["excluded_reason"] = "group_motion_outlier"
            entry["selection_reason"] = "group_motion_outlier"
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
            "anchor_beacon": anchor["ent"], "anchor_switch_pending": pending,
            **({"motion_gate": motion} if motion else {}),
            "outdoor": {"estimated_uncertainty_m": uncertainty,
                        "uncertainty_method": "beacon_fusion_heuristic",
                        "beacons_used": len(accepted),
                        "combined_beacon_uncertainty_m": combined,
                        "beacon_scatter_m": scatter,
                        "confidence": confidence,
                        **({"detection_only": True, "direction_known": False,
                            "detection_receiver": anchor["detection_receiver"], "receiver": anchor["detection_receiver"],
                            "detection_range_m": anchor["detection_range_m"]} if detection_only else {}),
                        **({"environment_fallback": True} if environment_fallback else {}),
                        "receivers_used": receivers_used,
                        "receiver_observations": sum(p["receivers_used"] for p in accepted)}}


def fuse_groups(layout, positions, scales, now, max_age_s=DEFAULT_MAX_AGE_S, *,
                use_observation_age=True, known_trackers=None, previous=None, stability=None):
    """Normalize and fuse configured groups; disabled layouts do no work."""
    outdoor = layout.get("outdoor_tracking") if isinstance(layout, Mapping) else None
    if not isinstance(outdoor, Mapping) or outdoor.get("enabled") is not True:
        if isinstance(stability, dict):
            stability.clear()
        return []
    index = _position_index(positions)
    known = set(known_trackers or ())
    known.update(ent for ent, p in index.items() if not p.get("group"))
    groups = normalize_groups(layout, known_trackers=known)
    previous = previous if isinstance(previous, Mapping) else {}
    if isinstance(stability, dict):
        active = {"bps_group_" + g["id"] for g in groups}
        for key in list(stability):
            if key not in active:
                stability.pop(key)
    return [payload for group in groups
            if (payload := fuse_group(group, index, scales, now, max_age_s,
                                      use_observation_age=use_observation_age,
                                      previous=previous.get("bps_group_" + group["id"]),
                                      stability=stability.setdefault("bps_group_" + group["id"], {})
                                      if isinstance(stability, dict) else None)) is not None]
