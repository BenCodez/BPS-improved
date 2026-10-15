"""Read-only comparisons of one detached full snapshot with user supplied truth."""
import json
import math

from .environment import compile_environment, outdoor_settings, path_reliability, reading_max_age
from .radio_diagnostics import RadioEvidence

MAX_MARKERS = 32
MAX_READINGS = 32768
MAX_PATH_EVALUATIONS = 256
MAX_GEOMETRY_WORK = 250000
MAX_COMPARISON_BYTES = 2 * 1024 * 1024
GUIDE = (
    "Known positions apply only at this snapshot instant, never to past or future history. "
    "Range errors compare corrected slant distances with saved receiver geometry and assumed heights. "
    "Accuracy is the user's comparison tolerance, not sensor confidence. Mapped paths describe relative trust, "
    "not RF loss. Disagreement does not prove a receiver fault or justify calibration changes. "
    "Published positions retain their own observation clocks; current readings do not refresh old fixes."
    " Filtered Bermuda distances can carry smoothing history; keep the target stationary for 30–60 seconds before capture."
    " Mapped paths are assessed for at most 256 of the largest ranked range discrepancies within a shared geometric complexity budget; omitted paths are not assessed."
)


def number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _scalar(value):
    return value if value is None or isinstance(value, (str, bool)) or number(value) else None


def _json_size(value):
    return len(json.dumps(value, separators=(",", ":"), allow_nan=False, ensure_ascii=False).encode("utf-8"))


def _check_comparison_bytes(size):
    if size > MAX_COMPARISON_BYTES:
        raise ValueError("Known-position comparison exceeds 2 MiB; reduce comparison targets or receiver scope")


def _unevaluated_path(reason):
    return {"classification": "not_evaluated", "environmental_weight": None,
            "mapped_environmental_weight": None, "reason": reason}


def _geometry_work(floor):
    """Conservative edge-work estimate before compilation or path traversal.

    Compilation and per-wall lookup can be quadratic in vertex count. Charge
    that upper-bound shape cost even on cache hits; malformed input is not free.
    Stop counting as soon as the entire snapshot allowance is exceeded.
    """
    records = floor.get("environment", [])
    if not isinstance(records, list):
        return MAX_GEOMETRY_WORK + 1
    work = 1
    for record in records:
        points = record.get("points", []) if isinstance(record, dict) else []
        count = len(points) if isinstance(points, list) else 0
        work += count * count + 4 * count + 1
        if work > MAX_GEOMETRY_WORK:
            break
    return work


def validate_markers(snapshot, markers):
    """Validate only against the captured configuration, including group membership."""
    if not isinstance(markers, list) or not 1 <= len(markers) <= MAX_MARKERS:
        raise ValueError("Specify 1–32 known positions")
    context = snapshot.get("context")
    if not isinstance(context, dict) or not isinstance(context.get("layout"), dict):
        raise ValueError("Save a BPS layout before comparing a known position")
    floors = context["layout"].get("floor")
    members = context.get("target_members")
    if not isinstance(floors, list) or not isinstance(members, dict):
        raise ValueError("Snapshot has no valid layout or target membership")
    result, occupied, targets = [], set(), set()
    for marker in markers:
        if not isinstance(marker, dict) or set(marker) - {"target", "floor", "x_m", "y_m", "accuracy_m", "label"}:
            raise ValueError("Known position must contain only target, floor, coordinates, accuracy and label")
        target, floor = marker.get("target"), marker.get("floor")
        if not isinstance(target, str) or not 1 <= len(target) <= 128 or target not in members:
            raise ValueError("Select a target present in the captured snapshot")
        constituents = members[target]
        if not isinstance(constituents, list) or not constituents or any(
                not isinstance(b, str) or not b for b in constituents):
            raise ValueError("Target has no valid captured member beacons")
        if target in targets or occupied.intersection(constituents):
            raise ValueError("Known positions cannot repeat targets or overlap group members")
        candidates = [f for f in floors if isinstance(f, dict) and f.get("name") == floor]
        if not isinstance(floor, str) or len(candidates) != 1 or not number(candidates[0].get("scale")) or not 1e-6 <= candidates[0]["scale"] <= 1e6:
            raise ValueError("Select a saved floor with a valid metric scale")
        if any(not number(marker.get(k)) or abs(marker[k]) > 100000 for k in ("x_m", "y_m")):
            raise ValueError("Known coordinates must be finite metres within ±100000")
        accuracy = marker.get("accuracy_m", 1.0)
        if not number(accuracy) or not 0 <= accuracy <= 100:
            raise ValueError("Accuracy must be 0–100 metres")
        label = marker.get("label", "")
        if not isinstance(label, str) or len(label) > 240:
            raise ValueError("Marker label must be at most 240 characters")
        targets.add(target)
        occupied.update(constituents)
        result.append({"target": target, "floor": floor, "x_m": marker["x_m"],
                       "y_m": marker["y_m"], "accuracy_m": accuracy, "label": label})
    return result


