"""Conservative auto updates evaluated on independent receiver observations.

Probe transmit power is a nuisance variable, so validation removes one common
log-distance offset per transmitter. This measures relative receiver accuracy,
not collar position accuracy. No mutable HA state enters this worker.
"""
import math

import numpy as np

WINDOW_S = 1800
FRESH_S = 90
MIN_OBSERVATIONS = 10
MIN_SPAN_S = 240
MIN_LINKS = 3
MAX_DISAGREEMENT = .15
MAX_STEP = .10
MIN_FACTOR = .2
MAX_FACTOR = 5.


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _supported_pairs(snapshot, history, now, true_distance):
    pairs = {}
    usable_nodes = set()
    for key, observations in history.items():
        tx, rx = key.split("|", 1)
        if tx not in snapshot["receivers"] or rx not in snapshot["receivers"]:
            continue
        distance = true_distance(snapshot, tx, rx)
        if not finite(distance) or distance < .3:
            continue
        rows = [(t, d) for t, d in observations
                if finite(t) and finite(d) and d > 0 and now - WINDOW_S <= t <= now]
        if len(rows) >= 5 and now - rows[-1][0] <= FRESH_S:
            usable_nodes.update((tx, rx))
        if (len(rows) >= MIN_OBSERVATIONS and now - rows[-1][0] <= FRESH_S
                and rows[-1][0] - rows[0][0] >= MIN_SPAN_S):
            pairs[key] = rows
    # Require three independently transmitting neighbours in BOTH directions.
    # Iterative pruning prevents poorly supported nodes influencing a good fit.
    nodes = set(snapshot["receivers"])
    while True:
        neighbours = {s: {r for r in nodes if r != s and f"{s}|{r}" in pairs
                           and f"{r}|{s}" in pairs} for s in nodes}
        supported = {s for s in nodes if len(neighbours[s]) >= MIN_LINKS}
        if supported == nodes:
            break
        nodes = supported
    if not nodes:
        return {}, [], usable_nodes
    # Never normalize unrelated components against each other.
    components, remaining = [], set(nodes)
    while remaining:
        pending, component = [min(remaining)], set()
        while pending:
            node = pending.pop()
            if node in component:
                continue
            component.add(node)
            pending.extend(neighbours[node] - component)
        remaining -= component
        components.append(sorted(component))
    selected = sorted(components, key=lambda c: (-len(c), c))[0]
    selected_set = set(selected)
    return {k: v for k, v in pairs.items()
            if all(s in selected_set for s in k.split("|", 1))}, selected, usable_nodes


def _score(matrix, observations, factors, true_distance):
    """Equal weight per probe; strip TX power without stripping RX differences."""
    by_tx = {}
    for row in matrix:
        tx, rx = row["tx"], row["rx"]
        measured = float(np.median(observations["samples"][f"{tx}|{rx}"]))
        residual = math.log10(measured) + math.log10(factors[rx]) - math.log10(true_distance(observations, tx, rx))
        by_tx.setdefault(row["tx"], []).append(residual)
    errors = {}
    all_errors = []
    for tx, values in by_tx.items():
        centre = float(np.median(values))
        absolute = [abs(v - centre) for v in values]
        errors[tx] = float(np.mean(absolute))
        all_errors.extend(absolute)
    return {"mean_log_error": float(np.mean(list(errors.values()))),
            "p90_log_error": float(np.percentile(all_errors, 90)), "by_transmitter": errors}


