"""Real Bermuda schema, registry ownership, secret boundaries and fallback."""
import datetime
from enum import Enum
import json
import sys
from types import SimpleNamespace as NS

import bps
from bps import bermuda_diagnostics as module


class ConfigEntryState(Enum):
    LOADED = "loaded"
    SETUP_RETRY = "setup_retry"
    SETUP_ERROR = "setup_error"


class RegistryEntryDisabler(Enum):
    INTEGRATION = "integration"


def fixture(hass, monkeypatch):
    secret = "private-identity-resolving-key"
    entries = {}
    states = {}
    def add(eid, uid, platform="bermuda", value="4", disabled=None):
        entries[eid] = NS(entity_id=eid, unique_id=uid, platform=platform, device_id="tag-device",
                          config_entry_id="bermuda-entry", original_name=eid, disabled_by=disabled)
        if value is not None:
            states[eid] = NS(entity_id=eid, state=value, attributes={"unit_of_measurement": "m", "secret": secret},
                            last_updated=datetime.datetime.fromtimestamp(100, datetime.timezone.utc))
    add("sensor.tag_distance_to_proxy", secret + "_aa:bb:cc:dd:ee:ff_range")
    add("sensor.renamed_raw_range", secret + "_aa:bb:cc:dd:ee:ff_range_raw", value=None, disabled=RegistryEntryDisabler.INTEGRATION)
    add("number.renamed_calibration", secret + "_ref_power", value="-70")
    add("sensor.tag_rssi", secret + "_rssi", value="-80")
    add("sensor.unrelated_distance_to_proxy", "fake_range", platform="esphome")
    add("sensor.group_zone", "bps_group_dog_zone", platform="bps", value="Barn")
    states["sensor.group_zone"].attributes.update(tracker_key="bps_group_dog", floor="Home")
    registry = NS(entities=entries, async_get=entries.get)
    monkeypatch.setattr(module.er, "async_get", lambda _h: registry, raising=False)
    hass.states = NS(get=states.get, async_all=lambda _domain: [s for s in states.values() if s.entity_id.startswith("sensor.")])
    advert = NS(scanner_address="aa:bb:cc:dd:ee:ff", rssi=-80, tx_power=-59, rssi_distance=4,
                rssi_distance_raw=8, ref_power=-70, conf_ref_power=-60, conf_attenuation=2.1,
                conf_rssi_offset=-3, conf_max_velocity=2, conf_smoothing_samples=30, stamp=490,
                manufacturer_data={"secret": secret})
    device = NS(unique_id=secret, adverts={"secret-address": advert}, irk=secret)
    options = {"ref_power": -60, "attenuation": 2.1, "smoothing_samples": 30, "max_velocity": 2,
               "update_interval": 3, "rssi_offsets": {"aa:bb:cc:dd:ee:ff": -3, secret: 9},
               "configured_devices": [secret], "token": secret}
    coordinator = NS(options=options, devices={secret: device}, sensor_interval=3, last_update_success=True)
    entry = NS(entry_id="bermuda-entry", title="Bermuda", state=ConfigEntryState.LOADED, options=options,
               data={"token": secret}, runtime_data=NS(coordinator=coordinator))
    hass.config_entries = NS(async_entries=lambda domain: [entry] if domain == "bermuda" else [])
    monkeypatch.setattr(module.time, "monotonic", lambda: 500)
    return entry, coordinator, entries, states, secret


def test_settings_entities_and_per_receiver_rssi_are_detached_and_secret_free(hass, monkeypatch):
    entry, coordinator, entries, states, secret = fixture(hass, monkeypatch)
    report = module.snapshot(hass, 110)
    assert report["available"]
    settings = report["entries"][0]
    assert settings["state"] == "loaded"
    assert settings["configured_settings"]["ref_power"] == -60
    assert settings["effective_settings"]["attenuation"] == 2.1
    assert settings["sensor_update_interval_s"] == 3
    assert settings["configured_settings"]["rssi_offsets"] == {"aa:bb:cc:dd:ee:ff": -3}
    by_id = {r["entity_id"]: r for r in report["entities"]}
    assert "sensor.unrelated_distance_to_proxy" not in by_id and "sensor.group_zone" not in by_id
    assert by_id["sensor.renamed_raw_range"]["kind"] == "unfiltered_distance"
    assert by_id["sensor.renamed_raw_range"]["disabled_by"] == "integration"
    assert by_id["sensor.renamed_raw_range"]["state"] is None
    assert by_id["number.renamed_calibration"]["bps_tracker_keys"] == ["tag"]
    assert by_id["sensor.tag_distance_to_proxy"]["age_s"] == 10
    assert report["adverts"][0]["observation_age_s"] == 10
    assert report["adverts"][0]["rssi_distance_raw"] == 8
    assert secret not in json.dumps(report)
    coordinator.options["ref_power"] = -40
    states["number.renamed_calibration"].state = "-50"
    assert settings["effective_settings"]["ref_power"] == -60
    assert by_id["number.renamed_calibration"]["state"] == "-70"


