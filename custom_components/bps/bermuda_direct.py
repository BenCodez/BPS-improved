"""Optional, entry-scoped Bermuda v1 measurement bridge, without an install dependency.

Capture once per caller batch. A successful API with stale/missing measurements is
still authoritative for covered pairs; availability failures alone enable legacy
fallback. Names never prove device identity. All exported values are bounded and
private BLE keys use Bermuda's public SHA-256 identity spelling.
"""
import hashlib
import importlib
import inspect
import json
import logging
import math
import re
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from itertools import islice
from types import SimpleNamespace

MAX_ENTRIES = 32
MAX_DEVICES = 256
MAX_SCANNERS = 128
MAX_OBSERVATIONS = 4096
MAC = re.compile(r"^(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$")
RANGE_MAC = re.compile(r"_((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})_range(?:_raw)?$")
PRIVATE = re.compile(r"^[0-9a-fA-F]{32}$")
_LOGGER = logging.getLogger(__name__)
SCANNER_FIELDS = ("scanner_id", "address", "name", "slug", "unique_id", "entity_id_address",
                  "address_wifi_mac", "address_ble_mac", "device_registry_id", "binding", "source_aliases",
                  "area_id", "area_name", "scanner_last_seen_age")
MEASUREMENT_FIELDS = ("device_id", "source_id", "observed", "observed_clock", "stamp", "age", "status",
    "rssi", "raw_rssi", "filtered_rssi", "rssi_offset", "reference_power", "attenuation", "raw_distance",
    "filtered_distance", "distance", "distance_raw", "model_version", "reference_power_verified", "quality",
    "unavailable", "history", "history_status", "history_omitted")
DEVICE_FIELDS = ("device_id", "owner_id", "address", "name", "slug", "unique_id", "source_aliases", "tracked",
                 "address_type", "area_id", "area_name")


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        return None


def public_id(value):
    if not isinstance(value, str):
        return None
    return "private_" + hashlib.sha256(value.lower().encode()).hexdigest()[:32] if PRIVATE.fullmatch(value) else value


def detached(value, depth=0):
    """JSON bounds and private-ID hygiene even for optional nested diagnostics."""
    if depth > 6:
        return None
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return re.sub(r"(?<!private_)(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])",
                      lambda match: public_id(match[0]), value[:512])
    if isinstance(value, (int, float)):
        return number(value)
    if isinstance(value, Mapping):
        return {detached(k): detached(v, depth + 1) for k, v in islice(value.items(), 128) if isinstance(k, str)}
    if isinstance(value, (list, tuple)):
        return [detached(v, depth + 1) for v in value[:64]]
    return None


def fields(row, names):
    return {field: detached(row[field]) for field in names if field in row}


def scanner_mac(row):
    """Prefer the entity's declared retained identity; never infer MAC arithmetic."""
    declared = row.get("entity_id_address") or row.get("address_wifi_mac") or row.get("address")
    return declared.lower() if isinstance(declared, str) and MAC.fullmatch(declared) else None


