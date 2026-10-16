"""Bounded, detached cross-mark radio evidence for offline inspection.

No Home Assistant objects, calibration changes, or source records are touched.
Only named fields from the known-position comparison enter the export.
"""

import json
import math


MAX_SAMPLES = 4096
MAX_PAIRS = 1024
MAX_BYTES = 8 * 1024 * 1024
SAMPLE_BYTE_BUDGET = 5 * 1024 * 1024
RAW_MAX_AGE_S = 3
STAMP_CLUSTER_S = 60
GUIDE = (
    "Each row is a labelled radio observation at one known position. Only fresh, "
    "owned packets with usable geometry and a matching radio model enter pair summaries. "
    "Repeated packets count once; conflicting position labels exclude that packet. "
    "Filtered and BPS-corrected ranges are optional comparisons. Median errors and "
    "RSSI residuals are diagnostic evidence, not calibrated offsets or fit results. "
    "Distance bands use measured slant range, never the radio estimate. Compare "
    "near and far bands before extrapolating an indoor model outdoors; 10 m is "
    "a comparison boundary, not a radio limit. Unknown hardware limits generalization across devices. "
    "Moving phone GPS references remain available in samples and full captured reports, "
    "but are excluded from stationary radio-model summaries."
)


def _number(value, *, minimum=None):
    try:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and (minimum is None or value >= minimum))
    except OverflowError:
        return False


def _text(value, limit=128):
    return value[:limit] if isinstance(value, str) else None


def _num(value, *, minimum=None):
    return value if _number(value, minimum=minimum) else None


def _sha(value):
    return value.lower() if isinstance(value, str) and len(value) == 64 and all(
        c in "0123456789abcdefABCDEF" for c in value) else None


def _fields(value, keys):
    value = value if isinstance(value, dict) else {}
    result = {}
    for key in keys:
        item = value.get(key)
        if isinstance(item, str):
            result[key] = _text(item, 96)
        elif isinstance(item, bool):
            result[key] = item
        elif _number(item):
            result[key] = item
    return result


def _hardware(value):
    value = value if isinstance(value, dict) else {}
    return {
        "detected": _fields(value.get("detected"), ("manufacturer", "model", "hardware_type")),
        "effective": _fields(value.get("effective"), ("manufacturer", "model", "hardware_type",
                                                     "external_antenna")),
        "provenance": _text(value.get("provenance"), 96),
        "bps_profile": _fields(value.get("bps_profile"), ("hardware_model", "external_antenna",
                                                     "antenna_gain_dbi", "antenna_notes", "notes")),
        "radio_profile": _fields(value.get("radio_profile"), ("manufacturer", "model",
                                                         "hardware_type", "antenna", "external_antenna",
                                                         "antenna_gain_dbi", "antenna_gain_db", "notes", "environment")),
    }


def _known_hardware(value):
    for section in ("detected", "effective", "bps_profile", "radio_profile"):
        fields = value[section]
        for key in ("model", "hardware_type", "hardware_model"):
            item = fields.get(key)
            if isinstance(item, str) and item and item.lower() not in ("unknown", "unavailable"):
                return True
    return False


