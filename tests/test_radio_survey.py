"""Cross-mark export uses detached evidence and never invents calibration."""

import copy
import json
import math

import pytest

from bps import radio_survey


HASH = "a" * 64


def hardware(name="esp32"):
    return {"detected": {"manufacturer": "Example", "model": name},
            "effective": {"manufacturer": "Example", "model": name},
            "provenance": "home_assistant_device_registry",
            "bps_profile": {"hardware_model": name},
            "radio_profile": {"model": name}}


def row(*, stamp=100, observed=None, age=1, distance=2, receiver_hardware=None,
        **radio_changes):
    radio = {
        "status": "available", "path": "bermuda",
        "identity": {"entry_id": "owner", "device_id": "device", "scanner_mac": "AA:BB"},
        "packet": {"observed": stamp - 1 if observed is None else observed,
                   "stamp": stamp, "age_s": age,
                   "observed_clock": "source_epoch"},
        "raw_rssi_dbm": -68, "filtered_rssi_dbm": -67,
        "reference_power_dbm": -59, "rssi_offset_db": 0, "attenuation": 2,
        "raw_distance_m": distance + 1, "filtered_distance_m": distance + .5,
        "model_version": "v1", "reference_power_verified": False,
        "receiver_hardware": receiver_hardware or hardware(),
        "beacon_hardware": hardware("tag"), "context_sha256": HASH,
        "model_check": {"status": "matches", "calculated_raw_distance_m": distance + 1},
        "comparison": {"raw_error_m": 1, "filtered_error_m": .5,
                       "corrected_error_m": .25, "expected_rssi_dbm": -65,
                       "rssi_residual_db": -3, "required_offset_delta_db": 3},
        "exclusions": [],
    }
    radio.update(radio_changes)
    return {"tracker": "beacon", "receiver": "receiver", "expected_distance_m": distance,
            "height_model": "slant", "measured_distance_m": distance + .5,
            "corrected_distance_m": distance + .25, "signed_error_m": .25,
            "environment": {"classification": "clear"}, "radio": radio}


def record(index, *, x=0, floor="Garden", context=HASH, readings=None, accuracy=.2):
    readings = [row(stamp=100 + index, distance=2 + index)] if readings is None else readings
    for reading in readings:
        if isinstance(reading.get("radio"), dict):
            reading["radio"]["context_sha256"] = context
    return {"id": f"capture-{index}", "time": 100 + index,
            "capture": {"versions": {"bps": "1.0"}, "settings_sha256": "b" * 64},
            "marker": {"target": "beacon", "floor": floor, "x_m": x, "y_m": 0,
                       "accuracy_m": accuracy},
            "comparison": {"markers": [{"ranked_readings": readings,
                                        "unscored_readings": []}]}}


def test_three_fresh_distinct_marks_expose_diagnostic_diversity_without_fit():
    records = [record(i, x=i * 3) for i in range(3)]
    before = copy.deepcopy(records)
    output = radio_survey.export(records)
    assert records == before
    assert output["format"] == "bps-radio-survey-v1"
    assert output["counts"]["eligible_samples"] == 3
    pair = output["pairs"][0]
    assert pair["counts"] == {"samples": 3, "unique_packets": 3, "distinct_locations": 3}
    assert pair["true_range_span_m"] == 2
    assert pair["median_errors_m"] == {"raw_error_m": 1,
                                       "filtered_error_m": .5, "corrected_error_m": .25}
    assert pair["median_rssi_residual_db"] == -3
    assert pair["readiness"]["exploratory_fitting"] == "candidate"
    assert "offset_recommendation" not in json.dumps(output)


def test_pair_cohorts_separate_owner_model_context_path_and_hardware():
    changes = [
        {},
        {"identity": {"entry_id": "other", "device_id": "device", "scanner_mac": "AA:BB"}},
        {"reference_power_dbm": -60},
        {"context_sha256": "c" * 64},
        {"path": "direct"},
        {"receiver_hardware": hardware("raspberry_pi")},
    ]
    records = []
    for i, change in enumerate(changes):
        records.append(record(i, x=i * 3, context=change.get("context_sha256", HASH),
                              readings=[row(stamp=100 + i, **change)]))
    output = radio_survey.export(records)
    assert len(output["pairs"]) == len(changes)
    assert all(pair["counts"]["unique_packets"] == 1 for pair in output["pairs"])


