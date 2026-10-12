"""Synthetic full snapshot truth comparisons, without live state or writes."""
import copy
import math

import pytest

from bps import snapshot_truth as truth


def snapshot():
    return {"time": 100, "context": {"layout": {"floor": [
        {"name": "Garden", "scale": 10, "receivers": [
            {"entity_id": "receiver_a", "cords": {"x": 0, "y": 0}, "height": 4},
            {"entity_id": "receiver_b", "cords": {"x": 60, "y": 0}, "height": 1}]},
        {"name": "Upper", "scale": 10, "receivers": [
            {"entity_id": "receiver_c", "cords": {"x": 0, "y": 0}, "height": 1}]}],
        "reading_max_age": 30}, "target_members": {
        "beacon_a": ["beacon_a"], "beacon_b": ["beacon_b"],
        "bps_group_pet": ["beacon_a", "beacon_b"]}},
        "readings": [reading()], "positions": [
            {"ent": "beacon_a", "floor": "Garden", "cords": [30, 40], "updated": 99}],
        "bermuda": {"adverts": [{"rssi": -65}]}}


def reading(**changes):
    return {"tracker": "beacon_a", "receiver": "receiver_a", "floor": "Garden",
            "entity_id": "sensor.beacon_a_distance_to_receiver_a", "status": "current",
            "age_s": 1, "observed": 99, "ignored": False, "assumed_unit_m": False,
            "unit": "ft", "raw_state": "10", "measured_distance_m": 3.048,
            "corrected_distance_m": 6.096, "tracker_height_m": 1, **changes}


def marker(**changes):
    return {"target": "beacon_a", "floor": "Garden", "x_m": 3, "y_m": 4, **changes}


def test_slant_geometry_converted_range_and_immutable_all_data():
    data = snapshot()
    before = copy.deepcopy(data)
    result = truth.analyse(data, [marker(accuracy_m=.1)])
    row = result["markers"][0]["ranked_readings"][0]
    assert row["expected_horizontal_m"] == 5
    assert row["expected_distance_m"] == pytest.approx(math.sqrt(34))
    assert row["measured_distance_m"] == 3.048
    assert row["corrected_distance_m"] == 6.096
    assert row["signed_error_m"] == pytest.approx(6.096 - math.sqrt(34))
    assert row["relative_error_pct"] == pytest.approx((6.096 / math.sqrt(34) - 1) * 100)
    assert row["disagrees"] and row["rank"] == 1
    assert result["markers"][0]["position_errors"][0]["error_m"] == 0
    assert data == before


def test_group_expands_members_and_retains_missing_stale_wrong_floor_positions():
    data = snapshot()
    data["positions"] += [
        {"ent": "bps_group_pet", "floor": "Upper", "cords": [30, 40], "updated": 99},
        {"ent": "beacon_b", "floor": "Garden", "cords": [20, 20], "updated": 20}]
    result = truth.analyse(data, [marker(target="bps_group_pet")])["markers"][0]
    assert result["members"] == ["beacon_a", "beacon_b"]
    assert [r["status"] for r in result["position_errors"]] == ["wrong_floor", "current", "stale"]
    assert result["position_errors"][0]["wrong_floor"]
    assert not result["position_errors"][2]["scored"]
    assert result["unscored_readings"][0]["reasons"] == ["missing_readings"]


@pytest.mark.parametrize("changes, reason", [
    ({"status": "missing", "observed": None, "age_s": None}, "missing"),
    ({"status": "stale", "age_s": 40, "observed": 60}, "stale"),
    ({"status": "current", "age_s": 40, "observed": 60}, "stale"),
    ({"status": "unknown"}, "unknown"),
    ({"status": "ambiguous_scanner_identity"}, "ambiguous_scanner_identity"),
    ({"assumed_unit_m": True}, "unproven_unit"),
    ({"ignored": True}, "ignored"),
    ({"age_s": None}, "unknown_age"),
    ({"observed": 101}, "unknown_age"),
    ({"corrected_distance_m": float("nan")}, "invalid_distance"),
    ({"receiver": "receiver_c", "floor": "Upper"}, "off_floor")])
