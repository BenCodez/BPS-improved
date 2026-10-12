"""Retained range identity stays distinct from real BLE observation addresses."""
import asyncio
from types import SimpleNamespace as NS

import pytest

import bps
from bps import calibration as C
from bps import bermuda_calibration as B
from test_calibration_alias_lifecycle import fixture


@pytest.mark.parametrize("saved_mac", ["none", "retained", "ble"])
def test_real_ingestion_and_runtime_model_use_proven_retained_to_ble_relationship(hass, monkeypatch, saved_mac):
    data, retained, devices = fixture(hass, monkeypatch)
    ble = {slug: f"dd:ee:ff:90:80:{index:02x}" for index, slug in enumerate(devices)}
    for slug, device in devices.items():
        device.address_wifi_mac = device.address
        device.address = ble[slug]
    for rec in data["floor"][0]["receivers"]:
        slug = rec["entity_id"]
        if saved_mac != "none":
            rec.update(scanner_mac=retained[slug] if saved_mac == "retained" else ble[slug], scanner_config_entry_id="entry")
    hass.data["bps"]["layout"] = data
    cal = C.get_calibration_state(hass)
    C._set_auto_receivers(hass, cal, data)
    geometry = cal["receivers"]
    assert len(geometry) == 4
    assert all(row["scanner_mac"] == retained[slug] for slug, row in geometry.items())
    payload = {ble[slug]: {"_is_scanner": True, "address": ble[slug], "address_wifi_mac": retained[slug],
                          "name": devices[slug].name, "adverts": {}}
               for slug in devices}
    for tx in geometry:
        for rx in geometry:
            if tx == rx:
                continue
            advert = {"scanner_address": ble[rx], "stamp": 100,
                      "rssi_distance_raw": C._true_distance_m(cal, tx, rx),
                      "conf_attenuation": 2., "conf_rssi_offset": -7., "conf_ref_power": -60., "ref_power": -60.}
            payload[ble[tx]]["adverts"][ble[rx]] = advert
            devices[tx].adverts[ble[rx]] = NS(**advert)
    for stamp in range(100, 100 + C.MIN_SAMPLES_PER_PAIR):
        for row in payload.values():
            for advert in row["adverts"].values():
                advert["stamp"] = stamp
        C._ingest_dump(cal, payload)
    assert set(cal["samples"]) == {f"{tx}|{rx}" for tx in geometry for rx in geometry if tx != rx}
    assert all(len(values) == C.MIN_SAMPLES_PER_PAIR for values in cal["samples"].values())
    assert set(C._match_scanners(cal, payload)) == {ble[slug] for slug in geometry}
    source = cal["_bermuda_source"]["receivers"]
    for slug, row in source.items():
        assert row["scanner_mac"] == ble[slug]
        assert row["geometry"]["scanner_mac"] == retained[slug]
        assert row["rssi_offset_db"] == -7.
    coordinator = hass.config_entries.async_entries("bermuda")[0].runtime_data.coordinator
    coordinator.options = {"attenuation": 2., "ref_power": -60.,
                           "rssi_offsets": {**{mac: -25. for mac in retained.values()}, **{mac: -7. for mac in ble.values()}}}
    runtime = B.runtime_context(hass, data, "Ground")["receivers"]
    assert set(runtime) == set(geometry)
    for slug, row in runtime.items():
        assert row["scanner_mac"] == ble[slug] and row["geometry"]["scanner_mac"] == retained[slug]
        assert row["entry_id"] == "entry" and row["rssi_offset_db"] == -7.
    devices["shared_old"].address_wifi_mac = "invalid-declared-identity"
    assert B.runtime_context(hass, data, "Ground")["receivers"].get("shared_old", {}).get("scanner_mac") is None


@pytest.mark.parametrize("age", [0, 99])
def test_dump_liveness_age_is_keyed_by_retained_entity_identity(hass, monkeypatch, age):
    data, retained, devices = fixture(hass, monkeypatch)
    hass.data["bps"]["layout"] = data
    for index, (slug, device) in enumerate(devices.items()):
        device.address_wifi_mac = device.address
        device.address = f"dd:ee:ff:90:80:{index:02x}"
    target = devices["shared_old"]
    payload = {target.address: {"_is_scanner": True, "address": target.address,
               "address_wifi_mac": retained["shared_old"], "name": target.name, "last_seen": 100 - age},
               "clock": {"last_seen": 100}}
    async def dump(*args, **kwargs):
        return payload
    hass.services.async_call = dump
    asyncio.run(bps.update_receiver_liveness(hass))
    dom = hass.data["bps"]
    assert dom["rl_mac_ages"] == {retained["shared_old"]: age}
    assert ("shared_old" in dom["rl_offline"]) is (age == 99)
    assert "shared_old" not in dom["rl_unknown"]
