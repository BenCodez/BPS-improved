"""Useful-change flags keep evidence and copying separate from no-op rows."""
import copy
import asyncio
import json

import pytest

from bps import bermuda_calibration as B
from test_bermuda_calibration import fixture, runtime


@pytest.mark.parametrize("offset_delta,useful", [(0., False), (.49, False), (.5, True), (-.5, True)])
def test_preview_material_changes_use_actual_attenuation(offset_delta, useful):
    layout, _, source, result, tracking = fixture(n=3.)
    factor = 10 ** (-offset_delta / 30)
    layout["floor"][0]["receivers"][0]["correction"] = factor
    result["receivers"]["r0"] = factor
    tracking["candidate_corrections"]["r0"] = factor
    before = copy.deepcopy((layout, source, result, tracking))
    row = B.build_preview(layout, "Yard", result, source, tracking)["rows"][0]
    assert row["candidate_eligible"] and row["transfer_current_eligible"]
    assert row["candidate_useful"] is useful
    assert row["transfer_current_useful"] is useful
    assert (layout, source, result, tracking) == before


def test_unknown_model_is_not_mislabeled_as_invalid_factor():
    layout, _, source, result, tracking = fixture()
    source["receivers"]["r0"]["attenuation"] = None
    result["bermuda_source"] = copy.deepcopy(source)
    row = B.build_preview(layout, "Yard", result, source, tracking)["rows"][0]
    assert "effective_bermuda_model_unavailable" in row["reasons"]
    assert "proposed_factor_conversion_unavailable" in row["reasons"]
    assert "proposed_bps_factor_invalid" not in row["reasons"]
    assert not row["candidate_eligible"] and not row["candidate_useful"]


def test_runtime_missing_scanners_explains_unknown_rows_without_guessing(hass):
    layout, devices, _, result, tracking = fixture()
    coordinator, _ = runtime(hass, layout, devices)
    for device in coordinator.devices.values():
        device._is_scanner = False
    source = B.runtime_context(hass, layout, "Yard")
    assert source["runtime"] == {"entries": 1, "scanners": 0, "usable_scanners": 0, "matched_receivers": 0}
    preview = B.build_preview(layout, "Yard", result, source, tracking)
    assert preview["runtime_warnings"] == ["runtime_scanners_unavailable"]
    assert all(row["scanner_mac"] is None and not row["candidate_eligible"] for row in preview["rows"])


def test_full_diagnostics_separates_loaded_runtime_from_live_scanners(hass, monkeypatch):
    from bps import bermuda_diagnostics as D
    from test_bermuda_diagnostics import fixture as diagnostic_fixture
    _entry, coordinator, *_ = diagnostic_fixture(hass, monkeypatch)
    report = D.snapshot(hass, 110)
    assert report["entries"][0]["runtime_scanner_counts"] == {
        "devices": 1, "scanners": 0, "missing_scanner_flag": 1}
    next(iter(coordinator.devices.values()))._is_scanner = True
    report = D.snapshot(hass, 110)
    assert report["entries"][0]["runtime_scanner_counts"]["scanners"] == 1


def test_preview_verification_keeps_only_bound_tracking_evidence():
    layout, _, source, result, tracking = fixture()
    tracking.update(baseline={"samples": 12, "median_m": 4, "p95_m": 8},
                    candidate={"samples": 12, "median_m": 3, "p95_m": 6},
                    verification={"sufficient": True}, private_data="not-for-export")
    preview = B.build_preview(layout, "Yard", result, source, tracking)
    assert preview["verification"]["tracking"]["baseline"]["samples"] == 12
    assert "private_data" not in preview["verification"]["tracking"]
    tracking["candidate_solved_at"] = "old-solve"
    preview = B.build_preview(layout, "Yard", result, source, tracking)
    assert preview["verification"]["tracking"] == {}
    assert preview["verification"]["tracking_verdict"] == "unavailable"


def test_authenticated_preview_cannot_certify_a_group_with_an_entirely_missing_member(hass, monkeypatch):
    from test_bermuda_rematched_preview import prepared

    async def scenario():
        post, current, solved, coordinator, _entries = await prepared(hass, monkeypatch)
        recording = hass.data["bps"]["_diagnostics"]
        data = json.loads(await recording.export())
        for context in data["contexts"].values():
            context["target_members"]["bps_group_dog"].append("missing_tag")
        async def export():
            return json.dumps(data).encode()
        recording.export = export
        before = copy.deepcopy((current, solved, coordinator.options, hass._store_saves))
        response = await post("preview_bermuda")
        assert response.status == 200
        preview = response.json_body
        assert preview["verification"]["candidate_matches_test"]
        assert preview["verification"]["tracking_verdict"] == "inconclusive"
        evidence = preview["verification"]["tracking"]
        assert evidence["trackers"]["missing_tag"]["eligible_samples"] == 0
        assert evidence["verification"]["trackers"] == 2
        assert evidence["verification"]["sufficient_trackers"] == 1
        assert not any(row["candidate_eligible"] for row in preview["rows"])
        assert (current, solved, coordinator.options, hass._store_saves) == before
    asyncio.run(scenario())
