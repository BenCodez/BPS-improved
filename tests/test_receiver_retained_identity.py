"""Synthetic Bermuda BLE observations and retained range entity identities."""
import copy
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from bps.receiver_matching import inventory
from bps.receiver_sources import ReceiverSources


RETAINED = "aa:bb:cc:11:22:30"
BLE = "aa:bb:cc:11:22:32"
OTHER = "dd:ee:ff:44:55:60"


def setup(hass, monkeypatch, *, wifi=RETAINED, bluetooth=BLE, name="Scanner One"):
    import bps.receiver_matching as matching
    entries, devices, states = {}, {}, {}
    registry = NS(entities=entries)
    monkeypatch.setattr(matching.er, "async_get", lambda _hass: registry, raising=False)
    monkeypatch.setattr(matching, "slugify", lambda value: value.lower().replace(" ", "_"))
    scanner = NS(_is_scanner=True, address=bluetooth, address_wifi_mac=wifi, name=name)
    devices["scanner"] = scanner
    entry = NS(entry_id="owner", runtime_data=NS(coordinator=NS(devices=devices)))
    hass.config_entries = NS(async_entries=lambda _domain: [entry])
    hass.states = NS(get=states.get)

    def add(slug="scanner_one", *, mac=RETAINED, owner="owner", disabled=None):
        entity_id = "sensor.synthetic_tag_distance_to_" + slug
        entries[entity_id] = NS(entity_id=entity_id, platform="bermuda", config_entry_id=owner,
                                unique_id="synthetic-tag_" + mac + "_range", disabled_by=disabled)
        states[entity_id] = NS(state="3", last_updated=datetime.fromtimestamp(100, timezone.utc))
        return entity_id

    return entries, devices, states, scanner, entry, add


def test_inventory_joins_ble_runtime_to_retained_range_entities(hass, monkeypatch):
    entries, devices, states, scanner, entry, add = setup(hass, monkeypatch, wifi=RETAINED.upper(), bluetooth=BLE.upper())
    eid = add()
    add("previous_scanner", disabled="user")
    entries["sensor.renamed_range"] = NS(entity_id="sensor.renamed_range", platform="bermuda",
        config_entry_id="owner", unique_id="synthetic-tag_" + RETAINED + "_range", disabled_by=None)
    result = inventory(hass)
    options = {option["entity_id"]: option for option in result["options"]}
    assert set(options) == {"scanner_one", "previous_scanner"}
    current = options["scanner_one"]
    assert current["name"] == "Scanner One"
    assert current["scanner_mac"] == RETAINED
    assert current["config_entry_ids"] == ["owner"]
    assert current["aliases"] == ["previous_scanner", "scanner_one"]
    assert current["distance_sensors"] == ["sensor.renamed_range", eid]
    assert current["selectable"] and not current["identity_ambiguous"]
    assert result["mac_owners"] == {RETAINED: ["owner"]}
    assert result["runtime_address_map"] == [{"config_entry_id": "owner", "scanner_mac": RETAINED, "scanner_ble_mac": BLE}]


def test_mapping_revision_changes_without_changing_retained_options(hass, monkeypatch):
    entries, devices, states, scanner, entry, add = setup(hass, monkeypatch)
    add()
    before = inventory(hass)
    scanner.address = "aa:bb:cc:11:22:34"
    after = inventory(hass)
    assert before["options"] == after["options"]
    assert before["revision"] != after["revision"]
    assert before["runtime_address_map"][0]["scanner_ble_mac"] == BLE


@pytest.mark.parametrize("wifi", [None, ""])
def test_no_retained_wifi_identity_preserves_ble_identity(hass, monkeypatch, wifi):
    setup(hass, monkeypatch, wifi=wifi)
    result = inventory(hass)
    assert result["options"][0]["scanner_mac"] == BLE
    assert result["runtime_address_map"][0]["scanner_mac"] == BLE


@pytest.mark.parametrize("wifi", ["112230", "invalid", 123])
def test_invalid_declared_retained_identity_cannot_fall_back_to_ble(hass, monkeypatch, wifi):
    setup(hass, monkeypatch, wifi=wifi)
    result = inventory(hass)
    assert result["options"] == [] and result["runtime_address_map"] == []


def test_invalid_ble_does_not_invalidate_proven_retained_identity(hass, monkeypatch):
    setup(hass, monkeypatch, bluetooth="invalid")
    result = inventory(hass)
    assert result["options"][0]["scanner_mac"] == RETAINED
    assert result["runtime_address_map"] == []


