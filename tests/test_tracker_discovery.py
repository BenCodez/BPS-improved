"""API-only tracked devices receive stable, owner-scoped BPS tracker keys."""
import asyncio
import copy
import hashlib
from types import SimpleNamespace as NS

import bps
from bps.bermuda_direct import BermudaDirect
from bps.receiver_sources import ReceiverSources, synchronous_reader
from test_bermuda_direct import MAC, prepared, snapshot


def api_only(hass, monkeypatch, *, did="new-device", name="Friendly beacon", observations=True):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    entries.clear()
    states.clear()
    device = snapshot(did=did)["devices"][did]
    device.update(name=name, slug="reused_label")
    if not observations:
        device["scanners"] = {}
    payloads["entry"]["devices"] = {did: device}
    rec = data["floor"][0]["receivers"][0]
    rec.update(entity_id="current_station", scanner_mac=MAC, scanner_config_entry_id="entry")
    key = "bps_ble_" + hashlib.sha256(f"entry\0{did}".encode()).hexdigest()[:24]
    return data, entries, states, add, payloads, rec, key


def test_tracked_device_without_range_entity_is_a_choice_without_fabricated_position(hass, monkeypatch):
    data, entries, states, add, payloads, rec, key = api_only(hass, monkeypatch, observations=False)
    sources = ReceiverSources(hass)
    assert sources.direct_trackers() == {key}
    assert sources.fallback_trackers == {key: ("entry", "new-device")}
    assert sources.fallback_labels == {key: "Friendly beacon"}
    from bps.sensor import get_filtered_entities
    hass.states.async_all = lambda _domain=None: list(states.values())
    assert get_filtered_entities(hass) == [key]
    selected, state = sources.source(key, rec, now=1000.)
    assert selected == f"sensor.{key}_distance_to_current_station"
    assert state.state == "unavailable"
    assert state.attributes["bps_source"]["reason"] == "measurement_missing"
    asyncio.run(bps.update_receiver_radii(hass, {"entity": key, "data": data}, sources))
    assert "distance" not in rec


def test_fresh_measurement_tracks_and_old_accepted_packet_expires(hass, monkeypatch):
    data, entries, states, add, payloads, rec, key = api_only(hass, monkeypatch)
    sources = ReceiverSources(hass)
    selected, state = sources.source(key, rec, now=1000.)
    assert selected == f"sensor.{key}_distance_to_current_station"
    assert state.state == 6. and state.last_updated.timestamp() == 998.
    asyncio.run(bps.update_receiver_radii(hass, {"entity": key, "data": data}, sources))
    assert rec["distance"] == 12.
    data["reading_max_age"] = 10.
    ad = payloads["entry"]["devices"]["new-device"]["scanners"][MAC]
    ad["quality"].update(mode="pet", age=100.)
    stale = ReceiverSources(hass)
    asyncio.run(bps.update_receiver_radii(hass, {"entity": key, "data": data}, stale))
    assert "distance" not in rec


def test_key_uses_owner_and_canonical_id_never_name_or_slug(hass, monkeypatch):
    data, entries, states, add, payloads, rec, key = api_only(hass, monkeypatch)
    second = NS(entry_id="second", state="loaded")
    configs = hass.config_entries.async_entries("bermuda") + [second]
    hass.config_entries.async_entries = lambda _domain: configs
    payloads["second"] = snapshot(entry="second", did="new-device")
    payloads["second"]["devices"]["new-device"].update(name="Friendly beacon", slug="reused_label")
    first = ReceiverSources(hass)
    assert len(first.direct_trackers()) == 2
    other_key = next(k for k, owner_device in first.fallback_trackers.items() if owner_device[0] == "second")
    assert key != other_key and first.fallback_labels[other_key] == "Friendly beacon"
    payloads["entry"]["devices"]["new-device"].update(name="Renamed beacon", slug="renamed_beacon")
    renamed = ReceiverSources(hass)
    assert key in renamed.direct_trackers() and renamed.fallback_labels[key] == "Renamed beacon"
    replacement = snapshot(did="replacement-device")["devices"]["replacement-device"]
    replacement.update(name="Friendly beacon", slug="reused_label")
    payloads["entry"]["devices"] = {"replacement-device": replacement}
    changed = ReceiverSources(hass)
    assert key not in changed.direct_trackers()
    assert changed.fallback_labels != renamed.fallback_labels
    assert changed.fallback_trackers[next(k for k, value in changed.fallback_trackers.items()
        if value[0] == "entry")] == ("entry", "replacement-device")