def _identity(link):
    """Only explicit owner-scoped matching proof establishes scanner identity."""
    match = link.get("matching")
    if not isinstance(match, dict) or match.get("identity_ambiguous"):
        return None
    owners, mac = match.get("config_entry_ids"), match.get("scanner_mac")
    if isinstance(owners, list) and len(owners) == 1 and isinstance(owners[0], str) and isinstance(mac, str) and mac:
        return (owners[0], mac.lower())
    return None


def _position_error(target, marker, positions, floor, context, now, role):
    row = {"target": target, "role": role, "status": "missing", "scored": False}
    candidates = positions.get(target, [])
    if len(candidates) != 1:
        if candidates:
            row["status"] = "ambiguous"
        return row
    pos = candidates[0]
    outdoor = isinstance(pos.get("outdoor"), dict)
    quality = pos["outdoor"] if outdoor else {}
    observed = pos.get("updated") if quality.get("use_observation_age") is False else quality.get("observed", pos.get("updated"))
    age = max(0, now - observed) if number(observed) and 0 <= observed <= now else None
    layout, defaults = context["layout"], context.get("defaults", {})
    if outdoor:
        threshold = quality.get("stale_after_s", 30)
        threshold = threshold if number(threshold) and threshold > 0 else 30
        expired = age is not None and age >= threshold
    else:
        default = defaults.get("position_timeout_s", 300) if isinstance(defaults, dict) else 300
        default = default if number(default) and default > 0 else 300
        threshold = layout.get("position_timeout", default)
        threshold = threshold if number(threshold) and threshold > 0 else default
        # Indoor pruning uses a strict boundary on the last published fix.
        expired = age is not None and age > threshold
    stale = quality.get("stale") is True or expired
    detection_only = quality.get("detection_only") is True or quality.get("direction_known") is False
    row.update(floor=pos.get("floor"), updated=_scalar(pos.get("updated")), observed=_scalar(observed),
               age_s=age, stale_after_s=threshold, wrong_floor=pos.get("floor") != marker["floor"], stale=stale,
               detection_only=detection_only)
    if pos.get("group"):
        row["group_state_source"] = {"section": "positions", "ent": target}
        row["anchor_beacon"] = pos.get("anchor_beacon")
        row["member_status"] = {member["ent"]: member.get("selection_reason", "unknown")
            for member in pos.get("beacon_positions", [])
            if isinstance(member, dict) and isinstance(member.get("ent"), str)}
    xy = pos.get("cords")
    valid = isinstance(xy, (list, tuple)) and len(xy) == 2 and all(number(v) for v in xy)
    if valid and not row["wrong_floor"]:
        x, y = xy[0] / floor["scale"], xy[1] / floor["scale"]
        error = math.hypot(x - marker["x_m"], y - marker["y_m"])
        valid = all(number(v) for v in (x, y, error))
        if valid:
            row.update(x_m=x, y_m=y)
            if detection_only:
                row.update(error_m=None, within_accuracy=None, receiver_centered_distance_m=error,
                           direction_known=False)
            else:
                row.update(error_m=error, within_accuracy=error <= marker["accuracy_m"])
    row["status"] = ("stale" if stale else "unknown_age" if age is None else
                     "wrong_floor" if row["wrong_floor"] else "invalid" if not valid else
                     "detection_only" if detection_only else "current")
    row["scored"] = row["status"] == "current"
    return row


