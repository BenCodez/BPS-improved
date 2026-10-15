"""Proven scanner aliases and fresh filtered readings, without changing layouts."""
import copy
import hashlib
import math
import time
from functools import partial

from homeassistant.helpers import entity_registry as er

from .bermuda_diagnostics import MAC
from .bermuda_direct import BermudaDirect
from .receiver_matching import _RANGE_MAC, inventory


def synchronous_reader(sources):
    """Freeze registry collisions for one read pass without any await.

    Call again after yielding to HA; registry changes then take effect before
    the next pass. Standalone source() calls always revalidate immediately.
    """
    if not isinstance(sources, ReceiverSources) or not sources.fallback_trackers:
        return sources.source
    return partial(sources.source, _registry_proofs=sources._registry_device_proofs())


class ReceiverSources:
    """One detached registry/identity index per update or diagnostic snapshot."""

    def __init__(self, hass, *, direct=None):
        self.hass = hass
        lookup = getattr(er, "async_get", None)
        self.registry = lookup(hass) if lookup else None
        self.direct = direct if direct is not None else BermudaDirect(hass)
        # The direct API is detached once for this batch. Resolve each owned
        # device ID once, while registry collisions are still checked against
        # current entries after a solver suspension.
        self._device_cache = {}
        # Minimal callers without HA registry retain exact lookup; they cannot
        # expand aliases. Real HA always supplies the entity registry.
        self.matching = inventory(hass, self.direct) if self.registry is not None else {"options": []}
        self.direct_tracker_proofs = {}
        for ent in getattr(self.registry, "entities", {}).values():
            eid = ent.entity_id
            uid = getattr(ent, "unique_id", "") or ""
            match = _RANGE_MAC.search(uid)
            if ent.platform == "bermuda" and match and eid.startswith("sensor.") and "_distance_to_" in eid:
                tracker = eid.removeprefix("sensor.").split("_distance_to_", 1)[0]
                owner = getattr(ent, "config_entry_id", None)
                self.direct_tracker_proofs.setdefault((tracker, owner), []).append((eid, uid, uid[:match.start()]))
        self.options = {o["entity_id"]: o for o in self.matching["options"]}
        self.by_identity, self.owners_by_mac, self.readings, self.tracker_ids, self.reading_proofs = {}, {}, {}, {}, {}
        self.tracker_proofs = {}
        self.by_ble_address = {}
        for item in self.matching.get("runtime_address_map", []):
            if item["scanner_ble_mac"] != item["scanner_mac"]:
                self.by_ble_address.setdefault(item["scanner_ble_mac"], set()).add(
                    (item["config_entry_id"], item["scanner_mac"]))
        self.owners_by_mac.update({mac: set(owners) for mac, owners in self.matching.get("mac_owners", {}).items()})
        self.option_keys = {}
        for option in self.options.values():
            keys = {(item["config_entry_id"], item["scanner_mac"])
                    for item in option.get("scanner_identities", [])}
            exact = self.option_identity(option)
            if exact:
                keys.add(exact)
            self.option_keys[option["entity_id"]] = keys
            for key in keys:
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
        # API-only tracked devices have no range entity from which BPS can
        # derive its traditional tracker key. Keep their keys independent of
        # Bermuda's mutable name and slug, and never duplicate a device that
        # already has a registry range proof (even a disabled/conflicting one).
        self.fallback_trackers = {}
        self.fallback_labels = {}
        registry_keys, represented = self._registry_device_proofs()
        for owner, snapshot in self.direct.snapshots.items():
            if not isinstance(owner, str) or not owner:
                continue
            for did, device in snapshot["devices"].items():
                if (not isinstance(did, str) or not did or device.get("tracked") is not True
                        or device.get("device_id") != did or self._device(owner, did) != did
                        or device.get("unique_id") and self._device(owner, device["unique_id"]) != did
                        or any(ad.get("device_id") != did for ad in device["scanners"].values())
                        or (owner, did) in represented):
                    continue
                key = "bps_ble_" + hashlib.sha256(f"{owner}\0{did}".encode()).hexdigest()[:24]
                if key in registry_keys or key in self.fallback_trackers:
                    continue
                self.fallback_trackers[key] = (owner, did)
                name = device.get("name")
                self.fallback_labels[key] = name.strip() if isinstance(name, str) and name.strip() else did

    def _device(self, owner, identity):
        """Memoize only identities from this immutable direct snapshot."""
        if not isinstance(identity, str):
            return None
        key = (owner, identity)
        if key not in self._device_cache:
            self._device_cache[key] = self.direct.device(owner, identity)
        return self._device_cache[key]

    def _registry_device_proofs(self):
        """Read current keys and owned device proofs, including disabled ranges."""
        keys, represented = set(), set()
        for ent in getattr(self.registry, "entities", {}).values():
            eid = ent.entity_id
            if not eid.startswith("sensor.") or "_distance_to_" not in eid:
                continue
            keys.add(eid.removeprefix("sensor.").split("_distance_to_", 1)[0])
            if ent.platform != "bermuda":
                continue
            uid = getattr(ent, "unique_id", "") or ""
            match = _RANGE_MAC.search(uid)
            owner = getattr(ent, "config_entry_id", None)
            if match and owner in self.direct.snapshots:
                did = self._device(owner, uid[:match.start()])
                if did:
                    represented.add((owner, did))
        return keys, represented

    @staticmethod
    def option_identity(option):
        if not option or option.get("identity_ambiguous") or not option.get("scanner_mac"):
            return None
        owners = option.get("config_entry_ids", [])
        return (owners[0], option["scanner_mac"]) if len(owners) == 1 else None

    def direct_trackers(self):
        """Retain registry tracker keys even when distance entities are disabled."""
        registry_keys, represented = self._registry_device_proofs()
        result = {key for key, owner_device in self.fallback_trackers.items()
            if key not in registry_keys and owner_device not in represented}
        for (tracker, owner), proofs in self.direct_tracker_proofs.items():
            if not self._tracker_proofs_current(owner, proofs):
                continue
            identities = {identity for _, _, identity in proofs}
            if len(identities) != 1:
                continue
            device = self._device(owner, next(iter(identities)))
            if device and self.direct.snapshots[owner]["devices"][device].get("tracked") is True:
                result.add(tracker)
        return result

    def _tracker_proofs_current(self, owner, proofs):
        return bool(proofs) and all((entry := self.registry.entities.get(eid)) is not None and entry.platform == "bermuda"
            and (getattr(entry, "unique_id", "") or "") == uid
            and getattr(entry, "config_entry_id", None) == owner for eid, uid, _identity in proofs)

    def source_info(self, binding, state):
        provenance = getattr(state, "attributes", {}).get("bps_source") if state is not None else None
        if isinstance(provenance, dict):
            return copy.deepcopy(provenance)
        owner = binding.get("identity", (None,))[0] if binding.get("identity") else None
        status = next((row for row in self.direct.statuses if row["entry_id"] == owner), {})
        return {"path": "legacy_entity", "age_clock": "ha_publication", "reason":
                binding["status"] if self.blocked(binding) else
                status.get("reason", "identity_not_proven") if status.get("path") != "direct" else "direct_identity_not_proven"}

    def binding(self, receiver):
        if not isinstance(receiver, dict):
            return {"identity": None, "status": "invalid_scanner_identity", "aliases": []}
        slug = receiver.get("entity_id", "")
        mac, owner = receiver.get("scanner_mac"), receiver.get("scanner_config_entry_id")
        if (not isinstance(slug, str) or not slug
                or mac is not None and (not isinstance(mac, str) or not MAC.fullmatch(mac))
                or owner is not None and not isinstance(owner, str)):
            return {"identity": None, "status": "invalid_scanner_identity", "aliases": []}
        option = self.options.get(slug)
        exact = self.option_identity(option)
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
            if option and option.get("identity_ambiguous") and key not in self.option_keys.get(slug, ()):
                return {"identity": None, "status": "scanner_identity_conflict", "aliases": []}
        else:
            if option and option.get("identity_ambiguous"):
                return {"identity": None, "status": "ambiguous_scanner_identity", "aliases": []}
            key = exact
        return {"identity": key, "status": "matched" if key or option else "unmatched",
                "aliases": sorted(self.by_identity.get(key, {slug}))}

    def resolved_option(self, slug, binding):
        """Narrow an ambiguous display option to the saved hardware proof."""
        option = self.options.get(slug)
        if not option or self.blocked(binding):
            return None
        key = binding.get("identity")
        if not option.get("identity_ambiguous"):
            return option if key is None or self.option_identity(option) == key else None
        for row in option.get("scanner_identities", []):
            if (row["config_entry_id"], row["scanner_mac"]) == key:
                return {**option, **row, "config_entry_ids": [key[0]], "identity_ambiguous": False,
                        "liveness_name_unique": False}
        return None

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

    def source(self, tracker, receiver, binding=None, *, now=None, _registry_proofs=None):
        """Prefer public direct measurements only with owner/device/scanner proof."""
        binding = binding or self.binding(receiver)
        entity_id = f'sensor.{tracker}_distance_to_{receiver.get("entity_id", "")}'
        key = binding.get("identity")
        fallback = self.fallback_trackers.get(tracker)
        if fallback:
            owner, did = fallback
            # A captured fallback cannot borrow an entity or a different
            # receiver owner introduced during the same tracking batch.
            registry_keys, represented = (_registry_proofs if _registry_proofs is not None
                                          else self._registry_device_proofs())
            if (self.blocked(binding) or key is None or key[0] != owner
                    or fallback in represented or tracker in registry_keys):
                return entity_id, None
            ad = self.direct.measurement(owner, did, key[1])
            if (self._device(owner, did) != did
                    or key not in self.direct.scanners and (owner, did, key[1]) not in self.direct.pairs
                    or ad is not None and ad.get("device_id") != did):
                return entity_id, None
            return entity_id, self.direct.reading(owner, did, key[1], entity_id, now=now)
        if not self.blocked(binding) and key and key[0] in self.direct.snapshots:
            proofs = self.direct_tracker_proofs.get((tracker, key[0]), [])
            anchor = [proof for proof in proofs if proof[0] == entity_id]
            # The owner/device proof belongs to the tracker, not one receiver.
            # It can authorize a newly placed scanner with no pair entity yet.
            # An exact anchor still wins; conflicting whole-tracker identities
            # remain ambiguous and cannot authorize a new receiver.
            selected_proofs = anchor or proofs
            # Recheck after executor suspension, including disabled anchors.
            valid = self._tracker_proofs_current(key[0], proofs)
            exact = self.registry.entities.get(entity_id)
            captured = any(eid == entity_id for eid, _, _ in proofs)
            if proofs and (not valid or exact is not None and not captured):
                return entity_id, None
            identities = {identity for _, _, identity in selected_proofs}
            device = self._device(key[0], next(iter(identities))) if len(identities) == 1 else None
            if device and (key in self.direct.scanners or (key[0], device, key[1]) in self.direct.pairs):
                return entity_id, self.direct.reading(key[0], device, key[1], entity_id, now=now)
        return self._legacy_source(tracker, receiver, binding, now=now)

    def _legacy_source(self, tracker, receiver, binding=None, *, now=None):
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
