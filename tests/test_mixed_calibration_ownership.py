"""Mixed calibration service rows require positive, current owner/source proof."""
import asyncio
import copy
from types import SimpleNamespace as NS

import pytest

import bps
from bps import calibration as C
from test_bermuda_direct import MAC, OTHER, calibration_setup

LEGACY_A = "aa:bb:cc:77:88:01"
LEGACY_B = "aa:bb:cc:77:88:02"
LEGACY_BLE = "dd:ee:ff:77:88:01"
UNKNOWN = "aa:bb:cc:77:88:03"


def row(address, name, adverts=None, retained=None):
    return {"_is_scanner": True, "address": address, "address_wifi_mac": retained or address,
            "name": name, "adverts": adverts or {}}


def advert(receiver, distance=7.):
    return {"scanner_address": receiver, "stamp": 498., "rssi_distance_raw": distance,
            "conf_attenuation": 3., "conf_rssi_offset": 0., "conf_ref_power": -59., "ref_power": -59.}


def mixed(hass, monkeypatch):
    data, payloads, cal = calibration_setup(hass, monkeypatch)
    legacy = NS(entry_id="legacy", state="loaded", runtime_data=None)
    configs = hass.config_entries.async_entries("bermuda") + [legacy]
    hass.config_entries.async_entries = lambda _: configs
    entries = bps.er.async_get(hass).entities
    for index, address in enumerate((LEGACY_A, LEGACY_B)):
        eid = f"sensor.synthetic_renamed_legacy_{index}"
        entries[eid] = NS(entity_id=eid, platform="bermuda", config_entry_id="legacy",
                          disabled_by="user", unique_id=f"synthetic-legacy_{address}_range")
        cal["receivers"][f"legacy_{index}"] = {
            "scanner_mac": address, "scanner_config_entry_id": "legacy", "scanner_owner_unique": True,
            "strict_identity": True, "x": index * 100., "y": 50., "scale": 10., "floor": "Ground"}
    service = {LEGACY_A: row(LEGACY_A, "Legacy A", {LEGACY_B: advert(LEGACY_B)}),
               LEGACY_B: row(LEGACY_B, "Legacy B", {LEGACY_A: advert(LEGACY_A, 8.)})}
    async def dump(*args, **kwargs):
        return service
    hass.services.async_call = dump
    return payloads, cal, legacy, entries, service


@pytest.mark.parametrize("missing", ["missing", "truncated"])
def test_missing_direct_scanner_cannot_be_revived_by_service_or_legacy_advert(hass, monkeypatch, missing):
    payloads, cal, legacy, entries, service = mixed(hass, monkeypatch)
    del payloads["entry"]["scanners"][OTHER]
    payloads["entry"]["omitted"]["scanners"] = int(missing == "truncated")
    # The direct scanner is still owned by its disabled/renamed registry anchor.
    service[OTHER] = row(OTHER, "Other station", {MAC: advert(MAC, 999.)})
    service[UNKNOWN] = row(UNKNOWN, "Current station", {MAC: advert(MAC, 999.)})
    service[LEGACY_A]["adverts"].update({OTHER: advert(OTHER, 999.), MAC: advert(MAC, 999.),
                                         UNKNOWN: advert(UNKNOWN, 999.)})
    original = copy.deepcopy(service)
    rows = asyncio.run(C._dump_devices(hass))
    assert set(rows) == {MAC, LEGACY_A, LEGACY_B}
    assert rows[LEGACY_A]["_bps_config_entry_id"] == "legacy"
    assert set(rows[LEGACY_A]["adverts"]) == {LEGACY_B}
    C._ingest_dump(cal, rows)
    assert {key: list(values) for key, values in cal["samples"].items()} == {
        "legacy_0|legacy_1": [7.], "legacy_1|legacy_0": [8.]}
    assert service == original