def test_existing_disabled_and_conflicting_proofs_keep_legacy_keys(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    for ent in entries.values():
        ent.disabled_by = "user"
    states.clear()
    sources = ReceiverSources(hass)
    assert sources.direct_trackers() == {"tag"}
    assert sources.fallback_trackers == {}
    add("sensor.other_distance_to_current_station", None, uid=f"synthetic-tracker_{MAC}_range")
    conflicting = ReceiverSources(hass)
    assert conflicting.fallback_trackers == {}
    assert "tag" in conflicting.direct_trackers()


def test_untracked_ambiguous_and_forged_canonical_rows_are_not_discovered(hass, monkeypatch):
    data, entries, states, add, payloads, rec, key = api_only(hass, monkeypatch)
    row = payloads["entry"]["devices"]["new-device"]
    row["tracked"] = False
    assert ReceiverSources(hass).direct_trackers() == set()
    row["tracked"] = True
    row["device_id"] = "forged-id"
    assert ReceiverSources(hass).direct_trackers() == set()
    row["device_id"] = "new-device"
    other = copy.deepcopy(row)
    other.update(device_id="other-device", unique_id="new-device")
    payloads["entry"]["devices"]["other-device"] = other
    assert ReceiverSources(hass).direct_trackers() == set()


def test_fallback_rejects_cross_owner_and_forged_measurement(hass, monkeypatch):
    data, entries, states, add, payloads, rec, key = api_only(hass, monkeypatch)
    sources = ReceiverSources(hass)
    wrong = {**rec, "scanner_config_entry_id": "second"}
    assert sources.source(key, wrong)[1] is None
    ad = payloads["entry"]["devices"]["new-device"]["scanners"][MAC]
    ad["device_id"] = "forged-id"
    assert ReceiverSources(hass).source(key, rec)[1] is None
    ad["device_id"] = "new-device"
    add(f"sensor.{key}_distance_to_current_station", "2", uid=f"other-device_{MAC}_range")
    assert ReceiverSources(hass).direct_trackers() == set()
    assert sources.source(key, rec)[1] is None


def test_fallback_caches_detached_identity_resolution_but_rechecks_registry_changes(hass, monkeypatch):
    data, entries, states, add, payloads, rec, key = api_only(hass, monkeypatch)
    existing = snapshot(did="existing-device")["devices"]["existing-device"]
    payloads["entry"]["devices"]["existing-device"] = existing
    for index in range(100):
        add(f"sensor.existing_distance_to_station_{index}", "2", uid=f"existing-device_{MAC}_range")
    calls = []
    original = BermudaDirect.device

    def resolve(direct, owner, identity):
        calls.append((owner, identity))
        return original(direct, owner, identity)

    monkeypatch.setattr(BermudaDirect, "device", resolve)
    sources = ReceiverSources(hass)
    before = len(calls)
    for _ in range(100):
        assert sources.source(key, rec, now=1000.)[1].state == 6.
    assert len(calls) == before
    # A newly introduced conflicting key invalidates the captured fallback.
    add(f"sensor.{key}_distance_to_current_station", "2", uid=f"other-device_{MAC}_range")
    assert sources.source(key, rec, now=1000.)[1] is None
    assert key not in sources.direct_trackers()


def test_synchronous_read_pass_scans_registry_once_and_refreshes_after_yield(hass, monkeypatch):
    data, entries, states, add, payloads, rec, key = api_only(hass, monkeypatch)
    sources = ReceiverSources(hass)
    scans = []
    original = sources._registry_device_proofs

    def scan():
        scans.append(True)
        return original()

    monkeypatch.setattr(sources, "_registry_device_proofs", scan)
    read = synchronous_reader(sources)
    for _ in range(100):
        assert read(key, rec, now=1000.)[1].state == 6.
    assert len(scans) == 1

    async def next_pass():
        await asyncio.sleep(0)
        add(f"sensor.{key}_distance_to_current_station", "2", uid=f"other-device_{MAC}_range")
        # A caller must capture a new reader after yielding to HA.
        return synchronous_reader(sources)(key, rec, now=1000.)[1]

    assert asyncio.run(next_pass()) is None
    assert len(scans) == 2


def test_tracking_and_diagnostic_sweeps_do_not_scan_registry_per_reading(hass, monkeypatch):
    data, entries, states, add, payloads, rec, key = api_only(hass, monkeypatch)
    data["floor"][0]["receivers"] = [copy.deepcopy(rec) for _ in range(32)]
    hass.data["bps"]["layout"] = data
    scans = []
    original = ReceiverSources._registry_device_proofs

    def scan(sources):
        scans.append(True)
        return original(sources)

    monkeypatch.setattr(ReceiverSources, "_registry_device_proofs", scan)
    sources = ReceiverSources(hass)
    before = len(scans)
    asyncio.run(bps.update_receiver_radii(hass, {"entity": key, "data": data}, sources))
    assert len(scans) - before == 1
    before = len(scans)
    report = bps._diagnostic_snapshot(hass, None, 1000.)
    assert len(report["readings"]) == 32
    # Initial discovery, inventory validation and one synchronous reading pass.
    assert len(scans) - before <= 3


def test_issued_api_key_survives_new_range_entity_for_same_canonical_device(hass, monkeypatch):
    data, entries, states, add, payloads, rec, key = api_only(hass, monkeypatch)
    first = ReceiverSources(hass)
    assert first.direct_trackers() == {key}
    add("sensor.phone_distance_to_current_station", "2", uid=f"new-device_{MAC}_range")
    later = ReceiverSources(hass)
    assert later.direct_trackers() == {key, "phone"}
    assert first.source(key, rec, now=1000.)[1].state == 6.
    assert later.source(key, rec, now=1000.)[1].state == 6.
    assert later.fallback_trackers[key] == ("entry", "new-device")


def test_registered_or_saved_api_key_is_recovered_after_restart(hass, monkeypatch):
    for registry_proof in [True, False]:
        data, entries, states, add, payloads, rec, key = api_only(hass, monkeypatch)
        add("sensor.phone_distance_to_current_station", "2", uid=f"new-device_{MAC}_range")
        hass.data["bps"].pop("_api_tracker_keys", None)
        if registry_proof:
            add("sensor.renamed_bps_sensor", "Home", uid=f"bps_floor_{key}")
            entries["sensor.renamed_bps_sensor"].platform = "bps"
        else:
            data["tracker_groups"] = [{"id": "dog", "beacons": [key], "enabled": True}]
            hass.data["bps"]["layout"] = data
        sources = ReceiverSources(hass)
        assert key in sources.direct_trackers()
        assert sources.source(key, rec, now=1000.)[1].state == 6.
