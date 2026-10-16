"""Synthetic v1 contract exercised through actual tracking/calibration/report consumers."""
import asyncio
import copy
import importlib
import json
import math
import sys
from types import SimpleNamespace as NS

import pytest

import bps
from bps import bermuda_calibration as B, bermuda_diagnostics as D, calibration as C
from bps.bermuda_direct import BermudaDirect, public_id
from bps.receiver_sources import ReceiverSources
from test_receiver_aliases import aliases, timestamp
from test_receiver_matching import MAC

OTHER = "aa:bb:cc:44:55:66"
BLE = "dd:ee:ff:11:22:33"


def scanner(sid=MAC, retained=None, name="Current station"):
    return {"scanner_id": sid, "address": sid, "name": name, "slug": name.lower().replace(" ", "_"),
        "unique_id": retained or sid, "entity_id_address": retained or sid,
        "address_wifi_mac": retained, "address_ble_mac": sid, "device_registry_id": "synthetic-proxy",
        "binding": {"source": "synthetic"}, "source_aliases": [], "area_id": "synthetic-room",
        "area_name": "Synthetic room", "scanner_last_seen_age": 2.}


def measurement(sid=MAC, retained=None, device="synthetic-tracker", mode="legacy", age=2., accepted_age=2.):
    return {**scanner(sid, retained), "device_id": device, "source_id": device,
        "observed": 1000. - age, "observed_clock": "derived_monotonic", "stamp": 500. - age,
        "age": age, "status": "current", "rssi": -80., "raw_rssi": -80., "filtered_rssi": None,
        "rssi_offset": -3., "reference_power": -60., "attenuation": 2., "raw_distance": 9.,
        "filtered_distance": 6., "distance": 6., "distance_raw": 9., "model_version": "synthetic-model-v1",
        "reference_power_verified": False,
        # accepted=False is the real FilterState.snapshot read contract.
        "quality": {"mode": mode, "accepted": False, "fresh": True, "status": "current",
            "last_raw_stamp": 500. - age, "last_valid_stamp": 500. - accepted_age,
            "last_valid_observed": 1000. - accepted_age, "age": accepted_age,
            "noise_db": 3., "samples": 8, "timeout_s": 30., "reliability": .8,
            "rejection_reasons": [], "reasons": []},
        "history": [{"rssi": -80., "raw_distance": 9., "stamp": 498. - i, "age": 2. + i} for i in range(5)],
        "history_status": "runtime_recent", "history_omitted": 3, "unavailable": ["filtered_rssi"]}


def snapshot(entry="entry", did="synthetic-tracker", sid=MAC, retained=None):
    ad = measurement(sid, retained, did)
    return {"version": 1, "entry_id": entry, "stamp": 500., "captured": 1000.,
        "clocks": {"stamp": "monotonic", "captured": "utc_epoch"}, "history_status": "runtime_recent",
        "scanners": {sid: scanner(sid, retained)}, "devices": {did: {
            "device_id": did, "owner_id": did, "address": did, "name": "Synthetic beacon", "slug": "tag",
            "unique_id": did, "tracked": True, "source_aliases": [], "scanners": {sid: ad}}},
        "limits": {"devices": 256, "scanners": 128, "observations": 4096},
        "omitted": {"devices": 0, "scanners": 0, "observations": 0, "source_aliases": 0}}


def install(monkeypatch, payloads):
    calls = []
    def getter(hass, **kwargs):
        calls.append(kwargs)
        value = payloads.get(kwargs["entry_id"])
        if isinstance(value, Exception):
            raise value
        value = copy.deepcopy(value)
        if isinstance(value, dict) and isinstance(value.get("devices"), dict):
            value["devices"] = {did: device for did, device in value["devices"].items()
                if (not kwargs.get("tracked_only") or device.get("tracked") is True)
                and (kwargs.get("addresses") is None or did in kwargs["addresses"])}
        return value
    monkeypatch.setitem(sys.modules, "custom_components.bermuda.api", NS(async_get_advert_snapshot=getter))
    return calls


def prepared(hass, monkeypatch, *, outdoor=False, sid=MAC, retained=None):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch, outdoor=outdoor)
    devices.clear()  # Direct scanner provenance must work without private runtime.
    config = hass.config_entries.async_entries("bermuda")[0]
    config.title = "Synthetic Bermuda"; config.options = {}; config.state = "loaded"
    payloads = {"entry": snapshot(sid=sid, retained=retained)}
    calls = install(monkeypatch, payloads)
    return data, entries, states, add, old, new, payloads, calls


@pytest.mark.parametrize("outdoor", [False, True])
def test_tracking_consumes_direct_metres_and_preserves_all_existing_corrections(hass, monkeypatch, outdoor):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch, outdoor=outdoor, sid=BLE, retained=MAC)
    data["tracker_ref_offsets"] = {"tag": 5.}
    states[new].state = "1000"; states[new].attributes["unit_of_measurement"] = "ft"
    hass.data["bps"]["_diagnostics"] = NS(active=True, sources={"tag"}, members={})
    rec = data["floor"][0]["receivers"][0]
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    expected = 6. * 2. * bps._tracker_distance_factor(data, "tag")
    assert rec["distance"] == expected
    assert rec["cords"]["r"] == pytest.approx(10. * math.sqrt(expected ** 2 - 1.))
    assert rec["_diagnostic_reading"]["observed"] == 998.
    assert rec["entity_id"] == "old_station" and rec["custom"] == {"keep": True}
    if outdoor:
        assert rec["_outdoor_reading"]["measured_distance_m"] == 6.
        assert rec["_outdoor_reading"]["observed"] == 998.
    assert len(calls) == 1 and calls[0]["entry_id"] == "entry"
    assert calls[0]["tracked_only"] is True


