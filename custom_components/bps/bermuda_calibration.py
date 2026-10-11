"""Read-only conversion of BPS distance factors to Bermuda scanner offsets.

Bermuda computes d = 10 ** ((reference - (RSSI + offset)) / (10 * n)).
Changing its offset by -10*n*log10(factor) therefore multiplies raw distance
by factor. Solver factors are absolute replacements against Bermuda distances,
not additional multipliers on the currently configured BPS correction.
"""
import copy
import math
from collections.abc import Mapping

from .bermuda_diagnostics import MAC, _coordinator


def number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def delta_db(factor, attenuation):
    if not number(factor) or not .2 <= factor <= 5 or not number(attenuation) or attenuation <= 0:
        return None
    value = -10 * (attenuation * math.log10(factor))
    return value if math.isfinite(value) else None


def source_snapshot(receivers, devices, scanner_map):
    """Allowlisted provenance from the same actual adverts as calibration.

    The caller supplies the existing canonical scanner match; this function
    never reconstructs a MAC from a slug. Missing/conflicting models stay unknown.
    """
    rows = {}
    for slug, geometry in receivers.items():
        addresses = sorted(a.lower() for a, s in scanner_map.items()
                           if s == slug and isinstance(a, str) and MAC.fullmatch(a))
        row = {"geometry": copy.deepcopy(geometry), "scanner_mac": addresses[0] if len(addresses) == 1 else None,
               "attenuation": None, "rssi_offset_db": None, "reference_power_dbm": None}
        models = set()
        for dev in devices.values():
            if not isinstance(dev, Mapping) or dev.get("_is_scanner") is not True:
                continue
            ads = dev.get("adverts")
            if not isinstance(ads, Mapping):
                continue
            for ad in ads.values():
                if not isinstance(ad, Mapping) or str(ad.get("scanner_address", "")).lower() != row["scanner_mac"]:
                    continue
                n, offset, ref = (ad.get(k) for k in ("conf_attenuation", "conf_rssi_offset", "conf_ref_power"))
                if number(n) and n > 0 and number(offset) and number(ref):
                    models.add((n, offset, ref))
        if len(models) == 1:
            row["attenuation"], row["rssi_offset_db"], row["reference_power_dbm"] = next(iter(models))
        # Transmitter reference power changes also invalidate these raw samples.
        refs = set()
        for address, matched in scanner_map.items():
            if matched != slug:
                continue
            for dev in devices.values():
                if not isinstance(dev, Mapping) or str(dev.get("address", "")).lower() != address.lower():
                    continue
                ads = dev.get("adverts")
                if not isinstance(ads, Mapping):
                    continue
                for ad in ads.values():
                    if isinstance(ad, Mapping):
                        ref = ad.get("ref_power")
                        if ref == 0:
                            ref = ad.get("conf_ref_power")
                        if number(ref):
                            refs.add(ref)
        row["transmitter_reference_power_dbm"] = sorted(refs)
        rows[slug] = row
    return {"format": "bps-bermuda-source-v1", "receivers": rows}


