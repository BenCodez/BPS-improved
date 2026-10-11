"""Synthetic physical alias calibration applies and shared multi-floor inputs."""
import asyncio
import copy
import json
from collections import deque
from types import SimpleNamespace as NS

import pytest

import bps
from bps import calibration as C
from bps.receiver_sources import ReceiverSources
from bps.storage import get_bps_data_for_edit, save_bps_data
from test_receiver_matching import configure

NOW = 10000.


def fixture(hass, monkeypatch, *, upstairs=False, renamed=False, duplicate=False):
    entries, states, devices, add = configure(hass, monkeypatch)
    monkeypatch.setattr(C.time, "time", lambda: NOW)
    data = {"floor": []}
    macs = {}
    for index, name in enumerate(["shared_old", "g1", "g2", "g3", "u1", "u2", "u3"]):
        mac = f"aa:bb:cc:10:20:{index:02x}"; macs[name] = mac
        add(f"sensor.tag_distance_to_{name}", "4", uid="synthetic-tag_" + mac + "_range")
        devices[name] = NS(_is_scanner=True, address=mac, name=name, adverts={})
    add("sensor.tag_distance_to_shared_current", "4", uid="synthetic-tag_" + macs["shared_old"] + "_range")
    devices["shared_old"].name = "shared_current"
    macs["shared_current"] = macs["shared_old"]
    for floor_index, floor_name in enumerate(["Ground", "Upstairs"] if upstairs else ["Ground"]):
        slugs = (["shared_current" if renamed else "shared_old", "u1", "u2", "u3"]
                 if floor_index else ["shared_old", "g1", "g2", "g3"])
        floor = {"name": floor_name, "scale": 10 if not floor_index else 20, "receivers": []}
        for index, (slug, (x, y)) in enumerate(zip(slugs, [(0, 0), (200, 0), (0, 200), (200, 200)])):
            floor["receivers"].append({"entity_id": slug, "cords": {"x": x + floor_index * 70, "y": y + floor_index * 30},
                "height": .5 + index * .3 + floor_index, "correction": 1., "outdoor_policy": "auto", "custom": {"keep": True}})
        data["floor"].append(floor)
    if duplicate:
        clone = copy.deepcopy(data["floor"][0]["receivers"][0]); clone["entity_id"] = "shared_current"
        data["floor"][0]["receivers"].append(clone)
    return data, macs, devices


def sampled_dump(hass, cal, data, macs, stamp):
    payload = {mac: {"_is_scanner": True, "address": mac, "name": slug, "adverts": {}}
               for slug, mac in macs.items()}
    for floor in data["floor"]:
        geometry = C._build_receiver_map({"floor": [floor]}, hass=hass)
        factors = {slug: factor for slug, factor in zip(geometry, [.5, 2., 1., 1.])}
        for tx in geometry:
            for rx in geometry:
                if tx == rx:
                    continue
                payload[macs[tx]]["adverts"][macs[rx]] = {"scanner_address": macs[rx], "stamp": stamp,
                    "rssi_distance_raw": C._true_distance_m({"receivers": geometry}, tx, rx) / factors[rx],
                    "conf_attenuation": 2., "conf_rssi_offset": 0., "conf_ref_power": -60., "ref_power": -60.}
    C._ingest_dump(cal, payload)
    return payload


def seed_history(hass, monkeypatch, cal, data, macs):
    C._set_auto_receivers(hass, cal, data)
    for index in range(20):
        monkeypatch.setattr(C.time, "time", lambda i=index: NOW - (19 - i) * 30)
        payload = sampled_dump(hass, cal, data, macs, 100 + index)
        C._ingest_dump(cal, payload)  # A cached response never doubles evidence.
    monkeypatch.setattr(C.time, "time", lambda: NOW)
    return payload


