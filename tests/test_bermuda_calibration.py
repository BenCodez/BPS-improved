"""Bermuda exports use installed models, preserve factors, and never write."""
import asyncio
import copy
import json
import math
from types import SimpleNamespace as NS

import pytest

from bps import calibration as C
from bps import bermuda_calibration as B
from bps.storage import save_bps_data


def fixture(n=4., current=.5):
    layout = {"floor": [{"name": "Yard", "scale": 10., "receivers": [
        {"entity_id": f"r{i}", "correction": current if i == 0 else 1., "cords": {"x": x, "y": y}}
        for i, (x, y) in enumerate(((0, 0), (100, 0), (0, 100), (100, 100)))]}]}
    receivers = C._build_receiver_map(layout)
    addresses = {s: f"aa:bb:cc:dd:ee:{i:02x}" for i, s in enumerate(receivers)}
    devices = {}
    for tx, address in addresses.items():
        devices[address] = {"_is_scanner": True, "address": address, "name": tx, "adverts": {
            addresses[rx]: {"scanner_address": addresses[rx], "conf_attenuation": n,
                "conf_rssi_offset": -9., "conf_ref_power": -45., "ref_power": 0.,
                "stamp": 100., "rssi_distance_raw": C._true_distance_m({"receivers": receivers}, tx, rx)}
            for rx in receivers if tx != rx}}
    mapping = C._match_scanners({"receivers": receivers}, devices)
    source = B.source_snapshot(receivers, devices, mapping)
    result = {"floor": "Yard", "solved_at": "2026-10-11T12:00:00+00:00", "receivers": {s: 2. for s in receivers},
        "low_confidence": [], "error_factor_before": 2., "error_factor_after": 1.5, "bermuda_source": source}
    tracking = {"verdict": "improves", "candidate_solved_at": result["solved_at"],
                "candidate_corrections": copy.deepcopy(result["receivers"])}
    return layout, devices, source, result, tracking


def test_absolute_replacement_and_current_transfer_use_actual_attenuation_and_existing_offset():
    layout, _, source, result, tracking = fixture()
    before = copy.deepcopy((layout, source, result, tracking))
    export = B.build_preview(layout, "Yard", result, source, tracking)
    row = export["rows"][0]
    assert row["candidate_eligible"] and row["transfer_current_eligible"]
    assert row["offset_delta_db"] == pytest.approx(-40 * math.log10(2))
    assert row["proposed_offset_db"] == pytest.approx(-9 - 40 * math.log10(2))
    assert row["transfer_current_offset_db"] == pytest.approx(-9 - 40 * math.log10(.5))
    assert row["incremental_distance_factor"] == 4
    assert row["bps_factor_after_manual_transfer"] == 1
    # The proposal replaces .5 with 2, rather than multiplying .5 * 2.
    assert 10 ** (-(row["proposed_offset_db"] - row["current_offset_db"]) / 40) == pytest.approx(2)
    assert 10 ** (-(row["transfer_current_offset_db"] - row["current_offset_db"]) / 40) == pytest.approx(.5)
    assert export["units"]["offset"] == "dB"
    assert export["read_only"]
    assert (layout, source, result, tracking) == before
    json.dumps(export, allow_nan=False)


@pytest.mark.parametrize("issue,reason", [
    ("legacy", "sample_source_unverified"), ("offset", "sample_source_changed"),
    ("geometry", "sample_source_changed"), ("attenuation", "sample_source_changed"),
    ("worse", "solve_worsens_reference_error"), ("weak", "weak_receiver_confidence"),
    ("missing_tracking", "measured_tracking_revalidation_required"),
    ("stale_tracking", "measured_tracking_revalidation_required"),
    ("tracking_worse", "measured_tracking_worse"), ("tracking_inconclusive", "measured_tracking_inconclusive")])
