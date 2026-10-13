"""Read-only, bounded Bermuda telemetry. Never exports arbitrary entry data."""
from collections.abc import Mapping
from enum import Enum
import hashlib
import json
import math
import re
import sys
import time

from homeassistant.helpers import entity_registry as er

MAX_ENTITIES = 4096
MAX_ADVERTS = 2048
MAX_PROFILE_METADATA_BYTES = 128 * 1024
PROFILE_TEXT_FIELDS = ("hardware_type", "manufacturer", "model", "antenna", "environment", "notes")
PROFILE_ENVIRONMENT_VALUES = ("indoor", "outdoor", "unknown")
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


def settings(values, *, compact=False, metadata_budget=None):
    values = values if isinstance(values, Mapping) else {}
    if not compact and metadata_budget is not None:
        view_quota = metadata_budget["remaining"] // max(1, metadata_budget.get("views_remaining", 1))
        metadata_budget["views_remaining"] = max(0, metadata_budget.get("views_remaining", 1) - 1)
    else:
        view_quota = MAX_PROFILE_METADATA_BYTES
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
    toolkit = values.get("measurement_toolkit")
    if isinstance(toolkit, Mapping):
        from .bermuda_direct import detached, public_id
        allowed = {key: scalar(toolkit[key]) for key in ("auto_calibration", "fingerprint_area_selection",
            "history_enabled", "history_window_s", "reliability_weighting") if key in toolkit}
        profile_fields = ("hardware_type", "manufacturer", "model", "antenna", "environment", "notes",
            "reference_power", "attenuation", "rssi_offset", "height_m", "antenna_gain_dbi", "antenna_gain_db",
            "distance_accuracy_m", "reference_power_verified", "external_antenna")
        if compact:
            profile_fields = ("reference_power", "attenuation", "rssi_offset", "reference_power_verified")
        if not compact and metadata_budget is None:
            metadata_budget = {"remaining": MAX_PROFILE_METADATA_BYTES}
        omitted, omitted_fields, metadata_bytes = {}, 0, 0
        signature = hashlib.sha256()
        for namespace in ("receiver_profiles", "beacon_profiles", "scanner_bindings", "scanner_identity_recovery"):
            rows = toolkit.get(namespace)
            if isinstance(rows, Mapping):
                keys = ("device_registry_id",) if namespace == "scanner_bindings" else (
                    ("entity_id",) if namespace == "scanner_identity_recovery" else profile_fields)
                allowed[namespace] = {}
                for identity, row in sorted((identity, row) for identity, row in list(rows.items())[:128]
                                           if isinstance(identity, str) and isinstance(row, Mapping)):
                    identity = public_id(identity)
                    profile = {key: detached(row[key]) for key in keys if key in row}
                    if not compact and namespace in ("receiver_profiles", "beacon_profiles"):
                        # Sign the allowlisted complete profile, including omitted
                        # metadata. Numeric fields and identity always remain.
                        signature.update(json.dumps([namespace, identity, profile], sort_keys=True,
                            ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode())
                        # Keep the bounded radio-context enum, while charging
                        # free-form environment text from other/older sources.
                        metadata = {key: profile[key] for key in PROFILE_TEXT_FIELDS if key in profile
                            and not (key == "environment" and profile[key] in PROFILE_ENVIRONMENT_VALUES)}
                        size = len(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode()) if metadata else 0
                        if size <= min(metadata_budget["remaining"], view_quota - metadata_bytes):
                            metadata_budget["remaining"] -= size
                            metadata_bytes += size
                        else:
                            omitted[namespace] = omitted.get(namespace, 0) + 1
                            omitted_fields += len(metadata)
                            profile = {key: value for key, value in profile.items() if key not in metadata}
                    allowed[namespace][identity] = profile
        if omitted:
            allowed["profile_metadata_export"] = {"budget_bytes": MAX_PROFILE_METADATA_BYTES,
                "view_budget_bytes": view_quota, "included_bytes": metadata_bytes,
                "omitted_profiles": omitted, "omitted_fields": omitted_fields,
                "signature": signature.hexdigest(), "guide": "Rich metadata shares a UTF-8 byte budget across report entries and configured/effective settings. Omitted text fields are excluded whole; profile identities and numeric fields remain."}
        for namespace in ("filters", "receiver_limits"):
            rows = toolkit.get(namespace)
            if isinstance(rows, Mapping):
                allowed[namespace] = {public_id(identity): detached(value) for identity, value in list(rows.items())[:128]
                    if isinstance(identity, str) and isinstance(value, (int, float, str)) and not isinstance(value, bool)}
        result["measurement_toolkit"] = allowed
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


def toolkit_snapshot(coordinator):
    """Read stored health/summary fields; never invoke analysis or area election."""
    from .bermuda_direct import detached, fields
    manager = getattr(coordinator, "toolkit", None)
    if manager is None:
        return {"available": False, "reason": "toolkit_runtime_unavailable"}
    result = {"available": True, "read_only": True,
              "model_version": detached(getattr(manager, "_version_cache", None)),
              "closed": getattr(manager, "_closed", None),
              "worker_busy": getattr(manager, "_worker", None) is not None}
    perf = getattr(manager, "_perf", None)
    result["performance"] = fields(perf, ("updates", "last_update_s", "max_update_s", "background_skipped",
        "observations", "scanners")) if isinstance(perf, Mapping) else {"available": False}
    states = getattr(manager, "_states", None)
    result["filter_states"] = len(states) if isinstance(states, Mapping) else None
    status_fields = ("status", "eligible", "reasons", "model_version", "created_at", "latest_observed",
        "applied_at", "automatic", "rolled_back", "offsets", "deltas", "improvement_m", "metrics",
        "applied", "apply", "rollback", "accepted", "sufficient", "mode", "rejected", "coverage",
        "target_groups", "data_id", "step_scale", "requires_step_validation", "step_metrics", "proposal_id")
    result["calibration"] = {key: fields(value, status_fields) if isinstance(value, Mapping) else None
        for key in ("proposal", "last_result", "trial") for value in (getattr(manager, key, None),)}
    history = getattr(manager, "history", None)
    result["history"] = ({"available": False} if history is None else {
        key: detached(getattr(history, attr, None)) for key, attr in (
            ("running", "enabled"), ("session_epoch", "session_epoch"), ("rows", "_row_count"),
            ("bytes", "_byte_count"), ("dropped_rows", "dropped_rows"), ("window_s", "window_s"),
            ("max_frames", "max_frames"), ("max_rows", "max_rows"), ("max_bytes", "max_bytes"))})
    frames = getattr(history, "_frames", None)
    if isinstance(frames, (list, tuple)):
        result["history"]["frames"] = len(frames)
    shadow = getattr(manager, "shadow", None)
    result["shadow"] = {"state": "absent"} if shadow is None else {
        key: detached(getattr(shadow, key, None)) for key in (
            "state", "cancel_reason", "model_version", "signature", "min_labelled_rows", "min_locations")}
    return result


def settings_snapshot(hass):
    """Bound recurring radio context; descriptive profiles belong in full debug."""
    manager = getattr(hass, "config_entries", None)
    entries = manager.async_entries("bermuda") if manager is not None else []
    const = sys.modules.get("custom_components.bermuda.const")
    rows = []
    budget = 64 * 1024
    for entry in entries[:32]:
        coordinator = _coordinator(hass, entry)
        row = {"entry_id": entry.entry_id,
               "model_version": scalar(getattr(getattr(coordinator, "toolkit", None), "_version_cache", None))}
        for key, values in (("configured_settings", entry.options),
                            ("effective_settings", getattr(coordinator, "options", {}))):
            radio = settings(values, compact=True)
            encoded = json.dumps(radio, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            row[key + "_signature"] = hashlib.sha256(encoded).hexdigest()
            if len(encoded) <= budget:
                row[key] = radio
                budget -= len(encoded)
                continue
            # Retain scalar controls and bounded map counts. The radio signature
            # still changes for a model setting omitted from the numeric view.
            row[key] = {field: value for field, value in radio.items() if not isinstance(value, Mapping)}
            row[key]["maps_compacted"] = True
            row[key]["map_counts"] = {field: len(value) for field, value in radio.items() if isinstance(value, Mapping)}
            toolkit = radio.get("measurement_toolkit")
            if isinstance(toolkit, Mapping):
                row[key]["measurement_toolkit"] = {field: value for field, value in toolkit.items()
                    if not isinstance(value, Mapping)}
                row[key]["measurement_toolkit"]["map_counts"] = {field: len(value) for field, value in toolkit.items()
                    if isinstance(value, Mapping)}
        rows.append(row)
    return {"entries": rows, "entries_truncated": len(entries) > 32,
            "installed_defaults": {key: scalar(getattr(const, attr))
                for key, attr in SETTING_DEFAULTS.items() if const is not None and hasattr(const, attr)}}


def snapshot(hass, now):
    """Public HA states/config plus optional, allowlisted Bermuda runtime data."""
    result = {"available": False, "entries": [], "entities": [], "adverts": [], "warnings": [],
              "guide": "HA state ages are publication ages. Advert ages use Bermuda's monotonic observation clock. Runtime distances are metres; RSSI/reference power are dBm and RSSI offsets are dB. Per-device ref_power 0 uses Bermuda's global default. BPS receiver corrections and tracker trims are additional adjustments. Settings and telemetry are read only; missing runtime fields are not inferred."}
    from .bermuda_direct import BermudaDirect
    direct = BermudaDirect(hass, now=now, include_history=True, include_scanner_devices=True)
    result["direct"] = direct.diagnostics()
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
    metadata_budget = {"remaining": MAX_PROFILE_METADATA_BYTES,
        "views_remaining": sum(1 + (_coordinator(hass, entry) is not None) for entry in entries[:32])}
    for entry in entries[:32]:
        coordinator = _coordinator(hass, entry)
        row = {"entry_id": entry.entry_id, "title": scalar(entry.title),
               "state": scalar(getattr(entry, "state", None)),
               "configured_settings": settings(entry.options, metadata_budget=metadata_budget),
               "runtime_available": coordinator is not None, "toolkit": toolkit_snapshot(coordinator)}
        result["entries"].append(row)
        if coordinator is None:
            continue
        row.update(effective_settings=settings(getattr(coordinator, "options", {}), metadata_budget=metadata_budget),
                   sensor_update_interval_s=scalar(getattr(coordinator, "sensor_interval", None)),
                   last_update_success=scalar(getattr(coordinator, "last_update_success", None)))
        if entry.entry_id in direct.snapshots:
            row["measurement_path"] = "direct"
            continue
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
        def address_evidence(value):
            valid = isinstance(value, str) and MAC.fullmatch(value) is not None
            return {"value": value.lower() if valid else None, "valid": valid,
                    "type": type(value).__name__, "length": len(value) if isinstance(value, str) else None,
                    "colon_count": value.count(":") if isinstance(value, str) else None,
                    "hyphen_count": value.count("-") if isinstance(value, str) else None}
        scanners = [device for device in devices.values() if getattr(device, "_is_scanner", None) is True]
        row["scanner_identity_validation"] = [{
            "name": scalar(getattr(device, "name", None)),
            "source_address": address_evidence(getattr(device, "address", None)),
            "ble_address": address_evidence(getattr(device, "address_ble_mac", None)),
            "wifi_address": address_evidence(getattr(device, "address_wifi_mac", None)),
        } for device in scanners[:128]]
        row["scanner_identity_validation_truncated"] = len(scanners) > 128
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
    if result["available"] and not result["adverts"] and not direct.snapshots:
        result["warnings"].append("Per-receiver RSSI runtime data is unavailable; enabled Bermuda entities are still included")
    return result
