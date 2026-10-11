"""Automatic calibration must earn a bounded update on independent data."""
import asyncio
import copy
import json
import math
from collections import deque
from types import SimpleNamespace

import pytest

from bps import calibration as C
from bps.calibration_auto import assess_update
from bps.storage import get_bps_data_for_edit, save_bps_data

NOW = 10000.
FACTORS = {"r0": .5, "r1": 2., "r2": 1., "r3": 1.}


def fixture_data(factors=FACTORS, later=None):
    floor = {"name": "Yard", "scale": 10., "receivers": [
        {"entity_id": f"r{i}", "cords": {"x": x, "y": y}}
        for i, (x, y) in enumerate(((0, 0), (200, 0), (0, 200), (200, 200)))]}
    snapshot = {"receivers": C._build_receiver_map({"floor": [floor]})}
    history = {}
    for tx in FACTORS:
        for rx in FACTORS:
            if rx == tx:
                continue
            distance = C._true_distance_m(snapshot, tx, rx)
            history[f"{tx}|{rx}"] = [(NOW - (19 - i) * 30,
                distance / (later if later is not None and i >= 10 else factors)[rx]) for i in range(20)]
    return floor, snapshot, history


def assess(snapshot, history, current=None):
    return assess_update(snapshot, history, current or {s: 1. for s in snapshot["receivers"]},
                         "Yard", NOW, C.solve, C._true_distance_m)


def test_stable_real_fit_earns_gradual_update_on_held_out_samples():
    _floor, snapshot, history = fixture_data()
    before = copy.deepcopy((snapshot, history))
    result, updates, status = assess(snapshot, history)
    assert status["state"] == "applied"
    assert result["receivers"] == FACTORS
    assert .90 <= updates["r0"] < 1. < updates["r1"] <= 1.10
    assert math.prod(updates.values()) == pytest.approx(1., abs=1e-6)
    assert status["reference_after"]["mean_log_error"] < status["reference_before"]["mean_log_error"]
    assert (snapshot, history) == before


def test_ble_jumps_do_not_turn_into_large_correction_updates():
    _floor, snapshot, history = fixture_data()
    noise = (1.03, .97, 1.01, .99, 1., 8., .98, 1.02, 1., 1.)
    for key, rows in history.items():
        history[key] = [(t, d * noise[i % 10]) for i, (t, d) in enumerate(rows)]
    _result, updates, status = assess(snapshot, history)
    assert status['state'] == 'applied'
    assert max(abs(factor - 1) for factor in updates.values()) <= .100001
    assert updates['r0'] < 1 < updates['r1']


def test_current_layout_scale_and_unsupported_receiver_are_preserved():
    _floor, snapshot, history = fixture_data()
    snapshot["receivers"]["unsupported"] = dict(snapshot["receivers"]["r0"], x=600)
    current = {s: 1.5 for s in snapshot["receivers"]}
    current["unsupported"] = .4
    _result, updates, status = assess(snapshot, history, current)
    assert status["state"] == "applied"
    assert "unsupported" not in updates
    assert status["unchanged_receivers"] == ["unsupported"]
    assert math.prod(updates.values()) == pytest.approx(1.5 ** 4, abs=1e-5)
    assert max(abs(updates[s] / current[s] - 1) for s in updates) <= .100001


@pytest.mark.parametrize("issue", ["stale", "old", "count", "span", "sparse", "nonfinite"])
def test_weak_evidence_never_replaces_existing_corrections(issue):
    _floor, snapshot, history = fixture_data()
    if issue == "sparse":
        history = {k: v for k, v in history.items() if "r3" not in k}
    else:
        for key, rows in history.items():
            if issue == "stale": rows = [(t - 100, d) for t, d in rows]
            elif issue == "old": rows = [(t - 2000, d) for t, d in rows]
            elif issue == "count": rows = rows[-9:]
            elif issue == "span": rows = [(NOW - i, d) for i, (_, d) in enumerate(rows)]
            else: rows = [(t, math.nan) for t, _ in rows]
            history[key] = rows
    _result, updates, status = assess(snapshot, history)
    assert not updates and status["state"] == "skipped"


def test_disagreement_and_correction_limits_require_manual_review():
    _floor, snapshot, history = fixture_data(later={"r0": 2., "r1": .5, "r2": 1., "r3": 1.})
    _result, updates, status = assess(snapshot, history)
    assert not updates and "disagree" in status["reason"]
    _floor, snapshot, history = fixture_data({"r0": .05, "r1": 20., "r2": 1., "r3": 1.})
    _result, updates, status = assess(snapshot, history)
    assert not updates and "limit" in status["reason"]


def test_stable_candidate_is_rejected_when_actual_update_worsens_validation():
    later = {"r0": .55, "r1": 1 / .55, "r2": 1., "r3": 1.}
    _floor, snapshot, history = fixture_data(later=later)
    _result, updates, status = assess(snapshot, history, later)
    assert not updates and "held-out" in status["reason"]
    assert status["reference_after"]["mean_log_error"] > status["reference_before"]["mean_log_error"]