class BermudaDirect:
    """Discover a local API and detach each loaded entry separately."""

    def __init__(self, hass, *, now=None, include_history=False, include_scanner_devices=False):
        self.now = time.time() if now is None else now
        self.include_scanner_devices = include_scanner_devices
        self.snapshots, self.statuses, self.scanners, self.pairs = {}, [], {}, {}
        manager = getattr(hass, "config_entries", None)
        entries = manager.async_entries("bermuda") if manager is not None else []
        self.entries = entries[:MAX_ENTRIES]
        self.entries_truncated = len(entries) > MAX_ENTRIES
        api = sys.modules.get("custom_components.bermuda.api")
        if api is None and entries:
            try:
                api = importlib.import_module("custom_components.bermuda.api")
            except Exception:
                # Stock Bermuda need not provide this optional module.
                api = None
        getter = getattr(api, "async_get_advert_snapshot", None)
        for entry in entries[:MAX_ENTRIES]:
            owner = getattr(entry, "entry_id", None)
            state = getattr(entry, "state", None)
            state = getattr(state, "value", state)
            row = {"entry_id": owner, "path": "legacy", "reason": "api_unavailable"}
            self.statuses.append(row)
            if state is not None and state not in ("loaded", "setup_in_progress"):
                row["reason"] = "entry_not_loaded"
                continue
            if not callable(getter) or inspect.iscoroutinefunction(getter):
                continue
            try:
                snapshot = getter(hass, entry_id=owner, include_history=include_history,
                                  tracked_only=True, include_empty=True, now=self.now)
                if inspect.isawaitable(snapshot):
                    if inspect.iscoroutine(snapshot):
                        snapshot.close()
                    row["reason"] = "unsupported_async_api"
                    continue
                if snapshot is None:
                    row["reason"] = "entry_unavailable"
                    continue
                if not isinstance(snapshot, Mapping) or type(snapshot.get("version")) is not int or snapshot.get("version") != 1:
                    row["reason"] = "unsupported_snapshot_version"
                    continue
                if snapshot.get("entry_id") != owner or not isinstance(snapshot.get("devices"), Mapping) \
                        or not isinstance(snapshot.get("scanners"), Mapping):
                    row["reason"] = "invalid_snapshot"
                    continue
                snapshot = self._snapshot(snapshot)
                if include_scanner_devices:
                    row["scanner_capture_available"] = True
                    if snapshot["scanners"]:
                        try:
                            extra = getter(hass, entry_id=owner, addresses=list(snapshot["scanners"]),
                                include_history=include_history, tracked_only=False, include_empty=True, now=self.now)
                            if inspect.iscoroutine(extra):
                                extra.close()
                            if not isinstance(extra, Mapping) or type(extra.get("version")) is not int or extra.get("version") != 1 \
                                    or extra.get("entry_id") != owner or not isinstance(extra.get("devices"), Mapping) \
                                    or not isinstance(extra.get("scanners"), Mapping):
                                row.update(scanner_capture_available=False, scanner_capture_reason="scanner_snapshot_unavailable")
                            else:
                                extra = self._snapshot(extra)
                                # Tracked device rows have first priority. Scanner
                                # captures add only the deliberately selected IDs.
                                for did, device in extra["devices"].items():
                                    if did not in snapshot["devices"] and did in snapshot["scanners"]:
                                        snapshot["devices"][did] = device
                                for field, count in extra["bridge_omitted"].items():
                                    snapshot["bridge_omitted"][field] += count
                        except Exception as err:
                            row.update(scanner_capture_available=False, scanner_capture_reason="scanner_api_error",
                                       scanner_error_type=type(err).__name__)
                self.snapshots[owner] = snapshot
                row.update(path="direct", reason="version_1", version=1)
                for sid, scanner in snapshot["scanners"].items():
                    mac = scanner_mac(scanner)
                    if mac:
                        self.scanners.setdefault((owner, mac), []).append((sid, scanner))
                for did, device in snapshot["devices"].items():
                    for sid, advert in device["scanners"].items():
                        mac = scanner_mac(advert)
                        if mac:
                            self.pairs.setdefault((owner, did, mac), []).append(advert)
            except Exception as err:
                # A failing optional integration must not interrupt BPS tracking.
                row.update(reason="api_error", error_type=type(err).__name__)
                _LOGGER.debug("Bermuda direct API unavailable for entry %s (%s); using legacy sources", owner, type(err).__name__)

    @staticmethod
    def _snapshot(source):
        result = fields(source, ("version", "entry_id", "stamp", "captured", "clocks", "omitted", "limits", "history_status"))
        result["scanners"] = {sid: fields(row, SCANNER_FIELDS)
            for sid, row in islice(source["scanners"].items(), MAX_SCANNERS)
            if isinstance(sid, str) and isinstance(row, Mapping)}
        result["devices"] = {}
        result["bridge_omitted"] = {"devices": max(0, len(source["devices"]) - MAX_DEVICES),
            "scanners": max(0, len(source["scanners"]) - MAX_SCANNERS), "observations": 0}
        remaining = MAX_OBSERVATIONS
        for did, row in islice(source["devices"].items(), MAX_DEVICES):
            if not isinstance(did, str) or not isinstance(row, Mapping):
                continue
            device = fields(row, DEVICE_FIELDS)
            ads = row.get("scanners", {})
            device["scanners"] = {}
            if isinstance(ads, Mapping):
                result["bridge_omitted"]["observations"] += max(0, len(ads) - min(MAX_SCANNERS, remaining))
                for sid, advert in islice(ads.items(), min(MAX_SCANNERS, remaining)):
                    if isinstance(sid, str) and isinstance(advert, Mapping):
                        ad = fields(advert, SCANNER_FIELDS + MEASUREMENT_FIELDS)
                        if isinstance(ad.get("history"), list):
                            ad["history"] = ad["history"][:5]
                        device["scanners"][sid] = ad
                        remaining -= 1
            result["devices"][public_id(did)] = device
        return result

    def device(self, owner, identity):
        """Resolve only canonical/unique IDs, not names or rotating source aliases."""
        wanted = public_id(identity)
        matches = [did for did, row in self.snapshots.get(owner, {}).get("devices", {}).items()
                   if wanted and wanted in (did, row.get("device_id"), row.get("unique_id"))]
        return matches[0] if len(matches) == 1 else None

    def measurement(self, owner, device, mac):
        rows = self.pairs.get((owner, device, mac), [])
        # Distinct source IDs sharing one retained scanner identity are ambiguous.
        return rows[0] if len(rows) == 1 else None

    def reading(self, owner, device, mac, entity_id, *, now=None):
        """HA-like read-only value dated by the actual accepted packet."""
        clock = self.now if now is None else now
        ad = self.measurement(owner, device, mac)
        info = {"path": "direct", "entry_id": owner, "version": 1, "device_id": device,
                "scanner_mac": mac, "status": "missing", "reason": "measurement_missing"}
        observed, value = None, None
        if ad is not None:
            quality = ad.get("quality") if isinstance(ad.get("quality"), Mapping) else {}
            source_age = number(ad.get("age"))
            age = source_age
            # Snapshot accepted=False means no new processing occurred in the
            # read. The accepted filter's stamp/age is the freshness authority.
            if quality.get("mode") in ("pet", "stationary"):
                age = number(quality.get("age"))
            if age is not None and age >= 0:
                observed = self.now - age
            info.update(status=ad.get("status"), reason="filtered_measurement", scanner_id=ad.get("scanner_id"),
                        source_status=ad.get("status"), source_observed=ad.get("observed"),
                        source_age_s=source_age, accepted_age_s=age, quality=detached(quality),
                        model_version=ad.get("model_version"))
            value = number(ad.get("filtered_distance", ad.get("distance")))
            if ad.get("status") != "current" or source_age is None or source_age < 0:
                info["reason"], value = "source_not_current", None
            elif quality.get("status") not in (None, "current"):
                info["reason"], value = "accepted_data_not_current", None
            elif observed is None or observed < 0 or observed > clock or value is None or value < 0:
                info["reason"], value = "filtered_measurement_unavailable", None
            elif quality.get("mode") in ("pet", "stationary") and number(quality.get("last_valid_stamp")) is None:
                info["reason"], value = "accepted_stamp_unavailable", None
        # Invalid covered pairs intentionally return an unavailable value; they
        # must never borrow a recently published entity value.
        if value is None and info["status"] == "current":
            info["status"] = "stale" if ad and isinstance(ad.get("quality"), Mapping) and ad["quality"].get("status") == "stale" else "unavailable"
        try:
            stamp = datetime.fromtimestamp(observed, timezone.utc) if observed is not None and observed >= 0 else None
        except (OverflowError, OSError, ValueError):
            stamp, value = None, None
            info.update(status="unavailable", reason="invalid_observation_clock")
        return SimpleNamespace(entity_id=entity_id, state=value if value is not None else "unavailable",
            attributes={"unit_of_measurement": "m", "bps_source": info}, last_updated=stamp)

    def scanner_age(self, identity):
        rows = self.scanners.get(identity, [])
        return number(rows[0][1].get("scanner_last_seen_age")) if len(rows) == 1 else None

    @property
    def needs_legacy(self):
        """Inactive entries have no live source to retrieve through a service."""
        return not self.snapshots or any((row["path"] != "direct" or row.get("scanner_capture_available") is False)
                                         and row["reason"] != "entry_not_loaded"
                                         for row in self.statuses) or self.entries_truncated

    @property
    def calibration_entries(self):
        return {row["entry_id"] for row in self.statuses if row["path"] == "direct"
                and row.get("scanner_capture_available") is not False}

    def diagnostics(self):
        snapshots, remaining, budget = [], 512, 256 * 1024
        omitted = {"observations": 0, "scanners": 0, "devices": 0}
        for source in self.snapshots.values():
            row = {key: value for key, value in source.items() if key not in ("devices", "scanners")}
            row.update(devices={}, scanners={})
            for sid, scanner in source["scanners"].items():
                size = len(json.dumps({sid: scanner}).encode())
                if size <= budget:
                    row["scanners"][sid] = scanner
                    budget -= size
                else:
                    omitted["scanners"] += 1
            for did, device in source["devices"].items():
                copied = {key: value for key, value in device.items() if key != "scanners"}
                copied["scanners"] = {}
                size = len(json.dumps({did: copied}).encode())
                if size > budget:
                    omitted["devices"] += 1
                    omitted["observations"] += len(device["scanners"])
                    continue
                budget -= size
                for sid, ad in device["scanners"].items():
                    size = len(json.dumps({sid: ad}).encode())
                    if remaining and size <= budget:
                        copied["scanners"][sid] = ad
                        remaining -= 1
                        budget -= size
                    else:
                        omitted["observations"] += 1
                if copied["scanners"] or not device["scanners"]:
                    row["devices"][did] = copied
            snapshots.append(row)
        return {"read_only": True, "entries": detached(self.statuses), "entries_truncated": self.entries_truncated,
                "snapshots": snapshots, "diagnostic_omitted": omitted,
                "guide": "Direct distances are metres. Packet age and accepted-filter age are separate from HA publication age. Runtime history is the recent five readings, not replay or measured position accuracy."}

    def dump_devices(self, fallback=None):
        """Feed existing raw scanner calibration without changing the ranging model.

        Covered entries replace their whole legacy scanner rows, including empty,
        stale or rejected pairs. Ownerless service rows with colliding addresses
        cannot claim another entry. Ambiguous same-address owners are omitted.
        """
        candidates = []
        for owner, snapshot in self.snapshots.items():
            if owner not in self.calibration_entries:
                continue
            for sid, scanner in snapshot["scanners"].items():
                if not MAC.fullmatch(sid) or scanner_mac(scanner) is None:
                    continue
                did = self.device(owner, sid)
                dev = snapshot["devices"].get(did, {})
                row = {"_is_scanner": True, "address": sid.lower(), "address_wifi_mac": scanner_mac(scanner),
                       "name": scanner.get("name"), "_bps_config_entry_id": owner, "adverts": {}}
                for rid, ad in dev.get("scanners", {}).items():
                    quality = ad.get("quality") if isinstance(ad.get("quality"), Mapping) else {}
                    age = number(ad.get("age"))
                    accepted = quality.get("status") in (None, "current")
                    if quality.get("mode") in ("pet", "stationary"):
                        accepted = accepted and number(quality.get("last_valid_stamp")) == number(ad.get("stamp")) \
                            and number(ad.get("stamp")) is not None
                    row["adverts"][rid] = {"scanner_address": rid.lower(), "stamp": ad.get("stamp"),
                        "rssi_distance_raw": ad.get("raw_distance"), "conf_attenuation": ad.get("attenuation"),
                        "conf_rssi_offset": ad.get("rssi_offset"), "conf_ref_power": ad.get("reference_power"),
                        "ref_power": ad.get("reference_power"), "_bps_direct_age": age,
                        "_bps_direct_usable": accepted and ad.get("status") == "current" and age is not None and age >= 0,
                        "_bps_model_version": ad.get("model_version")}
                candidates.append(row)
        direct_addresses = {row["address"] for row in candidates}
        for dev in (fallback or {}).values():
            if not isinstance(dev, dict):
                continue
            address = str(dev.get("address") or "").lower()
            if dev.get("_bps_config_entry_id") in self.calibration_entries or address in direct_addresses:
                continue
            candidates.append(dev)
        counts = {}
        for row in candidates:
            address = str(row.get("address") or "").lower()
            counts[address] = counts.get(address, 0) + 1
        return {str(row.get("address") or index).lower(): row for index, row in enumerate(candidates)
                if counts[str(row.get("address") or "").lower()] == 1}

    def legacy_scanners(self, hass):
        """Allowlisted private-runtime fallback for mixed older/newer entries.

        No callable toolkit hooks or data mutation. The owner attached here is
        stronger identity evidence than an ownerless legacy service response.
        """
        from .bermuda_diagnostics import _coordinator
        from .scanner_addresses import retained_mac, ble_mac
        result = {}
        inactive = {row["entry_id"] for row in self.statuses if row["reason"] == "entry_not_loaded"}
        for entry in self.entries:
            if entry.entry_id in self.calibration_entries or entry.entry_id in inactive:
                continue
            devices = getattr(_coordinator(hass, entry), "devices", {})
            if not isinstance(devices, Mapping):
                continue
            for dev in islice(devices.values(), MAX_DEVICES):
                if getattr(dev, "_is_scanner", False) is not True:
                    continue
                address, retained = ble_mac(dev), retained_mac(dev)
                if not address or not retained:
                    continue
                row = {"_is_scanner": True, "address": address, "address_wifi_mac": retained,
                       "name": getattr(dev, "name", ""), "_bps_config_entry_id": entry.entry_id, "adverts": {}}
                adverts = getattr(dev, "adverts", {})
                if isinstance(adverts, Mapping):
                    for index, ad in enumerate(islice(adverts.values(), MAX_SCANNERS)):
                        row["adverts"][str(index)] = {key: getattr(ad, key, None) for key in (
                            "scanner_address", "stamp", "rssi_distance_raw", "conf_attenuation", "conf_rssi_offset",
                            "conf_ref_power", "ref_power")}
                result[entry.entry_id + "/" + address] = row
        return result

    def calibration_dump(self, hass, service):
        """Combine mixed-install raw rows using current, unique ownership proof.

        A service response has no reliable entry owner. Registry range suffixes
        prove retained MAC ownership; a different BLE source additionally needs
        an explicit current runtime/API pair. Names and service owner markers
        cannot provide that proof, including for each advert's receiver.
        """
        from homeassistant.helpers import entity_registry as er
        from .bermuda_diagnostics import _coordinator
        from .scanner_addresses import retained_mac, ble_mac

        inactive = {row["entry_id"] for row in self.statuses if row["reason"] == "entry_not_loaded"}
        eligible = {entry.entry_id for entry in self.entries
                    if entry.entry_id not in self.calibration_entries and entry.entry_id not in inactive}
        claims, pairs = {}, {}

        def claim(owner, address):
            if address:
                claims.setdefault(address, set()).add(owner)

        def pair(owner, address, retained):
            if not address or not retained:
                return
            identity = (owner, address, retained)
            for alias in (address, retained):
                claim(owner, alias)
                pairs.setdefault(alias, set()).add(identity)

        # Include direct and inactive owners in collision proofs even though
        # their service rows cannot be used. Missing/truncated direct scanners
        # can still be identified by their owned registry anchors.
        for (owner, retained), scanners in self.scanners.items():
            for sid, _ in scanners:
                pair(owner, sid.lower() if MAC.fullmatch(sid) else None, retained)
        for entry in self.entries:
            devices = getattr(_coordinator(hass, entry), "devices", {})
            if not isinstance(devices, Mapping):
                continue
            for dev in islice(devices.values(), MAX_DEVICES):
                is_scanner = dev.get("_is_scanner") if isinstance(dev, Mapping) else getattr(dev, "_is_scanner", False)
                if is_scanner is True:
                    pair(entry.entry_id, ble_mac(dev), retained_mac(dev))
        lookup = getattr(er, "async_get", None)
        registry = lookup(hass) if lookup else None
        for ent in getattr(registry, "entities", {}).values():
            if getattr(ent, "platform", None) != "bermuda" or not ent.entity_id.startswith("sensor."):
                continue
            match = RANGE_MAC.search(getattr(ent, "unique_id", "") or "")
            if match:
                claim(getattr(ent, "config_entry_id", None), match[1].lower())

        def owner_for(address, retained, *, legacy=True):
            owners = claims.get(address, set())
            if len(owners) != 1 or owners != claims.get(retained, set()):
                return None
            owner = next(iter(owners))
            if legacy and owner not in eligible:
                return None
            known = pairs.get(address, set()) | pairs.get(retained, set())
            if known and known != {(owner, address, retained)}:
                return None
            if not known and address != retained:
                return None
            return owner

        def receiver_owner(address, *, legacy=True):
            if not isinstance(address, str) or not MAC.fullmatch(address):
                return None
            address = address.lower()
            known = pairs.get(address, set())
            if known:
                if len(known) != 1:
                    return None
                _, source, retained = next(iter(known))
                if source != address:
                    return None
            else:
                retained = address
            return owner_for(address, retained, legacy=legacy)

        runtime = self.legacy_scanners(hass)
        runtime_addresses = {row["address"] for row in runtime.values()}
        service = service if isinstance(service, Mapping) else {}
        candidates = [row for row in islice(service.values(), MAX_ENTRIES * MAX_DEVICES)
                      if isinstance(row, dict) and ble_mac(row) not in runtime_addresses]
        candidates.extend(runtime.values())
        result = {}
        for index, row in enumerate(candidates):
            if row.get("_is_scanner") is not True:
                continue
            address, retained = ble_mac(row), retained_mac(row)
            owner = owner_for(address, retained)
            if owner is None or row.get("_bps_config_entry_id", owner) != owner:
                continue
            copied = {**row, "address": address, "address_wifi_mac": retained,
                      "_bps_config_entry_id": owner, "adverts": {}}
            adverts = row.get("adverts", {})
            if isinstance(adverts, Mapping):
                for key, ad in islice(adverts.items(), MAX_SCANNERS):
                    if isinstance(ad, dict) and receiver_owner(ad.get("scanner_address")) == owner:
                        copied["adverts"][key] = dict(ad)
            result[str(index)] = copied
        # A calibration window can retain an earlier owner-unique placement.
        # Recheck direct rows as well, so a newly conflicting retained alias
        # cannot use that old uniqueness flag to bind to the legacy placement.
        combined = {}
        for key, row in self.dump_devices(result).items():
            owner = row.get("_bps_config_entry_id")
            if owner_for(ble_mac(row), retained_mac(row), legacy=False) != owner:
                continue
            copied = {**row, "adverts": {key: ad for key, ad in row["adverts"].items()
                      if receiver_owner(ad.get("scanner_address"), legacy=False) == owner}}
            combined[key] = copied
        return combined
