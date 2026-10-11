"""Proven scanner aliases and fresh filtered readings, without changing layouts."""
import copy
import math
import time

from homeassistant.helpers import entity_registry as er

from .bermuda_diagnostics import MAC
from .receiver_matching import _RANGE_MAC, inventory


class ReceiverSources:
    """One detached registry/identity index per update or diagnostic snapshot."""

    def __init__(self, hass):
        self.hass = hass
        lookup = getattr(er, "async_get", None)
        self.registry = lookup(hass) if lookup else None
        # Minimal callers without HA registry retain exact lookup; they cannot
        # expand aliases. Real HA always supplies the entity registry.
        self.matching = inventory(hass) if self.registry is not None else {"options": []}
        self.options = {o["entity_id"]: o for o in self.matching["options"]}
        self.by_identity, self.owners_by_mac, self.readings, self.tracker_ids, self.reading_proofs = {}, {}, {}, {}, {}
        self.tracker_proofs = {}
        self.by_ble_address = {}
        for item in self.matching.get("runtime_address_map", []):
            if item["scanner_ble_mac"] != item["scanner_mac"]:
                self.by_ble_address.setdefault(item["scanner_ble_mac"], set()).add(
                    (item["config_entry_id"], item["scanner_mac"]))
        self.owners_by_mac.update({mac: set(owners) for mac, owners in self.matching.get("mac_owners", {}).items()})
        for option in self.options.values():
            key = self.option_identity(option)
            if key:
                self.by_identity.setdefault(key, set()).add(option["entity_id"])
                self.owners_by_mac.setdefault(key[1], set()).add(key[0])
        for ent in getattr(self.registry, "entities", {}).values():
            eid = ent.entity_id
            uid = getattr(ent, "unique_id", "") or ""
            if ent.platform != "bermuda" or not eid.startswith("sensor.") or "_distance_to_" not in eid or uid.endswith("_range_raw"):
                continue
            tracker, slug = eid.removeprefix("sensor.").split("_distance_to_", 1)
            match = _RANGE_MAC.search(uid)
            # Disabled exact entities remain identity evidence for the beacon,
            # but never contribute a reading or expand the source inventory.
            self.tracker_ids[eid] = uid[:match.start()] if match else None
            self.tracker_proofs[eid] = (uid, getattr(ent, "config_entry_id", None))
            if getattr(ent, "disabled_by", None) is not None:
                continue
            owner = getattr(ent, "config_entry_id", None)
            self.reading_proofs[eid] = (uid, owner)
            key = (owner, match[1].lower()) if match and isinstance(owner, str) and owner else None
            self.readings.setdefault((tracker, key or slug), []).append(eid)

    @staticmethod
    def option_identity(option):
        if not option or option.get("identity_ambiguous") or not option.get("scanner_mac"):
            return None
        owners = option.get("config_entry_ids", [])
        return (owners[0], option["scanner_mac"]) if len(owners) == 1 else None

    def binding(self, receiver):
        if not isinstance(receiver, dict):
            return {"identity": None, "status": "invalid_scanner_identity", "aliases": []}
        slug = receiver.get("entity_id", "")
        mac, owner = receiver.get("scanner_mac"), receiver.get("scanner_config_entry_id")
        if (not isinstance(slug, str) or not slug
                or mac is not None and not isinstance(mac, str)
                or owner is not None and not isinstance(owner, str)):
            return {"identity": None, "status": "invalid_scanner_identity", "aliases": []}
        option = self.options.get(slug)
        exact = self.option_identity(option)
        if option and option.get("identity_ambiguous"):
            return {"identity": None, "status": "ambiguous_scanner_identity", "aliases": []}
        if isinstance(mac, str) and MAC.fullmatch(mac):
            mac = mac.lower()
            # Older layouts can carry Bermuda's Bluetooth address while range
            # entity IDs retain its Wi-Fi identity. Translate only a proven,
            # owner-scoped runtime pair; never infer MAC arithmetic or persist it.
            mapped = self.by_ble_address.get(mac, set())
            if mapped:
                owners = {key[0] for key in mapped} | self.owners_by_mac.get(mac, set())
                if not owner and len(owners) != 1:
                    return {"identity": None, "status": "scanner_identity_unavailable", "aliases": []}
                candidates = {key for key in mapped if not owner or key[0] == owner}
                candidates.update((candidate_owner, mac) for candidate_owner in self.owners_by_mac.get(mac, ())
                                  if not owner or candidate_owner == owner)
                if len(candidates) > 1:
                    return {"identity": None, "status": "scanner_identity_conflict", "aliases": []}
                if not candidates:
                    return {"identity": None, "status": "scanner_identity_unavailable", "aliases": []}
                owner, mac = next(iter(candidates))
            if exact and (exact[1] != mac or owner and owner != exact[0]):
                return {"identity": None, "status": "scanner_identity_conflict", "aliases": []}
            if owner:
                key = (owner, mac)
            elif exact:
                key = exact
            else:
                keys = [key for key in self.by_identity if key[1] == mac]
                key = keys[0] if len(keys) == 1 and len(self.owners_by_mac.get(mac, ())) == 1 else None
            if key is None or key not in self.by_identity:
                return {"identity": None, "status": "scanner_identity_unavailable", "aliases": []}
        else:
            key = exact
        return {"identity": key, "status": "matched" if key or option else "unmatched",
                "aliases": sorted(self.by_identity.get(key, {slug}))}

    def floor_bindings(self, floor):
        receivers = floor.get("receivers", [])
        if not isinstance(receivers, list):
            return []
        bindings = [self.binding(rec) for rec in receivers]
        groups = {}
        for index, binding in enumerate(bindings):
            if binding["identity"]:
                groups.setdefault(binding["identity"], []).append(index)
        for indices in groups.values():
            if len(indices) < 2:
                continue
            signatures = []
            for index in indices:
                rec = receivers[index]
                cords = rec.get("cords")
                cords = cords if isinstance(cords, dict) else {}
                signatures.append((cords.get("x"), cords.get("y"),
                    rec.get("height"), rec.get("correction", 1), rec.get("outdoor_policy", "auto"), rec.get("distance_accuracy_m")))
            conflict = any(signature != signatures[0] for signature in signatures[1:])
            for index in indices if conflict else indices[1:]:
                bindings[index] = {**bindings[index], "status": "conflicting_scanner_placements" if conflict else "duplicate_scanner_alias"}
        return bindings

    @staticmethod
    def blocked(binding):
        return binding["status"] not in ("matched", "unmatched")

    def source(self, tracker, receiver, binding=None, *, now=None):
        binding = binding or self.binding(receiver)
        direct = f'sensor.{tracker}_distance_to_{receiver.get("entity_id", "")}'
        if self.blocked(binding):
            return direct, None
        if self.registry is None:
            return direct, self.hass.states.get(direct)
        exact = self.registry.entities.get(direct)
        if exact is not None and (exact.platform != "bermuda" or
                (getattr(exact, "unique_id", "") or "", getattr(exact, "config_entry_id", None)) != self.tracker_proofs.get(direct)):
            # Even a disabled exact entity anchors tracker identity. Changed or
            # newly introduced proof takes effect in the next batch only.
            return direct, None
        key = binding["identity"] or receiver.get("entity_id", "")
        candidates = list(self.readings.get((tracker, key), []))
        # Runtime identity can upgrade an exact legacy entity whose unique ID
        # cannot prove a scanner MAC. Preserve only that enabled, owned exact
        # source; its unknown tracker identity below prevents alias expansion.
        if binding["identity"] and direct in self.readings.get((tracker, receiver.get("entity_id", "")), []):
            entry = self.registry.entities.get(direct)
            if getattr(entry, "config_entry_id", None) == binding["identity"][0] and direct not in candidates:
                candidates.append(direct)
        tracker_id = self.tracker_ids.get(direct)
        if tracker_id:
            candidates = [eid for eid in candidates if self.tracker_ids.get(eid) == tracker_id]
        elif direct in self.tracker_ids:
            candidates = [eid for eid in candidates if eid == direct]
        else:
            identities = {self.tracker_ids.get(eid) for eid in candidates}
            if len(identities) > 1 or None in identities:
                candidates = [eid for eid in candidates if eid == direct]
        # The index is shared across a tracking batch. Registry edits while
        # another tracker awaits its solver must not lend replacement hardware
        # the captured source identity. These are constant-time candidate checks.
        candidates = [eid for eid in candidates if self._reading_current(eid)]
        clock = time.time() if now is None else now
        future = set()
        valid = []
        for eid in candidates:
            state = self.hass.states.get(eid)
            if state is None:
                continue
            try:
                value = float(state.state)
                if not math.isfinite(value) or value < 0:
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            stamp = getattr(state, "last_updated", None) or getattr(state, "last_changed", None)
            try:
                observed = stamp.timestamp()
                if not math.isfinite(observed):
                    observed = -math.inf
                elif observed > clock:
                    future.add(eid)
                    continue
            except (AttributeError, ValueError, TypeError, OSError, OverflowError):
                observed = -math.inf
            valid.append((observed, eid == direct, eid, state))
        if valid:
            _, _, eid, state = max(valid, key=lambda row: row[:3])
            return eid, state
        # Diagnostic consumers retain the exact invalid/unavailable state;
        # numeric tracking consumers reject it without rejuvenating its clock.
        fallback = direct if direct in candidates else next(iter(sorted(candidates)), direct)
        return fallback, self.hass.states.get(fallback) if fallback in candidates and fallback not in future else None

    def _reading_current(self, eid):
        entry = self.registry.entities.get(eid)
        return (entry is not None and entry.platform == "bermuda" and getattr(entry, "disabled_by", None) is None
                and (getattr(entry, "unique_id", "") or "", getattr(entry, "config_entry_id", None)) == self.reading_proofs.get(eid))

    def calibration_layout(self, coords):
        """Augment detached geometry with proven owner/MAC, exclude duplicates."""
        result = copy.deepcopy(coords)
        for floor in result.get("floor", []):
            kept = []
            for rec, binding in zip(floor.get("receivers", []), self.floor_bindings(floor)):
                if self.blocked(binding):
                    continue
                key = binding["identity"]
                if key:
                    rec["scanner_mac"], rec["scanner_config_entry_id"] = key[1], key[0]
                    rec["scanner_owner_unique"] = len(self.owners_by_mac.get(key[1], ())) == 1
                kept.append(rec)
            floor["receivers"] = kept
        return result
