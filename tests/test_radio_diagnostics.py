"""Measured marks preserve pair-specific radio stages and hardware evidence."""
import copy
import json
import math
import io
import zipfile

import pytest

import bps
from bps import diagnostics, ground_truth, snapshot_truth
from bps.bermuda_direct import BermudaDirect
from bps.outdoor_config import validate_outdoor_layout
from bps.radio_diagnostics import RadioEvidence
from test_bermuda_direct import MAC, prepared
from test_snapshot_truth import marker, snapshot


def located_capture(hass, monkeypatch, *, at=1000, distance=10, ratio=1.5):
    layout, entries, states, add, old, new, payloads, _calls = prepared(hass, monkeypatch)
    monkeypatch.setattr(bps.time, "time", lambda: at)
    layout["tracker_hardware"] = {"tag": {"hardware_model": "Synthetic pet tag", "notes": "High TX setting; exact dBm unknown"}}
    receiver = layout["floor"][0]["receivers"][0]
    receiver.update(hardware_model="Synthetic C6", external_antenna=True, antenna_gain_dbi=20)
    hass.data["bps"]["layout"] = layout
    source = payloads["entry"]
    source["captured"] = at
    source["stamp"] = at - 500
    device = source["devices"]["synthetic-tracker"]
    device["hardware"] = {"effective": {"model": "Synthetic tag"}, "provenance": "home_assistant_device_registry"}
    source["scanners"][MAC]["hardware"] = {"detected": {"model": "Synthetic board"},
        "effective": {"model": "Synthetic C6", "external_antenna": True}, "provenance": "esphome"}
    ad = device["scanners"][MAC]
    ad.update(raw_rssi=-60 - 20 * math.log10(distance * ratio), reference_power=-60,
              rssi_offset=0, attenuation=2, raw_distance=distance * ratio,
              filtered_distance=distance * 1.2, age=1, observed=at - 1, stamp=at - 501)
    ad["quality"].update(age=1, last_valid_observed=at - 1, last_valid_stamp=at - 501)
    height = bps._tracker_height(layout, "tag")
    dx = math.sqrt(distance ** 2 - (receiver["height"] - height) ** 2)
    known = {"target": "tag", "floor": layout["floor"][0]["name"],
             "x_m": receiver["cords"]["x"] / layout["floor"][0]["scale"] + dx,
             "y_m": receiver["cords"]["y"] / layout["floor"][0]["scale"], "accuracy_m": .2}
    return layout, payloads, known


def test_mark_preserves_raw_filter_bps_stages_and_hardware_despite_display_truncation(hass, monkeypatch):
    layout, payloads, known = located_capture(hass, monkeypatch)
    before = copy.deepcopy((layout, payloads))
    monkeypatch.setattr(BermudaDirect, "diagnostics", lambda _self: {
        "snapshots": [], "diagnostic_omitted": {"observations": 42}})
    captured = bps._diagnostic_report_snapshot(hass, 1000)
    record = ground_truth.prepare(captured, known, "synthetic-radio-capture")
    row = record["comparison"]["markers"][0]["ranked_readings"][0]
    radio = row["radio"]
    assert (layout, payloads) == before
    assert record["report"]["bermuda"]["direct"]["snapshots"] == []
    assert "_radio_sources" not in record["report"]
    assert radio["identity"] == {"entry_id": "entry", "device_id": "synthetic-tracker", "scanner_mac": MAC}
    assert radio["packet"] == {"observed": 999, "stamp": 499, "age_s": 1, "observed_clock": "derived_monotonic"}
    assert radio["model_check"]["status"] == "matches" and radio["exclusions"] == []
    assert radio["comparison"]["raw_error_m"] == pytest.approx(5)
    assert radio["comparison"]["filtered_error_m"] == pytest.approx(2)
    assert radio["comparison"]["corrected_error_m"] == pytest.approx(14)
    assert radio["comparison"]["required_offset_delta_db"] == pytest.approx(20 * math.log10(1.5))
    assert row["receiver_correction"] == 2 and row["tracker_distance_factor"] == 1
    assert radio["receiver_hardware"]["effective"]["model"] == "Synthetic C6"
    assert radio["receiver_hardware"]["bps_profile"]["antenna_gain_dbi"] == 20
    assert radio["beacon_hardware"]["bps_profile"]["hardware_model"] == "Synthetic pet tag"
    assert radio["tx_power_dbm"] is None and radio["reference_power_verified"] is False