@pytest.mark.parametrize("failure,reason", [
    (None, "entry_unavailable"), (RuntimeError("synthetic failure"), "api_error"),
    ({"version": 99}, "unsupported_snapshot_version"), ({"version": True}, "unsupported_snapshot_version"),
    ({"version": 1, "entry_id": "different-entry", "devices": {}, "scanners": {}}, "invalid_snapshot")])
def test_unavailable_or_unsupported_api_retains_legacy_results_and_explains_fallback(hass, monkeypatch, failure, reason):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    payloads["entry"] = failure
    sources = ReceiverSources(hass)
    assert sources.source("tag", data["floor"][0]["receivers"][0]) == (new, states[new])
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}, sources))
    assert data["floor"][0]["receivers"][0]["distance"] == 8.
    hass.data["bps"]["layout"] = data
    row = bps._diagnostic_snapshot(hass, ["tag"], 1000)["readings"][0]
    assert row["measurement_source"]["path"] == "legacy_entity"
    assert row["measurement_source"]["reason"] == reason


@pytest.mark.parametrize("failure", ["stale", "missing_pair", "invalid_age", "future_age", "missing_range", "accepted_stale"])
def test_covered_bad_pair_cannot_be_replaced_by_fresh_entity_state(hass, monkeypatch, failure):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch, outdoor=True)
    ad = payloads["entry"]["devices"]["synthetic-tracker"]["scanners"][MAC]
    states[new].state = "4"; states[new].last_updated = timestamp(1000)
    if failure == "stale": ad.update(status="stale", age=100., filtered_distance=None)
    elif failure == "missing_pair": payloads["entry"]["devices"]["synthetic-tracker"]["scanners"].clear()
    elif failure == "invalid_age": ad["age"] = None
    elif failure == "future_age": ad["age"] = -2.
    elif failure == "missing_range": ad["filtered_distance"] = None
    else: ad["quality"].update(mode="pet", status="stale", age=100.)
    rec = data["floor"][0]["receivers"][0]
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert "distance" not in rec and "r" not in rec["cords"]
    hass.data["bps"]["layout"] = data
    row = bps._diagnostic_snapshot(hass, ["tag"], 1000)["readings"][0]
    assert row["measurement_source"]["path"] == "direct"
    assert "measured_distance_m" not in row


@pytest.mark.parametrize("mode", ["pet", "stationary"])
def test_rejected_raw_packet_retains_accepted_filtered_clock_and_expires_at_bps_gate(hass, monkeypatch, mode):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch, outdoor=True)
    ad = measurement(mode=mode, age=2., accepted_age=12.)
    ad["quality"]["reasons"] = [{"code": "unconfirmed_jump"}]
    payloads["entry"]["devices"]["synthetic-tracker"]["scanners"][MAC] = ad
    rec = data["floor"][0]["receivers"][0]
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert rec["distance"] == 12. and rec["_outdoor_reading"]["observed"] == 988.
    assert rec["_outdoor_reading"]["reading_age_s"] == 12.
    data["reading_max_age"] = 10.
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert "distance" not in rec


def test_cached_batch_clock_ages_and_new_batch_refreshes_only_from_source_age(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    sources = ReceiverSources(hass)
    rec = data["floor"][0]["receivers"][0]
    first = sources.source("tag", rec, now=1000)[1]
    second = sources.source("tag", rec, now=1010)[1]
    assert first.last_updated == second.last_updated == timestamp(998.)
    assert bps._reading_age_secs(second, now=1010) == 12.
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["owner", "tracker", "new_exact", "remove"])
def test_registry_replacement_cannot_reuse_captured_direct_tracker_proof(hass, monkeypatch, change):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    if change == "new_exact":
        del entries[old]
        data["floor"][0]["receivers"][0].update(scanner_mac=MAC, scanner_config_entry_id="entry")
    sources = ReceiverSources(hass)
    rec = data["floor"][0]["receivers"][0]
    assert sources.source("tag", rec)[1].state == 6.
    if change == "owner": entries[old].config_entry_id = "replacement-entry"
    elif change == "tracker": entries[old].unique_id = f"replacement-device_{MAC}_range"
    elif change == "new_exact": add(old, uid=f"replacement-device_{MAC}_range")
    else: del entries[new]
    assert sources.source("tag", rec)[1] is None