@pytest.mark.parametrize("auto", [False, True])
def test_manual_and_auto_apply_keep_equivalent_alias_corrections_equal_and_tracking_usable(hass, monkeypatch, auto):
    async def scenario():
        data, macs, devices = fixture(hass, monkeypatch, duplicate=True)
        await save_bps_data(hass, data)
        cal = C.get_calibration_state(hass); cal["mode"] = "auto" if auto else "manual"
        seed_history(hass, monkeypatch, cal, data, macs)
        if auto:
            await C._auto_solve_and_apply_locked(hass, cal)
            assert cal["auto_status"]["Ground"]["state"] == "applied"
        else:
            cal["results"]["Ground"] = C.solve(C.solve_snapshot(cal), "Ground")
            assert await C.apply_corrections(hass, cal, "Ground") == 5
        saved = get_bps_data_for_edit(hass)
        receivers = saved["floor"][0]["receivers"]
        assert receivers[0]["correction"] != 1.
        assert receivers[0]["correction"] == receivers[-1]["correction"]
        assert cal["applied"]["Ground"]["shared_current"] == receivers[0]["correction"]
        assert [r["cords"] for r in receivers] == [r["cords"] for r in data["floor"][0]["receivers"]]
        assert all(r["custom"] == {"keep": True} for r in receivers)
        await bps.update_receiver_radii(hass, {"entity": "tag", "data": saved})
        assert sum("distance" in r for r in saved["floor"][0]["receivers"]) == 4
        assert ReceiverSources(hass).floor_bindings(saved["floor"][0])[-1]["status"] == "duplicate_scanner_alias"
    asyncio.run(scenario())


@pytest.mark.parametrize("conflict", ["cords", "correction", "outdoor_policy", "owner"])
def test_manual_apply_never_repairs_preexisting_conflicting_alias_properties(hass, monkeypatch, conflict):
    async def scenario():
        data, macs, devices = fixture(hass, monkeypatch, duplicate=True)
        alias = data["floor"][0]["receivers"][-1]
        if conflict == "cords": alias["cords"]["x"] += 100
        elif conflict == "owner": alias.update(scanner_mac=macs["shared_old"], scanner_config_entry_id="different-owner")
        else: alias[conflict] = 1.5 if conflict == "correction" else "ignore"
        await save_bps_data(hass, data)
        cal = C.get_calibration_state(hass)
        cal["results"]["Ground"] = {"floor": "Ground", "receivers": {"shared_old": .8},
            "pairs_used": 6, "error_factor_before": 2., "error_factor_after": 1.}
        before = get_bps_data_for_edit(hass)
        if conflict == "owner":
            assert await C.apply_corrections(hass, cal, "Ground") == 1
            saved = get_bps_data_for_edit(hass)
            assert saved["floor"][0]["receivers"][-1] == before["floor"][0]["receivers"][-1]
        else:
            with pytest.raises(ValueError, match="No eligible receiver"):
                await C.apply_corrections(hass, cal, "Ground")
            assert get_bps_data_for_edit(hass) == before and not cal["applied"]
    asyncio.run(scenario())


@pytest.mark.parametrize("renamed", [False, True])
def test_auto_calibrates_each_four_receiver_floor_with_shared_physical_scanner_and_own_geometry(hass, monkeypatch, renamed):
    async def scenario():
        data, macs, devices = fixture(hass, monkeypatch, upstairs=True, renamed=renamed)
        await save_bps_data(hass, data)
        cal = C.get_calibration_state(hass); cal.update(mode="auto", state="sampling")
        seed_history(hass, monkeypatch, cal, data, macs)
        assert len(cal["receivers"]) == 7
        assert all(len(rows) == 20 for rows in cal["_auto_samples"].values())
        captures = {}
        async def executor(func, *args):
            if func is C.assess_update:
                captures[args[3]] = copy.deepcopy(args[:3])
            return func(*args)
        hass.async_add_executor_job = executor
        await C._auto_solve_and_apply_locked(hass, cal)
        saved = get_bps_data_for_edit(hass)
        for floor, original in zip(saved["floor"], data["floor"]):
            name = floor["name"]
            assert cal["auto_status"][name]["state"] == "applied"
            expected = C._build_receiver_map({"floor": [original]}, hass=hass)
            assert captures[name][0]["receivers"] == expected
            assert len(captures[name][1]) == 12
            assert set(cal["results"][name]["receivers"]) == set(expected)
            source = cal["results"][name]["bermuda_source"]
            assert set(source["receivers"]) == set(expected)
            assert all(row["geometry"] == expected[slug] and row["scanner_mac"] == macs[slug]
                       for slug, row in source["receivers"].items())
            assert floor["receivers"][0]["correction"] != 1.
            assert [r["cords"] for r in floor["receivers"]] == [r["cords"] for r in original["receivers"]]
        # Editing only the second floor invalidates all shared reference history.
        saved["floor"][1]["receivers"][0]["height"] += .5
        C.refresh_receivers_from_coords(hass, json.dumps(saved))
        assert cal["_auto_samples"] == {} and cal["samples"] == {}
        assert "_auto_sources" not in cal
    asyncio.run(scenario())


