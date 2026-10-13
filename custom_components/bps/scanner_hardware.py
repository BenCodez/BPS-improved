"""Allowlisted hardware descriptions, independent of the ranging model."""
from collections.abc import Mapping

from homeassistant.helpers import device_registry as dr

from .bermuda_direct import detached
from .scanner_addresses import ble_mac, retained_mac

TEXT_FIELDS = ("manufacturer", "model", "hardware_type")
PROVENANCE = ("home_assistant_device_registry", "esphome", "unknown")


def hardware_metadata(value):
    """Detach only documented descriptions; antenna presence is never guessed."""
    value = value if isinstance(value, Mapping) else {}
    result = {"detected": {}, "effective": {}, "provenance": "unknown"}
    for section in ("detected", "effective"):
        fields = value.get(section)
        if not isinstance(fields, Mapping):
            continue
        result[section] = {key: detached(fields[key]) for key in TEXT_FIELDS
                           if isinstance(fields.get(key), str)}
    effective = value.get("effective")
    if isinstance(effective, Mapping) and type(effective.get("external_antenna")) is bool:
        result["effective"]["external_antenna"] = effective["external_antenna"]
    if value.get("provenance") in PROVENANCE:
        result["provenance"] = value["provenance"]
    return result


def legacy_hardware(hass, scanner):
    """Older Bermuda: require an exact registry connection to the physical MAC.

    A scanner entry ID or friendly name alone cannot establish a physical match.
    Deliberately do not repeat Bermuda's historical MAC-offset heuristics.
    """
    getter = getattr(dr, "async_get", None)
    registry = getter(hass) if getter else None
    devices = getattr(registry, "devices", {})
    if not isinstance(devices, Mapping):
        return hardware_metadata(None)
    binding = getattr(scanner, "scanner_binding", None)
    if isinstance(binding, Mapping) and binding.get("status") != "resolved":
        return hardware_metadata(None)
    identifier = binding.get("device_registry_id") if isinstance(binding, Mapping) else getattr(scanner, "entry_id", None)
    device = devices.get(identifier) if isinstance(identifier, str) else None
    if device is None:
        return hardware_metadata(None)
    address, network = ble_mac(scanner), retained_mac(scanner)
    connections = getattr(device, "connections", ()) or ()
    exact_ble = ("bluetooth", address) in {(kind, mac.lower()) for kind, mac in connections if isinstance(mac, str)}
    exact_network = ("mac", network) in {(kind, mac.lower()) for kind, mac in connections if isinstance(mac, str)}
    entry_ids = (getattr(device, "config_entry_id", None),) if getattr(device, "config_entry_id", None) else getattr(device, "config_entries", ()) or ()
    manager = getattr(hass, "config_entries", None)
    entry_getter = getattr(manager, "async_get_entry", None)
    physical_provider = any(getattr(entry_getter(key), "domain", None) in ("esphome", "shelly") for key in entry_ids) if entry_getter else False
    if not exact_ble and not (exact_network and physical_provider):
        return hardware_metadata(None)
    fields = {key: getattr(device, key, None) for key in ("manufacturer", "model")}
    fields = {key: item for key, item in fields.items() if isinstance(item, str) and item}
    return hardware_metadata({"detected": fields, "effective": fields,
                              "provenance": "home_assistant_device_registry"})