def test_excluded_readings_are_never_ranked(changes, reason):
    data = snapshot()
    data["readings"] = [reading(**changes)]
    result = truth.analyse(data, [marker()])["markers"][0]
    assert result["ranked_readings"] == []
    assert reason in result["unscored_readings"][0]["reasons"]


def test_zero_expected_distance_and_missing_height_are_explicit():
    data = snapshot()
    data["context"]["layout"]["floor"][0]["receivers"][0]["height"] = "unknown"
    row = truth.analyse(data, [marker(x_m=0, y_m=0)])["markers"][0]["ranked_readings"][0]
    assert row["height_model"] == "horizontal"
    assert row["expected_distance_m"] == 0 and row["relative_error_pct"] is None


@pytest.mark.parametrize("physical_proof", [False, True])
def test_deduplicates_exact_entity_sources_and_owner_scoped_physical_aliases(physical_proof):
    data = snapshot()
    data["readings"].append(reading(receiver="receiver_b",
        entity_id="sensor.beacon_a_distance_to_alias" if physical_proof else reading()["entity_id"]))
    if physical_proof:
        data["scanner_linking"] = {"placed": [{"entity_id": receiver, "floor": "Garden",
            "matching": {"scanner_mac": "00:11:22:33:44:55", "config_entry_ids": ["synthetic"]}}
            for receiver in ("receiver_a", "receiver_b")]}
    result = truth.analyse(data, [marker()])["markers"][0]
    assert len(result["ranked_readings"]) == 1
    assert result["unscored_readings"][0]["reasons"] == ["duplicate_source"]


def test_same_mac_in_distinct_owners_is_not_deduplicated():
    data = snapshot()
    data["readings"].append(reading(receiver="receiver_b", entity_id="sensor.beacon_a_distance_to_other"))
    data["scanner_linking"] = {"placed": [{"entity_id": receiver, "floor": "Garden",
        "matching": {"scanner_mac": "00:11:22:33:44:55", "config_entry_ids": [owner]}}
        for receiver, owner in [("receiver_a", "synthetic_a"), ("receiver_b", "synthetic_b")]]}
    assert len(truth.analyse(data, [marker()])["markers"][0]["ranked_readings"]) == 2


def test_mapped_path_is_evaluated_at_truth_using_saved_scale_and_strength():
    data = snapshot()
    data["context"]["layout"]["outdoor_tracking"] = {"obstruction_strength": 3}
    data["context"]["layout"]["floor"][0]["environment"] = [{"id": "trees", "type": "dense_trees",
        "points": [{"x": 10, "y": 0}, {"x": 20, "y": 0}, {"x": 20, "y": 50}, {"x": 10, "y": 50}]}]
    data["positions"][0]["cords"] = [0, 0]
    row = truth.analyse(data, [marker()])["markers"][0]["ranked_readings"][0]
    path = row["environment"]
    assert path["classification"] == "dense_trees"
    assert path["obstruction_strength"] == 3 and path["path_scale_px_per_m"] == 10
    assert path["intersections"][0]["path_length_m"] == pytest.approx(5 / 3)
    assert path["environmental_weight"] == pytest.approx(.75 ** 3)


@pytest.mark.parametrize("bad", [None, {}, [], [marker()] * 33,
    [marker(), marker()], [marker(target="missing")], [marker(target=[])],
    [marker(floor="missing")], [marker(x_m=True)], [marker(y_m=float("inf"))],
    [marker(x_m=100001)], [marker(x_m=10 ** 1000)], [marker(accuracy_m=-1)],
    [marker(accuracy_m=101)], [marker(accuracy_m=True)], [marker(label="x" * 241)],
    [marker(extra=True)], [marker(target="bps_group_pet"), marker()]])
def test_rejects_malformed_or_overlapping_markers(bad):
    with pytest.raises(ValueError):
        truth.analyse(snapshot(), bad)


