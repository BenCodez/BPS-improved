"""Self-test uses physical raw links with detached floor-local geometry."""
import asyncio
import copy
import json
import threading
from collections import deque

import pytest

import bps
from bps import calibration as C
from test_calibration_alias_lifecycle import fixture, seed_history
from test_eval import ev
from test_positioning import SQUARE, _exact_samples, _hass_with


def _sampled(hass, monkeypatch, **options):
    data, macs, devices = fixture(hass, monkeypatch, **options)
    for floor in data["floor"]:
        for rec, correction in zip(floor["receivers"], [.5, 2., 1., 1., .5]):
            rec["correction"] = correction
    hass.data["bps"]["layout"] = data
    cal = C.get_calibration_state(hass)
    cal["mode"] = "auto"
    seed_history(hass, monkeypatch, cal, data, macs)
    return data, cal, macs, devices


@pytest.mark.parametrize("renamed", [False, True])
def test_shared_scanner_uses_canonical_raw_keys_and_each_floors_geometry(hass, monkeypatch, renamed):
    data, cal, _, _ = _sampled(hass, monkeypatch, upstairs=True, renamed=renamed)
    assert cal["_auto_routes"]["Upstairs"]["shared_current" if renamed else "shared_old"] == "shared_old"
    assert all("shared_current" not in pair for pair in cal["samples"])
    original = copy.deepcopy(data)
    result = bps.run_selftest(hass)
    assert result["counts"] == {"placed": 8, "solved": 8, "unsolved": 0}
    assert max(row["error_m"] for row in result["receivers"]) < .01
    for floor in data["floor"]:
        rows = {row["entity"]: row for row in result["receivers"] if row["floor"] == floor["name"]}
        assert len(rows) == 4
        for rec in floor["receivers"]:
            assert rows[rec["entity_id"]]["known"] == [rec["cords"]["x"], rec["cords"]["y"]]
            assert rows[rec["entity_id"]]["heard_by"] == 3
    assert data == original
    # The public endpoint JSON remains consumable by the evaluation CLI and
    # accuracy sensor even when an entity occurs on more than one floor.
    stats = ev.selftest_stats(json.loads(json.dumps(result)))
    assert stats["placed"] == stats["solved"] == 8
    assert {name: row["solved"] for name, row in stats["per_floor"].items()} == {"Ground": 4, "Upstairs": 4}
    state, attrs = bps._selftest_summary(result)
    assert state < .01 and attrs["placed"] == attrs["solved"] == 8


@pytest.mark.parametrize("alias_first", [False, True])
def test_equivalent_aliases_contribute_one_anchor_and_one_target(hass, monkeypatch, alias_first):
    data, cal, _, _ = _sampled(hass, monkeypatch, duplicate=True)
    if alias_first:
        # Change which equivalent placement is retained after collecting the
        # raw window. Its physical proof must still find the old canonical key.
        data["floor"][0]["receivers"].insert(0, data["floor"][0]["receivers"].pop())
    result = bps.run_selftest(hass)
    assert result["counts"] == {"placed": 4, "solved": 4, "unsolved": 0}
    assert {r["entity"] for r in result["receivers"]} == {
        "shared_current" if alias_first else "shared_old", "g1", "g2", "g3"}
    assert all(r["heard_by"] == 3 and r["error_m"] < .01 for r in result["receivers"])
    assert "shared_old" in cal["receivers"]


@pytest.mark.parametrize("changed", ["cords", "correction", "height"])
def test_conflicting_alias_placements_are_excluded_from_targets_and_anchors(hass, monkeypatch, changed):
    data, _, _, _ = _sampled(hass, monkeypatch, duplicate=True)
    duplicate = data["floor"][0]["receivers"][-1]
    if changed == "cords":
        duplicate["cords"]["x"] += 10
    else:
        duplicate[changed] += .2
    result = bps.run_selftest(hass)
    assert result["counts"] == {"placed": 3, "solved": 0, "unsolved": 3}
    assert {r["entity"] for r in result["unsolved"]} == {"g1", "g2", "g3"}
    assert all(r["heard_by"] == 2 for r in result["unsolved"])


@pytest.mark.parametrize("replacement", ["mac", "owner"])
def test_raw_window_does_not_follow_replacement_hardware(hass, monkeypatch, replacement):
    data, _, _, _ = _sampled(hass, monkeypatch)
    entries = bps.er.async_get(hass).entities
    for slug in ("shared_old", "shared_current"):
        entry = entries[f"sensor.tag_distance_to_{slug}"]
        if replacement == "mac":
            entry.unique_id = "synthetic-tag_dd:ee:ff:10:20:00_range"
        else:
            entry.config_entry_id = "replacement-owner"
    result = bps.run_selftest(hass)
    assert result["counts"] == {"placed": 4, "solved": 0, "unsolved": 4}
    heard = {row["entity"]: row["heard_by"] for row in result["unsolved"]}
    assert heard == {"shared_old": 0, "g1": 2, "g2": 2, "g3": 2}