def test_already_good_corrections_are_not_rewritten():
    _floor, snapshot, history = fixture_data()
    _result, updates, status = assess(snapshot, history, FACTORS)
    assert not updates and status["state"] == "unchanged"


def test_dense_bipartite_network_has_no_identifiable_auto_correction():
    _floor, snapshot, _history = fixture_data()
    for i in (4, 5):
        snapshot["receivers"][f"r{i}"] = dict(snapshot["receivers"]["r0"], x=300, y=i * 100)
    history = {f"r{a}|r{b}": [(NOW - (19 - i) * 30, 10.) for i in range(20)]
               for a in range(6) for b in range(6) if (a < 3) != (b < 3)}
    _result, updates, status = assess(snapshot, history)
    assert not updates and "distinguish" in status["reason"]


def dump(stamp):
    return {f"m{i}": {"_is_scanner": True, "address": f"m{i}", "name": f"r{i}",
        "adverts": {"first": {"scanner_address": f"m{1-i}", "stamp": stamp,
                                "rssi_distance_raw": 4.},
                    "second": {"scanner_address": f"m{1-i}", "stamp": stamp + .1,
                                 "rssi_distance_raw": 5.}}} for i in range(2)}


def test_cached_adverts_count_once_and_geometry_or_clock_reset_restarts_evidence(hass, monkeypatch):
    _floor, snapshot, _history = fixture_data()
    cal = C.get_calibration_state(hass)
    cal.update(mode="auto", receivers=snapshot["receivers"])
    monkeypatch.setattr(C.time, "time", lambda: NOW)
    for _ in range(20):
        C._ingest_dump(cal, dump(100))
    assert list(cal["samples"]["r0|r1"]) == [5.]
    assert list(cal["_auto_samples"]["r0|r1"]) == [(NOW, 5.)]
    C._ingest_dump(cal, dump(101))
    assert len(cal["_auto_samples"]["r0|r1"]) == 2
    cal["receivers"]["r0"]["height"] = 2.
    C._ingest_dump(cal, dump(102))
    assert len(cal["_auto_samples"]["r0|r1"]) == 1
    assert list(cal["samples"]["r0|r1"]) == [5.]
    C._ingest_dump(cal, dump(1))  # Bermuda's monotonic clock restarted
    assert len(cal["_auto_samples"]["r0|r1"]) == 1


@pytest.mark.parametrize("failure", [False, True])
def test_auto_apply_uses_live_layout_and_publishes_only_after_save(hass, monkeypatch, failure):
    async def scenario():
        floor, snapshot, history = fixture_data()
        await save_bps_data(hass, {"floor": [floor]})
        cal = C.get_calibration_state(hass)
        cal.update(mode="auto", receivers=snapshot["receivers"], applied={"Yard": FACTORS})
        C._refresh_auto_history(cal)
        cal["_auto_samples"] = {k: deque(v) for k, v in history.items()}
        old_layout = get_bps_data_for_edit(hass)
        old_applied = copy.deepcopy(cal["applied"])
        if failure:
            async def fail_save(*_args):
                raise OSError("disk full")
            monkeypatch.setattr(C, "save_bps_data", fail_save)
            with pytest.raises(OSError, match="disk full"):
                await C._auto_solve_and_apply_locked(hass, cal)
            assert get_bps_data_for_edit(hass) == old_layout
            assert cal["applied"] == old_applied
            assert cal["auto_status"]["Yard"]["state"] == "skipped"
        else:
            await C._auto_solve_and_apply_locked(hass, cal)
            saved = get_bps_data_for_edit(hass)["floor"][0]
            values = {r["entity_id"]: r["correction"] for r in saved["receivers"]}
            assert values == cal["applied"]["Yard"]
            assert values != FACTORS  # actual layout's baseline was 1, not stale metadata
            assert saved["calibration"]["auto_decision"]["state"] == "applied"
            assert C._status_payload(cal)["auto_status"]["Yard"]["max_change_pct"] <= 10.01

    monkeypatch.setattr(C.time, "time", lambda: NOW)
    asyncio.run(scenario())


def test_restored_legacy_samples_cannot_trigger_an_automatic_update(hass, monkeypatch):
    async def scenario():
        floor, snapshot, history = fixture_data()
        await save_bps_data(hass, {"floor": [floor]})
        cal = C.get_calibration_state(hass)
        cal["samples"] = {k: deque(d for _, d in v) for k, v in history.items()}
        before = get_bps_data_for_edit(hass)
        await C._auto_solve_and_apply_locked(hass, cal)
        assert get_bps_data_for_edit(hass) == before
        assert cal["auto_status"]["Yard"]["state"] == "skipped"

    monkeypatch.setattr(C.time, "time", lambda: NOW)
    asyncio.run(scenario())


