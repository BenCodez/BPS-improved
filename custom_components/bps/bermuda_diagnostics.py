"""Read-only, bounded Bermuda telemetry. Never exports arbitrary entry data."""
from collections.abc import Mapping
from enum import Enum
import math
import re
import sys
import time

from homeassistant.helpers import entity_registry as er

MAX_ENTITIES = 4096
MAX_ADVERTS = 2048
SETTING_DEFAULTS = {
    "ref_power": "DEFAULT_REF_POWER", "attenuation": "DEFAULT_ATTENUATION",
    "smoothing_samples": "DEFAULT_SMOOTHING_SAMPLES", "max_velocity": "DEFAULT_MAX_VELOCITY",
    "update_interval": "DEFAULT_UPDATE_INTERVAL", "max_area_radius": "DEFAULT_MAX_RADIUS",
    "devtracker_nothome_timeout": "DEFAULT_DEVTRACK_TIMEOUT",
}
ATTRIBUTES = ("unit_of_measurement", "device_class", "state_class", "area_id", "area_name",
              "area_scanner_name", "area_scanner_mac", "rssi", "tx_power", "ref_power",
              "attenuation", "min", "max", "step", "mode")
BPS_ATTRIBUTES = ("tracker_key", "floor", "zone", "nearest_zone", "group", "name", "x", "y",
                  "beacons_reporting", "total_beacons", "beacon_disagreement_m", "fusion_confidence")
MAC = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")


def scalar(value):
    if isinstance(value, Enum):
        value = value.value
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            return value if math.isfinite(value) else None
        except OverflowError:
            return None
    return value[:256] if isinstance(value, str) else None


def timestamp(value):
    try:
        return scalar(value.timestamp())
    except (AttributeError, ValueError, TypeError, OSError, OverflowError):
        return None


def settings(values):
    values = values if isinstance(values, Mapping) else {}
    result = {key: scalar(values[key]) for key in SETTING_DEFAULTS if key in values}
    offsets = values.get("rssi_offsets")
    if isinstance(offsets, Mapping):
        # Scanner MACs are relevant to offsets; IRKs and arbitrary nested data
        # are not. Only the documented numeric scanner-offset map is exported.
        result["rssi_offsets"] = {key: scalar(value) for key, value in list(offsets.items())[:MAX_ENTITIES]
            if isinstance(key, str) and MAC.fullmatch(key) and isinstance(value, (int, float)) and not isinstance(value, bool)}
        if len(offsets) > MAX_ENTITIES:
            result["rssi_offsets_truncated"] = True
    for key in ("configured_devices", "configured_scanners"):
        if isinstance(values.get(key), (list, tuple)):
            result[key + "_count"] = len(values[key])
    return result


def entity_snapshot(hass, platform, now):
    """Registry ownership handles renamed and disabled diagnostic entities."""
    registry = er.async_get(hass)
    owned = [entry for entry in registry.entities.values() if entry.platform == platform]
    owned.sort(key=lambda entry: entry.entity_id)
    rows, tracker_devices, runtime_ids = [], {}, {}
    for entry in owned[:MAX_ENTITIES]:
        state = hass.states.get(entry.entity_id)
        unique = getattr(entry, "unique_id", "") or ""
        kind = ("unfiltered_distance" if unique.endswith("_range_raw") else
                "filtered_distance" if unique.endswith("_range") else
                "reference_power" if unique.endswith("_ref_power") else
                "rssi" if unique.endswith("_rssi") else "other")
        device_id = getattr(entry, "device_id", None)
        row = {"entity_id": entry.entity_id, "device_id": device_id,
               "config_entry_id": getattr(entry, "config_entry_id", None), "kind": kind,
               "name": scalar(getattr(entry, "name", None) or getattr(entry, "original_name", None)),
               "disabled_by": scalar(getattr(entry, "disabled_by", None)), "state": None}
        # Use unique IDs for internal linkage only: private BLE device IDs can
        # contain an identity-resolving key. Never put them in the report.
        if kind == "reference_power":
            runtime_ids[(getattr(entry, "config_entry_id", None), unique[:-len("_ref_power")])] = device_id
        if state is not None:
            updated = timestamp(getattr(state, "last_updated", None))
            row.update(state=scalar(state.state), updated=updated,
                       age_s=max(0.0, now - updated) if updated is not None else None,
                       attributes={key: scalar(state.attributes[key]) for key in
                           (BPS_ATTRIBUTES if platform == "bps" else ATTRIBUTES) if key in state.attributes})
        if kind != "unfiltered_distance" and entry.entity_id.startswith("sensor.") and "_distance_to_" in entry.entity_id:
            key = entry.entity_id[len("sensor."):].split("_distance_to_", 1)[0]
            if device_id:
                tracker_devices.setdefault(device_id, set()).add(key)
        rows.append(row)
    for row in rows:
        row["bps_tracker_keys"] = sorted(tracker_devices.get(row["device_id"], ()))
    return rows, runtime_ids, {"total": len(owned), "included": len(rows), "truncated": len(owned) > MAX_ENTITIES}


def _coordinator(hass, entry):
    runtime = getattr(entry, "runtime_data", None)
    current = getattr(runtime, "coordinator", None)
    if current is not None:
        return current
    # Older Bermuda releases stored the coordinator in hass.data.
    bucket = hass.data.get("bermuda", {})
    legacy = bucket.get(entry.entry_id) if isinstance(bucket, Mapping) else None
    return legacy.get("coordinator") if isinstance(legacy, Mapping) else legacy