def runtime_context(hass, coords, floor_name):
    """Detach public scanner fields from installed Bermuda coordinators."""
    from .calibration import _all_placed_slugs, _build_receiver_map, _match_scanners
    receivers = _build_receiver_map(coords, floor_name, hass=hass)
    manager = getattr(hass, "config_entries", None)
    entries = manager.async_entries("bermuda") if manager is not None else []
    devices, owners, options_by_mac, offset_keys = {}, {}, {}, {}
    for entry in entries:
        coordinator = _coordinator(hass, entry)
        live = getattr(coordinator, "devices", {})
        if not isinstance(live, Mapping):
            continue
        for dev in live.values():
            if getattr(dev, "_is_scanner", False) is not True:
                continue
            address = getattr(dev, "address", None)
            if not isinstance(address, str) or not MAC.fullmatch(address):
                continue
            original_address = address
            address = address.lower()
            row = {"_is_scanner": True, "address": address, "name": getattr(dev, "name", ""), "_bps_config_entry_id": entry.entry_id, "adverts": {}}
            ads = getattr(dev, "adverts", {})
            if isinstance(ads, Mapping):
                for index, advert in enumerate(ads.values()):
                    row["adverts"][str(index)] = {k: getattr(advert, k, None) for k in
                        ("scanner_address", "conf_attenuation", "conf_rssi_offset", "conf_ref_power", "ref_power")}
            # Never combine identically addressed scanners from different entries.
            owners.setdefault(address, set()).add(entry.entry_id)
            devices[address] = row
            options_by_mac[address] = getattr(coordinator, "options", {})
            offset_keys[address] = original_address
    private = {"receivers": receivers, "all_placed_slugs": _all_placed_slugs(coords)}
    scanner_map = _match_scanners(private, devices)
    scanner_map = {a: s for a, s in scanner_map.items() if len(owners.get(a, ())) == 1}
    source = source_snapshot(receivers, devices, scanner_map)
    for slug, row in source["receivers"].items():
        address = row["scanner_mac"]
        row["entry_id"] = next(iter(owners[address])) if address else None
        row["scanner_name"] = devices[address]["name"] if address else None
        options = options_by_mac.get(address, {})
        if not isinstance(options, Mapping):
            continue
        # Bermuda's actual effective options are authoritative for a manual
        # transfer, including a scanner not currently receiving probe adverts.
        n, ref = options.get("attenuation"), options.get("ref_power")
        offsets = options.get("rssi_offsets", {})
        offset = offsets.get(offset_keys.get(address), 0) if isinstance(offsets, Mapping) else None
        if number(n) and n > 0 and number(offset) and number(ref):
            row.update(attenuation=n, rssi_offset_db=offset, reference_power_dbm=ref)
    return source


def comparable_source(source):
    if not isinstance(source, Mapping):
        return None
    return {"format": source.get("format"), "receivers": {
        slug: {k: copy.deepcopy(v) for k, v in row.items() if k not in ("entry_id", "scanner_name")}
        for slug, row in source.get("receivers", {}).items()}}


def source_for_floor(source, floor_name):
    if not isinstance(source, Mapping):
        return None
    source = comparable_source(source)
    source["receivers"] = {s: r for s, r in source["receivers"].items()
        if str(r.get("geometry", {}).get("floor", "")).strip().lower() == str(floor_name).strip().lower()}
    return source