def test_unsafe_proposals_are_disabled_but_current_transfer_remains_available(issue, reason):
    layout, _, source, result, tracking = fixture()
    if issue == "legacy": result.pop("bermuda_source")
    if issue == "offset": source = copy.deepcopy(source); source["receivers"]["r0"]["rssi_offset_db"] -= 1
    if issue == "attenuation": source = copy.deepcopy(source); source["receivers"]["r0"]["attenuation"] = 3
    if issue == "geometry": source = copy.deepcopy(source); source["receivers"]["r0"]["geometry"]["x"] += 1
    if issue == "worse": result["error_factor_after"] = 2.365; result["error_factor_before"] = 2.187
    if issue == "weak": result["low_confidence"] = ["r0"]
    if issue == "missing_tracking": tracking = None
    if issue == "stale_tracking": tracking["candidate_solved_at"] = "old"
    if issue.startswith("tracking_"): tracking["verdict"] = issue[len("tracking_"):]
    row = B.build_preview(layout, "Yard", result, source, tracking)["rows"][0]
    assert not row["candidate_eligible"] and reason in row["reasons"]
    assert row["transfer_current_eligible"]


@pytest.mark.parametrize("issue", ["mapping", "unknown_n", "unknown_offset", "auto", "invalid_current"])
def test_unknown_model_or_active_auto_blocks_manual_transfer(issue):
    layout, _, source, result, tracking = fixture()
    if issue == "mapping": source["receivers"]["r0"]["scanner_mac"] = None
    if issue == "unknown_n": source["receivers"]["r0"]["attenuation"] = None
    if issue == "unknown_offset": source["receivers"]["r0"]["rssi_offset_db"] = None
    if issue == "auto": layout["auto_calibration"] = True
    if issue == "invalid_current": layout["floor"][0]["receivers"][0]["correction"] = False
    row = B.build_preview(layout, "Yard", result, source, tracking)["rows"][0]
    assert not row["candidate_eligible"] and not row["transfer_current_eligible"]


def runtime(hass, layout, devices, options=None):
    live = {a: NS(**{**dev, "adverts": {k: NS(**v) for k, v in dev["adverts"].items()}}) for a, dev in devices.items()}
    coordinator = NS(devices=live, options=options or {"attenuation": 4., "ref_power": -45.,
        "rssi_offsets": {a: -9. for a in devices}})
    entry = NS(entry_id="bermuda-entry", runtime_data=NS(coordinator=coordinator))
    hass.config_entries = NS(async_entries=lambda _: [entry])
    return coordinator, entry


def test_runtime_uses_canonical_mapping_and_options_not_guessed_mac(hass):
    layout, devices, _, _, _ = fixture()
    coordinator, entry = runtime(hass, layout, devices)
    # A stale advert model must not override the installed settings.
    coordinator.options["attenuation"] = 2.7
    first = next(iter(devices))
    coordinator.options["rssi_offsets"][first] = -15
    source = B.runtime_context(hass, layout, "Yard")
    assert source["receivers"]["r0"]["scanner_mac"] == first
    assert source["receivers"]["r0"]["attenuation"] == 2.7
    assert source["receivers"]["r0"]["rssi_offset_db"] == -15
    # Unrelated arbitrary slug is not turned into a scanner MAC.
    layout["floor"][0]["receivers"][0]["entity_id"] = "arbitrary_receiver"
    assert B.runtime_context(hass, layout, "Yard")["receivers"]["arbitrary_receiver"]["scanner_mac"] is None


def test_ambiguous_name_match_and_duplicate_entry_ownership_are_unknown(hass):
    layout, devices, _, _, _ = fixture()
    duplicate = copy.deepcopy(next(iter(devices.values())))
    duplicate["address"] = "ff:bb:cc:dd:ee:01"
    devices[duplicate["address"]] = duplicate
    runtime(hass, layout, devices)
    assert B.runtime_context(hass, layout, "Yard")["receivers"]["r0"]["scanner_mac"] is None
    devices.pop(duplicate["address"])
    _, entry = runtime(hass, layout, devices)
    other = NS(entry_id="another-entry", runtime_data=entry.runtime_data)
    hass.config_entries.async_entries = lambda _: [entry, other]
    assert B.runtime_context(hass, layout, "Yard")["receivers"]["r0"]["scanner_mac"] is None