@pytest.mark.parametrize("slug", ["scanner_one", "removed_slug"])
@pytest.mark.parametrize("owner", [None, "owner"])
def test_saved_ble_binding_uses_filtered_retained_source_without_rewriting(hass, monkeypatch, slug, owner):
    entries, devices, states, scanner, entry, add = setup(hass, monkeypatch)
    eid = add()
    receiver = {"entity_id": slug, "scanner_mac": BLE.upper(), "custom": "preserved"}
    if owner is not None:
        receiver["scanner_config_entry_id"] = owner
    before = copy.deepcopy(receiver)
    resolver = ReceiverSources(hass)
    binding = resolver.binding(receiver)
    assert binding == {"identity": ("owner", RETAINED), "status": "matched", "aliases": ["scanner_one"]}
    assert resolver.source("synthetic_tag", receiver, binding, now=100) == (eid, states[eid])
    assert receiver == before
    assert resolver.binding({"entity_id": slug, "scanner_mac": RETAINED})["identity"] == ("owner", RETAINED)


@pytest.mark.parametrize("same_owner", [False, True])
def test_conflicting_ble_mappings_fail_closed(hass, monkeypatch, same_owner):
    entries, devices, states, scanner, first, add = setup(hass, monkeypatch)
    add()
    second_owner = "owner" if same_owner else "second-owner"
    other_devices = {"scanner": NS(_is_scanner=True, address=BLE, address_wifi_mac=OTHER, name="Scanner Two")}
    if same_owner:
        devices.update({"other": other_devices["scanner"]})
    else:
        second = NS(entry_id=second_owner, runtime_data=NS(coordinator=NS(devices=other_devices)))
        hass.config_entries.async_entries = lambda _domain: [first, second]
    add("scanner_two", mac=OTHER, owner=second_owner)
    resolver = ReceiverSources(hass)
    assert resolver.binding({"entity_id": "scanner_one", "scanner_mac": BLE})["status"] == (
        "scanner_identity_conflict" if same_owner else "scanner_identity_unavailable")
    scoped = resolver.binding({"entity_id": "scanner_one", "scanner_mac": BLE, "scanner_config_entry_id": "owner"})
    assert scoped["status"] == ("scanner_identity_conflict" if same_owner else "matched")
    assert resolver.binding({"entity_id": "scanner_two", "scanner_mac": BLE, "scanner_config_entry_id": "owner"})["status"] == "scanner_identity_conflict"


def test_retained_address_colliding_with_another_ble_address_is_conflict(hass, monkeypatch):
    entries, devices, states, scanner, first, add = setup(hass, monkeypatch)
    add()
    devices["other"] = NS(_is_scanner=True, address=RETAINED, address_wifi_mac=OTHER, name="Scanner Two")
    add("scanner_two", mac=OTHER)
    resolver = ReceiverSources(hass)
    assert resolver.binding({"entity_id": "scanner_one", "scanner_mac": RETAINED, "scanner_config_entry_id": "owner"})["status"] == "scanner_identity_conflict"


def test_wrong_owner_and_unproven_mac_cannot_use_retained_alias(hass, monkeypatch):
    entries, devices, states, scanner, entry, add = setup(hass, monkeypatch)
    add()
    resolver = ReceiverSources(hass)
    assert resolver.binding({"entity_id": "scanner_one", "scanner_mac": BLE, "scanner_config_entry_id": "foreign-owner"})["status"] == "scanner_identity_unavailable"
    assert resolver.binding({"entity_id": "scanner_one", "scanner_mac": "aa:bb:cc:11:22:31"})["status"] == "scanner_identity_conflict"


def test_unnamed_runtime_owner_still_blocks_ownerless_ble_inference(hass, monkeypatch):
    entries, devices, states, scanner, first, add = setup(hass, monkeypatch)
    add()
    second = NS(entry_id="second-owner", runtime_data=NS(coordinator=NS(devices={
        "scanner": NS(_is_scanner=True, address=BLE, address_wifi_mac=OTHER, name=None)})))
    hass.config_entries.async_entries = lambda _domain: [first, second]
    resolver = ReceiverSources(hass)
    assert len(resolver.matching["runtime_address_map"]) == 2
    assert resolver.binding({"entity_id": "scanner_one", "scanner_mac": BLE})["status"] == "scanner_identity_unavailable"


def test_runtime_ble_mapping_is_indexed_once_per_snapshot(hass, monkeypatch):
    import bps.receiver_sources as sources

    class CountingRows(list):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    entries, devices, states, scanner, entry, add = setup(hass, monkeypatch)
    add()
    matching = inventory(hass)
    rows = CountingRows(matching["runtime_address_map"])
    matching["runtime_address_map"] = rows
    monkeypatch.setattr(sources, "inventory", lambda _hass: matching)
    resolver = ReceiverSources(hass)
    for _ in range(100):
        assert resolver.binding({"entity_id": "scanner_one", "scanner_mac": BLE})["identity"] == ("owner", RETAINED)
    assert rows.iterations == 1
