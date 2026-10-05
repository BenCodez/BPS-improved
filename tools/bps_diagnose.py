#!/usr/bin/env python3
"""Analyse a panel diagnostic export offline, using only the standard library.

Usage: python3 tools/bps_diagnose.py bps-diagnostics.json --out diagnosis.json
Does not contact Home Assistant, send data, or change calibration.
"""
import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import statistics


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def percentile(values, p):
    if not values:
        return None
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    return round(values[lo] + (values[hi] - values[lo]) * (k - lo), 4)


def metrics(values):
    return {"samples": len(values), "median_m": percentile(values, .5), "p95_m": percentile(values, .95)}


def truth_for(bundle, target, at, members):
    """Intervals are explicit and half-open; notes never clear a known position."""
    active = {}
    for mark in bundle.get("annotations", []):
        if mark["time"] > at:
            break
        key = mark.get("target")
        if mark["kind"] == "known_position":
            active[key] = mark
        elif mark["kind"] == "clear_position":
            active.pop(key, None)
    candidates = [mark for key, mark in active.items()
                  if key == target or target in members.get(key, [])]
    return max(candidates, key=lambda m: m["time"]) if candidates else None


def observation_times(position, positions):
    if position.get("group"):
        # With reading_max_age=0 a group's member timestamp is the solve time,
        # not the measurement time. Consult the original fixes as well.
        times = []
        for source in position.get("beacon_positions", []):
            if source.get("used"):
                original = positions.get(source.get("ent"))
                times.extend(observation_times(original, positions) if original and not original.get("group") else [None])
        return times
    if "diagnostic_inputs" in position:
        return [reading.get("observed") for reading in position["diagnostic_inputs"]]
    diagnostic = position.get("outdoor", {})
    times = [d.get("observed") for d in diagnostic.get("receiver_diagnostics", [])
             if d.get("used", True) and not d.get("excluded")]
    return times or [diagnostic.get("observed")]