def test_ingested_source_survives_solve_and_model_changes_start_fresh_window():
    layout, devices, _, _, _ = fixture()
    cal = {"receivers": C._build_receiver_map(layout), "samples": {}}
    for index in range(C.MIN_SAMPLES_PER_PAIR):
        for dev in devices.values():
            for ad in dev["adverts"].values(): ad["stamp"] = 100 + index
        C._ingest_dump(cal, devices)
    result = C.solve(C.solve_snapshot(cal), "Yard")
    assert result["bermuda_source"]["receivers"]["r0"]["attenuation"] == 4
    assert len(cal["samples"]["r0|r1"]) == C.MIN_SAMPLES_PER_PAIR
    for dev in devices.values():
        for ad in dev["adverts"].values():
            ad["conf_attenuation"] = 3.; ad["stamp"] += 1
    C._ingest_dump(cal, devices)
    assert len(cal["samples"]["r0|r1"]) == 1
    assert cal["_bermuda_source"]["receivers"]["r0"]["attenuation"] == 3


def test_preview_http_is_authenticated_read_only_and_does_not_require_a_solve(hass):
    async def scenario():
        layout, devices, _, result, _ = fixture()
        await save_bps_data(hass, layout)
        runtime(hass, layout, devices)
        cal = C.get_calibration_state(hass)
        cal["results"]["Yard"] = result
        before = copy.deepcopy((cal, hass._store_saves, hass._store_backing))
        async def body(): return {"action": "preview_bermuda", "floor": "Yard"}
        request = NS(app={"hass": hass}, json=body)
        api = C.BPSCalibrationAPI()
        assert api.requires_auth
        response = await api.post(request)
        assert response.status == 200 and response.json_body["read_only"]
        assert (cal, hass._store_saves, hass._store_backing) == before
        assert response.json_body["rows"][0]["transfer_current_eligible"]
        cal["results"].clear()
        response = await api.post(request)
        assert response.status == 200
        assert "receiver_unsupported_by_solve" in response.json_body["rows"][0]["reasons"]
        assert response.json_body["rows"][0]["transfer_current_eligible"]
    asyncio.run(scenario())


@pytest.mark.parametrize("factor,n", [(float("nan"), 3), (True, 3), (.1, 3), (1, 0), (2, float("inf")), (2, 1e308)])
def test_invalid_math_never_exports_an_offset(factor, n):
    assert B.delta_db(factor, n) is None


@pytest.mark.parametrize("changed", ["current_factor", "bermuda_offset", "calibration_replaced"])
def test_validation_cannot_certify_a_preview_after_its_inputs_change(hass, changed):
    async def scenario():
        layout, devices, source, result, tracking = fixture()
        await save_bps_data(hass, layout)
        coordinator, _ = runtime(hass, layout, devices)
        cal = C.get_calibration_state(hass)
        cal["results"]["Yard"] = result
        async def validate(_hass, _floor):
            if changed == "current_factor":
                updated = copy.deepcopy(layout)
                updated["floor"][0]["receivers"][0]["correction"] = 1.25
                await save_bps_data(hass, updated)
            elif changed == "bermuda_offset":
                coordinator.options["rssi_offsets"][next(iter(devices))] = -6
            else:
                hass.data["bps"]["calibration"] = {"results": {}}
            return tracking
        async def body(): return {"action": "preview_bermuda", "floor": "Yard"}
        response = await C.BPSCalibrationAPI(validate).post(NS(app={"hass": hass}, json=body))
        row = response.json_body["rows"][0]
        assert not row["candidate_eligible"]
        assert "measured_tracking_revalidation_required" in row["reasons"]
        assert row["transfer_current_eligible"]
        if changed == "current_factor": assert row["current_bps_factor"] == 1.25
        if changed == "bermuda_offset": assert row["current_offset_db"] == -6
    asyncio.run(scenario())


def test_restored_samples_without_provenance_cannot_join_a_new_verified_window():
    layout, devices, _, _, _ = fixture()
    cal = {"receivers": C._build_receiver_map(layout), "samples": {"r0|r1": [99.] * 20}}
    C._ingest_dump(cal, devices)
    assert len(cal["samples"]["r0|r1"]) == 1
    assert cal["samples"]["r0|r1"][0] != 99


def test_conflicting_actual_advert_models_stay_unknown():
    layout, devices, _, _, _ = fixture()
    next(iter(devices.values()))["adverts"]["aa:bb:cc:dd:ee:01"]["conf_attenuation"] = 2
    mapping = C._match_scanners({"receivers": C._build_receiver_map(layout)}, devices)
    source = B.source_snapshot(C._build_receiver_map(layout), devices, mapping)
    assert source["receivers"]["r1"]["attenuation"] is None
