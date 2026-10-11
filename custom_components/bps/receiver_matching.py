"""Read-only Bermuda scanner identities; never expose device unique IDs or options."""
import hashlib
import json
import re
from collections.abc import Mapping

from homeassistant.helpers import entity_registry as er
from homeassistant.util import slugify

from .bermuda_diagnostics import _coordinator
from .scanner_addresses import ble_mac, retained_mac

# Only the public scanner MAC suffix of a filtered range unique ID is read.
_RANGE_MAC = re.compile(r"_((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})_range$")


def inventory(hass):
    """Identity revision excludes fluctuating readings, includes registry changes."""
    rows, runtime, address_pairs = {}, {}, set()
    manager = getattr(hass, "config_entries", None)
    entries = manager.async_entries("bermuda") if manager is not None else []
    for entry in entries:
        devices = getattr(_coordinator(hass, entry), "devices", {})
        if not isinstance(devices, Mapping):
            continue
        for dev in devices.values():
            if getattr(dev, "_is_scanner", False) is not True:
                continue
            address, bluetooth = retained_mac(dev), ble_mac(dev)
            if address is None:
                continue
            if bluetooth is not None:
                address_pairs.add((entry.entry_id, address, bluetooth))
            name = getattr(dev, "name", None)
            runtime.setdefault((entry.entry_id, address), set())
            if isinstance(name, str) and name:
                runtime[(entry.entry_id, address)].add(name[:256])

    def row(slug):
        return rows.setdefault(slug, {"entity_id": slug, "names": set(), "identities": set(),
                                     "distance_sensors": [], "renamed_sensors": [], "disabled_sensors": []})

    lookup = getattr(er, "async_get", None)
    registry = lookup(hass) if lookup else None
    for ent in getattr(registry, "entities", {}).values():
        if ent.platform != "bermuda" or not ent.entity_id.startswith("sensor."):
            continue
        unique = getattr(ent, "unique_id", "") or ""
        if unique.endswith("_range_raw"):
            continue
        match = _RANGE_MAC.search(unique)
        suffix = ent.entity_id.split("_distance_to_", 1)[1] if "_distance_to_" in ent.entity_id else None
        if suffix is None and not match:
            continue
        owner = getattr(ent, "config_entry_id", None)
        mac = match[1].lower() if match else None
        names = runtime.get((owner, mac), set())
        # A renamed HA entity has no predictable lookup slug: report it under
        # its proven scanner name, without rewriting HA aliases or tracker keys.
        slug = suffix or (slugify(next(iter(names))) if len(names) == 1 else None)
        if not slug:
            continue
        target = row(slug)
        if names:
            target["names"].update(names)
        if mac:
            target["identities"].add((owner, mac))
        target["distance_sensors"].append(ent.entity_id)
        if suffix is None:
            target["renamed_sensors"].append(ent.entity_id)
        if getattr(ent, "disabled_by", None) is not None:
            target["disabled_sensors"].append(ent.entity_id)

    # Scanner-only installations remain selectable even with all distance
    # entities disabled/absent. Prefer actual registry slugs for proven MACs.
    runtime_name_ids = {}
    for identity, names in runtime.items():
        for name in names:
            runtime_name_ids.setdefault(slugify(name), set()).add(identity)
    for identity, names in runtime.items():
        if len(names) == 1 and len(runtime_name_ids[slugify(next(iter(names)))]) == 1:
            target = row(slugify(next(iter(names))))
            target["identities"].add(identity)
            target["names"].update(names)
    mac_owners = {}
    for owner, mac in runtime:
        mac_owners.setdefault(mac, set()).add(owner)
    for target in rows.values():
        for owner, mac in target["identities"]:
            if isinstance(owner, str) and owner:
                mac_owners.setdefault(mac, set()).add(owner)
    options = []
    for slug, target in sorted(rows.items()):
        if not slug:
            continue
        identities = target.pop("identities")
        ambiguous = len(identities) > 1 or len(target["names"]) > 1
        mac = next(iter(identities))[1] if len(identities) == 1 and not ambiguous else None
        names = sorted(target.pop("names"))
        renamed_only = bool(target["distance_sensors"]) and len(target["renamed_sensors"]) == len(target["distance_sensors"])
        target.update(name=names[0] if len(names) == 1 else slug, scanner_mac=mac,
                      config_entry_ids=sorted({owner for owner, _ in identities if isinstance(owner, str)}),
                      token=mac.replace(":", "")[-6:] if mac else None,
                      identity_ambiguous=ambiguous, selectable=not ambiguous and not renamed_only,
                      warning="Bermuda scanner identity is ambiguous across owners or names." if ambiguous else
                      "Restore Bermuda distance entity IDs with _distance_to_<scanner slug> before matching." if renamed_only else None)
        for key in ("distance_sensors", "renamed_sensors", "disabled_sensors"):
            target[key].sort()
        target["label"] = f'{target["name"]} · {slug}' + (f' · {mac}' if mac else '')
        options.append(target)
    for option in options:
        option["aliases"] = sorted(other["entity_id"] for other in options
            if option["scanner_mac"] and not option["identity_ambiguous"] and not other["identity_ambiguous"]
            and option["scanner_mac"] == other["scanner_mac"]
            and option["config_entry_ids"] == other["config_entry_ids"])
    name_identities = {name: {((owner,), mac) for owner, mac in identities}
                       for name, identities in runtime_name_ids.items()}
    for option in options:
        name = slugify(option["name"])
        identity = (tuple(option["config_entry_ids"]), option["scanner_mac"] or option["entity_id"])
        name_identities.setdefault(name, set()).add(identity)
    for option in options:
        option["liveness_name_unique"] = len(name_identities[slugify(option["name"])]) == 1
    mac_owners = {mac: sorted(owners) for mac, owners in sorted(mac_owners.items())}
    address_map = [{"config_entry_id": owner, "scanner_mac": mac, "scanner_ble_mac": bluetooth}
                   for owner, mac, bluetooth in sorted(address_pairs)]
    identities = {"options": options, "mac_owners": mac_owners, "runtime_address_map": address_map}
    revision = hashlib.sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest()
    return {"read_only": True, "revision": revision, **identities}