@pytest.mark.parametrize("broken", ["missing_layout", "bad_scale", "duplicate_floor", "missing_members"])
def test_rejects_invalid_captured_context(broken):
    data = snapshot()
    if broken == "missing_layout":
        del data["context"]["layout"]
    elif broken == "bad_scale":
        data["context"]["layout"]["floor"][0]["scale"] = True
    elif broken == "duplicate_floor":
        data["context"]["layout"]["floor"].append(copy.deepcopy(data["context"]["layout"]["floor"][0]))
    else:
        del data["context"]["target_members"]
    with pytest.raises(ValueError):
        truth.analyse(data, [marker()])


def test_limits_detached_rows():
    data = snapshot()
    data["readings"] = [reading()] * (truth.MAX_READINGS + 1)
    with pytest.raises(ValueError, match="32768"):
        truth.analyse(data, [marker()])


def test_prior_solver_evidence_keeps_its_own_clock_and_only_safe_fields():
    data = snapshot()
    data["positions"][0]["updated"] = 60
    data["positions"][0]["outdoor"] = {"observed": 59, "receiver_diagnostics": [
        {"receiver": "receiver_a", "observed": 58, "entity_id": "sensor.old_source", "used": False,
         "reliability_weight": .2, "range_trust_weight": .5, "horizontal_residual_m": 4,
         "arbitrary": "excluded"}]}
    result = truth.analyse(data, [marker()])["markers"][0]
    evidence = result["ranked_readings"][0]["solver_evidence"]
    assert evidence["kind"] == "prior_solver_input"
    assert evidence["position_updated"] == 60 and evidence["position_observed"] == 59
    assert evidence["reading"]["observed"] == 58 and evidence["reading"]["used"] is False
    assert "arbitrary" not in evidence["reading"]
    assert result["ranked_readings"][0]["observed"] == 99
    assert result["position_errors"][0]["status"] == "stale"


def test_summary_counts_exclusions_and_bounds_largest_errors():
    data = snapshot()
    data["readings"].extend([reading(status="missing"), reading(assumed_unit_m=True),
                             reading(floor="Upper", receiver="receiver_c")])
    summary = truth.analyse(data, [marker()])["markers"][0]["summary"]
    assert summary["ranked_readings"] == 1 and summary["unscored_readings"] == 3
    assert summary["reason_counts"] == {"missing": 1, "unproven_unit": 1, "off_floor": 1}
    assert len(summary["largest_range_errors"]) == 1


def test_optional_environment_failure_preserves_range_analysis(monkeypatch):
    def failure(_floor):
        raise RuntimeError("synthetic failure")
    monkeypatch.setattr(truth, "compile_environment", failure)
    result = truth.analyse(snapshot(), [marker()])["markers"][0]
    assert result["summary"]["ranked_readings"] == 1
    assert result["ranked_readings"][0]["environment"]["classification"] == "unknown"
    assert result["ranked_readings"][0]["environment"]["environment_error"] == "RuntimeError"