def settings_snapshot(hass):
    """Cheap allowlisted settings context, without registry/state/advert scans."""
    manager = getattr(hass, "config_entries", None)
    entries = manager.async_entries("bermuda") if manager is not None else []
    const = sys.modules.get("custom_components.bermuda.const")
    rows = []
    for entry in entries[:32]:
        coordinator = _coordinator(hass, entry)
        rows.append({"entry_id": entry.entry_id, "configured_settings": settings(entry.options),
                     "effective_settings": settings(getattr(coordinator, "options", {}))})
    return {"entries": rows, "entries_truncated": len(entries) > 32,
            "installed_defaults": {key: scalar(getattr(const, attr))
                for key, attr in SETTING_DEFAULTS.items() if const is not None and hasattr(const, attr)}}


def snapshot(hass, now):
    """Public HA states/config plus optional, allowlisted Bermuda runtime data."""
    result = {"available": False, "entries": [], "entities": [], "adverts": [], "warnings": [],
              "guide": "HA state ages are publication ages. Advert ages use Bermuda's monotonic observation clock. Runtime distances are metres; RSSI/reference power are dBm and RSSI offsets are dB. Per-device ref_power 0 uses Bermuda's global default. BPS receiver corrections and tracker trims are additional adjustments. Settings and telemetry are read only; missing runtime fields are not inferred."}
    try:
        rows, runtime_ids, counts = entity_snapshot(hass, "bermuda", now)
        result.update(entities=rows, entity_counts=counts)
    except (AttributeError, KeyError, TypeError):
        runtime_ids = {}
        result["warnings"].append("Bermuda entity registry is unavailable")
    manager = getattr(hass, "config_entries", None)
    entries = manager.async_entries("bermuda") if manager is not None else []
    const = sys.modules.get("custom_components.bermuda.const")
    result["installed_defaults"] = {key: scalar(getattr(const, attr))
        for key, attr in SETTING_DEFAULTS.items() if const is not None and hasattr(const, attr)}
    loader = sys.modules.get("homeassistant.loader")
    lookup = getattr(loader, "async_get_loaded_integration", None)
    result["version"] = None
    if callable(lookup):
        try:
            result["version"] = scalar(getattr(lookup(hass, "bermuda"), "version", None))
        except (KeyError, AttributeError):
            pass
    mono = time.monotonic()
    for entry in entries:
        coordinator = _coordinator(hass, entry)
        row = {"entry_id": entry.entry_id, "title": scalar(entry.title),
               "state": scalar(getattr(entry, "state", None)),
               "configured_settings": settings(entry.options),
               "runtime_available": coordinator is not None}
        result["entries"].append(row)
        if coordinator is None:
            continue
        row.update(effective_settings=settings(getattr(coordinator, "options", {})),
                   sensor_update_interval_s=scalar(getattr(coordinator, "sensor_interval", None)),
                   last_update_success=scalar(getattr(coordinator, "last_update_success", None)))
        devices = getattr(coordinator, "devices", {})
        if not isinstance(devices, Mapping):
            continue
        # A loaded coordinator is not proof that live scanner models are
        # available. Separate that failure from registry-backed distance data.
        row["runtime_scanner_counts"] = {
            "devices": len(devices),
            "scanners": sum(getattr(device, "_is_scanner", None) is True for device in devices.values()),
            "missing_scanner_flag": sum(getattr(device, "_is_scanner", None) is None for device in devices.values()),
        }
        for device in devices.values():
            if len(result["adverts"]) >= MAX_ADVERTS:
                result["adverts_limit_reached"] = True
                break
            uid = getattr(device, "unique_id", None)
            if not isinstance(uid, str):
                continue
            identity = (entry.entry_id, uid)
            if identity not in runtime_ids:
                continue
            adverts = getattr(device, "adverts", {})
            if not isinstance(adverts, Mapping):
                continue
            for advert in adverts.values():
                if len(result["adverts"]) >= MAX_ADVERTS:
                    result["adverts_limit_reached"] = True
                    break
                ad = {"entry_id": entry.entry_id, "device_id": runtime_ids[identity]}
                for key in ("rssi", "tx_power", "rssi_distance", "rssi_distance_raw", "ref_power",
                            "conf_ref_power", "conf_attenuation", "conf_rssi_offset", "conf_max_velocity",
                            "conf_smoothing_samples", "stale_update_count"):
                    if hasattr(advert, key):
                        ad[key] = scalar(getattr(advert, key))
                address = getattr(advert, "scanner_address", None)
                ad["scanner_mac"] = address if isinstance(address, str) and MAC.fullmatch(address) else None
                stamp = scalar(getattr(advert, "stamp", None))
                ad["observation_age_s"] = mono - stamp if isinstance(stamp, (int, float)) and not isinstance(stamp, bool) and 0 < stamp <= mono else None
                result["adverts"].append(ad)
    result["available"] = bool(entries or result["entities"])
    if result["available"] and not result["adverts"]:
        result["warnings"].append("Per-receiver RSSI runtime data is unavailable; enabled Bermuda entities are still included")
    return result