def test_disabled_distance_entities_still_prove_existing_tracker_keys(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    for eid in (old, new):
        entries[eid].disabled_by = "user"
        del states[eid]
    sources = ReceiverSources(hass)
    assert sources.direct_trackers() == {"tag"}
    from bps.sensor import get_filtered_entities
    hass.states.async_all = lambda _domain=None: list(states.values())
    assert get_filtered_entities(hass) == ["tag"]
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert data["floor"][0]["receivers"][0]["distance"] == 12.
    hass.data["bps"]["layout"] = data
    assert bps._diagnostic_inventory(hass)["trackers"] == [{"key": "tag", "name": "tag"}]


def test_public_private_device_hash_matches_registry_without_exporting_irk(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    irk = "0123456789abcdef0123456789abcdef"  # Deliberately synthetic.
    for eid in (old, new): entries[eid].unique_id = irk + "_" + MAC + "_range"
    payloads["entry"] = snapshot(did=public_id(irk))
    hass.data["bps"]["layout"] = data
    report = bps._diagnostic_report_snapshot(hass, 1000.)
    assert report["readings"][0]["measured_distance_m"] == 6.
    text = bps.diagnostics.format_report(report)
    assert public_id(irk) in text and irk not in text
    direct = report["bermuda"]["direct"]["snapshots"][0]
    assert direct["devices"][public_id(irk)]["device_id"] == public_id(irk)


def test_mixed_entries_fall_back_independently_without_borrowing_identity(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    legacy = NS(entry_id="legacy", runtime_data=NS(coordinator=NS(devices={}, options={})))
    configs = hass.config_entries.async_entries("bermuda") + [legacy]
    hass.config_entries.async_entries = lambda _: configs
    add("sensor.tag_distance_to_legacy", "5", uid=f"synthetic-tracker_{OTHER}_range")
    entries["sensor.tag_distance_to_legacy"].config_entry_id = "legacy"
    states["sensor.tag_distance_to_legacy"].last_updated = timestamp(999.)
    data["floor"][0]["receivers"].append({"entity_id": "legacy", "cords": {"x": 50, "y": 40}})
    sources = ReceiverSources(hass)
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}, sources))
    assert [r["distance"] for r in data["floor"][0]["receivers"]] == [12., 5.]
    assert {row["entry_id"]: row["path"] for row in sources.direct.statuses} == {"entry": "direct", "legacy": "legacy"}