def test_absent_runtime_keeps_config_and_states_and_reports_missing_rssi(hass, monkeypatch):
    entry, coordinator, entries, states, secret = fixture(hass, monkeypatch)
    entry.runtime_data = None
    report = module.snapshot(hass, 110)
    assert not report["entries"][0]["runtime_available"]
    assert report["entities"] and report["adverts"] == []
    assert report["warnings"]
    hass.data["bermuda"] = {entry.entry_id: {"coordinator": coordinator}}
    assert module.snapshot(hass, 110)["entries"][0]["runtime_available"]


def test_config_entry_lifecycle_states_survive_serialization(hass, monkeypatch):
    entry, *_ = fixture(hass, monkeypatch)
    entry.runtime_data = None
    for state in ConfigEntryState:
        entry.state = state
        report = module.snapshot(hass, 110)
        assert report["entries"][0]["state"] == state.value
        assert not report["entries"][0]["runtime_available"]
        json.dumps(report, allow_nan=False)
    entry.state = "loaded"
    assert module.snapshot(hass, 110)["entries"][0]["state"] == "loaded"


def test_no_bermuda_and_collection_limits_are_explicit(hass, monkeypatch):
    entry, coordinator, entries, states, secret = fixture(hass, monkeypatch)
    monkeypatch.setattr(module, "MAX_ADVERTS", 1)
    ad = next(iter(coordinator.devices.values())).adverts
    ad["second"] = next(iter(ad.values()))
    report = module.snapshot(hass, 110)
    assert len(report["adverts"]) == 1 and report["adverts_limit_reached"]
    monkeypatch.setattr(module, "MAX_ENTITIES", 2)
    report = module.snapshot(hass, 110)
    assert report["entity_counts"] == {"total": 4, "included": 2, "truncated": True}
    entries.clear()
    hass.config_entries.async_entries = lambda _d: []
    assert not module.snapshot(hass, 110)["available"]


def test_installed_defaults_and_invalid_numeric_telemetry_are_reported_without_guessing(hass, monkeypatch):
    entry, coordinator, entries, states, secret = fixture(hass, monkeypatch)
    monkeypatch.setitem(sys.modules, "custom_components.bermuda.const", NS(DEFAULT_REF_POWER=-59, DEFAULT_ATTENUATION=4))
    advert = next(iter(next(iter(coordinator.devices.values())).adverts.values()))
    advert.rssi_distance_raw = float("nan")
    advert.stamp = 999
    report = module.snapshot(hass, 110)
    assert report["installed_defaults"] == {"ref_power": -59, "attenuation": 4}
    assert report["adverts"][0]["rssi_distance_raw"] is None
    assert report["adverts"][0]["observation_age_s"] is None
    json.dumps(report, allow_nan=False)


def test_bps_registry_states_link_groups_without_exporting_unrelated_attributes(hass, monkeypatch):
    fixture(hass, monkeypatch)
    rows, _, counts = module.entity_snapshot(hass, "bps", 110)
    assert counts["total"] == 1 and rows[0]["state"] == "Barn"
    assert rows[0]["attributes"] == {"tracker_key": "bps_group_dog", "floor": "Home"}


def test_full_bps_report_includes_bermuda_and_ages_old_fixes_without_mutating_them(hass, monkeypatch):
    fixture(hass, monkeypatch)
    quality = {"observed": 50, "stale_after_s": 30, "position_age_s": 0, "stale": False}
    hass.data["bps"] = {"layout": {"floor": [], "history_min_move_m": 1},
        "apitricords": [{"ent": "tag", "updated": 100, "outdoor": quality}]}
    report = bps._diagnostic_report_snapshot(hass, 110)
    assert report["bermuda"]["entries"][0]["configured_settings"]["smoothing_samples"] == 30
    assert report["bps_entities"][0]["attributes"]["tracker_key"] == "bps_group_dog"
    assert report["positions"][0]["outdoor"]["position_age_s"] == 60
    assert report["positions"][0]["outdoor"]["stale"] is True
    assert quality["position_age_s"] == 0 and quality["stale"] is False
    assert report["runtime"]["effective_history_settings"]["min_move_m"] == 1
    assert not hass._store_saves