def test_distance_bands_use_true_geometry_and_separate_absolute_error_from_bias():
    records = []
    for i, distance in enumerate((4, 5, 9, 10, 25, 50, 100)):
        item = row(stamp=100 + i, distance=distance, raw_distance_m=distance * 2)
        item["radio"]["comparison"]["raw_error_m"] = distance
        records.append(record(i, x=i * 3, readings=[item]))
    pair = radio_survey.export(records)["pairs"][0]
    assert [(b["lower_inclusive_m"], b["upper_exclusive_m"], b["samples"])
            for b in pair["distance_bands"]] == [(0, 5, 1), (5, 10, 2), (10, 25, 1), (25, 50, 1), (50, None, 2)]
    assert all(b["median_raw_to_true_ratio"] == 2 for b in pair["distance_bands"])
    far = pair["distance_bands"][-1]
    assert far["median_absolute_errors_m"]["raw_error_m"] == 75


def test_same_board_with_different_antenna_gain_is_a_separate_cohort():
    records = []
    for i, gain in enumerate((8, 20)):
        hw = hardware("synthetic_board")
        hw["bps_profile"].update(external_antenna=True, antenna_gain_dbi=gain)
        records.append(record(i, readings=[row(stamp=100 + i, receiver_hardware=hw)]))
    pairs = radio_survey.export(records)["pairs"]
    assert len(pairs) == 2
    assert {p["receiver_hardware"]["bps_profile"]["antenna_gain_dbi"] for p in pairs} == {8, 20}


def test_same_packet_nearby_labels_count_once_but_conflicting_labels_exclude_all():
    nearby = radio_survey.export([
        record(0, x=0, readings=[row(stamp=100)]),
        record(1, x=.3, readings=[row(stamp=100)]),
    ])
    assert nearby["counts"]["eligible_samples"] == 1
    assert nearby["pairs"][0]["counts"]["unique_packets"] == 1
    assert "duplicate_packet" in nearby["samples"][1]["exclusions"]

    conflicting = radio_survey.export([
        record(0, x=0, readings=[row(stamp=100)]),
        record(1, x=2, readings=[row(stamp=100)]),
    ])
    assert conflicting["pairs"] == []
    assert conflicting["counts"]["eligible_samples"] == 0
    assert all("conflicting_packet_location" in s["exclusions"]
               for s in conflicting["samples"])


def test_same_packet_on_other_floor_or_layout_conflicts():
    for second in (record(1, floor="Upper", readings=[row(stamp=100)]),
                   record(1, context="c" * 64, readings=[row(stamp=100)])):
        output = radio_survey.export([record(0, readings=[row(stamp=100)]), second])
        assert output["counts"]["eligible_samples"] == 0
        assert output["pairs"] == []


def test_reused_monotonic_stamp_after_restart_is_a_new_packet():
    first = record(0, readings=[row(stamp=80, observed=99)])
    later = record(1000, x=50, readings=[row(stamp=80, observed=1099)])
    output = radio_survey.export([first, later])
    assert output["counts"]["unique_packets"] == 2
    assert output["counts"]["eligible_samples"] == 2
    assert not any("conflicting_packet_location" in item["exclusions"]
                   for item in output["samples"])


def test_receiver_aliases_share_canonical_owned_pair_and_near_points_are_one_location():
    marks = [record(i, x=i * .1, readings=[row(stamp=100 + i)]) for i in range(3)]
    marks[1]["comparison"]["markers"][0]["ranked_readings"][0]["receiver"] = "receiver_alias"
    output = radio_survey.export(marks)
    assert len(output["pairs"]) == 1
    pair = output["pairs"][0]
    assert pair["receiver_labels"] == ["receiver", "receiver_alias"]
    assert pair["counts"]["distinct_locations"] == 1
    assert pair["readiness"]["exploratory_fitting"] == "insufficient_diversity"


