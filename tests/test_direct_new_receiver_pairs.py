"""A proven tracker can use a new receiver without a per-pair HA entity."""
import asyncio
from types import SimpleNamespace as NS

import pytest

import bps
from bps.receiver_sources import ReceiverSources
from test_bermuda_direct import OTHER, measurement, prepared, scanner, snapshot


def new_pair(hass, monkeypatch, *, disabled=False):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    if disabled:
        states.clear()
        for entry in entries.values():
            entry.disabled_by = "user"
    station = scanner(OTHER, name="New station")
    station["device_registry_id"] = "synthetic-new-proxy"
    payloads["entry"]["scanners"][OTHER] = station
    reading = measurement(OTHER)
    reading["device_registry_id"] = station["device_registry_id"]
    payloads["entry"]["devices"]["synthetic-tracker"]["scanners"][OTHER] = reading
    receiver = {"entity_id": "new_station", "scanner_mac": OTHER,
        "scanner_config_entry_id": "entry", "cords": {"x": 100, "y": 20}}
    data["floor"][0]["receivers"].append(receiver)
    return data, entries, add, old, new, payloads, calls, receiver


@pytest.mark.parametrize("disabled", [False, True])
def test_current_direct_pair_uses_tracker_anchor_from_another_receiver(hass, monkeypatch, disabled):
    data, entries, add, old, new, payloads, calls, receiver = new_pair(hass, monkeypatch, disabled=disabled)
    sources = ReceiverSources(hass)
    eid = "sensor.tag_distance_to_new_station"
    assert eid not in entries
    assert sources.binding(receiver)["status"] == "matched"
    assert sources.direct_trackers() == {"tag"}
    selected, state = sources.source("tag", receiver)
    assert selected == eid and state.state == 6.0
    assert state.attributes["bps_source"]["path"] == "direct"
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}, sources))
    assert receiver["distance"] == 6.0
    assert len(calls) == 1


@pytest.mark.parametrize("change", ["remove", "owner", "identity", "new_exact"])
def test_new_receiver_cannot_reuse_replaced_tracker_or_conflicting_exact_entity(hass, monkeypatch, change):
    data, entries, add, old, new, payloads, calls, receiver = new_pair(hass, monkeypatch, disabled=True)
    sources = ReceiverSources(hass)
    assert sources.source("tag", receiver)[1].state == 6.0
    if change == "remove":
        del entries[old]
    elif change == "owner":
        entries[old].config_entry_id = "replacement-entry"
    elif change == "identity":
        entries[old].unique_id = "replacement-device_" + OTHER + "_range"
    else:
        add("sensor.tag_distance_to_new_station", None, uid="replacement-device_" + OTHER + "_range")
    assert sources.source("tag", receiver)[1] is None
    if change != "new_exact":
        assert sources.direct_trackers() == set()


def test_new_receiver_rejects_ambiguous_whole_tracker_identity(hass, monkeypatch):
    data, entries, add, old, new, payloads, calls, receiver = new_pair(hass, monkeypatch)
    add("sensor.tag_distance_to_other_anchor", None, uid="different-device_" + OTHER + "_range")
    payloads["entry"]["devices"].update(snapshot(did="different-device")["devices"])
    sources = ReceiverSources(hass)
    assert sources.binding(receiver)["status"] == "matched"
    assert sources.direct_trackers() == set()
    assert sources.source("tag", receiver)[1] is None


def test_new_receiver_does_not_borrow_tracker_proof_from_another_entry(hass, monkeypatch):
    data, entries, add, old, new, payloads, calls, receiver = new_pair(hass, monkeypatch)
    configs = hass.config_entries.async_entries("bermuda") + [NS(entry_id="second", state="loaded")]
    hass.config_entries.async_entries = lambda _domain: configs
    payloads["second"] = snapshot(entry="second", sid=OTHER)
    receiver["scanner_config_entry_id"] = "second"
    sources = ReceiverSources(hass)
    assert sources.source("tag", receiver)[1] is None