def assess_update(snapshot, history, current, floor_name, now, solve, true_distance):
    """Return (preview result, incremental factors, visible decision)."""
    decision = {"state": "skipped", "reason": "Collecting fresh, independent receiver observations",
                "checked_at": now, "updated_receivers": 0}
    pairs, nodes, usable_nodes = _supported_pairs(snapshot, history, now, true_distance)
    if len(nodes) < 4:
        return None, {}, decision
    # Fit only supported links, but retain all placements and identity matches
    # so the preview can distinguish offline/unmatched receivers from nodes
    # with real samples that simply failed the stronger automatic gates.
    private = {"receivers": snapshot["receivers"], "samples": {},
               "matched_placed": snapshot.get("matched_placed", {})}
    # The design matrix must have only the usual shared RX/TX gauge. Even a
    # dense bipartite network can contain an extra unidentifiable offset.
    index = {s: i for i, s in enumerate(nodes)}
    design = np.zeros((len(pairs), 2 * len(nodes)))
    for i, key in enumerate(pairs):
        tx, rx = key.split("|", 1)
        design[i, index[rx]] = design[i, len(nodes) + index[tx]] = 1
    if np.linalg.matrix_rank(design) < 2 * len(nodes) - 1:
        decision["reason"] = "Receiver network cannot distinguish receive and transmit biases yet"
        return None, {}, decision
    validation = {"receivers": private["receivers"], "samples": {}}
    for key, rows in pairs.items():
        split = len(rows) // 2
        private["samples"][key] = [d for _, d in rows[:split]]
        validation["samples"][key] = [d for _, d in rows[split:]]
    try:
        result, check = solve(private, floor_name), solve(validation, floor_name)
    except ValueError as error:
        decision["reason"] = str(error)
        return None, {}, decision
    proposed = result["receivers"]
    excluded = sorted(usable_nodes - set(proposed))
    result["auto_excluded_receivers"] = excluded
    for key in ("missing_unmatched", "missing_no_data"):
        result[key] = [s for s in result[key] if s not in excluded]
    decision["supported_receivers"] = len(proposed)
    decision["unchanged_receivers"] = sorted(set(current) - set(proposed))
    if set(proposed) != set(check["receivers"]) or len(proposed) != len(nodes):
        decision["reason"] = "Too few usable independent links for every receiver"
        return result, {}, decision
    if any(not finite(v) or v <= MIN_FACTOR or v >= MAX_FACTOR
           for factors in (proposed, check["receivers"]) for v in factors.values()):
        decision["reason"] = "Candidate reaches a correction limit; keeping current corrections"
        return result, {}, decision
    if any(abs(math.log(check["receivers"][s] / v)) > math.log1p(MAX_DISAGREEMENT)
           for s, v in proposed.items()):
        decision["reason"] = "Separate sampling periods disagree; keeping current corrections"
        return result, {}, decision
    baseline = {s: current.get(s, 1.) for s in proposed}
    if any(not finite(v) or not MIN_FACTOR <= v <= MAX_FACTOR for v in baseline.values()):
        decision["reason"] = "Existing correction is outside the supported range; review it manually"
        return result, {}, decision
    # Anchor the subset to its EXISTING geometric mean. Never undo a manual
    # fleet-scale adjustment or move unsupported receivers to make the mean 1.
    shifts = {s: math.log(proposed[s] / baseline[s]) for s in proposed}
    mean_shift = sum(shifts.values()) / len(shifts)
    shifts = {s: d - mean_shift for s, d in shifts.items()}
    largest = max(abs(d) for d in shifts.values())
    if largest < math.log1p(.01):
        decision.update(state="unchanged", reason="Current corrections already match the stable estimate")
        return result, {}, decision
    step = min(1., math.log1p(MAX_STEP) / largest)
    for s, d in shifts.items():
        if d > 0:
            step = min(step, math.log(MAX_FACTOR / baseline[s]) / d)
        elif d < 0:
            step = min(step, math.log(MIN_FACTOR / baseline[s]) / d)
    candidate = {s: round(baseline[s] * math.exp(d * step), 6) for s, d in shifts.items()}
    before = _score(check["matrix"], validation, baseline, true_distance)
    after = _score(check["matrix"], validation, candidate, true_distance)
    decision.update(reference_before=before, reference_after=after,
                    max_change_pct=round(max(abs(candidate[s] / baseline[s] - 1) for s in candidate) * 100, 2))
    # Validate the ACTUAL bounded update on the held-out half, never the
    # in-sample RX+TX fit. Also prevent an improvement hiding a worse probe.
    if (before["mean_log_error"] - after["mean_log_error"] < max(.002, .02 * before["mean_log_error"])
            or after["p90_log_error"] > before["p90_log_error"] + .002
            or any(after["by_transmitter"][s] > before["by_transmitter"][s] + .02
                   for s in before["by_transmitter"])):
        decision["reason"] = "No reliable improvement on held-out receiver observations"
        return result, {}, decision
    decision.update(state="applied", updated_receivers=len(candidate),
                    reason="Stable estimate improves held-out receiver observations; applied a gradual update")
    return result, candidate, decision