def test_incomplete_model_provenance_cannot_reuse_perfloor_certification(hass, monkeypatch):
    data, macs, devices = fixture(hass, monkeypatch, upstairs=True, renamed=True)
    cal = C.get_calibration_state(hass); cal["mode"] = "auto"
    payload = seed_history(hass, monkeypatch, cal, data, macs)
    assert set(cal["_auto_sources"]) == {"Ground", "Upstairs"}
    cal.pop("_bermuda_source", None)
    for dev in payload.values():
        for advert in dev["adverts"].values(): advert.pop("conf_attenuation")
    C._ingest_dump(cal, payload)
    assert "_auto_sources" not in cal and "_bermuda_source" not in cal


def test_auto_never_applies_old_reference_evidence_to_conflicting_alias_placements(hass, monkeypatch):
    async def scenario():
        data, macs, devices = fixture(hass, monkeypatch, duplicate=True)
        cal = C.get_calibration_state(hass); cal["mode"] = "auto"
        seed_history(hass, monkeypatch, cal, data, macs)
        data["floor"][0]["receivers"][-1]["correction"] = 1.5
        await save_bps_data(hass, data)
        before = get_bps_data_for_edit(hass)
        await C._auto_solve_and_apply_locked(hass, cal)
        assert cal["auto_status"]["Ground"]["state"] == "skipped"
        assert get_bps_data_for_edit(hass) == before and not cal["applied"]
    asyncio.run(scenario())


@pytest.mark.parametrize("auto", [False, True])
def test_alias_updates_publish_atomically_only_after_layout_save(hass, monkeypatch, auto):
    async def scenario():
        data, macs, devices = fixture(hass, monkeypatch, duplicate=True)
        await save_bps_data(hass, data)
        cal = C.get_calibration_state(hass); cal["mode"] = "auto" if auto else "manual"
        seed_history(hass, monkeypatch, cal, data, macs)
        cal["results"]["Ground"] = C.solve(C.solve_snapshot(cal), "Ground")
        before = get_bps_data_for_edit(hass)
        async def fail_save(*args): raise OSError("synthetic storage failure")
        monkeypatch.setattr(C, "save_bps_data", fail_save)
        with pytest.raises(OSError, match="synthetic storage failure"):
            if auto: await C._auto_solve_and_apply_locked(hass, cal)
            else: await C.apply_corrections(hass, cal, "Ground")
        assert get_bps_data_for_edit(hass) == before and not cal["applied"]
        if auto: assert cal["auto_status"]["Ground"]["state"] == "skipped"
    asyncio.run(scenario())


def test_manual_and_new_auto_sessions_discard_old_perfloor_routing_and_source_certification(hass, monkeypatch):
    async def scenario():
        data, macs, devices = fixture(hass, monkeypatch, upstairs=True, renamed=True)
        await save_bps_data(hass, data)
        cal = C.get_calibration_state(hass); cal["mode"] = "auto"
        seed_history(hass, monkeypatch, cal, data, macs)
        cal.update(mode="off", state="idle")
        hass.async_create_task = asyncio.create_task
        try:
            await C.start_calibration(hass, "Ground", 60)
            assert "_auto_floor_receivers" not in cal and "_auto_sources" not in cal
            assert set(cal["receivers"]) == {"shared_old", "g1", "g2", "g3"}
            cal["_auto_sources"] = {"obsolete": {}}
            await C.set_auto_calibration(hass, True)
            assert set(cal["_auto_floor_receivers"]) == {"Ground", "Upstairs"}
            assert cal["_auto_routes"]["Upstairs"]["shared_current"] == "shared_old"
            assert "_auto_sources" not in cal and cal["_auto_samples"] == {}
        finally:
            await C.async_shutdown_calibration(hass)
    asyncio.run(scenario())