def test_global_path_budget_prioritizes_largest_ranked_discrepancies_across_markers(monkeypatch):
    data = snapshot()
    count = 320
    data["context"]["layout"]["floor"][0]["receivers"] = [
        {"entity_id": f"receiver_{i}", "cords": {"x": i, "y": 0}, "height": 1}
        for i in range(count)]
    data["readings"] = [reading(tracker=beacon, receiver=f"receiver_{i}",
        entity_id=f"sensor.{beacon}_distance_to_receiver_{i}",
        measured_distance_m=offset + i, corrected_distance_m=offset + i)
        for beacon, offset in [("beacon_a", 1000), ("beacon_b", 2000)] for i in range(count)]
    data["readings"].append(reading(tracker="beacon_b", receiver="receiver_0", status="stale",
                                    observed=50, age_s=50))
    before = copy.deepcopy(data)
    calls = []
    def assess(_compiled, receiver_xy, _truth_xy, **kwargs):
        calls.append(receiver_xy)
        return {"classification": "clear", "environmental_weight": 1,
                "path_scale_px_per_m": kwargs["scale_px_per_m"]}
    monkeypatch.setattr(truth, "path_reliability", assess)
    result = truth.analyse(data, [marker(x_m=0, y_m=0), marker(target="beacon_b", x_m=0, y_m=0)])
    assert len(calls) == truth.MAX_PATH_EVALUATIONS == 256
    assert calls == [(i, 0) for i in range(319, 63, -1)]
    assert result["path_evaluation"] == {"limit": 256, "evaluated": 256, "omitted": 384, "truncated": True,
        "geometry_work_limit": truth.MAX_GEOMETRY_WORK, "geometry_work_used": 257}
    first, second = result["markers"]
    assert len(first["ranked_readings"]) == len(second["ranked_readings"]) == count
    assert first["summary"]["path_evaluated_readings"] == 0
    assert first["summary"]["path_unevaluated_readings"] == count
    assert second["summary"]["path_evaluated_readings"] == 256
    for row in first["ranked_readings"] + second["ranked_readings"][256:]:
        assert row["environment"]["classification"] == "not_evaluated"
        assert row["environment"]["environmental_weight"] is None
        assert row["environment"]["reason"] == "path_evaluation_budget"
    stale = second["unscored_readings"][0]
    assert stale["environment"]["classification"] == "not_evaluated"
    assert stale["environment"]["reason"] == "reading_unscored"
    assert data == before


def test_complex_map_is_not_compiled_or_traversed_when_work_exceeds_budget(monkeypatch):
    data = snapshot()
    data["context"]["layout"]["floor"][0]["environment"] = [
        {"id": f"trees_{i}", "type": "dense_trees", "points": [{"x": j, "y": j} for j in range(256)]}
        for i in range(128)]
    monkeypatch.setattr(truth, "compile_environment", lambda *_: pytest.fail("must budget before compiling"))
    monkeypatch.setattr(truth, "path_reliability", lambda *_: pytest.fail("must budget before traversing"))
    result = truth.analyse(data, [marker()])
    row = result["markers"][0]["ranked_readings"][0]
    assert row["status"] == "current" and row["abs_error_m"] is not None
    assert row["environment"] == truth._unevaluated_path("geometry_work_budget")
    assert result["path_evaluation"]["evaluated"] == 0
    assert result["path_evaluation"]["geometry_work_used"] == 0


def test_geometry_work_budget_counts_compile_and_every_path_globally(monkeypatch):
    data = snapshot()
    floor = data["context"]["layout"]["floor"][0]
    floor["environment"] = [{"id": "trees", "type": "dense_trees",
                             "points": [{"x": 0, "y": 0}, {"x": 10, "y": 0}, {"x": 0, "y": 10}]}]
    floor["receivers"] = [{"entity_id": f"receiver_{i}", "cords": {"x": i, "y": 0}, "height": 1} for i in range(4)]
    data["readings"] = [reading(receiver=f"receiver_{i}", entity_id=f"sensor.range_{i}",
        measured_distance_m=100 + i, corrected_distance_m=100 + i) for i in range(4)]
    cost = truth._geometry_work(floor)
    monkeypatch.setattr(truth, "MAX_GEOMETRY_WORK", cost * 3)
    compile_calls, path_calls = [], []
    def compile_floor(f):
        compile_calls.append(f)
        return None
    def path(*args, **kwargs):
        path_calls.append(args)
        return {"classification": "clear", "environmental_weight": 1}
    monkeypatch.setattr(truth, "compile_environment", compile_floor)
    monkeypatch.setattr(truth, "path_reliability", path)
    result = truth.analyse(data, [marker()])
    assert len(compile_calls) == 1 and len(path_calls) == 2
    assert result["path_evaluation"]["geometry_work_used"] == cost * 3
    rows = result["markers"][0]["ranked_readings"]
    assert [r["receiver"] for r in rows[:2]] == ["receiver_3", "receiver_2"]
    assert all(r["environment"]["reason"] == "geometry_work_budget" for r in rows[2:])