def test_raw_packet_freshness_is_independent_of_filtered_status():
    fresh = row(stamp=90, observed=99, age=2, filtered_distance_m=None, filtered_rssi_dbm=None)
    fresh["status"] = "filter_pending"
    fresh["radio"]["comparison"]["filtered_error_m"] = None
    old = row(stamp=91, observed=99, age=3.01)
    unknown = row(stamp=92, packet={"observed": 99, "stamp": 92,
                                    "age_s": 1, "observed_clock": "unknown"})
    mismatch = row(stamp=93, observed=99, model_check={"status": "mismatch"})
    output = radio_survey.export([record(0, readings=[fresh, old, unknown, mismatch])])
    assert output["counts"]["eligible_samples"] == 1
    assert output["pairs"][0]["median_errors_m"]["filtered_error_m"] is None
    assert "stale_packet" in output["samples"][1]["exclusions"]
    assert "unknown_clock" in output["samples"][2]["exclusions"]
    assert "model_mismatch" in output["samples"][3]["exclusions"]


def test_raw_packet_age_boundary_and_future_clock():
    exact = row(stamp=80, observed=97, age=3)
    future = row(stamp=81, observed=101, age=0)
    output = radio_survey.export([record(0, readings=[exact, future])])
    assert output["samples"][0]["eligible"] is True
    assert "unknown_clock" in output["samples"][1]["exclusions"]


def test_old_records_and_unavailable_radio_are_explicit():
    legacy = record(0, readings=[{"tracker": "beacon", "receiver": "receiver"}])
    unavailable = row(status="unavailable", packet={"observed": None})
    output = radio_survey.export([legacy, record(1, readings=[unavailable])])
    assert output["counts"]["records_missing_radio"] == 1
    assert output["counts"]["rows_missing_radio"] == 1
    assert output["counts"]["excluded_samples"] == 1
    assert "radio_unavailable" in output["samples"][0]["exclusions"]


def test_unknown_hardware_allows_owned_pair_comparison_but_limits_generalization():
    unknown = {"detected": {}, "effective": {}, "provenance": "unknown",
               "bps_profile": {}, "radio_profile": {}}
    output = radio_survey.export([record(0, readings=[row(receiver_hardware=unknown)])])
    assert output["counts"]["eligible_samples"] == 1
    pair = output["pairs"][0]
    assert pair["hardware_metadata"] == "unknown_or_partial"
    assert pair["readiness"]["comparison"] == "ready"
    assert pair["readiness"]["hardware_generalization"] == "unknown_hardware"


def test_advertised_antenna_gain_remains_descriptive_and_separates_hardware():
    first = hardware()
    first["bps_profile"]["antenna_gain_dbi"] = 20
    second = hardware()
    second["bps_profile"]["antenna_gain_dbi"] = 18
    output = radio_survey.export([
        record(0, readings=[row(receiver_hardware=first)]),
        record(1, x=3, readings=[row(stamp=101, receiver_hardware=second)]),
    ])
    assert len(output["pairs"]) == 2
    assert {pair["receiver_hardware"]["bps_profile"]["antenna_gain_dbi"]
            for pair in output["pairs"]} == {18, 20}
    assert "corrected_antenna_gain" not in json.dumps(output)


def test_projection_rejects_arbitrary_fields_and_nonfinite_values():
    source = row(raw_rssi_dbm=math.nan, comparison={"raw_error_m": math.inf,
                                                    "private": "secret"}, secret="secret")
    source["secret"] = "secret"
    result = radio_survey.export([record(0, readings=[source])])
    serialized = json.dumps(result, allow_nan=False)
    assert "secret" not in serialized and "private" not in serialized
    assert result["samples"][0]["radio"]["raw_rssi_dbm"] is None
    assert "invalid_rssi" in result["samples"][0]["exclusions"]


def test_sample_and_json_limits(monkeypatch):
    records = [record(i, x=i, readings=[row(stamp=i, observed=99 + i)]) for i in range(10)]
    monkeypatch.setattr(radio_survey, "MAX_SAMPLES", 4)
    output = radio_survey.export(records)
    assert output["counts"]["candidate_rows"] == 10
    assert len(output["samples"]) == 4
    assert output["omissions"]["sample_limit"] == 6
    assert len(json.dumps(output, allow_nan=False).encode()) <= output["limits"]["json_bytes"]

    monkeypatch.setattr(radio_survey, "MAX_BYTES", 3000)
    bounded = radio_survey.export(records)
    assert len(json.dumps(bounded, ensure_ascii=False, allow_nan=False,
                          separators=(",", ":")).encode()) <= 3000
    assert bounded["omissions"]["byte_limit"] > 0


def test_requires_list_input():
    with pytest.raises(ValueError):
        radio_survey.export({})