def test_explicit_solve_api_during_auto_uses_selected_floor_raw_samples_and_provenance(hass, monkeypatch):
    async def scenario():
        data, macs, devices = fixture(hass, monkeypatch, upstairs=True, renamed=True)
        await save_bps_data(hass, data)
        cal = C.get_calibration_state(hass); cal.update(mode="auto", state="sampling")
        seed_history(hass, monkeypatch, cal, data, macs)
        cal["_auto_samples"] = {}  # Explicit Solve uses raw windows, independently of auto admission.
        captures = []
        async def executor(func, *args):
            captures.append(copy.deepcopy(args[0]))
            return func(*args)
        hass.async_add_executor_job = executor
        async def body(): return {"action": "solve", "floor": "Upstairs"}
        response = await C.BPSCalibrationAPI().post(NS(app={"hass": hass}, json=body))
        assert response.status == 200
        expected = C._build_receiver_map(data, "Upstairs", hass=hass)
        assert captures[0]["receivers"] == expected and len(captures[0]["samples"]) == 12
        result = cal["results"]["Upstairs"]
        assert set(result["receivers"]) == set(expected)
        assert set(result["bermuda_source"]["receivers"]) == set(expected)
        assert result["receivers"]["shared_current"] == pytest.approx(.5, abs=.01)
        assert "Ground" not in cal["results"]
    asyncio.run(scenario())


@pytest.mark.parametrize("auto", [False, True])
def test_tracking_validation_replays_one_effective_input_per_physical_scanner(hass, monkeypatch, auto):
    from test_calibration_validation import recording
    from test_calibration_auto import fixture_data
    async def scenario():
        entries, states, devices, add = configure(hass, monkeypatch)
        bundle, floor = recording()
        for index, rec in enumerate(floor["receivers"]):
            mac = f"aa:bb:cc:40:50:{index:02x}"
            add(f'sensor.tag_distance_to_{rec["entity_id"]}', "4", uid="synthetic-tag_" + mac + "_range")
            devices[rec["entity_id"]] = NS(_is_scanner=True, name=rec["entity_id"], address=mac, adverts={})
        alias = copy.deepcopy(floor["receivers"][0]); alias["entity_id"] = "r0_alias"
        floor["receivers"].extend([alias, copy.deepcopy(floor["receivers"][0])])
        add("sensor.tag_distance_to_r0_alias", "4", uid="synthetic-tag_aa:bb:cc:40:50:00_range")
        for frame in bundle["frames"]:
            if frame["readings"]:
                row = copy.deepcopy(frame["readings"][0]); row["receiver"] = "r0_alias"
                frame["readings"].extend([row, copy.deepcopy(frame["readings"][0])])
        data = bundle["contexts"]["c"]["layout"]
        await save_bps_data(hass, data)
        cal = C.get_calibration_state(hass)
        C._set_auto_receivers(hass, cal, data); C._refresh_auto_history(cal)
        _floor, _snapshot, history = fixture_data()
        cal["samples"] = {pair: deque(value for _, value in rows) for pair, rows in history.items()}
        cal["_auto_samples"] = {pair: deque(rows) for pair, rows in history.items()}
        cal["results"]["Yard"] = C.solve(C.solve_snapshot(cal), "Yard")
        async def export(): return json.dumps(bundle).encode()
        hass.data["bps"]["_diagnostics"] = NS(frames=[1], export=export)
        original_solver = bps.trilaterate; input_counts = []
        def solver(points, **kwargs):
            input_counts.append(len(points)); return original_solver(points, **kwargs)
        monkeypatch.setattr(bps, "trilaterate", solver)
        if auto:
            cal["mode"] = "auto"
            monkeypatch.setattr(C.time, "time", lambda: NOW)
            await C._auto_solve_and_apply_locked(hass, cal, await export())
            assert cal["auto_status"]["Yard"]["state"] == "applied"
            assert cal["auto_status"]["Yard"]["tracking_validation"]["verdict"] != "worse"
        else:
            report = await bps._calibration_tracking_check(hass, "Yard")
            assert report["verdict"] == "improves"
        assert input_counts and set(input_counts) == {4}
    asyncio.run(scenario())