@pytest.mark.parametrize("collision", ["registry_owner", "direct_retained_alias", "duplicate_source"])
def test_ambiguous_or_direct_owned_receiver_cannot_contribute_legacy_calibration(hass, monkeypatch, collision):
    payloads, cal, legacy, entries, service = mixed(hass, monkeypatch)
    if collision == "registry_owner":
        entries["sensor.conflicting_legacy_anchor"] = NS(entity_id="sensor.conflicting_legacy_anchor",
            platform="bermuda", config_entry_id="other-owner", unique_id=f"synthetic-other_{LEGACY_B}_range")
    elif collision == "direct_retained_alias":
        payloads["entry"]["scanners"][OTHER].update(entity_id_address=LEGACY_B, address_wifi_mac=LEGACY_B)
    else:
        devices = {
            "one": NS(_is_scanner=True, address=LEGACY_B, address_wifi_mac=LEGACY_B, name="One", adverts={}),
            "two": NS(_is_scanner=True, address=LEGACY_B, address_wifi_mac=UNKNOWN, name="Two", adverts={})}
        legacy.runtime_data = NS(coordinator=NS(devices=devices))
    rows = asyncio.run(C._dump_devices(hass))
    assert LEGACY_B not in rows
    assert rows[LEGACY_A]["adverts"] == {}
    C._ingest_dump(cal, rows)
    assert not any("legacy_" in key for key in cal["samples"])


@pytest.mark.parametrize("proof", ["runtime", "registry_only"])
def test_different_ble_and_retained_macs_need_explicit_current_pair(hass, monkeypatch, proof):
    payloads, cal, legacy, entries, service = mixed(hass, monkeypatch)
    del service[LEGACY_A]
    service[LEGACY_BLE] = row(LEGACY_BLE, "Legacy A", {LEGACY_B: advert(LEGACY_B)}, retained=LEGACY_A)
    service[LEGACY_B]["adverts"] = {LEGACY_BLE: advert(LEGACY_BLE, 8.)}
    if proof == "runtime":
        device = NS(_is_scanner=True, address=LEGACY_BLE, address_wifi_mac=LEGACY_A, name="Legacy A",
                    adverts={LEGACY_B: NS(**advert(LEGACY_B))})
        legacy.runtime_data = NS(coordinator=NS(devices={LEGACY_BLE: device}))
    rows = asyncio.run(C._dump_devices(hass))
    C._ingest_dump(cal, rows)
    if proof == "runtime":
        assert rows[LEGACY_BLE]["address_wifi_mac"] == LEGACY_A
        assert {key: list(values) for key, values in cal["samples"].items() if "legacy_" in key} == {
            "legacy_0|legacy_1": [7.], "legacy_1|legacy_0": [8.]}
    else:
        assert LEGACY_BLE not in rows and rows[LEGACY_B]["adverts"] == {}
        assert not any("legacy_" in key for key in cal["samples"])


def test_service_failure_keeps_owned_runtime_pair_and_filters_direct_receivers(hass, monkeypatch):
    payloads, cal, legacy, entries, service = mixed(hass, monkeypatch)
    devices = {address: NS(_is_scanner=True, address=address, name=value["name"],
                           adverts={key: NS(**ad) for key, ad in value["adverts"].items()})
               for address, value in service.items()}
    devices[LEGACY_A].adverts[MAC] = NS(**advert(MAC, 999.))
    legacy.runtime_data = NS(coordinator=NS(devices=devices))
    async def failed(*args, **kwargs):
        raise RuntimeError("Synthetic service failure")
    hass.services.async_call = failed
    rows = asyncio.run(C._dump_devices(hass))
    assert set(rows) == {MAC, OTHER, LEGACY_A, LEGACY_B}
    assert len(rows[LEGACY_A]["adverts"]) == 1
    C._ingest_dump(cal, rows)
    assert {key: list(values) for key, values in cal["samples"].items() if "legacy_" in key} == {
        "legacy_0|legacy_1": [7.], "legacy_1|legacy_0": [8.]}


def test_service_owner_marker_cannot_replace_or_override_current_proof(hass, monkeypatch):
    payloads, cal, legacy, entries, service = mixed(hass, monkeypatch)
    service[UNKNOWN] = {**row(UNKNOWN, "Legacy"), "_bps_config_entry_id": "legacy"}
    service[LEGACY_A]["_bps_config_entry_id"] = "other-owner"
    rows = asyncio.run(C._dump_devices(hass))
    assert UNKNOWN not in rows and LEGACY_A not in rows
    assert rows[LEGACY_B]["_bps_config_entry_id"] == "legacy"


def test_fully_legacy_service_response_is_returned_unchanged_without_owner_proof(hass, monkeypatch):
    payloads, cal, legacy, entries, service = mixed(hass, monkeypatch)
    payloads["entry"] = {"version": 99}
    entries.clear()
    service[UNKNOWN] = row(UNKNOWN, "Unknown", {MAC: advert(MAC)})
    service["legacy_scalar"] = 42
    assert asyncio.run(C._dump_devices(hass)) is service