@pytest.mark.parametrize("beacon_already_accurate", [False, True])
def test_auto_uses_measured_beacon_positions_to_veto_regressions(hass, monkeypatch, beacon_already_accurate):
    async def scenario():
        floor, snapshot, history = fixture_data()
        layout = {"floor": [floor]}
        await save_bps_data(hass, layout)
        cal = C.get_calibration_state(hass)
        cal.update(mode="auto", receivers=snapshot["receivers"])
        C._refresh_auto_history(cal)
        cal["_auto_samples"] = {k: deque(v) for k, v in history.items()}
        bundle = {"format": "bps-diagnostics-v1", "started": 0., "targets": ["dog"],
                  "contexts": {"c": {"layout": layout}}, "annotations": [], "frames": []}
        for i in range(1, 21):
            x, y = (6., 7.) if i <= 10 else (14., 12.)
            if i in (1, 11):
                bundle["annotations"].append({"time": float(i), "kind": "known_position",
                    "target": "dog", "floor": "Yard", "x_m": x, "y_m": y})
            rows = []
            for r in floor["receivers"]:
                distance = math.dist((x, y), (r["cords"]["x"] / 10, r["cords"]["y"] / 10))
                rows.append({"tracker": "dog", "receiver": r["entity_id"], "floor": "Yard",
                    "status": "current", "measured_distance_m": distance if beacon_already_accurate
                        else distance / FACTORS[r["entity_id"]], "observed": float(i),
                    # Deliberately different historical corrections: a guard
                    # comparing against these would approve a bad live update.
                    "receiver_correction": FACTORS[r["entity_id"]], "tracker_height_m": 0.})
            bundle["frames"].append({"time": float(i), "context_id": "c", "readings": rows})
        async def export():
            return json.dumps(bundle).encode()
        hass.data["bps"]["_diagnostics"] = SimpleNamespace(frames=[1], export=export)
        before = get_bps_data_for_edit(hass)
        await C._auto_solve_and_apply(hass, cal)
        decision = cal["auto_status"]["Yard"]
        if beacon_already_accurate:
            assert decision["state"] == "skipped"
            assert decision["tracking_validation"]["verdict"] == "worse"
            assert decision["tracking_validation"]["baseline"]["median_m"] < .01
            assert get_bps_data_for_edit(hass) == before
        else:
            assert decision["state"] == "applied"
            assert decision["tracking_validation"]["verdict"] != "worse"
            assert get_bps_data_for_edit(hass) != before

    monkeypatch.setattr(C.time, "time", lambda: NOW)
    asyncio.run(scenario())


def test_auto_session_restart_keeps_corrections_but_requires_new_evidence(hass, monkeypatch):
    async def scenario():
        floor, snapshot, history = fixture_data()
        for receiver in floor["receivers"]:
            receiver["correction"] = FACTORS[receiver["entity_id"]]
        await save_bps_data(hass, {"floor": [floor], "auto_calibration": True})
        cal = C.get_calibration_state(hass)
        cal["samples"] = {k: deque(d for _, d in v) for k, v in history.items()}
        cal["applied"] = {"Yard": FACTORS}
        cal["results"] = {"Yard": C.solve({**snapshot, "samples": cal["samples"]}, "Yard")}
        await C.save_calibration_state(hass)
        del hass.data["bps"]["calibration"]
        await C.async_restore_calibration_state(hass)
        restored = C.get_calibration_state(hass)
        hass.async_create_task = asyncio.create_task
        before = get_bps_data_for_edit(hass)
        await C.async_start_auto_if_enabled(hass)
        try:
            assert restored["mode"] == "auto" and not restored["task"].done()
            assert restored["results"]["Yard"]["receivers"] == FACTORS
            assert restored["_auto_samples"] == {}
            await C._auto_solve_and_apply_locked(hass, restored)
            assert restored["auto_status"]["Yard"]["state"] == "skipped"
            assert get_bps_data_for_edit(hass) == before
        finally:
            await C.async_shutdown_calibration(hass)

    monkeypatch.setattr(C.time, "time", lambda: NOW)
    asyncio.run(scenario())


def test_auto_executor_owns_inputs_while_live_samples_change(hass, monkeypatch):
    async def scenario():
        floor, snapshot, history = fixture_data()
        await save_bps_data(hass, {"floor": [floor]})
        cal = C.get_calibration_state(hass)
        cal.update(mode="auto", receivers=snapshot["receivers"])
        C._refresh_auto_history(cal)
        cal["_auto_samples"] = {k: deque(v) for k, v in history.items()}
        started, release = asyncio.Event(), asyncio.Event()
        async def executor(func, *args):
            before = copy.deepcopy(args[:3])
            started.set()
            await release.wait()
            assert args[:3] == before
            return func(*args)
        hass.async_add_executor_job = executor
        pending = asyncio.create_task(C._auto_solve_and_apply_locked(hass, cal))
        await started.wait()
        cal["_auto_samples"]["r0|r1"].append((NOW, 999.))
        cal["_auto_samples"]["new|pair"] = deque([(NOW, 42.)])
        release.set()
        await pending
        assert cal["auto_status"]["Yard"]["state"] == "applied"

    monkeypatch.setattr(C.time, "time", lambda: NOW)
    asyncio.run(asyncio.wait_for(scenario(), 5))