def _size(value):
    return len(json.dumps(value, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("utf-8"))


def _median(values):
    values = sorted(values)
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else \
        values[middle - 1] / 2 + values[middle] / 2


def _location_groups(group):
    """Nearby labels inside stated tolerances describe one measured location."""
    places = []
    for sample in group:
        location = sample["location"]
        matching = next((rows for rows in places if location["floor"] == rows[0]["location"]["floor"] and
                   math.hypot(location["x_m"] - rows[0]["location"]["x_m"],
                              location["y_m"] - rows[0]["location"]["y_m"]) <=
                   location["accuracy_m"] + rows[0]["location"]["accuracy_m"]), None)
        if matching is None:
            places.append([sample])
        else:
            matching.append(sample)
    return places


def _marker(value):
    value = value if isinstance(value, dict) else {}
    return {"target": _text(value.get("target")), "floor": _text(value.get("floor")),
            "x_m": _num(value.get("x_m")), "y_m": _num(value.get("y_m")),
            "accuracy_m": _num(value.get("accuracy_m"), minimum=0)}


def _sample(record, marker, source, radio):
    packet = radio.get("packet") if isinstance(radio.get("packet"), dict) else {}
    identity = radio.get("identity") if isinstance(radio.get("identity"), dict) else {}
    model_check = radio.get("model_check") if isinstance(radio.get("model_check"), dict) else {}
    comparison = radio.get("comparison") if isinstance(radio.get("comparison"), dict) else {}
    capture = record.get("capture") if isinstance(record.get("capture"), dict) else {}
    environment = source.get("environment") if isinstance(source.get("environment"), dict) else {}
    source_exclusions = radio.get("exclusions") if isinstance(radio.get("exclusions"), list) else []
    # The source list is diagnostic only. Unknown free-form text never enters the ZIP.
    allowed_exclusions = {
        "ignored", "invalid_geometry", "missing_geometry", "raw_source_not_current",
        "raw_packet_missing", "stale_packet", "unknown_clock", "future_clock",
        "invalid_rssi", "model_unavailable", "model_mismatch", "invalid_model",
        "ambiguous_identity", "unowned_identity", "radio_unavailable",
        "ignored_receiver", "positive_slant_geometry_required",
        "raw_packet_clock_unknown_or_future", "raw_packet_stale",
        "raw_rssi_unavailable", "radio_model_mismatch", "radio_model_unavailable",
        "radio_pair_missing_or_ambiguous", "radio_device_identity_conflict",
        "legacy_radio_pair_not_proven", "radio_identity_unavailable",
        "radio_scanner_identity_conflict",
    }
    reasons = [reason for reason in source_exclusions if isinstance(reason, str)
               and reason in allowed_exclusions]
    moving = isinstance(record.get("gps_reference"), dict)
    if moving:
        reasons.append("moving_gps_reference_not_stationary")
    if any(reason not in allowed_exclusions for reason in source_exclusions
           if isinstance(reason, str)) or any(not isinstance(reason, str)
                                              for reason in source_exclusions):
        reasons.append("source_exclusion_unrecognized")
    sample = {
        "capture": {"id": _text(record.get("id")),
                    "time": _num(record.get("time")) if _number(record.get("time"))
                    else _text(record.get("time"), 64),
                    "versions": {str(k)[:64]: _text(v, 96) if isinstance(v, str) else _num(v)
                                 for k, v in capture.get("versions", {}).items()
                                 if isinstance(capture.get("versions"), dict) and
                                 isinstance(k, str) and k in (
                                     "bps", "bermuda", "home_assistant", "python", "schema")}
                    if isinstance(capture.get("versions"), dict) else {},
                    "settings_sha256": _sha(capture.get("settings_sha256"))},
        "location": _marker(record.get("marker")),
        "reference_kind": "moving_phone_gps" if moving else "manual_point",
        "reading": {"tracker": _text(source.get("tracker")),
                    "receiver": _text(source.get("receiver")),
                    "expected_distance_m": _num(source.get("expected_distance_m"), minimum=0),
                    "height_model": _text(source.get("height_model"), 32),
                    "measured_distance_m": _num(source.get("measured_distance_m"), minimum=0),
                    "corrected_distance_m": _num(source.get("corrected_distance_m"), minimum=0),
                    "receiver_correction": _num(source.get("receiver_correction"), minimum=0),
                    "tracker_distance_factor": _num(source.get("tracker_distance_factor"), minimum=0),
                    "signed_error_m": _num(source.get("signed_error_m"))},
        "environment": {"classification": _text(environment.get("classification"), 64)},
        "radio": {
            "status": _text(radio.get("status"), 32), "path": _text(radio.get("path"), 64),
            "identity": {key: _text(identity.get(key)) for key in
                         ("entry_id", "device_id", "scanner_mac")},
            "packet": {"observed": _num(packet.get("observed")),
                       "stamp": _num(packet.get("stamp")) if _number(packet.get("stamp"))
                       else _text(packet.get("stamp"), 80),
                       "age_s": _num(packet.get("age_s"), minimum=0),
                       "observed_clock": _text(packet.get("observed_clock"), 48)},
            "raw_rssi_dbm": _num(radio.get("raw_rssi_dbm")),
            "filtered_rssi_dbm": _num(radio.get("filtered_rssi_dbm")),
            "tx_power_dbm": _num(radio.get("tx_power_dbm")),
            "reference_power_dbm": _num(radio.get("reference_power_dbm")),
            "rssi_offset_db": _num(radio.get("rssi_offset_db")),
            "attenuation": _num(radio.get("attenuation"), minimum=0),
            "raw_distance_m": _num(radio.get("raw_distance_m"), minimum=0),
            "filtered_distance_m": _num(radio.get("filtered_distance_m"), minimum=0),
            "model_version": _text(radio.get("model_version"), 96),
            "reference_power_verified": radio.get("reference_power_verified") is True,
            "quality": _fields(radio.get("quality"), ("mode", "status", "age", "samples", "noise_db",
                                                        "reliability", "last_raw_stamp", "last_valid_stamp",
                                                        "last_valid_observed")),
            "signal_interval": _fields(radio.get("signal_interval"), ("status", "reason", "lower_m", "center_m", "upper_m",
                "noise_db", "noise_floor_db", "noise_multiplier", "delta_db", "source_observed", "source_stamp",
                "solver_eligible", "solver_reason", "model_version")),
            "receiver_hardware": _hardware(radio.get("receiver_hardware")),
            "beacon_hardware": _hardware(radio.get("beacon_hardware")),
            "context_sha256": _sha(radio.get("context_sha256")),
            "model_check": {"status": _text(model_check.get("status"), 32),
                            "calculated_raw_distance_m": _num(
                                model_check.get("calculated_raw_distance_m"), minimum=0)},
            "comparison": {key: _num(comparison.get(key)) for key in (
                "raw_error_m", "filtered_error_m", "corrected_error_m",
                "expected_rssi_dbm", "rssi_residual_db", "required_offset_delta_db")},
        },
        "exclusions": [],
        "eligible": False,
    }
    loc, reading, r = sample["location"], sample["reading"], sample["radio"]
    p, ident = r["packet"], r["identity"]
    if r["status"] != "available":
        reasons.append("radio_unavailable")
    if source.get("ignored") is True:
        reasons.append("ignored")
    if not all(loc[k] is not None for k in ("floor", "x_m", "y_m", "accuracy_m")) or \
            not reading["expected_distance_m"] or reading["height_model"] != "slant":
        reasons.append("invalid_geometry")
    if p["stamp"] is None:
        reasons.append("raw_packet_missing")
    clock = p["observed_clock"]
    capture_time = sample["capture"]["time"]
    if not clock or clock.lower() in ("unknown", "unavailable", "none", "capture", "acceptance") or \
            p["age_s"] is None or p["observed"] is None or not _number(capture_time) or \
            p["observed"] < 0 or p["observed"] > capture_time:
        reasons.append("unknown_clock")
    elif max(p["age_s"], capture_time - p["observed"]) > RAW_MAX_AGE_S:
        reasons.append("stale_packet")
    if r["raw_rssi_dbm"] is None or not -127 <= r["raw_rssi_dbm"] < 0:
        reasons.append("invalid_rssi")
    if r["model_check"]["status"] == "mismatch":
        reasons.append("model_mismatch")
    elif r["model_check"]["status"] != "matches":
        reasons.append("model_unavailable")
    if any(r[key] is None for key in ("reference_power_dbm", "rssi_offset_db",
                                       "raw_distance_m")) or not r["attenuation"]:
        reasons.append("invalid_model")
    if not ident["entry_id"] or not ident["device_id"] or not ident["scanner_mac"]:
        reasons.append("unowned_identity")
    if not reading["tracker"] or not r["context_sha256"]:
        reasons.append("missing_context")
    sample["exclusions"] = list(dict.fromkeys(reasons))
    return sample


def _packet_key(sample):
    radio = sample["radio"]
    ident, packet = radio["identity"], radio["packet"]
    if not sample["reading"]["tracker"] or not ident["entry_id"] or \
            not ident["device_id"] or not ident["scanner_mac"] or \
            packet["stamp"] is None or not packet["observed_clock"]:
        return None
    # Moving references must never invalidate stationary labels of the same
    # source packet. Deduplicate/conflict-check within each reference cohort.
    return (sample["reference_kind"], ident["entry_id"], ident["device_id"],
            ident["scanner_mac"], packet["observed_clock"], packet["stamp"])


def _incompatible(a, b):
    x, y = a["location"], b["location"]
    if a["radio"]["context_sha256"] != b["radio"]["context_sha256"] or x["floor"] != y["floor"]:
        return True
    if any(x[k] is None or y[k] is None for k in ("x_m", "y_m", "accuracy_m")):
        return True
    return math.hypot(x["x_m"] - y["x_m"], x["y_m"] - y["y_m"]) > \
        x["accuracy_m"] + y["accuracy_m"]


def _pair_key(sample):
    r = sample["radio"]
    return json.dumps((r["identity"], r["path"], sample["environment"]["classification"],
                       r["model_version"], r["reference_power_dbm"], r["rssi_offset_db"],
                       r["attenuation"], r["context_sha256"], r["receiver_hardware"],
                       r["beacon_hardware"]), sort_keys=True, separators=(",", ":"))


def _range_summary(group):
    """Signed bias and absolute error remain separate, in measured range bands."""
    result = {"samples": len(group), "median_errors_m": {}, "median_absolute_errors_m": {}}
    for key in ("raw_error_m", "filtered_error_m", "corrected_error_m"):
        values = [s["radio"]["comparison"][key] for s in group
                  if s["radio"]["comparison"][key] is not None]
        result["median_errors_m"][key] = _median(values) if values else None
        result["median_absolute_errors_m"][key] = _median([abs(v) for v in values]) if values else None
    ratios = [s["radio"]["raw_distance_m"] / s["reading"]["expected_distance_m"] for s in group]
    ratios = [value for value in ratios if _number(value)]
    result["median_raw_to_true_ratio"] = _median(ratios) if ratios else None
    intervals = [s for s in group if s["radio"]["signal_interval"].get("status") == "available"]
    result["observed_interval_coverage"] = {"samples": len(intervals), "truth_inside": sum(
        s["radio"]["signal_interval"]["lower_m"] <= s["reading"]["expected_distance_m"] <= s["radio"]["signal_interval"]["upper_m"]
        for s in intervals)}
    return result


def _distance_bands(group):
    bands = []
    for lower, upper in ((0, 5), (5, 10), (10, 25), (25, 50), (50, None)):
        rows = [s for s in group if lower <= s["reading"]["expected_distance_m"]
                and (upper is None or s["reading"]["expected_distance_m"] < upper)]
        if rows:
            bands.append({"lower_inclusive_m": lower, "upper_exclusive_m": upper,
                          **_range_summary(rows)})
    return bands


def _distance_model_trend(group):
    """Equal-location exploratory trend; never turn a few packets into calibration."""
    places = _location_groups(group)
    points = [(_median([math.log10(s["reading"]["expected_distance_m"]) for s in place]),
               _median([s["radio"]["comparison"]["rssi_residual_db"] for s in place
                        if s["radio"]["comparison"]["rssi_residual_db"] is not None]))
              for place in places if any(s["radio"]["comparison"]["rssi_residual_db"] is not None for s in place)]
    span = max((p[0] for p in points), default=0) - min((p[0] for p in points), default=0)
    result = {"status": "insufficient_data", "locations": len(points), "unique_packets": len(group),
              "log10_range_span": span,
              "near_locations": sum(x < 1 for x, _ in points),
              "far_locations": sum(x >= 1 for x, _ in points),
              "long_distance_coverage": "near_and_far" if any(x < 1 for x, _ in points) and any(x >= 1 for x, _ in points) else "needs_near_and_far_marks",
              "guide": "RSSI residual versus log10 measured slant range, using one median per marked location. Positive slope means the captured model increasingly underestimates distance. An offset changes the intercept; attenuation changes the slope. This is an exploratory in-sample trend, not a verified correction or tracking improvement. Body/antenna orientation, walls, map scale and heights can mimic a model error; validate at held-out locations."}
    if len(points) < 3 or span < math.log10(2):
        result["reason"] = "Need at least three measured locations spanning a factor of two in range"
        return result
    xbar, ybar = sum(p[0] for p in points) / len(points), sum(p[1] for p in points) / len(points)
    denominator = sum((x - xbar) ** 2 for x, _ in points)
    slope = sum((x - xbar) * (y - ybar) for x, y in points) / denominator
    intercept = ybar - slope * xbar
    attenuation = group[0]["radio"]["attenuation"] - slope / 10
    rmse = math.hypot(*(y - intercept - slope * x for x, y in points)) / math.sqrt(len(points))
    if not all(_number(value) for value in (slope, intercept, attenuation, rmse)):
        result["reason"] = "Non-finite trend"
        return result
    result.update(status="exploratory", residual_slope_db_per_decade=slope,
                  residual_intercept_db_at_1m=intercept,
                  implied_attenuation=attenuation if attenuation > 0 else None,
                  trend_rmse_db=rmse,
                  validation="held_out_locations_required")
    return result


def _summaries(samples):
    groups = {}
    for sample in samples:
        if sample["eligible"]:
            groups.setdefault(_pair_key(sample), []).append(sample)
    summaries = []
    for group in groups.values():
        first, radio = group[0], group[0]["radio"]
        ranges = [s["reading"]["expected_distance_m"] for s in group]
        locations = len(_location_groups(group))
        medians = {}
        for key in ("raw_error_m", "filtered_error_m", "corrected_error_m", "rssi_residual_db"):
            values = [s["radio"]["comparison"][key] for s in group
                      if s["radio"]["comparison"][key] is not None]
            medians[key] = _median(values) if values else None
        span = max(ranges) - min(ranges)
        reasons = []
        if len(group) < 3:
            reasons.append("fewer_than_three_unique_packets")
        if locations < 3:
            reasons.append("fewer_than_three_distinct_locations")
        if span < 2:
            reasons.append("range_span_below_two_metres")
        known_hardware = (_known_hardware(radio["receiver_hardware"]) and
                          _known_hardware(radio["beacon_hardware"]))
        summaries.append({
            "tracker": first["reading"]["tracker"],
            "tracker_labels": sorted({s["reading"]["tracker"] for s in group})[:8],
            "receiver": first["reading"]["receiver"],
            "receiver_labels": sorted({s["reading"]["receiver"] for s in group
                                       if s["reading"]["receiver"] is not None})[:8],
            "identity": radio["identity"], "path": radio["path"],
            "environment_classification": first["environment"]["classification"],
            "model": {key: radio[key] for key in ("model_version", "reference_power_dbm",
                                                     "rssi_offset_db", "attenuation")},
            "context_sha256": radio["context_sha256"],
            "receiver_hardware": radio["receiver_hardware"],
            "beacon_hardware": radio["beacon_hardware"],
            "counts": {"samples": len(group), "unique_packets": len(group),
                       "distinct_locations": locations},
            "true_range_span_m": span,
            "median_errors_m": {k: medians[k] for k in (
                "raw_error_m", "filtered_error_m", "corrected_error_m")},
            "median_rssi_residual_db": medians["rssi_residual_db"],
            "distance_bands": _distance_bands(group),
            "distance_model_trend": _distance_model_trend(group),
            "hardware_metadata": "known" if known_hardware else "unknown_or_partial",
            "readiness": {"comparison": "ready", "exploratory_fitting": "candidate" if not reasons
                          else "insufficient_diversity", "reasons": reasons,
                          "hardware_generalization": "supported_metadata" if known_hardware
                          else "unknown_hardware"},
        })
    return summaries


def export(records):
    """Project saved ground-truth records into a bounded, JSON-safe survey.

    Input records are detached dictionaries. Invalid inputs raise ValueError;
    missing historic radio fields are counted and omitted from samples.
    """
    if not isinstance(records, (list, tuple)):
        raise ValueError("Radio survey records must be a list or tuple")
    counts = {"records": len(records), "records_missing_radio": 0,
              "rows_missing_radio": 0, "candidate_rows": 0,
              "samples": 0, "eligible_samples": 0, "excluded_samples": 0,
              "omitted_samples": 0, "unique_packets": 0}
    omissions = {"sample_limit": 0, "byte_limit": 0, "pair_limit": 0}
    samples, packet_groups, pair_keys = [], {}, set()
    sample_bytes = 0
    for record in records:
        if not isinstance(record, dict):
            counts["records_missing_radio"] += 1
            continue
        comparison = record.get("comparison") if isinstance(record.get("comparison"), dict) else {}
        markers = comparison.get("markers") if isinstance(comparison.get("markers"), list) else []
        found_radio = False
        for marker in markers:
            if not isinstance(marker, dict):
                continue
            for field in ("ranked_readings", "unscored_readings"):
                rows = marker.get(field)
                if not isinstance(rows, list):
                    continue
                for source in rows:
                    if not isinstance(source, dict):
                        continue
                    radio = source.get("radio")
                    if not isinstance(radio, dict):
                        counts["rows_missing_radio"] += 1
                        continue
                    found_radio = True
                    counts["candidate_rows"] += 1
                    sample = _sample(record, marker, source, radio)
                    packet_key = _packet_key(sample)
                    prior = packet_groups.get(packet_key) if packet_key is not None else None
                    # Bermuda's monotonic stamp may repeat after a restart. A
                    # derived epoch can jitter across polls of the same packet,
                    # so compare a generous observed-time cluster instead of
                    # using exact epoch equality in the packet identity.
                    group = ([other for other in prior if
                              sample["radio"]["packet"]["observed"] is not None and
                              other["radio"]["packet"]["observed"] is not None and
                              abs(sample["radio"]["packet"]["observed"] -
                                  other["radio"]["packet"]["observed"]) <= STAMP_CLUSTER_S]
                             if prior is not None else None)
                    if group:
                        if any(_incompatible(sample, other) for other in group):
                            for other in group:
                                if "conflicting_packet_location" not in other["exclusions"]:
                                    other["exclusions"].append("conflicting_packet_location")
                        if any("conflicting_packet_location" in other["exclusions"] for other in group):
                            sample["exclusions"].append("conflicting_packet_location")
                        else:
                            sample["exclusions"].append("duplicate_packet")
                    pair_key = _pair_key(sample)
                    if len(samples) >= MAX_SAMPLES:
                        omissions["sample_limit"] += 1
                        continue
                    if pair_key not in pair_keys and len(pair_keys) >= MAX_PAIRS:
                        omissions["pair_limit"] += 1
                        continue
                    projected_size = _size(sample)
                    if sample_bytes + projected_size > SAMPLE_BYTE_BUDGET:
                        omissions["byte_limit"] += 1
                        continue
                    samples.append(sample)
                    sample_bytes += projected_size
                    pair_keys.add(pair_key)
                    if packet_key is not None:
                        packet_groups.setdefault(packet_key, []).append(sample)
        if not found_radio:
            counts["records_missing_radio"] += 1

    for sample in samples:
        sample["eligible"] = not sample["exclusions"]
    result = {"format": "bps-radio-survey-v1", "guide": GUIDE,
              "limits": {"samples": MAX_SAMPLES, "pairs": MAX_PAIRS, "json_bytes": MAX_BYTES,
              "raw_packet_max_age_s": RAW_MAX_AGE_S,
              "stamp_epoch_cluster_s": STAMP_CLUSTER_S},
              "counts": counts, "omissions": omissions,
              "samples": samples, "pairs": _summaries(samples)}
    # Exact serialized size is authoritative, including the final counts.
    while True:
        result["pairs"] = _summaries(samples)
        counts["samples"] = len(samples)
        counts["eligible_samples"] = sum(row["eligible"] for row in samples)
        counts["excluded_samples"] = len(samples) - counts["eligible_samples"]
        counts["omitted_samples"] = sum(omissions.values())
        counts["unique_packets"] = sum(pair["counts"]["unique_packets"]
                                        for pair in result["pairs"])
        if _size(result) <= MAX_BYTES:
            break
        if not samples:
            raise ValueError("Radio survey metadata exceeds 8 MiB")
        samples.pop()
        omissions["byte_limit"] += 1
    return result