def test_large_derived_comparison_rejects_before_path_work(monkeypatch):
    data = snapshot()
    data["readings"] = [reading(entity_id=f"sensor.beacon_a_distance_to_alias_{i}") for i in range(5000)]
    before = copy.deepcopy(data)
    monkeypatch.setattr(truth, "path_reliability", lambda *_a, **_k: pytest.fail("reject oversized range rows before paths"))
    with pytest.raises(ValueError, match="comparison exceeds 2 MiB"):
        truth.analyse(data, [marker()])
    assert data == before


def test_oversized_path_payload_rejects_after_one_evaluation_without_mutating_source(monkeypatch):
    data = snapshot()
    data["readings"].append(reading(receiver="receiver_b", entity_id="sensor.beacon_a_distance_to_receiver_b"))
    before = copy.deepcopy(data)
    calls = []
    def large_path(*_a, **_k):
        calls.append(1)
        return {"classification": "building", "intersections": [{"payload": "x" * truth.MAX_COMPARISON_BYTES}]}
    monkeypatch.setattr(truth, "path_reliability", large_path)
    with pytest.raises(ValueError, match="comparison exceeds 2 MiB"):
        truth.analyse(data, [marker()])
    assert calls == [1] and data == before


@pytest.mark.parametrize("scale", [1e6 + 1, 1e-7, -1, float("inf")])
def test_floor_scale_must_fit_valid_metric_range(scale):
    data = snapshot()
    data["context"]["layout"]["floor"][0]["scale"] = scale
    with pytest.raises(ValueError, match="valid metric scale"):
        truth.analyse(data, [marker()])


def test_observation_clock_cannot_be_refreshed_by_inconsistent_reported_age():
    data = snapshot()
    data["readings"] = [reading(observed=50, age_s=1)]
    result = truth.analyse(data, [marker()])["markers"][0]
    assert not result["ranked_readings"]
    assert result["unscored_readings"][0]["source_age_s"] == 50
    assert "stale" in result["unscored_readings"][0]["reasons"]


@pytest.mark.parametrize("observed", [None, -1, 101])
def test_unknown_position_clock_is_unscored_and_keeps_selected_role(observed):
    data = snapshot()
    data["positions"][0]["outdoor"] = {"observed": observed}
    row = truth.analyse(data, [marker()])["markers"][0]["position_errors"][0]
    assert row["status"] == "unknown_age" and row["age_s"] is None
    assert row["role"] == "selected" and not row["scored"]


def test_detection_only_group_and_member_are_not_localization_error_scores():
    data = snapshot()
    data["positions"] += [
        {"ent": "beacon_b", "floor": "Garden", "cords": [0, 0], "updated": 99,
         "outdoor": {"detection_only": True, "direction_known": False, "observed": 99}},
        {"ent": "bps_group_pet", "group": True, "floor": "Garden", "cords": [0, 0], "updated": 99,
         "outdoor": {"detection_only": True, "direction_known": False, "observed": 99}}]
    result = truth.analyse(data, [marker(target="bps_group_pet")])["markers"][0]
    selected, actual_fix, detection = result["position_errors"]
    assert actual_fix["status"] == "current" and actual_fix["scored"] and actual_fix["error_m"] == 0
    for row in (selected, detection):
        assert row["status"] == "detection_only" and not row["scored"]
        assert row["error_m"] is None and row["within_accuracy"] is None
        assert row["receiver_centered_distance_m"] == 5
        assert row["direction_known"] is False
    assert result["summary"]["ranked_readings"] == 1


def test_stale_wrong_floor_detection_retains_flags_and_source_clock():
    data = snapshot()
    data["positions"][0].update(floor="Upper", updated=99,
        outdoor={"detection_only": True, "direction_known": False, "observed": 50})
    row = truth.analyse(data, [marker()])["markers"][0]["position_errors"][0]
    assert row["status"] == "stale" and row["age_s"] == 50
    assert row["stale"] and row["wrong_floor"] and row["detection_only"]
    assert not row["scored"] and "error_m" not in row