def _solver_evidence(raw, positions, solver_inputs):
    """Prior solve evidence is explicitly separate from the current reading."""
    candidates = positions.get(raw.get("tracker"), [])
    if len(candidates) != 1:
        return None
    position = candidates[0]
    quality = position.get("outdoor")
    if not isinstance(quality, dict):
        return None
    aliases = raw.get("scanner_aliases", [])
    aliases = {a for a in aliases if isinstance(a, str)} if isinstance(aliases, list) else set()
    aliases.add(raw.get("receiver"))
    matches = [d for alias in aliases for d in solver_inputs.get(
        (raw.get("tracker"), raw.get("floor"), alias), [])]
    if len(matches) != 1:
        return None
    keys = ("receiver", "observed", "entity_id", "used", "excluded", "status", "classification",
            "reliability_weight", "environmental_weight", "mapped_environmental_weight",
            "range_trust_weight", "range_trust_distance_m", "nearby_priority_reference_m",
            "accuracy_weight", "freshness_weight", "horizontal_residual_m",
            "measured_distance_m", "corrected_distance_m", "reading_age_s", "floor_election_weight")
    evidence = {k: matches[0][k] for k in keys if k in matches[0]
                and (matches[0][k] is None or isinstance(matches[0][k], (str, bool)) or number(matches[0][k]))}
    return {"kind": "prior_solver_input", "position_updated": _scalar(position.get("updated")),
            "position_observed": _scalar(quality.get("observed")), "reading": evidence}


