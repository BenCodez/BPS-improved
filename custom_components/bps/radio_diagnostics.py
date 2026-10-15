"""Detached radio-model evidence for instantaneous, measured location marks."""
import hashlib
import json
import math

from .bermuda_direct import detached, scanner_mac
from .scanner_hardware import hardware_metadata

MAX_PACKET_AGE_S = 3.0
PROFILE_FIELDS = ("manufacturer", "model", "hardware_type", "antenna", "external_antenna",
                  "antenna_gain_dbi", "antenna_gain_db", "notes", "environment")


def numeric(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def safe_fields(value, keys):
    value = value if isinstance(value, dict) else {}
    return {key: detached(value[key]) for key in keys if key in value}


class RadioEvidence:
    """One owner/device/scanner index, built off the HA loop from frozen data."""

    def __init__(self, snapshot):
        self.now = snapshot.get("time")
        self.layout = snapshot.get("context", {}).get("layout", {})
        self.bermuda = snapshot.get("bermuda", {})
        private = snapshot.get("_radio_sources", {})
        sources = private.get("snapshots", self.bermuda.get("direct", {}).get("snapshots", []))
        self.snapshots, self.pairs, self.scanners = {}, {}, {}
        for source in sources:
            owner = source.get("entry_id")
            self.snapshots.setdefault(owner, []).append(source)
            for did, device in source.get("devices", {}).items():
                for ad in device.get("scanners", {}).values():
                    key = (owner, did, scanner_mac(ad))
                    self.pairs.setdefault(key, []).append((ad, device))
            for scanner in source.get("scanners", {}).values():
                self.scanners.setdefault((owner, scanner_mac(scanner)), []).append(scanner)
        self.legacy_ids = private.get("legacy_identities", {})
        self.legacy_ads = {}
        for ad in self.bermuda.get("adverts", []):
            key = (ad.get("entry_id"), ad.get("device_id"), ad.get("scanner_mac"))
            self.legacy_ads.setdefault(key, []).append(ad)
        self.profiles = {}
        for entry in self.bermuda.get("entries", []):
            settings = entry.get("effective_settings", {})
            self.profiles[entry.get("entry_id")] = settings.get("measurement_toolkit", {})
        try:
            encoded = json.dumps(self.layout, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
            self.context_hash = hashlib.sha256(encoded).hexdigest()
        except (TypeError, ValueError):
            self.context_hash = None

    def hardware(self, metadata, manual, owner, namespace, identity):
        result = hardware_metadata(metadata)
        result["bps_profile"] = safe_fields(manual, ("hardware_model", "antenna_notes", "external_antenna", "antenna_gain_dbi", "notes"))
        profiles = self.profiles.get(owner, {}).get(namespace, {})
        result["radio_profile"] = safe_fields(profiles.get(identity), PROFILE_FIELDS)
        return result

    def row(self, raw, receiver, comparison, scanner_identity):
        source = raw.get("measurement_source")
        source = source if isinstance(source, dict) else {}
        if source.get("path") == "direct":
            owner, did, mac = (source.get(key) for key in ("entry_id", "device_id", "scanner_mac"))
            matches = self.pairs.get((owner, did, mac), [])
            if len(self.snapshots.get(owner, [])) != 1 or len(matches) != 1:
                return {"status": "unavailable", "exclusions": ["radio_pair_missing_or_ambiguous"]}
            ad, device = matches[0]
            if ad.get("device_id") != did or device.get("device_id") != did:
                return {"status": "unavailable", "exclusions": ["radio_device_identity_conflict"]}
            scanners = self.scanners.get((owner, mac), [])
            scanner_hw = scanners[0].get("hardware") if len(scanners) == 1 else None
            beacon_hw = device.get("hardware")
            packet = safe_fields(ad, ("observed", "stamp", "observed_clock"))
            packet["age_s"] = ad.get("age") if numeric(ad.get("age")) else None
            values = {"raw_rssi_dbm": ad.get("raw_rssi", ad.get("rssi")),
                      "filtered_rssi_dbm": ad.get("filtered_rssi"), "reference_power_dbm": ad.get("reference_power"),
                      "rssi_offset_db": ad.get("rssi_offset"), "attenuation": ad.get("attenuation"),
                      "raw_distance_m": ad.get("raw_distance"), "filtered_distance_m": ad.get("filtered_distance"),
                      "tx_power_dbm": ad.get("tx_power")}
        elif source.get("path") == "legacy_entity":
            identity = self.legacy_ids.get(raw.get("entity_id"), {})
            owner, did, mac = (identity.get(key) for key in ("entry_id", "device_id", "scanner_mac"))
            matches = self.legacy_ads.get((owner, identity.get("ha_device_id"), mac), [])
            if not did or scanner_identity != (owner, mac) or len(matches) != 1:
                return {"status": "unavailable", "exclusions": ["legacy_radio_pair_not_proven"]}
            ad, scanner_hw, beacon_hw = matches[0], None, None
            age = ad.get("observation_age_s")
            packet = {"age_s": age if numeric(age) else None,
                      "observed": self.now - age if numeric(age) and numeric(self.now) else None,
                      "observed_clock": "derived_legacy_monotonic_age", "stamp": ad.get("stamp")}
            reference = ad.get("ref_power")
            values = {"raw_rssi_dbm": ad.get("rssi"), "filtered_rssi_dbm": None,
                      "reference_power_dbm": ad.get("conf_ref_power") if reference == 0 else reference,
                      "rssi_offset_db": ad.get("conf_rssi_offset"), "attenuation": ad.get("conf_attenuation"),
                      "raw_distance_m": ad.get("rssi_distance_raw"), "filtered_distance_m": ad.get("rssi_distance"),
                      "tx_power_dbm": ad.get("tx_power")}
        else:
            return {"status": "unavailable", "exclusions": ["radio_identity_unavailable"]}
        if scanner_identity is not None and scanner_identity != (owner, mac):
            return {"status": "unavailable", "exclusions": ["radio_scanner_identity_conflict"]}
        if any(not isinstance(value, str) or not value for value in (owner, did, mac)):
            return {"status": "unavailable", "exclusions": ["radio_identity_unavailable"]}
        radio = {"status": "available", "path": source["path"],
                 "identity": {"entry_id": owner, "device_id": did, "scanner_mac": mac},
                 "packet": packet, "model_version": detached(ad.get("model_version")),
                 "reference_power_verified": ad.get("reference_power_verified") if type(ad.get("reference_power_verified")) is bool else None,
                 "quality": safe_fields(ad.get("quality"), ("mode", "status", "age", "samples", "noise_db", "reliability", "last_raw_stamp", "last_valid_stamp", "last_valid_observed")),
                 "context_sha256": self.context_hash, "exclusions": [],
                 **{key: value if numeric(value) else None for key, value in values.items()}}
        radio["receiver_hardware"] = self.hardware(scanner_hw, receiver, owner, "receiver_profiles", mac)
        radio["beacon_hardware"] = self.hardware(beacon_hw, self.layout.get("tracker_hardware", {}).get(raw.get("tracker")),
                                                 owner, "beacon_profiles", did)
        excluded = radio["exclusions"]
        if raw.get("ignored"):
            excluded.append("ignored_receiver")
        expected = comparison.get("expected_distance_m")
        if not numeric(expected) or expected <= 0 or comparison.get("height_model") != "slant":
            excluded.append("positive_slant_geometry_required")
        age, observed = packet.get("age_s"), packet.get("observed")
        if (not numeric(age) or age < 0 or not numeric(observed) or not numeric(self.now)
                or observed < 0 or observed > self.now or not packet.get("observed_clock")):
            excluded.append("raw_packet_clock_unknown_or_future")
        elif max(age, self.now - observed) > MAX_PACKET_AGE_S:
            excluded.append("raw_packet_stale")
        if source["path"] == "direct" and ad.get("status") != "current":
            excluded.append("raw_source_not_current")
        if not numeric(radio["raw_rssi_dbm"]) or not -127 <= radio["raw_rssi_dbm"] < 0:
            excluded.append("raw_rssi_unavailable")
        ref, offset, attenuation = (radio[key] for key in ("reference_power_dbm", "rssi_offset_db", "attenuation"))
        check = {"status": "unavailable", "calculated_raw_distance_m": None}
        if all(numeric(v) for v in (ref, offset, attenuation, radio["raw_rssi_dbm"])) and attenuation > 0:
            try:
                calculated = 10 ** ((ref - radio["raw_rssi_dbm"] - offset) / (10 * attenuation))
                actual = radio["raw_distance_m"]
                if numeric(calculated) and calculated > 0 and numeric(actual) and actual > 0:
                    check = {"status": "matches" if math.isclose(calculated, actual, rel_tol=1e-5, abs_tol=1e-6) else "mismatch",
                             "calculated_raw_distance_m": calculated}
            except (OverflowError, ZeroDivisionError):
                pass
        radio["model_check"] = check
        if check["status"] != "matches":
            excluded.append("radio_model_" + check["status"])
        residuals = {"raw_error_m": None, "filtered_error_m": None, "corrected_error_m": None,
                     "expected_rssi_dbm": None, "rssi_residual_db": None, "required_offset_delta_db": None}
        if numeric(expected) and expected > 0:
            for name, value in (("raw_error_m", radio["raw_distance_m"]), ("filtered_error_m", radio["filtered_distance_m"]),
                                ("corrected_error_m", raw.get("corrected_distance_m"))):
                residuals[name] = value - expected if numeric(value) and value >= 0 else None
            if check["status"] == "matches":
                wanted = ref - offset - 10 * attenuation * math.log10(expected)
                residual = radio["raw_rssi_dbm"] - wanted
                if numeric(wanted) and numeric(residual):
                    residuals.update(expected_rssi_dbm=wanted, rssi_residual_db=residual, required_offset_delta_db=-residual)
        radio["comparison"] = residuals
        return radio