def test_same_mac_and_name_across_entries_requires_explicit_owner(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    second = NS(entry_id="second", runtime_data=NS(coordinator=NS(devices={}, options={})))
    configs = hass.config_entries.async_entries("bermuda") + [second]
    hass.config_entries.async_entries = lambda _: configs
    payloads["second"] = snapshot(entry="second")
    payloads["second"]["devices"]["synthetic-tracker"]["scanners"][MAC]["filtered_distance"] = 99.
    add("sensor.other_distance_to_current_station", "20")
    entries["sensor.other_distance_to_current_station"].config_entry_id = "second"
    rec = data["floor"][0]["receivers"][0]
    sources = ReceiverSources(hass)
    assert sources.source("tag", {"entity_id": "current_station"})[1] is None
    rec.update(scanner_mac=MAC, scanner_config_entry_id="entry")
    assert sources.source("tag", rec)[1].state == 6.


def test_full_copyable_report_has_direct_history_profile_health_and_no_mutations(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    hass.data["bps"]["layout"] = data
    config = hass.config_entries.async_entries("bermuda")[0]
    config.title = "Synthetic Bermuda"; config.options = {}; config.state = "loaded"
    coordinator = config.runtime_data.coordinator
    coordinator.options = {"measurement_toolkit": {"receiver_profiles": {MAC: {
        "antenna_gain_dbi": 2., "distance_accuracy_m": .5, "rssi_offset": -3., "token": "synthetic-secret-do-not-export"}},
        "beacon_profiles": {"synthetic-tracker": {"reference_power_verified": False}},
        "filters": {"synthetic-tracker": "pet"}, "auto_calibration": False, "token": "synthetic-secret-do-not-export"}}
    def mutation(): raise AssertionError("Diagnostics must not run Bermuda analysis")
    coordinator.toolkit = NS(_state=mutation, version=mutation, area_suggestion=mutation,
        _perf={"updates": 42, "last_update_s": .003, "max_update_s": .005, "background_skipped": 2},
        _states={}, _worker=None, _closed=False, _version_cache="synthetic-model-v1",
        proposal={"eligible": False, "reasons": ["insufficient_data"]}, trial=None, last_result=None,
        history=NS(_frames=[{}], _row_count=1, _byte_count=100, enabled=False, window_s=3600.), shadow=None)
    before = copy.deepcopy((data, payloads, coordinator.options))
    report = bps._diagnostic_report_snapshot(hass, 1000.)
    text = bps.diagnostics.format_report(report)
    assert '"history_status": "runtime_recent"' in text
    direct = report["bermuda"]["direct"]["snapshots"][0]
    ad = direct["devices"]["synthetic-tracker"]["scanners"][MAC]
    assert len(ad["history"]) == 5 and ad["raw_distance"] == 9. and ad["filtered_distance"] == 6.
    assert ad["quality"]["accepted"] is False and ad["model_version"] == "synthetic-model-v1"
    toolkit = report["bermuda"]["entries"][0]["toolkit"]
    assert toolkit["performance"]["updates"] == 42 and toolkit["history"]["frames"] == 1
    assert toolkit["calibration"]["proposal"]["reasons"] == ["insufficient_data"]
    assert "synthetic-secret-do-not-export" not in text
    profile = report["bermuda"]["entries"][0]["effective_settings"]["measurement_toolkit"]["receiver_profiles"][MAC]
    assert profile["distance_accuracy_m"] == .5
    assert (data, payloads, coordinator.options) == before and not hass._store_saves


def test_hardware_direct_contract_is_allowlisted_identity_scoped_and_in_copyable_report(hass, monkeypatch):
    from bps.receiver_matching import inventory
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    hardware = {"detected": {"manufacturer": "Synthetic", "model": "ESP32 board", "password": "synthetic-secret"},
                "effective": {"model": "Manual model", "external_antenna": False, "token": "synthetic-secret"},
                "provenance": "esphome", "api_key": "synthetic-secret"}
    payloads["entry"]["scanners"][MAC]["hardware"] = hardware
    payloads["entry"]["devices"]["synthetic-tracker"]["hardware"] = hardware
    direct = BermudaDirect(hass)
    result = inventory(hass, direct)
    option = next(row for row in result["options"] if row["entity_id"] == "current_station")
    assert option["hardware"]["detected"]["model"] == "ESP32 board"
    assert option["hardware"]["effective"]["external_antenna"] is False
    assert option["scanner_identities"][0]["hardware"] == option["hardware"]
    assert "synthetic-secret" not in json.dumps((result, direct.snapshots))
    hass.data["bps"]["layout"] = data
    report = bps._diagnostic_report_snapshot(hass, 1000.)
    assert '"model": "ESP32 board"' in bps.diagnostics.format_report(report)
    before_revision = result["revision"]
    del payloads["entry"]["scanners"][MAC]["hardware"]
    fresh = inventory(hass)
    fresh_option = next(row for row in fresh["options"] if row["entity_id"] == "current_station")
    assert fresh_option["hardware"]["detected"] == {} and fresh["revision"] != before_revision
    assert not hass._store_saves


def test_direct_scanner_age_precedes_cached_legacy_dump_and_missing_age_stays_unknown(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    hass.data["bps"]["layout"] = data
    hass.data["bps"].update(rl_mac_ages={MAC: 0}, rl_anchor_wall=1000.)
    payloads["entry"]["scanners"][MAC]["scanner_last_seen_age"] = 100.
    async def forbidden(*args, **kwargs): raise AssertionError("All entries have a direct API")
    hass.services.async_call = forbidden
    asyncio.run(bps.update_receiver_liveness(hass))
    assert "old_station" in hass.data["bps"]["rl_offline"]
    payloads["entry"]["scanners"][MAC]["scanner_last_seen_age"] = None
    payloads["entry"]["devices"]["synthetic-tracker"]["scanners"][MAC].update(status="stale", filtered_distance=None)
    asyncio.run(bps.update_receiver_liveness(hass))
    assert "old_station" in hass.data["bps"]["rl_unknown"] and hass.data["bps"]["rl_offline"] == []


@pytest.mark.parametrize("consumer", ["liveness", "scanner_diagnostics", "scanner_linking"])
def test_receiver_views_reuse_one_direct_capture_for_scanner_matching(hass, monkeypatch, consumer):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    hass.data["bps"]["layout"] = data
    api = sys.modules["custom_components.bermuda.api"]
    capture = api.async_get_advert_snapshot
    def next_frame_after_capture(*args, **kwargs):
        result = capture(*args, **kwargs)
        payloads["entry"]["scanners"][MAC].update(name="Next frame station", slug="next_frame_station")
        return result
    api.async_get_advert_snapshot = next_frame_after_capture
    async def forbidden(*args, **kwargs):
        raise AssertionError("A fully direct liveness refresh needs no legacy service")
    hass.services.async_call = forbidden
    if consumer == "liveness":
        asyncio.run(bps.update_receiver_liveness(hass))
        status = hass.data["bps"]
        assert "old_station" not in status["rl_offline"] + status["rl_unknown"]
        assert all("next_frame_station" not in status[key] for key in ("rl_offline", "rl_unknown"))
    else:
        view = getattr(bps, "_" + consumer)(hass, json.dumps(data))
        assert "next_frame_station" not in json.dumps(view)
    assert len(calls) == 1


def calibration_payload():
    value = snapshot()
    value["scanners"][OTHER] = scanner(OTHER, name="Other station")
    value["devices"] = {}
    for tx, rx in ((MAC, OTHER), (OTHER, MAC)):
        ad = measurement(rx, device=tx)
        value["devices"][tx] = {"device_id": tx, "unique_id": tx, "tracked": False, "scanners": {rx: ad}}
    return value


def calibration_setup(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    payloads["entry"] = calibration_payload()
    add("sensor.tag_distance_to_other_station", "5", uid=f"synthetic-tracker_{OTHER}_range")
    data["floor"][0]["receivers"].append({"entity_id": "other_station", "cords": {"x": 100, "y": 20}})
    cal = {"receivers": C._build_receiver_map(data, hass=hass), "samples": {}}
    return data, payloads, cal


def test_calibration_consumes_raw_api_range_once_and_uses_effective_pair_model(hass, monkeypatch):
    data, payloads, cal = calibration_setup(hass, monkeypatch)
    async def forbidden(*args, **kwargs): raise AssertionError("Direct API must avoid dump service")
    hass.services.async_call = forbidden
    dump = asyncio.run(C._dump_devices(hass))
    C._ingest_dump(cal, dump); C._ingest_dump(cal, dump)
    assert {key: list(value) for key, value in cal["samples"].items()} == {
        "old_station|other_station": [9.], "other_station|old_station": [9.]}
    row = cal["_bermuda_source"]["receivers"]["old_station"]
    assert row["attenuation"] == 2. and row["rssi_offset_db"] == -3.
    assert row["model_versions"] == ["synthetic-model-v1"] and row["source_path"] == "direct"
    coordinator = hass.config_entries.async_entries("bermuda")[0].runtime_data.coordinator
    coordinator.options = {"attenuation": 4., "ref_power": -50., "rssi_offsets": {MAC: -20.}}
    context = B.runtime_context(hass, data, "Ground")["receivers"]["old_station"]
    assert context["attenuation"] == 2. and context["rssi_offset_db"] == -3.


@pytest.mark.parametrize("kind", ["stale", "rejected"])
def test_calibration_rejects_old_actual_packet_even_when_all_cached_packets_share_stamp(hass, monkeypatch, kind):
    data, payloads, cal = calibration_setup(hass, monkeypatch)
    for device in payloads["entry"]["devices"].values():
        for ad in device["scanners"].values():
            if kind == "stale": ad["age"] = C.STALE_ADVERT_SECS + 1.
            else: ad["quality"].update(mode="pet", last_valid_stamp=400.)
    C._ingest_dump(cal, asyncio.run(C._dump_devices(hass)))
    assert cal["samples"] == {}


def test_mixed_calibration_api_replaces_covered_legacy_rows_and_keeps_other_entry(hass, monkeypatch):
    data, payloads, cal = calibration_setup(hass, monkeypatch)
    legacy_mac = "aa:bb:cc:77:88:99"
    legacy = NS(entry_id="legacy", runtime_data=None)
    configs = hass.config_entries.async_entries("bermuda") + [legacy]
    hass.config_entries.async_entries = lambda _: configs
    registry = bps.er.async_get(hass)
    registry.entities["sensor.legacy_distance_to_legacy"] = NS(
        entity_id="sensor.legacy_distance_to_legacy", platform="bermuda", config_entry_id="legacy",
        unique_id=f"synthetic-legacy_{legacy_mac}_range")
    async def dump(*args, **kwargs):
        return {MAC: {"_is_scanner": True, "address": MAC, "name": "Current station", "adverts": {
            OTHER: {"scanner_address": OTHER, "stamp": 999., "rssi_distance_raw": 999.}}},
            legacy_mac: {"_is_scanner": True, "address": legacy_mac, "name": "Legacy", "adverts": {}}}
    hass.services.async_call = dump
    rows = asyncio.run(C._dump_devices(hass))
    assert set(rows) == {MAC, OTHER, legacy_mac}
    assert rows[MAC]["adverts"][OTHER]["rssi_distance_raw"] == 9.


def test_full_debug_direct_rows_and_history_are_bounded_and_json_finite(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    original = payloads["entry"]["devices"]["synthetic-tracker"]
    ad = original["scanners"][MAC]
    ad["history"] *= 100
    ad["quality"]["noise_db"] = math.nan
    payloads["entry"]["devices"] = {f"synthetic-device-{i}": copy.deepcopy(original) for i in range(600)}
    bridge = BermudaDirect(hass, now=1000., include_history=True)
    assert len(bridge.snapshots["entry"]["devices"]) == 256
    report = bridge.diagnostics()
    assert len(json.dumps(report, allow_nan=False).encode()) < 1024 * 1024
    for device in report["snapshots"][0]["devices"].values():
        assert len(device["scanners"][MAC]["history"]) == 5


def test_all_loaded_direct_entries_ignore_unloaded_entry_for_service_fallback(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    configs = hass.config_entries.async_entries("bermuda") + [NS(entry_id="unloaded", state="not_loaded")]
    hass.config_entries.async_entries = lambda _: configs
    async def forbidden(*args, **kwargs): raise AssertionError("Inactive entry must not trigger the service")
    hass.services.async_call = forbidden
    assert not BermudaDirect(hass).needs_legacy
    assert asyncio.run(C._dump_devices(hass))
    asyncio.run(bps.update_receiver_liveness(hass))


def test_mixed_calibration_retains_direct_and_owned_private_runtime_when_service_fails(hass, monkeypatch):
    data, payloads, cal = calibration_setup(hass, monkeypatch)
    legacy_mac = "aa:bb:cc:77:88:99"
    private = NS(_is_scanner=True, address=legacy_mac, name="Legacy", adverts={})
    configs = hass.config_entries.async_entries("bermuda") + [NS(entry_id="legacy", state="loaded",
        runtime_data=NS(coordinator=NS(devices={legacy_mac: private}))) ]
    hass.config_entries.async_entries = lambda _: configs
    async def unavailable(*args, **kwargs): raise RuntimeError("Legacy service unavailable")
    hass.services.async_call = unavailable
    rows = asyncio.run(C._dump_devices(hass))
    assert set(rows) == {MAC, OTHER, legacy_mac}
    assert rows[legacy_mac]["_bps_config_entry_id"] == "legacy"


def test_unknown_device_identity_keeps_exact_legacy_source_without_using_api_name(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    payloads["entry"] = snapshot(did="different-synthetic-device")
    sources = ReceiverSources(hass)
    assert sources.source("tag", data["floor"][0]["receivers"][0]) == (new, states[new])


def test_optional_api_module_missing_keeps_legacy_path_and_no_install_requirement(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    monkeypatch.delitem(sys.modules, "custom_components.bermuda.api")
    attempts = []
    def forbidden(name):
        attempts.append(name)
        raise AssertionError("Synchronous consumers must not import the API")
    monkeypatch.setattr(importlib, "import_module", forbidden)
    sources = ReceiverSources(hass)
    assert sources.source("tag", data["floor"][0]["receivers"][0]) == (new, states[new])
    assert sources.direct.statuses[0]["reason"] == "api_unavailable"
    assert attempts == []


@pytest.mark.parametrize("import_error", [None, ModuleNotFoundError("Stock Bermuda"), RuntimeError("Optional API failed")])
def test_entry_setup_prepares_optional_api_before_forwarding_sensors(hass, monkeypatch, import_error):
    from bps import bermuda_direct as module
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    api = sys.modules["custom_components.bermuda.api"]
    monkeypatch.delitem(sys.modules, "custom_components.bermuda.api")
    events = []

    async def import_api(_hass, name):
        assert name == "custom_components.bermuda.api"
        events.append("import")
        if import_error is not None:
            raise import_error
        monkeypatch.setitem(sys.modules, name, api)
        return api

    async def sensors(_entry, platforms):
        assert platforms == ["sensor"]
        events.append("sensors")
        sources = ReceiverSources(hass)
        assert sources.direct.statuses[0]["path"] == ("direct" if import_error is None else "legacy")
        assert sources.source("tag", data["floor"][0]["receivers"][0]) is not None

    async def setup(_hass, _entry):
        events.append("setup")
        return True

    monkeypatch.setattr(module, "async_import_module", import_api)
    monkeypatch.setattr(bps, "cleanup_legacy_bps_registry_and_states", lambda _hass: None)
    monkeypatch.setattr(bps, "async_setup", setup)
    hass.config_entries.async_forward_entry_setups = sensors
    entry = NS(async_on_unload=lambda _unsub: None, add_update_listener=lambda _listener: None)
    assert asyncio.run(bps.async_setup_entry(hass, entry))
    assert events == ["import", "sensors", "setup"]


def test_api_preparation_skips_loaded_module_and_absent_bermuda(hass, monkeypatch):
    from bps import bermuda_direct as module
    prepared(hass, monkeypatch)
    async def forbidden(*_args):
        raise AssertionError("No import should be scheduled")
    monkeypatch.setattr(module, "async_import_module", forbidden)
    asyncio.run(module.async_prepare_api(hass))
    monkeypatch.delitem(sys.modules, "custom_components.bermuda.api")
    hass.config_entries.async_entries = lambda _domain: []
    asyncio.run(module.async_prepare_api(hass))


@pytest.mark.parametrize("available", [True, False])
def test_bermuda_added_after_bps_setup_gets_one_nonblocking_preparation(hass, monkeypatch, available):
    from bps import bermuda_direct as module
    async def scenario():
        data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
        api = sys.modules["custom_components.bermuda.api"]
        monkeypatch.delitem(sys.modules, "custom_components.bermuda.api")
        entry = hass.config_entries.async_entries("bermuda")[0]
        roster = []
        hass.config_entries.async_entries = lambda _domain: roster
        hass.async_create_task = asyncio.create_task
        imports = []
        entered, release = asyncio.Event(), asyncio.Event()
        async def import_api(_hass, name):
            imports.append(name)
            entered.set()
            await release.wait()
            if not available:
                raise ModuleNotFoundError("Optional API absent")
            monkeypatch.setitem(sys.modules, name, api)
            return api
        monkeypatch.setattr(module, "async_import_module", import_api)
        await module.async_prepare_api(hass)  # Initial BPS setup without Bermuda.
        assert imports == []
        roster.append(entry)
        for _ in range(5):
            assert BermudaDirect(hass).needs_legacy
        assert imports == []  # The constructor did not execute the import.
        await asyncio.wait_for(entered.wait(), 1)
        pending = hass.data["bps"]["_bermuda_api_prepare"]
        for _ in range(5):
            BermudaDirect(hass)
            assert hass.data["bps"]["_bermuda_api_prepare"] is pending
        release.set()
        await asyncio.wait_for(pending, 1)
        for _ in range(5):
            sources = ReceiverSources(hass)
            assert sources.direct.statuses[0]["path"] == ("direct" if available else "legacy")
            if available:
                assert sources.source("tag", data["floor"][0]["receivers"][0])[1].state == 6.0
        await asyncio.sleep(0)
        assert imports == ["custom_components.bermuda.api"]
    asyncio.run(scenario())


def test_direct_duplicate_receiver_placement_still_feeds_one_solver_input(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch, outdoor=True)
    clone = copy.deepcopy(data["floor"][0]["receivers"][0])
    clone["entity_id"] = "current_station"
    data["floor"][0]["receivers"].append(clone)
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert sum("distance" in row for row in data["floor"][0]["receivers"]) == 1


def test_bounded_diagnostics_reports_bridge_truncation(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    original = payloads["entry"]["devices"]["synthetic-tracker"]
    payloads["entry"]["devices"] = {f"synthetic-device-{i}": copy.deepcopy(original) for i in range(300)}
    bridge = BermudaDirect(hass)
    assert bridge.snapshots["entry"]["bridge_omitted"]["devices"] == 44
    assert bridge.diagnostics()["diagnostic_omitted"]["observations"] > 0


def test_untracked_device_crowding_cannot_displace_registered_tracked_beacon(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    tracked = payloads["entry"]["devices"]["synthetic-tracker"]
    crowd = {f"synthetic-untracked-{i}": {"device_id": f"synthetic-untracked-{i}",
        "tracked": False, "scanners": {}} for i in range(300)}
    crowd["synthetic-tracker"] = tracked
    def capped_getter(_hass, **kwargs):
        value = copy.deepcopy(payloads["entry"])
        selected = [(did, row) for did, row in crowd.items()
            if not kwargs["tracked_only"] or row["tracked"]]
        value["devices"] = dict(selected[:256])
        return value
    monkeypatch.setitem(sys.modules, "custom_components.bermuda.api", NS(async_get_advert_snapshot=capped_getter))
    sources = ReceiverSources(hass)
    assert sources.source("tag", data["floor"][0]["receivers"][0])[1].state == 6.


def test_many_pairs_debug_export_obeys_shared_byte_budget_with_recent_history(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    original = payloads["entry"]["devices"]["synthetic-tracker"]
    original["scanners"] = {f"aa:bb:cc:dd:ee:{i:02x}": measurement() for i in range(128)}
    payloads["entry"]["devices"] = {f"synthetic-device-{i}": copy.deepcopy(original) for i in range(40)}
    bridge = BermudaDirect(hass, include_history=True)
    report = bridge.diagnostics()
    assert report["diagnostic_omitted"]["observations"] > 0
    assert bridge.snapshots["entry"]["bridge_omitted"]["observations"] == 1024
    text = bps.diagnostics.format_report({"bermuda": {"direct": report}})
    assert len(text.encode()) < 1024 * 1024


def test_async_api_callback_mismatch_falls_back_without_leaking_coroutine(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    async def unexpected(*args, **kwargs): return payloads["entry"]
    monkeypatch.setitem(sys.modules, "custom_components.bermuda.api", NS(async_get_advert_snapshot=unexpected))
    sources = ReceiverSources(hass)
    assert sources.source("tag", data["floor"][0]["receivers"][0]) == (new, states[new])


def test_scanner_specific_capture_failure_uses_legacy_calibration_without_disabling_tracking(hass, monkeypatch):
    data, payloads, cal = calibration_setup(hass, monkeypatch)
    def getter(_hass, **kwargs):
        if kwargs.get("addresses") is not None: raise RuntimeError("Synthetic scanner capture failure")
        return copy.deepcopy(payloads["entry"])
    monkeypatch.setitem(sys.modules, "custom_components.bermuda.api", NS(async_get_advert_snapshot=getter))
    async def dump(*args, **kwargs):
        return {MAC: {"_is_scanner": True, "address": MAC, "name": "Current station", "adverts": {
            OTHER: {"scanner_address": OTHER, "stamp": 498., "rssi_distance_raw": 7.}}},
            OTHER: {"_is_scanner": True, "address": OTHER, "name": "Other station", "adverts": {}}}
    hass.services.async_call = dump
    rows = asyncio.run(C._dump_devices(hass))
    assert rows[MAC]["adverts"][OTHER]["rssi_distance_raw"] == 7.


def test_beacon_debug_reads_direct_measurement_and_deduplicates_legacy_aliases(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    rows = bps._beacon_links(hass)
    assert rows == [{"device": "tag", "receivers": [{"scanner": "old_station", "distance": 6., "unit": "m"}]}]
    payloads["entry"]["devices"]["synthetic-tracker"]["scanners"][MAC].update(status="stale", filtered_distance=None)
    assert bps._beacon_links(hass) == [{"device": "tag", "receivers": []}]


@pytest.mark.parametrize("alias", ["both_disabled", "current_only", "renamed_current"])
def test_beacon_debug_enumerates_disabled_direct_anchors_without_ha_states(hass, monkeypatch, alias):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    states.clear()
    for entry in entries.values(): entry.disabled_by = "user"
    displayed = "old_station"
    if alias == "current_only":
        del entries[old]
        displayed = "current_station"
    elif alias == "renamed_current":
        entry = entries.pop(new)
        entry.entity_id = "sensor.synthetic_renamed_distance"
        entries[entry.entity_id] = entry
    before = len(calls)
    assert bps._beacon_links(hass) == [{"device": "tag", "receivers": [
        {"scanner": displayed, "distance": 6., "unit": "m"}]}]
    assert len(calls) == before + 1
    # A covered stale pair retains the proved beacon, with no live receivers.
    payloads["entry"]["devices"]["synthetic-tracker"]["scanners"][MAC].update(status="stale", filtered_distance=None)
    assert bps._beacon_links(hass) == [{"device": "tag", "receivers": []}]
    payloads["entry"]["devices"]["synthetic-tracker"]["scanners"].clear()
    assert bps._beacon_links(hass) == [{"device": "tag", "receivers": []}]


def test_beacon_debug_disabled_anchors_do_not_create_unsupported_legacy_readings(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    for entry in entries.values(): entry.disabled_by = "user"
    states.clear()
    payloads["entry"] = {"version": 99}
    assert bps._beacon_links(hass) == []
    # The same unsupported API retains the enabled legacy source and display.
    entries[new].disabled_by = None
    states[new] = NS(entity_id=new, state="4", attributes={"unit_of_measurement": "m"}, last_updated=timestamp(998.))
    assert bps._beacon_links(hass) == [{"device": "tag", "receivers": [
        {"scanner": "current_station", "distance": 4., "unit": "m"}]}]


def test_beacon_debug_disabled_direct_aliases_preserve_multi_entry_ambiguity(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    configs = hass.config_entries.async_entries("bermuda") + [NS(entry_id="second", state="loaded")]
    hass.config_entries.async_entries = lambda _domain: configs
    payloads["second"] = snapshot(entry="second")
    add("sensor.other_distance_to_old_station", None)
    add("sensor.other_distance_to_current_station", None)
    for eid, entry in entries.items():
        entry.disabled_by = "user"
        if eid.startswith("sensor.other_"): entry.config_entry_id = "second"
    states.clear()
    assert bps._beacon_links(hass) == [{"device": "other", "receivers": []}, {"device": "tag", "receivers": []}]


def _tick_with_retained_sensors(hass, monkeypatch, data):
    """Run the real tick and SensorEntity state writer for one bounded cycle."""
    hass.data["bps"]["layout"] = data
    previous = {"ent": "tag", "floor": "Ground", "zone": "synthetic_previous_zone",
        "cords": [10, 20], "updated": 998.}
    bps.apitricords[:] = [previous]
    hass.data["bps"]["apitricords"] = bps.apitricords
    sensors, writes, sleeps = {}, [], []
    for suffix, value in (("zone", "synthetic_previous_zone"), ("nearest_zone", "synthetic_previous_zone"),
                          ("floor", "Ground"), ("sub_zone", "unknown")):
        eid = f"sensor.tag_bps_{suffix}"
        sensor = NS(_state=value, _attrs={})
        sensor.async_write_ha_state = lambda eid=eid, sensor=sensor: writes.append((eid, sensor._state))
        sensors[eid] = sensor
    hass.data["bps_sensors"] = sensors
    monkeypatch.setattr(bps, "new_global_data", {})
    for name in ("last_liveness", "last_selftest", "last_history_flush"):
        monkeypatch.setattr(bps.update_tracked_entities, name, 1000., raising=False)
    async def stop_after_tick(delay):
        sleeps.append(delay)
        raise asyncio.CancelledError
    monkeypatch.setattr(bps.asyncio, "sleep", stop_after_tick)
    async def run():
        with pytest.raises(asyncio.CancelledError):
            await bps.update_tracked_entities(hass)
    asyncio.run(run())
    return previous, sensors, writes, sleeps


@pytest.mark.parametrize("reading_count", [0, 1, 2])
def test_indoor_tick_processes_proved_direct_tracker_after_adverts_clear_or_become_insufficient(hass, monkeypatch, reading_count):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    states.clear()
    for entry in entries.values(): entry.disabled_by = "user"
    ads = payloads["entry"]["devices"]["synthetic-tracker"]["scanners"]
    if reading_count == 0:
        ads.clear()  # Bermuda can deliberately clear iBeacon adverts on UUID change.
    elif reading_count == 2:
        payloads["entry"]["scanners"][OTHER] = scanner(OTHER, name="Other station")
        ads[OTHER] = measurement(OTHER)
        data["floor"][0]["receivers"].append({"entity_id": "other_station", "cords": {"x": 100, "y": 20}})
        add("sensor.tag_distance_to_other_station", None, uid=f"synthetic-tracker_{OTHER}_range")
        entries["sensor.tag_distance_to_other_station"].disabled_by = "user"
    previous, sensors, writes, sleeps = _tick_with_retained_sensors(hass, monkeypatch, data)
    expected = [("sensor.tag_bps_nearest_zone", "unknown")] if reading_count == 0 else []
    assert writes == expected
    assert sensors["sensor.tag_bps_nearest_zone"]._state == ("unknown" if reading_count == 0 else "synthetic_previous_zone")
    assert sensors["sensor.tag_bps_zone"]._state == "synthetic_previous_zone"
    assert sensors["sensor.tag_bps_floor"]._state == "Ground"
    assert bps.apitricords == [previous] and previous["updated"] == 998.
    assert [subject["entity"] for subject in bps.new_global_data] == ["tag"]
    assert sum("distance" in receiver for receiver in bps.new_global_data[0]["data"]["floor"][0]["receivers"]) == reading_count
    assert sleeps == [bps.secToUpdate] and len(calls) == 1


@pytest.mark.parametrize("legacy_count", [0, 1, 2])
@pytest.mark.parametrize("api", ["unproved", "unsupported"])
def test_indoor_tick_keeps_legacy_admission_without_proved_direct_tracker(hass, monkeypatch, legacy_count, api):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    for eid in list(states)[legacy_count:]: states.pop(eid)
    if api == "unproved":
        # Several unrelated API observations must not supply admission for the
        # legacy tracker merely because the API device happens to share a name.
        payloads["entry"] = snapshot(did="different-synthetic-device")
        ads = payloads["entry"]["devices"]["different-synthetic-device"]["scanners"]
        for i in range(4): ads[f"aa:bb:cc:dd:ee:{i:02x}"] = measurement()
    else:
        payloads["entry"] = {"version": 99}
    previous, sensors, writes, sleeps = _tick_with_retained_sensors(hass, monkeypatch, data)
    assert writes == [] and sleeps == [10]
    assert bps.new_global_data == {}
    assert sensors["sensor.tag_bps_nearest_zone"]._state == "synthetic_previous_zone"
    assert bps.apitricords == [previous]