def analyse(bundle):
    if not isinstance(bundle, dict) or bundle.get("format") != "bps-diagnostics-v1":
        raise ValueError("Expected a bps-diagnostics-v1 panel export")
    frames, contexts = bundle["frames"], bundle["contexts"]
    tracker = defaultdict(lambda: {"frames_with_position": 0, "frames_without_position": 0,
        "unique_fixes": 0, "wrong_floor_fixes": 0, "raw": [], "published": [], "inside_uncertainty": []})
    receiver = defaultdict(lambda: {"status": Counter(), "ignored_frames": 0, "horizontal_bias": [],
        "raw_range": [], "source_diagnostics": Counter()})
    seen_fixes, seen_ranges, seen_diagnostics = set(), set(), set()
    previous_context, context_since = None, bundle["started"]
    for frame in frames:
        now = frame["time"]
        context = contexts[frame["context_id"]]
        if frame["context_id"] != previous_context:
            context_since = now
            previous_context = frame["context_id"]
        members = context["target_members"]
        layout = context["layout"]
        floors = {f["name"]: f for f in layout.get("floor", [])}
        positions = {p["ent"]: p for p in frame.get("positions", [])}
        wanted = set(bundle["targets"]) | {b for v in members.values() for b in v}
        for key in wanted:
            stats = tracker[key]
            position = positions.get(key)
            stats["frames_with_position" if position else "frames_without_position"] += 1
            if not position:
                continue
            # Fusion can change while the same newest member remains unchanged;
            # retain meaningful output changes without counting identical polls.
            fingerprint = (key, position.get("updated"), json.dumps(position.get("cords")),
                           json.dumps(position.get("raw")), position.get("floor"),
                           json.dumps([(p.get("ent"), p.get("updated"), p.get("used"))
                                       for p in position.get("beacon_positions", [])]))
            if fingerprint in seen_fixes:
                continue
            seen_fixes.add(fingerprint)
            stats["unique_fixes"] += 1
            diagnostics = position.get("outdoor", {}).get("receiver_diagnostics", [])
            for d in diagnostics:
                ident = (key, position.get("floor"), d.get("receiver"), d.get("observed"), d.get("classification"))
                if ident not in seen_diagnostics:
                    seen_diagnostics.add(ident)
                    receiver[(key, position.get("floor"), d.get("receiver"))]["source_diagnostics"][d.get("classification", "unknown")] += 1
            truth = truth_for(bundle, key, now, members)
            times = observation_times(position, positions)
            # Never score old/future fixes against a newly marked location, or
            # reproject a cached fix using newly changed geometry/calibration.
            if not truth or not times or any(not number(t) or t < truth["time"] or t > now for t in times):
                continue
            if not number(position.get("updated")) or position["updated"] < context_since:
                continue
            if position.get("floor") != truth["floor"]:
                stats["wrong_floor_fixes"] += 1
                continue
            floor = floors.get(truth["floor"], {})
            scale = floor.get("scale")
            if not number(scale) or scale <= 0:
                continue
            for field, metric in (("cords", "published"), ("raw", "raw")):
                xy = position.get(field)
                if isinstance(xy, list) and len(xy) == 2 and all(number(v) for v in xy):
                    error = math.hypot(xy[0] / scale - truth["x_m"], xy[1] / scale - truth["y_m"])
                    stats[metric].append(error)
                    uncertainty = position.get("outdoor", {}).get("estimated_uncertainty_m")
                    if field == "cords" and number(uncertainty):
                        stats["inside_uncertainty"].append(error <= uncertainty)
        for reading in frame.get("readings", []):
            key = (reading["tracker"], reading["floor"], reading["receiver"])
            stats = receiver[key]
            stats["status"][reading["status"]] += 1
            stats["ignored_frames"] += bool(reading.get("ignored"))
            truth = truth_for(bundle, reading["tracker"], now, members)
            observed = reading.get("observed")
            signature = (*key, observed, reading.get("raw_state"))
            if not truth or reading["status"] != "current" or not number(observed) or not truth["time"] <= observed <= now or signature in seen_ranges:
                continue
            floor = floors.get(reading["floor"], {})
            scale = floor.get("scale")
            if truth["floor"] != reading["floor"] or not number(scale) or scale <= 0:
                continue
            rec = next((r for r in floor.get("receivers", []) if r.get("entity_id") == reading["receiver"]), {})
            xy = rec.get("cords", {})
            if not all(number(xy.get(k)) for k in ("x", "y")) or not number(reading.get("horizontal_distance_m")):
                continue
            expected = math.hypot(xy["x"] / scale - truth["x_m"], xy["y"] / scale - truth["y_m"])
            seen_ranges.add(signature)
            stats["horizontal_bias"].append(reading["horizontal_distance_m"] - expected)
            stats["raw_range"].append(reading["measured_distance_m"])
    trackers = {}
    for key, values in sorted(tracker.items()):
        raw, published, coverage = values.pop("raw"), values.pop("published"), values.pop("inside_uncertainty")
        trackers[key] = {**values, "raw_error": metrics(raw), "published_error": metrics(published),
                        "uncertainty_coverage_fraction": round(statistics.mean(coverage), 4) if coverage else None,
                        "uncertainty_coverage_samples": len(coverage)}
    receivers = []
    for (key, floor, slug), values in sorted(receiver.items(), key=lambda item: tuple(str(v) for v in item[0])):
        bias, raw = values.pop("horizontal_bias"), values.pop("raw_range")
        receivers.append({"tracker": key, "floor": floor, "receiver": slug, **values,
            "known_position_samples": len(bias), "horizontal_bias_median_m": percentile(bias, .5),
            "horizontal_absolute_error_p95_m": percentile([abs(v) for v in bias], .95),
            "raw_range_median_m": percentile(raw, .5)})
    return {"format": "bps-diagnosis-v1", "frames": len(frames), "context_versions": len(contexts),
        "duration_s": round(bundle["ended"] - bundle["started"], 1), "trackers": trackers, "receivers": receivers,
        "notes": ["Raw and published errors require explicit stationary known-position markers; null means no valid samples.",
                  "Status counts measure snapshot availability, not independent observations. Error samples deduplicate unchanged fixes/readings.",
                  "Horizontal bias depends on entered receiver coordinates, scale and assumed collar/mount heights; it does not prove a calibration fault.",
                  "Uncertainty coverage is empirical for this recording, not a statistical confidence level. No calibration has been changed."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--out", type=Path, help="Also save the analysis as JSON")
    args = parser.parse_args()
    try:
        with args.recording.open("rb") as stream:
            data = stream.read(20 * 1024 * 1024 + 1)
        if len(data) > 20 * 1024 * 1024:
            raise ValueError("Recording exceeds 20 MiB")
        report = analyse(json.loads(data))
        output = json.dumps(report, indent=2, allow_nan=False)
        if args.out:
            args.out.write_text(output + "\n", encoding="utf-8")
        print(output)
    except (OSError, ValueError, KeyError, TypeError) as err:
        parser.exit(2, f"Cannot analyse diagnostic recording: {err}\n")


if __name__ == "__main__":
    main()