def analyse(snapshot, markers):
    """Pure analysis; raw snapshot data and configuration are never modified."""
    markers = validate_markers(snapshot, markers)
    now = snapshot.get("time")
    if not number(now):
        raise ValueError("Snapshot time is invalid")
    readings = snapshot.get("readings", [])
    if not isinstance(readings, list) or len(readings) > MAX_READINGS:
        raise ValueError("Snapshot supports at most 32768 readings")
    context = snapshot["context"]
    radio_evidence = RadioEvidence(snapshot)
    floors = {f["name"]: f for f in context["layout"]["floor"] if isinstance(f, dict) and isinstance(f.get("name"), str)}
    placements, links, positions, solver_inputs = {}, {}, {}, {}
    for name, floor in floors.items():
        for receiver in floor.get("receivers", []):
            if isinstance(receiver, dict) and isinstance(receiver.get("entity_id"), str):
                placements.setdefault((name, receiver["entity_id"]), []).append(receiver)
    linking = snapshot.get("scanner_linking", {})
    for link in linking.get("placed", []) if isinstance(linking, dict) else []:
        if isinstance(link, dict):
            links.setdefault((link.get("floor"), link.get("entity_id")), []).append(link)
    for pos in snapshot.get("positions", []):
        if isinstance(pos, dict) and isinstance(pos.get("ent"), str):
            positions.setdefault(pos["ent"], []).append(pos)
            quality = pos.get("outdoor")
            if isinstance(quality, dict):
                for diagnostic in quality.get("receiver_diagnostics", []):
                    if isinstance(diagnostic, dict) and isinstance(diagnostic.get("receiver"), str):
                        solver_inputs.setdefault((pos["ent"], diagnostic.get("floor", pos.get("floor")),
                                                  diagnostic["receiver"]), []).append(diagnostic)
    rows_by_tracker = {}
    for row in readings:
        if isinstance(row, dict) and isinstance(row.get("tracker"), str):
            rows_by_tracker.setdefault(row["tracker"], []).append(row)
    output, path_candidates = [], []
    comparison_bytes = _json_size({"format": "bps-known-position-comparison-v1", "time": now, "guide": GUIDE})
    max_age = reading_max_age(context["layout"], context.get("defaults", {}).get("reading_max_age_s", 30))
    for marker_index, marker in enumerate(markers):
        members = list(dict.fromkeys(context["target_members"][marker["target"]]))
        floor = floors[marker["floor"]]
        ranked, unscored, seen_sources, seen_scanners = [], [], set(), set()
        for tracker in members:
            for raw in rows_by_tracker.get(tracker, []):
                row = {k: _scalar(raw.get(k)) for k in ("tracker", "receiver", "floor", "entity_id", "status", "age_s", "observed", "ignored", "assumed_unit_m", "measured_distance_m", "corrected_distance_m")}
                row["environment"] = _unevaluated_path("reading_unscored")
                rec = None
                receiver_xy = None
                evidence = _solver_evidence(raw, positions, solver_inputs)
                if evidence is not None:
                    row["solver_evidence"] = evidence
                reasons = []
                if raw.get("floor") != marker["floor"]:
                    reasons.append("off_floor")
                if raw.get("status") != "current":
                    reasons.append(str(raw.get("status") or "unknown"))
                if raw.get("ignored"):
                    reasons.append("ignored")
                if raw.get("assumed_unit_m") is not False:
                    reasons.append("unproven_unit")
                age, observed = raw.get("age_s"), raw.get("observed")
                source_age = now - observed if number(observed) and 0 <= observed <= now else None
                row["source_age_s"] = source_age
                if not number(age) or age < 0 or source_age is None:
                    reasons.append("unknown_age")
                elif max_age and max(age, source_age) >= max_age:
                    reasons.append("stale")
                if any(not number(raw.get(k)) or raw[k] < 0 for k in ("measured_distance_m", "corrected_distance_m")):
                    reasons.append("invalid_distance")
                key = (raw.get("floor"), raw.get("receiver"))
                candidates = placements.get(key, [])
                link_candidates = links.get(key, [])
                if len(candidates) != 1 or len(link_candidates) > 1:
                    reasons.append("ambiguous_placement" if candidates else "missing_placement")
                else:
                    rec = candidates[0]
                    xy = rec.get("cords")
                    if not isinstance(xy, dict) or any(not number(xy.get(k)) for k in ("x", "y")):
                        reasons.append("invalid_geometry")
                    elif raw.get("floor") == marker["floor"]:
                        horizontal = math.hypot(xy["x"] / floor["scale"] - marker["x_m"], xy["y"] / floor["scale"] - marker["y_m"])
                        height, tracker_height = rec.get("height"), raw.get("tracker_height_m")
                        height_used = number(height) and 0 <= height <= 10 and number(tracker_height) and 0 <= tracker_height <= 5
                        expected = math.hypot(horizontal, height - tracker_height) if height_used else horizontal
                        if not number(expected):
                            reasons.append("invalid_geometry")
                        else:
                            receiver_xy = (xy["x"], xy["y"])
                            row.update(expected_horizontal_m=horizontal, expected_distance_m=expected,
                                       height_model="slant" if height_used else "horizontal")
                        if number(expected) and number(raw.get("corrected_distance_m")):
                            error = raw["corrected_distance_m"] - expected
                            relative = 100 * (error / expected) if expected > 1e-9 else None
                            row.update(signed_error_m=error, abs_error_m=abs(error),
                                       relative_error_pct=relative if number(relative) else None,
                                       disagrees=abs(error) > marker["accuracy_m"])
                identity = _identity(link_candidates[0]) if len(link_candidates) == 1 else None
                row["radio"] = radio_evidence.row(raw, rec, row, identity)
                row["receiver_correction"] = _scalar(raw.get("receiver_correction"))
                row["tracker_distance_factor"] = _scalar(raw.get("tracker_distance_factor"))
                source_key = (tracker, raw.get("entity_id"))
                scanner_key = (tracker, identity)
                if not reasons:
                    if not isinstance(raw.get("entity_id"), str) or not raw["entity_id"]:
                        reasons.append("unproven_source")
                    elif source_key in seen_sources or identity is not None and scanner_key in seen_scanners:
                        reasons.append("duplicate_source")
                    else:
                        seen_sources.add(source_key)
                        if identity is not None:
                            seen_scanners.add(scanner_key)
                if reasons:
                    row["reasons"] = list(dict.fromkeys(reasons))
                    unscored.append(row)
                else:
                    row["environment"] = _unevaluated_path("path_evaluation_budget")
                    ranked.append(row)
                    path_candidates.append((row, marker_index, receiver_xy))
                comparison_bytes += _json_size(row)
                _check_comparison_bytes(comparison_bytes)
            if tracker not in rows_by_tracker:
                missing = {"tracker": tracker, "status": "missing", "reasons": ["missing_readings"],
                           "environment": _unevaluated_path("reading_unscored")}
                comparison_bytes += _json_size(missing)
                _check_comparison_bytes(comparison_bytes)
                unscored.append(missing)
        ranked.sort(key=lambda row: (-row["abs_error_m"], str(row["tracker"]), str(row["receiver"])))
        for rank, row in enumerate(ranked, 1):
            row["rank"] = rank
        targets = [marker["target"]] + [m for m in members if m != marker["target"]]
        errors = [_position_error(t, marker, positions, floor, context, now,
                                  "selected" if t == marker["target"] else "member") for t in targets]
        reason_counts = {}
        for row in unscored:
            for reason in row["reasons"]:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
        item = {**marker, "members": members, "summary": {
            "ranked_readings": len(ranked), "unscored_readings": len(unscored),
            "disagreeing_readings": sum(row["disagrees"] for row in ranked),
            "reason_counts": reason_counts,
            "largest_range_errors": [{k: row[k] for k in ("tracker", "receiver", "signed_error_m", "abs_error_m")}
                                     for row in ranked[:5]]},
            "ranked_readings": ranked, "unscored_readings": unscored, "position_errors": errors}
        comparison_bytes += _json_size({k: v for k, v in item.items() if k not in ("ranked_readings", "unscored_readings")})
        _check_comparison_bytes(comparison_bytes)
        output.append(item)
    # Globally prioritize the largest valid discrepancies, including across markers.
    path_candidates.sort(key=lambda item: (-item[0]["abs_error_m"], str(item[0]["tracker"]), str(item[0]["receiver"])))
    compiled_by_floor, evaluated_by_marker, work_by_floor = {}, {}, {}
    geometry_work = 0
    strength = outdoor_settings(context["layout"])["obstruction_strength"]
    for row, marker_index, receiver_xy in path_candidates[:MAX_PATH_EVALUATIONS]:
        marker = markers[marker_index]
        floor = floors[marker["floor"]]
        if marker["floor"] not in work_by_floor:
            work_by_floor[marker["floor"]] = _geometry_work(floor)
        work = work_by_floor[marker["floor"]]
        required_work = work * (2 if marker["floor"] not in compiled_by_floor else 1)
        if geometry_work + required_work > MAX_GEOMETRY_WORK:
            row["environment"] = _unevaluated_path("geometry_work_budget")
            continue
        geometry_work += required_work
        if marker["floor"] not in compiled_by_floor:
            try:
                compiled_by_floor[marker["floor"]] = (compile_environment(floor), None)
            except Exception as error:
                # Optional mapped-path evidence must not hide the full BLE snapshot.
                compiled_by_floor[marker["floor"]] = (None, type(error).__name__)
        compiled, compilation_error = compiled_by_floor[marker["floor"]]
        path = path_reliability(compiled, receiver_xy,
                                (marker["x_m"] * floor["scale"], marker["y_m"] * floor["scale"]),
                                scale_px_per_m=floor["scale"], obstruction_strength=strength)
        if compilation_error:
            path.update(classification="unknown", environment_error=compilation_error)
        comparison_bytes += _json_size(path) - _json_size(row["environment"])
        _check_comparison_bytes(comparison_bytes)
        row["environment"] = path
        evaluated_by_marker[marker_index] = evaluated_by_marker.get(marker_index, 0) + 1
    for index, item in enumerate(output):
        evaluated = evaluated_by_marker.get(index, 0)
        omitted = len(item["ranked_readings"]) - evaluated
        item["summary"].update(path_evaluated_readings=evaluated, path_unevaluated_readings=omitted,
                               path_evaluation_truncated=omitted > 0)
    evaluated = sum(evaluated_by_marker.values())
    comparison = {"format": "bps-known-position-comparison-v1", "time": now, "guide": GUIDE,
                  "markers": output, "path_evaluation": {"limit": MAX_PATH_EVALUATIONS,
                  "geometry_work_limit": MAX_GEOMETRY_WORK, "geometry_work_used": geometry_work,
                  "evaluated": evaluated, "omitted": len(path_candidates) - evaluated,
                  "truncated": len(path_candidates) > evaluated}}
    _check_comparison_bytes(_json_size(comparison))
    return comparison
