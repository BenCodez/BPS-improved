"""Saved hardware proof disambiguates names without borrowing another scanner."""
import asyncio
import copy
import json
from datetime import datetime, timezone

import pytest

import bps
from bps.calibration import _build_receiver_map
from bps.receiver_matching import inventory
from bps.receiver_sources import ReceiverSources
from test_receiver_matching import configure, MAC

OTHER = "dd:ee:ff:44:55:66"


def shared(hass, monkeypatch, *, same_mac=False, same_owner=False):
    entries, states, devices, add = configure(hass, monkeypatch)
    first = "sensor.tag_distance_to_shared"
    second = "sensor.other_distance_to_shared"
    add(first, "3")
    second_mac = MAC if same_mac else OTHER
    add(second, "9", uid="synthetic-tracker_" + second_mac + "_range")
    if not same_owner:
        entries[second].config_entry_id = "other-owner"
    for state in states.values():
        state.last_updated = datetime.fromtimestamp(998, timezone.utc)
    monkeypatch.setattr(bps.time, "time", lambda: 1000.)
    rec = {"entity_id": "shared", "scanner_mac": MAC, "scanner_config_entry_id": "entry",
           "height": 1, "cords": {"x": 10, "y": 20}, "custom": "preserve"}
    layout = {"floor": [{"name": "Ground", "scale": 10, "receivers": [rec]}]}
    hass.data["bps"]["layout"] = layout
    return entries, states, rec, layout, first, second


@pytest.mark.parametrize("same_mac,same_owner", [(False, False), (True, False), (False, True)])
def test_saved_owner_mac_selects_underlying_ambiguous_inventory_identity(hass, monkeypatch, same_mac, same_owner):
    entries, states, rec, layout, first, second = shared(hass, monkeypatch, same_mac=same_mac, same_owner=same_owner)
    before = copy.deepcopy(layout)
    option = inventory(hass)["options"][0]
    assert option["identity_ambiguous"] and not option["selectable"]
    assert len(option["scanner_identities"]) == 2
    resolver = ReceiverSources(hass)
    binding = resolver.binding(rec)
    assert binding == {"identity": ("entry", MAC), "status": "matched", "aliases": ["shared"]}
    assert resolver.source("tag", rec, binding, now=1000) == (first, states[first])
    assert resolver.source("other", rec, binding, now=1000)[1] is None
    assert resolver.binding({"entity_id": "shared"})["status"] == "ambiguous_scanner_identity"
    geometry = _build_receiver_map(layout, hass=hass)
    assert geometry["shared"]["scanner_mac"] == MAC
    assert geometry["shared"]["scanner_config_entry_id"] == "entry"
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": layout}))
    assert rec["distance"] == 3
    linked = bps._scanner_linking(hass, json.dumps(layout))["placed"][0]
    assert linked["binding_status"] == "matched" and linked["selected_sources"] == {"tag": first}
    assert [row["entity_id"] for row in linked["sensors"]] == [first]
    assert linked["matching"]["scanner_mac"] == MAC
    assert linked["matching"]["config_entry_ids"] == ["entry"]
    for field in ("entity_id", "scanner_mac", "scanner_config_entry_id", "custom", "height"):
        assert rec[field] == before["floor"][0]["receivers"][0][field]
    assert not hass._store_saves


@pytest.mark.parametrize("proof", ["wrong_owner", "wrong_mac", "ownerless_shared_mac", "invalid_mac"])
def test_ambiguous_slug_does_not_override_missing_or_conflicting_proof(hass, monkeypatch, proof):
    entries, states, rec, layout, first, second = shared(hass, monkeypatch, same_mac=proof == "ownerless_shared_mac")
    if proof == "wrong_owner": rec["scanner_config_entry_id"] = "missing-owner"
    if proof == "wrong_mac": rec["scanner_mac"] = "aa:bb:cc:77:88:99"
    if proof == "ownerless_shared_mac": rec.pop("scanner_config_entry_id")
    if proof == "invalid_mac": rec["scanner_mac"] = "112233"
    sources = ReceiverSources(hass)
    assert sources.blocked(sources.binding(rec))
    assert _build_receiver_map(layout, hass=hass) == {}
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": layout}))
    assert "distance" not in rec


def test_liveness_uses_only_saved_scanner_sources_for_ambiguous_name(hass, monkeypatch):
    entries, states, rec, layout, first, second = shared(hass, monkeypatch, same_owner=True)
    asyncio.run(bps.update_receiver_liveness(hass))
    assert "shared" not in hass.data["bps"]["rl_unknown"]
    states[first].state = "unavailable"
    states["binary_sensor.shared_status"] = type(states[first])(state="on", attributes={"device_class": "connectivity"})
    asyncio.run(bps.update_receiver_liveness(hass))
    assert "shared" in hass.data["bps"]["rl_unknown"]
    assert "shared" not in hass.data["bps"]["rl_offline"]
    rec.pop("scanner_mac"); rec.pop("scanner_config_entry_id")
    states[first].state = "3"
    asyncio.run(bps.update_receiver_liveness(hass))
    assert "shared" in hass.data["bps"]["rl_unknown"]


@pytest.mark.parametrize("status", ["off", "on"])
def test_unique_alias_does_not_reintroduce_shared_connectivity_evidence(hass, monkeypatch, status):
    from types import SimpleNamespace as NS
    entries, states, rec, layout, first, second = shared(hass, monkeypatch, same_owner=True)
    alias = "sensor.tag_distance_to_a_current"
    entries[alias] = NS(**{**vars(entries[first]), "entity_id": alias})
    states[alias] = NS(entity_id=alias, state="unavailable", attributes={}, last_updated=states[first].last_updated)
    states[first].state = "unavailable"
    states["binary_sensor.shared_status"] = NS(state=status, attributes={"device_class": "connectivity"})
    asyncio.run(bps.update_receiver_liveness(hass))
    assert {"shared", "a_current"} <= set(hass.data["bps"]["rl_unknown"])
    assert hass.data["bps"]["rl_offline"] == []
    states["binary_sensor.a_current_status"] = NS(state=status, attributes={"device_class": "connectivity"})
    asyncio.run(bps.update_receiver_liveness(hass))
    assert "shared" not in hass.data["bps"]["rl_unknown"]
    assert ("shared" in hass.data["bps"]["rl_offline"]) is (status == "off")