def test_model_mismatch_stale_raw_and_pending_filtered_stages_are_explicit(hass, monkeypatch):
    _layout, payloads, known = located_capture(hass, monkeypatch)
    ad = payloads["entry"]["devices"]["synthetic-tracker"]["scanners"][MAC]
    ad["raw_distance"] = 100
    captured = bps._diagnostic_report_snapshot(hass, 1000)
    row = snapshot_truth.analyse(captured, [known])["markers"][0]["ranked_readings"][0]
    assert row["radio"]["model_check"]["status"] == "mismatch"
    assert row["radio"]["comparison"]["required_offset_delta_db"] is None
    ad["raw_distance"] = 15
    ad.update(filtered_distance=None)
    captured = bps._diagnostic_report_snapshot(hass, 1000)
    row = snapshot_truth.analyse(captured, [known])["markers"][0]["unscored_readings"][0]
    assert row["radio"]["exclusions"] == []
    assert row["radio"]["comparison"]["filtered_error_m"] is None
    ad.update(age=10, observed=990)
    captured = bps._diagnostic_report_snapshot(hass, 1000)
    row = snapshot_truth.analyse(captured, [known])["markers"][0]["unscored_readings"][0]
    assert "raw_packet_stale" in row["radio"]["exclusions"]


def test_three_real_captures_export_frozen_radio_survey_in_diagnostic_zip(hass, monkeypatch):
    records = []
    for i, distance in enumerate((5, 10, 20)):
        at = 1000 + i * 30
        _layout, _payloads, known = located_capture(hass, monkeypatch, at=at, distance=distance)
        captured = bps._diagnostic_report_snapshot(hass, at)
        records.append(ground_truth.prepare(captured, known, f"synthetic-radio-capture-{i}"))
    from bps.radio_survey import export
    output = export(records)
    assert output["counts"]["eligible_samples"] == 3
    assert len(output["pairs"]) == 1
    pair = output["pairs"][0]
    assert pair["readiness"]["exploratory_fitting"] == "candidate"
    assert pair["true_range_span_m"] == pytest.approx(15)
    assert pair["median_rssi_residual_db"] == pytest.approx(-20 * math.log10(1.5))
    bundle = diagnostics.build_bundle(captured, {"status": "available", "records": records, "evicted": 0}, None, None)
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        survey = json.loads(archive.read("radio_survey.json"))
        manifest = json.loads(archive.read("manifest.json"))
    assert survey == output
    assert "radio_survey.json" in manifest["files"]
    assert {s["capture"]["time"] for s in survey["samples"]} == {1000, 1030, 1060}
    assert all(s["radio"]["tx_power_dbm"] is None for s in survey["samples"])
    assert {s["radio"]["packet"]["observed"] for s in survey["samples"]} == {999, 1029, 1059}


def test_scanner_owner_and_conflicting_hardware_are_never_guessed(hass, monkeypatch):
    _layout, payloads, known = located_capture(hass, monkeypatch)
    captured = bps._diagnostic_report_snapshot(hass, 1000)
    raw = captured["readings"][0]
    receiver = captured["context"]["layout"]["floor"][0]["receivers"][0]
    index = RadioEvidence(captured)
    assert index.row(raw, receiver, {}, ("another_owner", MAC))["status"] == "unavailable"
    source = captured["_radio_sources"]["snapshots"][0]
    source["devices"]["synthetic-tracker"]["device_id"] = "replacement"
    assert RadioEvidence(captured).row(raw, receiver, {}, ("entry", MAC))["status"] == "unavailable"


def test_legacy_pair_requires_registry_proof_and_exports_unique_packet_and_model():
    captured = snapshot()
    row = captured["readings"][0]
    row.update(measurement_source={"path": "legacy_entity"}, receiver_correction=2, tracker_distance_factor=1)
    captured["_radio_sources"] = {"legacy_identities": {row["entity_id"]: {
        "entry_id": "owner", "device_id": "beacon", "scanner_mac": MAC, "ha_device_id": "ha-device"}}}
    captured["bermuda"]["adverts"] = [{"entry_id": "owner", "device_id": "ha-device", "scanner_mac": MAC,
        "rssi": -80, "ref_power": 0, "conf_ref_power": -60, "conf_rssi_offset": 0, "conf_attenuation": 2,
        "rssi_distance_raw": 10, "rssi_distance": 8, "observation_age_s": 1, "stamp": 50}]
    captured["scanner_linking"] = {"placed": [{"floor": "Garden", "entity_id": "receiver_a",
        "matching": {"scanner_mac": MAC, "config_entry_ids": ["owner"]}}]}
    radio = snapshot_truth.analyse(captured, [marker()])["markers"][0]["ranked_readings"][0]["radio"]
    assert radio["model_check"]["status"] == "matches"
    assert radio["packet"]["stamp"] == 50 and radio["packet"]["observed"] == 99
    assert radio["model_version"] is None and radio["reference_power_dbm"] == -60
    captured["_radio_sources"]["legacy_identities"][row["entity_id"]]["entry_id"] = "other"
    radio = snapshot_truth.analyse(captured, [marker()])["markers"][0]["ranked_readings"][0]["radio"]
    assert radio["status"] == "unavailable"


@pytest.mark.parametrize("gain,valid", [(20, True), (8, True), (-20, True), (60, True),
    (True, False), (61, False), (float("inf"), False), ("20 dBm", False)])
def test_antenna_gain_is_descriptive_finite_dbi(gain, valid):
    data = {"floor": [{"name": "Ground", "receivers": [{"entity_id": "receiver", "antenna_gain_dbi": gain}]}]}
    error = validate_outdoor_layout(data)
    assert (error is None) == valid