@pytest.mark.parametrize("upstairs", [False, True])
@pytest.mark.parametrize("replacement", ["unchanged", "mac", "owner", "legacy"])
def test_restored_off_mode_selftest_uses_observation_identity_and_routes(hass, monkeypatch, upstairs, replacement):
    data, cal, _, devices = _sampled(hass, monkeypatch, upstairs=upstairs, renamed=upstairs)
    expected = bps.run_selftest(hass)

    async def restore():
        await C.save_calibration_state(hass)
        if replacement == "legacy":
            hass._store_backing["bps_calibration_state"].pop("sample_geometry")
        del hass.data["bps"]["calibration"]
        await C.async_restore_calibration_state(hass)

    asyncio.run(restore())
    restored = C.get_calibration_state(hass)
    assert restored["mode"] == "off" and not restored["receivers"]
    if replacement in ("mac", "owner"):
        entries = bps.er.async_get(hass).entities
        for slug in ("shared_old", "shared_current"):
            entry = entries[f"sensor.tag_distance_to_{slug}"]
            if replacement == "mac":
                entry.unique_id = "synthetic-tag_dd:ee:ff:10:20:00_range"
            else:
                entry.config_entry_id = "replacement-owner"
        if replacement == "mac":
            devices["shared_old"].address = "dd:ee:ff:10:20:00"
    before = copy.deepcopy(restored["_auto_geometry"])
    result = bps.run_selftest(hass)
    assert restored["_auto_geometry"] == before and restored["mode"] == "off"
    if replacement == "unchanged":
        assert result == expected
    else:
        assert result["counts"]["solved"] == 0
        assert result["counts"]["unsolved"] == result["counts"]["placed"]
        for row in result["unsolved"]:
            assert row["heard_by"] == (0 if replacement == "legacy" or row["entity"] in ("shared_old", "shared_current") else 2)


def test_legacy_exact_samples_and_explicit_samples_override_remain_supported():
    hass = _hass_with(SQUARE, {})
    samples = _exact_samples(SQUARE)
    assert "_auto_routes" not in hass.data["bps"]["calibration"]
    result = bps.run_selftest(hass, samples)
    assert result["counts"] == {"placed": 4, "solved": 4, "unsolved": 0}
    assert all(row["error_m"] < .01 for row in result["receivers"])
    assert hass.data["bps"]["calibration"]["samples"] == {}


def test_api_snapshots_registry_geometry_routes_and_raw_deques_before_executor(hass, monkeypatch):
    data, cal, _, _ = _sampled(hass, monkeypatch, upstairs=True, renamed=True)
    expected = bps.run_selftest(hass)
    loop_thread = threading.get_ident()
    registry = bps.er.async_get(hass)
    def loop_registry(_hass):
        assert threading.get_ident() == loop_thread
        return registry
    monkeypatch.setattr(bps.er, "async_get", loop_registry)
    entered, release = threading.Event(), threading.Event()
    solve = bps.trilaterate
    def paused_solve(*args, **kwargs):
        assert threading.get_ident() != loop_thread
        entered.set()
        assert release.wait(timeout=5)
        return solve(*args, **kwargs)
    monkeypatch.setattr(bps, "trilaterate", paused_solve)
    async def executor(function, *args):
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            # Panel edits, new adverts and registry changes land while the
            # worker is paused in its first solve, before it reads later links.
            for floor in data["floor"]:
                floor["scale"] *= 3
                for rec in floor["receivers"]:
                    rec["cords"]["x"] += 1000
                    rec["correction"] *= 2
            for values in cal["samples"].values():
                assert isinstance(values, deque)
                values.clear()
                values.extend([999.] * 3)
            cal["samples"]["new|pair"] = deque([123.])
            cal["_auto_routes"].clear()
            cal["receivers"].clear()
            registry.entities.clear()
        finally:
            release.set()
        return await worker
    hass.async_add_executor_job = executor
    result = asyncio.run(bps.BPSSelfTestAPI(hass).get(None)).json_body
    assert result == expected


def test_periodic_sensor_hands_detached_snapshot_to_executor(hass, monkeypatch):
    _sampled(hass, monkeypatch, upstairs=True, renamed=True)
    expected = bps._selftest_summary(bps.run_selftest(hass))
    monkeypatch.setattr(bps.update_tracked_entities, "last_selftest", 0., raising=False)
    monkeypatch.setattr(bps.update_tracked_entities, "last_liveness", 10000., raising=False)
    monkeypatch.setattr(bps.update_tracked_entities, "last_history_flush", 10000., raising=False)
    monkeypatch.setattr(bps, "_bermuda_distance_sensor_ids", lambda _hass: [])
    async def no_prune(_hass):
        pass
    monkeypatch.setattr(bps, "prune_stale_positions", no_prune)
    monkeypatch.setattr(bps, "update_tracker_groups", no_prune)
    published = []
    monkeypatch.setattr(bps, "update_bps_sensor_state", lambda *args: published.append(args[1:]))
    async def executor(function, *args):
        hass.data["bps"]["layout"] = {}
        hass.data["bps"]["calibration"] = {}
        return await asyncio.to_thread(function, *args)
    hass.async_add_executor_job = executor
    async def stop(_delay):
        raise asyncio.CancelledError
    monkeypatch.setattr(bps.asyncio, "sleep", stop)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bps.update_tracked_entities(hass))
    assert published == [(bps.ACCURACY_ENTITY_ID, *expected)]