def build_preview(coords, floor_name, result, current_source, tracking=None):
    """Pure export. No HA service, persistence, or calibration mutation."""
    floor = next((f for f in coords.get("floor", []) if str(f.get("name", "")).strip().lower() ==
                  str(floor_name or "").strip().lower()), None)
    if floor is None:
        raise ValueError("Select an existing floor first")
    result = result or {}
    source_rows = current_source.get("receivers", {})
    candidates = result.get("receivers", {})
    warnings = []
    if coords.get("auto_calibration"):
        warnings.append("automatic_bps_calibration_enabled")
    historical = result.get("bermuda_source")
    if not historical:
        warnings.append("sample_source_unverified")
    elif source_for_floor(historical, floor_name) != source_for_floor(current_source, floor_name):
        warnings.append("sample_source_changed")
    before, after = result.get("error_factor_before"), result.get("error_factor_after")
    if not number(before) or not number(after):
        warnings.append("solve_error_unavailable")
    elif after > before:
        warnings.append("solve_worsens_reference_error")
    validated = (tracking is not None and tracking.get("candidate_solved_at") == result.get("solved_at")
                 and tracking.get("candidate_corrections") == candidates)
    if not validated:
        warnings.append("measured_tracking_revalidation_required")
    elif tracking.get("verdict") != "improves":
        warnings.append("measured_tracking_" + str(tracking.get("verdict", "inconclusive")))
    rows = []
    for receiver in floor.get("receivers", []):
        slug = receiver.get("entity_id")
        current = receiver.get("correction", 1.)
        proposed = candidates.get(slug)
        model = source_rows.get(slug, {})
        n, offset = model.get("attenuation"), model.get("rssi_offset_db")
        row_warnings = list(warnings)
        if not model.get("scanner_mac"):
            row_warnings.append("scanner_mapping_unknown_or_ambiguous")
        if not number(offset) or not number(n) or n <= 0:
            row_warnings.append("effective_bermuda_model_unavailable")
        if not number(current) or not .2 <= current <= 5:
            row_warnings.append("current_bps_factor_invalid")
        if proposed is None:
            row_warnings.append("receiver_unsupported_by_solve")
        elif delta_db(proposed, n) is None:
            row_warnings.append("proposed_bps_factor_invalid")
        if slug in result.get("low_confidence", []):
            row_warnings.append("weak_receiver_confidence")
        delta = delta_db(proposed, n)
        transfer = delta_db(current, n)
        valid_offset = number(offset)
        if transfer is None:
            row_warnings.append("current_factor_conversion_unavailable")
        proposed_offset = offset + delta if valid_offset and delta is not None else None
        transfer_offset = offset + transfer if valid_offset and transfer is not None else None
        if proposed_offset is not None and not number(proposed_offset):
            proposed_offset = None
            row_warnings.append("offset_out_of_numeric_range")
        if transfer_offset is not None and not number(transfer_offset):
            transfer_offset = None
            row_warnings.append("offset_out_of_numeric_range")
        transfer_reasons = [r for r in row_warnings if r in
            ("automatic_bps_calibration_enabled", "scanner_mapping_unknown_or_ambiguous",
             "effective_bermuda_model_unavailable", "current_bps_factor_invalid", "offset_out_of_numeric_range",
             "current_factor_conversion_unavailable")]
        rows.append({"receiver": slug, "scanner_mac": model.get("scanner_mac"), "scanner_name": model.get("scanner_name"), "bermuda_entry_id": model.get("entry_id"),
            "attenuation": n if number(n) else None, "current_offset_db": offset if valid_offset else None,
            "proposed_offset_db": proposed_offset,
            "offset_delta_db": delta, "current_bps_factor": current if number(current) else None,
            "proposed_bps_factor": proposed if number(proposed) else None, "bps_factor_after_manual_transfer": 1.,
            "incremental_distance_factor": proposed / current if delta is not None and transfer is not None else None,
            "transfer_current_offset_db": transfer_offset,
            "transfer_current_delta_db": transfer, "transfer_current_eligible": not transfer_reasons,
            "transfer_current_reasons": transfer_reasons, "candidate_eligible": not row_warnings,
            "status": "eligible" if not row_warnings else "blocked", "reasons": row_warnings})
    return {"format": "bps-bermuda-correction-preview-v1", "read_only": True, "floor": floor["name"],
        "candidate_solved_at": result.get("solved_at"), "units": {"offset": "dB", "reference_power": "dBm",
            "distance_factor": "dimensionless multiplier", "attenuation": "dimensionless path-loss exponent"},
        "formula": "proposed_offset_db = current_offset_db - 10 * attenuation * log10(proposed_bps_factor)",
        "reference_error": {"before": before if number(before) else None, "after": after if number(after) else None},
        "warnings": warnings, "rows": rows,
        "instructions": "Preview only: uses the saved BPS layout and live Bermuda runtime; no Bermuda options, BPS layout, or calibration are saved. Disable BPS automatic calibration before a manual transfer. Apply only eligible rows manually in Bermuda Calibration 2 using the matched scanner MAC/name, then reset only those affected BPS receiver factors to 1. Use the floor-wide Reset only if every non-unit factor on that floor has been transferred; otherwise manually edit only the affected factors in the BPS layout. Solver factors replace current BPS factors; do not multiply them together. Re-check offsets and attenuation immediately before applying, collect fresh samples and validate at measured stationary locations afterward. RSSI conversion preserves raw distance scale; Bermuda filtering and velocity history may respond differently."}
