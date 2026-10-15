"""Opt-in capture, lifecycle, exact measurement context and offline scoring."""
import asyncio
import copy
import datetime
import json
from pathlib import Path
import sys
import subprocess
from types import SimpleNamespace

import pytest

import bps
from bps import diagnostics as diag
from bps import recent_tracking as recent
from bps.storage import get_bps_data

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import bps_diagnose as analyse_tool


def configure(hass, monkeypatch):
    data = {"floor": [{"name": "Yard", "scale": 10, "map": "private.png", "receivers": [
        {"entity_id": "proxy", "cords": {"x": 0, "y": 0}, "height": 1,
         "correction": 2, "outdoor_policy": "auto"}], "environment": [], "zones": []}],
        "tracker_height": 1, "tracker_icons": {"tag": "data:private"},
        "outdoor_tracking": {"enabled": True},
        "tracker_groups": [{"id": "dog", "name": "Dog", "beacons": ["tag", "tag2"]}]}
    hass.data.update({"bps_initialized": True, "bps": {"layout": data, "apitricords": []}})
    hass.data["bps_update_task"] = SimpleNamespace(done=lambda: False)
    hass.async_create_task = asyncio.create_task
    states = {}
    hass.states = SimpleNamespace(get=states.get)
    monkeypatch.setattr(bps, "_bermuda_distance_sensor_ids", lambda _h: [
        "sensor.tag_distance_to_proxy", "sensor.tag2_distance_to_proxy"])
    return data, states


def state(value, observed=100, unit="m"):
    return SimpleNamespace(state=value, attributes={"unit_of_measurement": unit},
        last_updated=datetime.datetime.fromtimestamp(observed, datetime.timezone.utc))


def test_automatic_history_retains_stale_member_clocks_and_context_changes(hass, monkeypatch):
    async def scenario():
        data, _ = configure(hass, monkeypatch)
        hass.states.async_all = lambda _domain: []
        clock = [140.0]
        monkeypatch.setattr(bps.time, "time", lambda: clock[0])
        positions = [{"ent": "tag", "updated": 100, "cords": [10, 10],
            "outdoor": {"observed": 100, "stale_after_s": 30,
                "receiver_diagnostics": [{"receiver": "proxy", "observed": 100,
                    "measured_distance_m": 3, "used": True}]}},
            {"ent": "tag2", "updated": 139, "cords": [300, 10], "outdoor": {"observed": 139}},
            {"ent": "bps_group_dog", "group": True, "updated": 139, "cords": [300, 10],
                "anchor_beacon": "tag2", "beacon_positions": [{"ent": "tag2", "used": True,
                    "selection_reason": "anchor", "observed": 139}]}]
        hass.data["bps"]["apitricords"] = positions
        before = copy.deepcopy(positions)
        await bps._capture_recent_tracking(hass)
        clock[0] = 141
        await bps._capture_recent_tracking(hass)
        data["floor"][0]["receivers"][0]["correction"] = 4
        clock[0] = 142
        await bps._capture_recent_tracking(hass)
        history = hass.data["bps"]["_recent_tracking"]
        exported = recent.export_snapshot(history.snapshot(142))
        assert len(exported["frames"]) == 2 and len(exported["contexts"]) == 2
        first = exported["frames"][0]
        first["positions"].sort(key=lambda p: {"tag": 0, "tag2": 1, "bps_group_dog": 2}[p["ent"]])
        assert first["positions"][0]["updated"] == first["positions"][0]["outdoor"]["observed"] == 100
        assert first["positions"][0]["outdoor"]["stale"] is True
        assert first["positions"][0]["outdoor"]["receiver_diagnostics"][0]["observed"] == 100
        assert first["positions"][2]["member_status"] == {"tag": "no_eligible_fix", "tag2": "anchor"}
        assert positions == before and not hass._store_saves
        report = diag.format_report(bps._diagnostic_report_snapshot(hass, 142))
        parsed = json.loads(report[report.index("{"):])
        assert parsed["recent_tracking"]["metadata"]["sample_count"] == 2
        assert "private.png" not in report and "data:private" not in report
        await diag.shutdown(hass)
        assert "_recent_tracking" not in hass.data["bps"]
        assert recent.export_snapshot(history.snapshot(142))["frames"] == []
    asyncio.run(scenario())


def test_automatic_history_retains_detailed_many_receiver_batches(hass, monkeypatch):
    async def scenario():
        configure(hass, monkeypatch)
        hass.states.async_all = lambda _domain: []
        clock = [140.0]
        monkeypatch.setattr(bps.time, "time", lambda: clock[0])
        receiver_rows = [{"receiver": f"synthetic_station_{i}", "observed": 138.125,
            "used": True, "excluded": False, "measured_distance_m": 25.25 + i,
            "corrected_distance_m": 25.25 + i, "reliability_weight": .75,
            "range_interval": {"lower_m": 12.125, "center_m": 25.25, "upper_m": 50.5,
                "quality_samples": 32, "noise_db": 3.125, "source_observed": 138.125,
                "solver_eligible": True, "model_version": "synthetic-model"},
            "classification": "building", "environmental_weight": .75,
            "intersections": [{"type": "building", "material": "wood", "weight": .75,
                "wall_crossings": [{"walls": [1], "point": [10.125, 20.25]}],
                "path_length_m": 8.125, "boundary_crossings": 1}]}
            for i in range(24)]
        positions = [{"ent": key, "updated": 139, "cords": [25, 30],
            "outdoor": {"observed": 138.125, "receiver_diagnostics": copy.deepcopy(receiver_rows)}}
            for key in ["tag", "tag2", *[f"synthetic_tag_{i}" for i in range(6)]]]
        positions.append({"ent": "bps_group_dog", "group": True, "updated": 139,
            "cords": [25, 30], "anchor_beacon": "tag", "beacon_positions": [
                {"ent": "tag", "used": True, "selection_reason": "anchor", "observed": 138.125},
                {"ent": "tag2", "used": True, "selection_reason": "agrees_with_anchor", "observed": 138.125}]})
        hass.data["bps"]["apitricords"] = positions
        original = copy.deepcopy(positions)
        await bps._capture_recent_tracking(hass)
        clock[0] += 2
        await bps._capture_recent_tracking(hass)
        history = hass.data["bps"]["_recent_tracking"]
        stored = history.snapshot(clock[0])
        assert len(stored.frames) == 2
        assert stored.frames[0].frame_size > recent.MAX_FRAME_BYTES
        assert dict(stored.metadata)["oversize_samples"] == 0
        report = diag.format_report(bps._diagnostic_report_snapshot(hass, clock[0]))
        result = json.loads(report[report.index("{"):])["recent_tracking"]
        assert result["metadata"]["sample_count"] == 2
        assert result["metadata"]["compressed_samples"] == 2
        fixes = {p["ent"]: p for p in result["frames"][0]["positions"]}
        assert len(fixes) == len(positions)
        assert fixes["tag"]["outdoor"]["receiver_diagnostics"] == receiver_rows
        assert fixes["tag"]["outdoor"]["observed"] == 138.125
        assert fixes["bps_group_dog"]["beacon_positions"] == positions[-1]["beacon_positions"]
        assert positions == original and not hass._store_saves
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["unload", "layout"])
def test_automatic_history_discards_executor_result_after_context_change(hass, monkeypatch, change):
    async def scenario():
        configure(hass, monkeypatch)
        entered, release = asyncio.Event(), asyncio.Event()
        async def executor(fn, *args):
            entered.set()
            await release.wait()
            return fn(*args)
        hass.async_add_executor_job = executor
        task = asyncio.create_task(bps._capture_recent_tracking(hass))
        await entered.wait()
        old = hass.data["bps"]["_recent_tracking"]
        if change == "unload":
            await diag.shutdown(hass)
        else:
            hass.data["bps"]["layout"] = copy.deepcopy(hass.data["bps"]["layout"])
        release.set()
        await task
        assert recent.export_snapshot(old.snapshot(bps.time.time()))["frames"] == []
    asyncio.run(scenario())


def test_automatic_history_failure_is_rate_limited_and_disabled_clears(hass, monkeypatch):
    async def scenario():
        data, _ = configure(hass, monkeypatch)
        clock = [140.0]
        monkeypatch.setattr(bps.time, "time", lambda: clock[0])
        calls = []
        def failure(_):
            calls.append(1)
            raise recent.PackError("oversize", "synthetic oversized frame")
        monkeypatch.setattr(recent, "pack", failure)
        await bps._capture_recent_tracking(hass)
        clock[0] = 141
        await bps._capture_recent_tracking(hass)
        exported = recent.export_snapshot(hass.data["bps"]["_recent_tracking"].snapshot(141))
        assert calls == [1] and exported["metadata"]["oversize_samples"] == 1
        data["outdoor_tracking"]["enabled"] = False
        await bps._capture_recent_tracking(hass)
        assert recent.export_snapshot(hass.data["bps"]["_recent_tracking"].snapshot(141))["metadata"]["oversize_samples"] == 0
    asyncio.run(scenario())


def test_recent_report_budget_preserves_large_live_snapshot():
    history = recent.RecentTrackingHistory()
    history.append(recent.pack({"time": 100, "context": {}, "positions": []}))
    snapshot = {"live": "x" * (diag.MAX_REPORT_BYTES - 2000),
                "_recent_tracking_snapshot": history.snapshot(100)}
    text = diag.format_report(snapshot)
    assert len(text.encode()) <= diag.MAX_REPORT_BYTES
    parsed = json.loads(text[text.index("{"):])
    assert parsed["live"] == snapshot["live"]
    assert parsed["recent_tracking"]["metadata"]["export_truncated"] is True


def test_recent_unicode_history_keeps_live_report_within_byte_budget():
    history = recent.RecentTrackingHistory()
    positions = [{"ent": "tag" + str(i), "floor": "é" * 128, "zone": "é" * 128}
                 for i in range(64)]
    for now in range(100, 180, 2):
        history.append(recent.pack({"time": now, "context": {}, "positions": positions}))
    snapshot = {"live": "x" * 2500000, "_recent_tracking_snapshot": history.snapshot(180)}
    text = diag.format_report(snapshot)
    assert len(text.encode()) <= diag.MAX_REPORT_BYTES
    report = json.loads(text[text.index("{"):])
    assert report["live"] == snapshot["live"]
    assert report["recent_tracking"]["frames"][-1]["positions"] == positions
    assert report["recent_tracking"]["metadata"]["export_truncated"] is True


def test_recent_history_samples_completed_batch_and_group_once(hass, monkeypatch):
    async def scenario():
        configure(hass, monkeypatch)
        calls = []
        async def tracker(_hass, _data, eids, receiver_sources=None):
            await asyncio.sleep(0)
            calls.append(eids["entity"])
        async def group(_hass):
            assert set(calls) == {"tag", "tag2"}
            calls.append("group")
        async def capture(_hass):
            assert calls[-1] == "group"
            calls.append("capture")
        monkeypatch.setattr(bps, "process_single_entity", tracker)
        monkeypatch.setattr(bps, "update_tracker_groups", group)
        monkeypatch.setattr(bps, "_capture_recent_tracking", capture)
        await bps.process_entities(hass, [{"entity": "tag"}, {"entity": "tag2"}])
        assert calls.count("capture") == 1
    asyncio.run(scenario())


def test_recent_bermuda_context_allowlists_settings_without_state_scan(hass):
    entry = SimpleNamespace(entry_id="synthetic", options={"attenuation": 3,
        "token": "excluded-secret", "rssi_offsets": {"00:11:22:33:44:55": -3, "private-key": 8}},
        runtime_data=SimpleNamespace(coordinator=SimpleNamespace(options={"smoothing_samples": 5})))
    hass.config_entries = SimpleNamespace(async_entries=lambda domain: [entry])
    context = bps.bermuda_diagnostics.settings_snapshot(hass)
    assert context["entries"][0]["effective_settings"] == {"smoothing_samples": 5}
    assert context["entries"][0]["configured_settings"] == {"attenuation": 3,
        "rssi_offsets": {"00:11:22:33:44:55": -3}}
    assert "excluded-secret" not in json.dumps(context)


def _metadata_heavy_toolkit_profiles():
    """Maximum valid profile maps with deliberately synthetic descriptions."""
    description = "Synthetic profile metadata " + "x" * (512 - len("Synthetic profile metadata "))
    return {"receiver_profiles": {f"aa:bb:cc:dd:ee:{i:02x}": {
        "hardware_type": description, "manufacturer": description, "model": description,
        "antenna": description, "notes": description, "environment": "indoor", "attenuation": 2.,
        "rssi_offset": -3.} for i in range(128)},
        "beacon_profiles": {f"synthetic-beacon-{i}": {
            "hardware_type": description, "manufacturer": description, "model": description,
            "antenna": description, "notes": description, "environment": "indoor", "reference_power": -60.}
            for i in range(128)}, "auto_calibration": False, "filters": {"synthetic-beacon-0": "pet"}}


def test_automatic_history_packs_maximum_descriptive_profiles_and_tracks_radio_changes(hass, monkeypatch):
    async def scenario():
        configure(hass, monkeypatch)
        hass.states.async_all = lambda _domain=None: []
        registry = SimpleNamespace(entities={}, async_get=lambda _eid: None)
        monkeypatch.setattr(bps.er, "async_get", lambda _h: registry, raising=False)
        monkeypatch.setattr(bps.dr, "async_get", lambda _h: SimpleNamespace(devices={}), raising=False)
        options = {"attenuation": 2., "measurement_toolkit": _metadata_heavy_toolkit_profiles()}
        coordinator = SimpleNamespace(options=copy.deepcopy(options), devices={})
        entry = SimpleNamespace(entry_id="synthetic", title="Synthetic Bermuda", state="loaded",
            options=options, runtime_data=SimpleNamespace(coordinator=coordinator))
        hass.config_entries = SimpleNamespace(async_entries=lambda _domain: [entry])
        clock = [1000.]
        monkeypatch.setattr(bps.time, "time", lambda: clock[0])
        # This is the real automatic recorder and real pack size gate, not a
        # helper-size assertion. Earlier full profiles exceeded its 256 KiB cap.
        await bps._capture_recent_tracking(hass)
        history = hass.data["bps"]["_recent_tracking"]
        first = history.snapshot(clock[0])
        assert len(first.frames) == 1 and dict(first.metadata)["oversize_samples"] == 0
        context = json.loads(first.contexts[0][1])
        radio = context["bermuda_settings"]["entries"][0]
        assert radio["effective_settings"]["measurement_toolkit"]["receiver_profiles"]["aa:bb:cc:dd:ee:7f"] == {
            "attenuation": 2., "rssi_offset": -3.}
        assert "Synthetic profile metadata" not in first.contexts[0][1].decode()
        # Descriptive edits have no ranging effect and do not duplicate contexts.
        coordinator.options["measurement_toolkit"]["receiver_profiles"]["aa:bb:cc:dd:ee:7f"]["notes"] = "Synthetic edited note"
        clock[0] += 2.
        await bps._capture_recent_tracking(hass)
        assert len(history.snapshot(clock[0]).contexts) == 1
        # A real model change must get a different context, including a receiver
        # near the end of the maximum profile map.
        coordinator.options["measurement_toolkit"]["receiver_profiles"]["aa:bb:cc:dd:ee:7f"]["attenuation"] = 2.5
        clock[0] += 2.
        await bps._capture_recent_tracking(hass)
        final = history.snapshot(clock[0])
        assert len(final.frames) == 3 and len(final.contexts) == 2
        assert dict(final.metadata)["oversize_samples"] == 0
        report = bps._diagnostic_report_snapshot(hass, clock[0])
        full = report["bermuda"]["entries"][0]["effective_settings"]["measurement_toolkit"]
        assert full["receiver_profiles"]["aa:bb:cc:dd:ee:7f"]["attenuation"] == 2.5
        configured = report["bermuda"]["entries"][0]["configured_settings"]["measurement_toolkit"]
        assert configured["receiver_profiles"]["aa:bb:cc:dd:ee:00"]["manufacturer"].startswith("Synthetic profile metadata")
        assert full["profile_metadata_export"]["omitted_profiles"]["receiver_profiles"] > 0
        assert "Synthetic profile metadata" in diag.format_report(report)
        assert not hass._store_saves
    asyncio.run(scenario())


def test_recurring_radio_context_budget_keeps_signatures_for_compacted_entries(hass, monkeypatch):
    configure(hass, monkeypatch)
    options = {"attenuation": 2., "measurement_toolkit": _metadata_heavy_toolkit_profiles()}
    entries = [SimpleNamespace(entry_id=f"synthetic-entry-{i}", options=options,
        runtime_data=SimpleNamespace(coordinator=SimpleNamespace(options=options))) for i in range(32)]
    hass.config_entries = SimpleNamespace(async_entries=lambda _domain: entries)
    first = recent.pack(bps._recent_tracking_snapshot(hass, 1000.))
    assert len(first.context_json) < recent.MAX_CONTEXT_BYTES
    context = json.loads(first.context_json)["bermuda_settings"]["entries"]
    assert context[-1]["effective_settings"]["maps_compacted"] is True
    # Only the last, compacted entry changes. Its signature still identifies the
    # changed radio inputs even though its full numeric maps were omitted.
    last = entries[-1].runtime_data.coordinator
    last.options = copy.deepcopy(options)
    last.options["measurement_toolkit"]["beacon_profiles"]["synthetic-beacon-127"]["reference_power"] = -65.
    second = recent.pack(bps._recent_tracking_snapshot(hass, 1002.))
    assert second.context_id != first.context_id and len(second.context_json) < recent.MAX_CONTEXT_BYTES


@pytest.mark.parametrize("entry_count", [1, 4])
def test_full_report_budgets_maximum_utf8_profiles_across_all_settings_views(hass, monkeypatch, entry_count):
    configure(hass, monkeypatch)
    hass.states.async_all = lambda _domain=None: []
    registry = SimpleNamespace(entities={}, async_get=lambda _eid: None)
    monkeypatch.setattr(bps.er, "async_get", lambda _h: registry, raising=False)
    monkeypatch.setattr(bps.dr, "async_get", lambda _h: SimpleNamespace(devices={}), raising=False)
    toolkit = _metadata_heavy_toolkit_profiles()
    # Each field is valid at Bermuda's 512-character profile limit, but four
    # UTF-8 bytes per satellite made even one entry exceed BPS's 4 MiB report.
    text = "🛰" * 512
    for namespace in ("receiver_profiles", "beacon_profiles"):
        for profile in toolkit[namespace].values():
            for field in ("hardware_type", "manufacturer", "model", "antenna", "notes"):
                profile[field] = text
            profile["environment"] = "unknown"
    options = {"attenuation": 2., "measurement_toolkit": toolkit}
    entries = [SimpleNamespace(entry_id=f"synthetic-entry-{i}", title="Synthetic Bermuda", state="loaded",
        options=copy.deepcopy(options), runtime_data=SimpleNamespace(coordinator=SimpleNamespace(
            options=copy.deepcopy(options), devices={}))) for i in range(entry_count)]
    hass.config_entries = SimpleNamespace(async_entries=lambda _domain: entries)
    before = copy.deepcopy([(entry.options, entry.runtime_data.coordinator.options) for entry in entries])
    report = bps._diagnostic_report_snapshot(hass, 1000.)
    formatted = diag.format_report(report)
    assert len(formatted.encode()) < diag.MAX_REPORT_BYTES
    emitted_bytes, omitted, signatures = 0, 0, []
    for entry in report["bermuda"]["entries"]:
        for view in ("configured_settings", "effective_settings"):
            settings = entry[view]["measurement_toolkit"]
            assert settings["receiver_profiles"]["aa:bb:cc:dd:ee:00"]["notes"] == text
            for namespace in ("receiver_profiles", "beacon_profiles"):
                assert len(settings[namespace]) == 128
                for identity, profile in settings[namespace].items():
                    assert profile["environment"] == "unknown"
                    radio = {"attenuation": 2., "rssi_offset": -3.} if namespace == "receiver_profiles" else {"reference_power": -60.}
                    assert all(profile[key] == value for key, value in radio.items())
                    rich = {field: profile[field] for field in bps.bermuda_diagnostics.PROFILE_TEXT_FIELDS
                            if field in profile and field != "environment"}
                    if rich:
                        assert all(value == text for value in rich.values())
                        emitted_bytes += len(json.dumps(rich, ensure_ascii=False, separators=(",", ":")).encode())
            loss = settings.get("profile_metadata_export")
            if loss:
                omitted += sum(loss["omitted_profiles"].values())
                signatures.append(loss["signature"])
                assert loss["omitted_fields"] == 5 * sum(loss["omitted_profiles"].values())
    assert emitted_bytes <= bps.bermuda_diagnostics.MAX_PROFILE_METADATA_BYTES and omitted > 0
    initial = report["bermuda"]["entries"][0]["configured_settings"]["measurement_toolkit"]
    assert initial["receiver_profiles"]["aa:bb:cc:dd:ee:00"]["notes"] == text
    assert [(entry.options, entry.runtime_data.coordinator.options) for entry in entries] == before
    assert all(len(signature) == 64 for signature in signatures) and not hass._store_saves
    if entry_count == 1:
        # Standalone settings consumers also get a default bounded view.
        views = {key: bps.bermuda_diagnostics.settings(options) for key in ("configured_settings", "effective_settings")}
        assert len(diag.format_report({"bermuda": {"entries": [views]}}).encode()) < diag.MAX_REPORT_BYTES
    # An omitted description still has a deterministic change witness.
    entries[-1].runtime_data.coordinator.options["measurement_toolkit"]["beacon_profiles"]["synthetic-beacon-127"]["notes"] = "Synthetic changed tail"
    updated = bps._diagnostic_report_snapshot(hass, 1002.)
    last = updated["bermuda"]["entries"][-1]["effective_settings"]["measurement_toolkit"]
    previous = report["bermuda"]["entries"][-1]["effective_settings"]["measurement_toolkit"]
    assert last["profile_metadata_export"]["signature"] != previous["profile_metadata_export"]["signature"]


@pytest.mark.parametrize("entry_count", [1, 8])
def test_full_report_budgets_freeform_environment_text_from_other_bermuda_sources(hass, monkeypatch, entry_count):
    configure(hass, monkeypatch)
    hass.states.async_all = lambda _domain=None: []
    monkeypatch.setattr(bps.er, "async_get", lambda _h: SimpleNamespace(entities={}, async_get=lambda _eid: None), raising=False)
    monkeypatch.setattr(bps.dr, "async_get", lambda _h: SimpleNamespace(devices={}), raising=False)
    text = "🛰" * 512
    toolkit = {"receiver_profiles": {f"aa:bb:cc:dd:ee:{i:02x}": {
        "environment": text, "attenuation": 2.} for i in range(128)},
        "beacon_profiles": {f"synthetic-beacon-{i}": {
            "environment": text, "reference_power": -60.} for i in range(128)}}
    # This fork uses a short enum, but BPS also reads other Bermuda sources;
    # their free-form or old values must not bypass the shared text limit.
    options = {"measurement_toolkit": toolkit}
    entries = [SimpleNamespace(entry_id=f"synthetic-entry-{i}", title="Synthetic Bermuda", state="loaded",
        options=copy.deepcopy(options), runtime_data=SimpleNamespace(coordinator=SimpleNamespace(
            options=copy.deepcopy(options), devices={}))) for i in range(entry_count)]
    hass.config_entries = SimpleNamespace(async_entries=lambda _domain: entries)
    before = copy.deepcopy([(entry.options, entry.runtime_data.coordinator.options) for entry in entries])
    report = bps._diagnostic_report_snapshot(hass, 1000.)
    assert len(diag.format_report(report).encode()) < diag.MAX_REPORT_BYTES
    included, omitted = 0, 0
    for entry in report["bermuda"]["entries"]:
        for view in ("configured_settings", "effective_settings"):
            settings = entry[view]["measurement_toolkit"]
            for namespace in ("receiver_profiles", "beacon_profiles"):
                assert len(settings[namespace]) == 128
                radio = {"attenuation": 2.} if namespace == "receiver_profiles" else {"reference_power": -60.}
                for profile in settings[namespace].values():
                    assert all(profile[key] == value for key, value in radio.items())
                    if "environment" in profile:
                        assert profile["environment"] == text
                        included += len(json.dumps({"environment": text}, ensure_ascii=False, separators=(",", ":")).encode())
            loss = settings["profile_metadata_export"]
            omitted += loss["omitted_fields"]
            assert loss["omitted_fields"] == sum(loss["omitted_profiles"].values())
    assert included <= bps.bermuda_diagnostics.MAX_PROFILE_METADATA_BYTES and omitted > 0
    assert [(entry.options, entry.runtime_data.coordinator.options) for entry in entries] == before
    assert not hass._store_saves


def test_normal_full_profiles_remain_complete_and_scanner_recovery_is_allowlisted(hass):
    from bps import bermuda_diagnostics as D
    profile = {"hardware_type": "Synthetic receiver", "manufacturer": "Synthetic", "model": "Synthetic model",
        "antenna": "Synthetic antenna", "notes": "Synthetic note", "environment": "unknown", "rssi_offset": -3.}
    recovery = {"aa:bb:cc:dd:ee:00": {"entity_id": "sensor.synthetic_retained_range", "private_key": "excluded-secret"}}
    options = {"measurement_toolkit": {"receiver_profiles": {"aa:bb:cc:dd:ee:00": profile},
        "scanner_identity_recovery": recovery}}
    full = D.settings(options)["measurement_toolkit"]
    assert full["receiver_profiles"]["aa:bb:cc:dd:ee:00"] == profile
    assert "profile_metadata_export" not in full
    assert full["scanner_identity_recovery"] == {"aa:bb:cc:dd:ee:00": {"entity_id": "sensor.synthetic_retained_range"}}
    entry = SimpleNamespace(entry_id="synthetic", options=options,
        runtime_data=SimpleNamespace(coordinator=SimpleNamespace(options=options)))
    hass.config_entries = SimpleNamespace(async_entries=lambda _domain: [entry])
    before = D.settings_snapshot(hass)
    assert before["entries"][0]["effective_settings"]["measurement_toolkit"]["scanner_identity_recovery"] == full["scanner_identity_recovery"]
    recovery["aa:bb:cc:dd:ee:00"]["entity_id"] = "sensor.synthetic_different_range"
    after = D.settings_snapshot(hass)
    assert before["entries"][0]["effective_settings_signature"] != after["entries"][0]["effective_settings_signature"]
    assert "excluded-secret" not in json.dumps(full) and "excluded-secret" not in json.dumps(after)


def test_recent_inventory_cap_preserves_groups_and_members(hass, monkeypatch):
    configure(hass, monkeypatch)
    positions = [{"ent": "other" + str(i)} for i in range(140)]
    positions.extend([{"ent": "tag"}, {"ent": "tag2"},
                      {"ent": "bps_group_dog", "group": True}])
    hass.data["bps"]["apitricords"] = positions
    frame = bps._recent_tracking_snapshot(hass, 100)
    assert [p["ent"] for p in frame["positions"][:3]] == ["bps_group_dog", "tag", "tag2"]
    assert len(frame["positions"]) == 128 and frame["omitted_positions"] == 15


def test_recent_inventory_cap_does_not_prioritize_disabled_group_members(hass, monkeypatch):
    data, _ = configure(hass, monkeypatch)
    positions = [{"ent": "other" + str(i)} for i in range(144)]
    disabled = [{"id": "disabled" + str(i), "enabled": False,
                 "beacons": ["other" + str(j) for j in range(i * 16, (i + 1) * 16)]}
                for i in range(9)]
    data["tracker_groups"] = disabled + data["tracker_groups"]
    positions.extend([{"ent": "tag"}, {"ent": "tag2"},
                      {"ent": "bps_group_dog", "group": True}])
    hass.data["bps"]["apitricords"] = positions
    frame = bps._recent_tracking_snapshot(hass, 100)
    assert [p["ent"] for p in frame["positions"][:3]] == ["bps_group_dog", "tag", "tag2"]


def test_recent_history_reserves_actual_large_panel_context():
    history = recent.RecentTrackingHistory()
    for now in range(100, 180, 2):
        history.append(recent.pack({"time": now, "context": {}, "positions": [{"payload": "x" * 40000}]}))
    panel = {"editor_layout": "x" * 1100000}
    panel_bytes = len(json.dumps({"panel_context": panel}, indent=2).encode()) + 256
    text = diag.format_report({"live": "x" * 2200000, "_panel_bytes": panel_bytes,
                              "_recent_tracking_snapshot": history.snapshot(180)})
    report = json.loads(text[text.index("{"):])
    report["panel_context"] = panel
    assert len(json.dumps(report, indent=2).encode()) + text.index("{") < diag.MAX_REPORT_BYTES
    assert report["recent_tracking"]["metadata"]["export_truncated"] is True
    assert "_panel_bytes" not in report


def test_recent_omission_notice_respects_explicit_panel_budget():
    history = recent.RecentTrackingHistory()
    history.append(recent.pack({"time": 100, "context": {}, "positions": []}))
    snapshot = {"live": "x" * 3200000}
    baseline = diag.format_report(snapshot)
    reservation = diag.MAX_REPORT_BYTES - len(baseline.encode()) - 10
    text = diag.format_report({**snapshot, "_panel_bytes": reservation,
                              "_recent_tracking_snapshot": history.snapshot(100)})
    assert len(text.encode()) + reservation <= diag.MAX_REPORT_BYTES
    assert text == baseline


@pytest.mark.parametrize("value", ["-1", "nan", "99999999", "", "１２３"])
def test_full_report_rejects_invalid_panel_byte_reservation(hass, value):
    async def scenario():
        api = diag.BPSDiagnosticsAPI(None, None, lambda *_: pytest.fail("should validate before snapshot"))
        response = await api.get(SimpleNamespace(app={"hass": hass}, query={"report": "1", "panel_bytes": value}))
        assert response.status == 400
    asyncio.run(scenario())


def test_snapshot_contains_raw_units_corrected_ranges_staleness_and_missing(hass, monkeypatch):
    data, states = configure(hass, monkeypatch)
    before = copy.deepcopy(data)
    states["sensor.tag_distance_to_proxy"] = state("10", unit="ft")
    result = bps._diagnostic_snapshot(hass, ["bps_group_dog"], 140)
    first, missing = result["readings"]
    assert first["measured_distance_m"] == pytest.approx(3.048)
    assert first["corrected_distance_m"] == pytest.approx(6.096)
    assert first["horizontal_distance_m"] == pytest.approx(6.096)
    assert first["observed"] == 100 and first["age_s"] == 40
    assert first["status"] == "stale" and missing["status"] == "missing"
    assert result["context"]["target_members"] == {"bps_group_dog": ["tag", "tag2"]}
    assert "map" not in result["context"]["layout"]["floor"][0]
    assert "tracker_icons" not in result["context"]["layout"]
    assert data == before
    data["floor"][0]["receivers"][0]["correction"] = 9
    assert result["context"]["layout"]["floor"][0]["receivers"][0]["correction"] == 2


def test_full_report_covers_all_trackers_and_saved_groups_without_recording_limits(hass, monkeypatch):
    data, states = configure(hass, monkeypatch)
    keys = ["tag", "tag2"] + [f"tag{i}" for i in range(3, 24)]
    allowed = [f"sensor.{key}_distance_to_proxy" for key in keys]
    monkeypatch.setattr(bps, "_bermuda_distance_sensor_ids", lambda _h: allowed)
    for eid in allowed:
        states[eid] = state("4")
        states[eid].entity_id = eid
    hass.states.async_all = lambda _domain: list(states.values())
    data["floor"][0]["environment"] = [{"type": "building", "material": "wood", "wall_materials": ["metal", "wood", "wood"], "points": []}]
    hass.data["bps"]["calibration"] = {"mode": "auto", "auto_status": {"Yard": {"reason": "Waiting for fresh evidence"}}}
    hass.data["bps"]["apitricords"] = [{"ent": "tag", "cords": [4, 5], "outdoor": {"estimated_uncertainty_m": 3}},
        {"ent": "bps_group_dog", "beacon_positions": [{"ent": "tag", "cords": [4, 5]}]}]
    # A running recording must not restrict the report to historical membership.
    rec = diag.get_recording(hass, bps._diagnostic_snapshot)
    rec.active, rec.members, rec.targets = True, {"bps_group_dog": ("old_tag",)}, ["bps_group_dog"]
    before_layout = copy.deepcopy(data)
    before_status = copy.deepcopy(rec.status())
    report = bps._diagnostic_report_snapshot(hass, 105)
    assert {row["tracker"] for row in report["readings"]} == set(keys)
    assert report["context"]["target_members"]["bps_group_dog"] == ["tag", "tag2"]
    assert {p["ent"] for p in report["positions"]} == {"tag", "bps_group_dog"}
    assert report["calibration_status"]["auto_status"]["Yard"]["reason"] == "Waiting for fresh evidence"
    text = diag.format_report(report)
    assert '"bps_version"' in text and '"wall_materials"' in text and '"measured_distance_m"' in text
    assert "private.png" not in text and "data:private" not in text
    assert data == before_layout and rec.status() == before_status
    assert not hass._store_saves and rec.task is None
    data["floor"][0]["receivers"][0]["correction"] = 9
    assert report["context"]["layout"]["floor"][0]["receivers"][0]["correction"] == 2


def test_live_report_api_is_read_only_on_fresh_install_and_reports_oversize(hass, monkeypatch):
    async def scenario():
        configure(hass, monkeypatch)
        hass.states.async_all = lambda _domain: []
        hass.data["bps"].pop("layout")
        api = diag.BPSDiagnosticsAPI(bps._diagnostic_snapshot, bps._diagnostic_inventory, bps._diagnostic_report_snapshot)
        req = SimpleNamespace(app={"hass": hass}, query={"report": "1"})
        monkeypatch.setattr(diag.web, "Response", lambda **kw: SimpleNamespace(**kw))
        response = await api.get(req)
        assert response.headers["Cache-Control"] == "no-store"
        assert response.content_type == "application/json"
        text = json.loads(response.body)["text"]
        assert '"positions": []' in text and '"calibration_status"' in text
        assert "_diagnostics" not in hass.data["bps"]
        monkeypatch.setattr(diag, "MAX_REPORT_BYTES", 10)
        error = await api.get(req)
        assert error.status == 400 and "exceeds 4 MiB" in error.json_body["error"]
        assert "_diagnostics" not in hass.data["bps"] and not hass._store_saves
    asyncio.run(scenario())


@pytest.mark.parametrize("value,expected", [("unavailable", "unavailable"), ("unknown", "unknown"),
    ("nan", "invalid"), ("inf", "invalid"), ("-2", "invalid"), ("garbage", "invalid")])
def test_bad_sensor_states_do_not_prevent_capture(hass, monkeypatch, value, expected):
    configure(hass, monkeypatch)[1]["sensor.tag_distance_to_proxy"] = state(value)
    assert bps._diagnostic_snapshot(hass, ["tag"], 101)["readings"][0]["status"] == expected


def test_capture_no_fix_is_exportable_contexts_versioned_and_calibration_unchanged(hass, monkeypatch):
    async def scenario():
        data, states = configure(hass, monkeypatch)
        original = copy.deepcopy(data)
        record = diag.get_recording(hass, bps._diagnostic_snapshot)
        assert record.task is None and not record.active
        await record.start(["tag"], 600)
        assert len(record.frames) == 1 and len(record.contexts) == 1
        first = json.loads(record.frames[0])
        assert first["positions"] == [] and first["readings"][0]["status"] == "missing"
        # Configuration changes are preserved alongside later frames.
        data["floor"][0]["receivers"][0]["correction"] = 3
        record._append(diag.pack(bps._diagnostic_snapshot(hass, ["tag"], first["time"] + 2)))
        assert len(record.contexts) == 2
        await record.annotate({"kind": "known_position", "target": "tag", "floor": "Yard", "x_m": 3, "y_m": 4})
        await record.annotate({"kind": "clear_position", "target": "tag"})
        await record.stop()
        export = json.loads(await record.export())
        assert export["format"] == diag.FORMAT and export["active_at_export"] is False
        assert len(export["annotations"]) == 2
        assert get_bps_data(hass)["tracker_height"] == original["tracker_height"]
        assert "calibration" not in hass.data["bps"]
        assert not hass._store_saves
        await record.clear()
        with pytest.raises(ValueError, match="No diagnostic"):
            await record.export()
    asyncio.run(scenario())


def test_invalid_start_preserves_previous_capture_and_recording_limits(hass, monkeypatch):
    async def scenario():
        configure(hass, monkeypatch)
        rec = diag.get_recording(hass, bps._diagnostic_snapshot)
        await rec.start(["tag"], 10)
        await rec.stop()
        previous = await rec.export()
        for duration in (True, float("nan"), 0, 1801, "600"):
            with pytest.raises(ValueError):
                await rec.start(["tag"], duration)
            assert await rec.export() == previous
        await rec.start(["tag"], 10)
        monkeypatch.setattr(diag, "MAX_BYTES", rec.bytes)
        rec._append(diag.pack(bps._diagnostic_snapshot(hass, ["tag"], 100)))
        assert not rec.active and rec.reason == "Recording limit reached" and len(rec.frames) == 1
        await rec.stop()
    asyncio.run(scenario())


def test_shutdown_during_start_cannot_leave_background_task(hass, monkeypatch):
    async def scenario():
        configure(hass, monkeypatch)
        rec = diag.get_recording(hass, bps._diagnostic_snapshot)
        entered, release = asyncio.Event(), asyncio.Event()
        async def executor(func, *args):
            entered.set()
            await release.wait()
            return func(*args)
        hass.async_add_executor_job = executor
        starting = asyncio.create_task(rec.start(["tag"], 10))
        await entered.wait()
        stopping = asyncio.create_task(diag.shutdown(hass))
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(ValueError, match="stopped"):
            await starting
        await stopping
        assert not rec.active and rec.task is None
        diag.activate(hass)
        await rec.start(["tag"], 10)
        task = rec.task
        await diag.shutdown(hass)
        assert task.done() and not rec.active
        assert json.loads(await rec.export())["reason"] == "Integration stopped"
    asyncio.run(scenario())


def test_annotation_validation_bounds_and_export_is_detached(hass, monkeypatch):
    async def scenario():
        configure(hass, monkeypatch)
        rec = diag.get_recording(hass, bps._diagnostic_snapshot)
        await rec.start(["tag"], 10)
        valid = {"kind": "known_position", "target": "tag", "floor": "Yard", "x_m": 1, "y_m": 2}
        for changes in ({"x_m": float("nan")}, {"y_m": True}, {"floor": "No floor"}, {"target": "tag2"}, {"label": "x" * 241}):
            with pytest.raises(ValueError):
                await rec.annotate({**valid, **changes})
        await rec.annotate(valid)
        monkeypatch.setattr(diag, "MAX_ANNOTATIONS", 1)
        with pytest.raises(ValueError, match="limit"):
            await rec.annotate({"kind": "note", "label": "test"})
        export = await rec.export()
        await rec.clear()
        assert len(json.loads(export)["frames"]) == 1 and len(json.loads(export)["annotations"]) == 1
    asyncio.run(scenario())


def test_worker_failure_preserves_data_and_duration_ends_cleanly(hass, monkeypatch):
    async def scenario():
        configure(hass, monkeypatch)
        rec = diag.get_recording(hass, bps._diagnostic_snapshot)
        await rec.start(["tag"], 10)
        # An immediate wakeup exercises the real worker without a timed wait.
        rec.deadline = 0
        await rec.task
        assert rec.reason == "Duration complete" and len(rec.frames) == 1
        await rec.start(["tag"], 10)
        rec.snapshot = lambda *a: (_ for _ in ()).throw(RuntimeError("failure"))
        monkeypatch.setattr(diag, "INTERVAL_S", 0)
        await rec.task
        assert not rec.active and "failed" in rec.reason.lower()
        assert len(json.loads(await rec.export())["frames"]) == 1
    asyncio.run(scenario())


def test_api_authenticated_validation_and_download(hass, monkeypatch):
    async def scenario():
        configure(hass, monkeypatch)
        api = diag.BPSDiagnosticsAPI(bps._diagnostic_snapshot, bps._diagnostic_inventory)
        assert api.requires_auth
        async def request(body, query=None):
            async def json_body():
                return body
            return SimpleNamespace(app={"hass": hass}, query=query or {}, json=json_body)
        assert (await api.post(await request([]))).status == 400
        assert (await api.post(await request({"action": "start", "targets": [{}]}))).status == 400
        assert (await api.post(await request({"action": "start", "targets": ["unrelated"]}))).status == 400
        assert (await api.post(await request({"action": "start", "targets": ["bps_group_dog"]}))).status == 200
        monkeypatch.setattr(diag.web, "Response", lambda **kw: SimpleNamespace(**kw))
        response = await api.get(await request({}, {"download": "1"}))
        assert response.headers["Cache-Control"] == "no-store"
        assert response.content_type == "application/json"
        assert json.loads(response.body)["targets"] == ["bps_group_dog"]
        await diag.shutdown(hass)
        assert (await api.post(await request({"action": "start", "targets": ["tag"]}))).status == 400
    asyncio.run(scenario())


def bundle():
    context = {"layout": {"floor": [{"name": "Yard", "scale": 10,
        "receivers": [{"entity_id": "proxy", "cords": {"x": 0, "y": 0}}]}]},
        "target_members": {"bps_group_dog": ["tag"]}}
    position = {"ent": "tag", "floor": "Yard", "updated": 3, "cords": [40, 40],
        "raw": [60, 40], "outdoor": {"observed": 3, "estimated_uncertainty_m": 2}}
    reading = {"tracker": "tag", "floor": "Yard", "receiver": "proxy", "status": "current",
        "observed": 3, "raw_state": "6", "measured_distance_m": 6, "horizontal_distance_m": 6}
    return {"format": diag.FORMAT, "started": 0, "ended": 10, "targets": ["bps_group_dog"],
        "contexts": {"c": context}, "annotations": [
            {"time": 2, "kind": "known_position", "target": "bps_group_dog", "floor": "Yard", "x_m": 3, "y_m": 4},
            {"time": 6, "kind": "clear_position", "target": "bps_group_dog"}],
        "frames": [{"time": 1, "context_id": "c", "positions": [], "readings": []},
            {"time": 4, "context_id": "c", "positions": [position], "readings": [reading]},
            {"time": 5, "context_id": "c", "positions": [copy.deepcopy(position)], "readings": [copy.deepcopy(reading)]}]}


def test_offline_known_position_error_range_bias_and_dedup():
    report = analyse_tool.analyse(bundle())
    tracker = report["trackers"]["tag"]
    assert tracker["unique_fixes"] == 1
    assert tracker["published_error"] == {"samples": 1, "median_m": 1, "p95_m": 1}
    assert tracker["raw_error"]["median_m"] == 3
    assert tracker["uncertainty_coverage_fraction"] == 1
    assert report["receivers"][0]["horizontal_bias_median_m"] == 1
    assert report["receivers"][0]["known_position_samples"] == 1
    assert report["receivers"][0]["status"] == {"current": 2}


@pytest.mark.parametrize("observed", [1, 8, None])
def test_old_future_unknown_observations_never_gain_ground_truth(observed):
    data = bundle()
    for frame in data["frames"][1:]:
        frame["positions"][0]["outdoor"]["observed"] = observed
        frame["readings"][0]["observed"] = observed
    report = analyse_tool.analyse(data)
    assert report["trackers"]["tag"]["published_error"]["samples"] == 0
    assert report["receivers"][0]["known_position_samples"] == 0


def test_future_publication_clock_does_not_score_a_fix_with_older_valid_measurements():
    data = bundle()
    for frame in data["frames"][1:]:
        frame["positions"][0]["updated"] = 8
    report = analyse_tool.analyse(data)
    assert report["trackers"]["tag"]["published_error"]["samples"] == 0
    assert report["receivers"][0]["known_position_samples"] == 1


def test_clear_marker_excludes_later_samples_and_wrong_floor_is_reported():
    data = bundle()
    wrong = copy.deepcopy(data["frames"][1])
    wrong["time"] = 5
    wrong["positions"][0].update({"floor": "House", "updated": 5})
    wrong["positions"][0]["outdoor"]["observed"] = 5
    after = copy.deepcopy(data["frames"][1])
    after["time"] = 8
    after["positions"][0].update({"updated": 8})
    after["positions"][0]["outdoor"]["observed"] = 8
    after["readings"][0]["observed"] = 8
    data["frames"].extend([wrong, after])
    report = analyse_tool.analyse(data)
    assert report["trackers"]["tag"]["wrong_floor_fixes"] == 1
    assert report["trackers"]["tag"]["published_error"]["samples"] == 1
    assert report["receivers"][0]["known_position_samples"] == 1


def test_group_uses_original_observation_clock_even_with_age_gate_disabled():
    data = bundle()
    for frame in data["frames"][1:]:
        original = frame["positions"][0]
        original["outdoor"]["observed"] = 1
        frame["positions"].append({"ent": "bps_group_dog", "group": True, "floor": "Yard", "updated": 3,
            "cords": [40, 40], "beacon_positions": [{"ent": "tag", "updated": 3, "used": True}]})
    report = analyse_tool.analyse(data)
    assert report["trackers"]["bps_group_dog"]["published_error"]["samples"] == 0


def test_context_change_never_rescales_an_older_fix():
    data = bundle()
    data["contexts"]["new"] = copy.deepcopy(data["contexts"]["c"])
    data["contexts"]["new"]["layout"]["floor"][0]["scale"] = 20
    data["frames"][1]["context_id"] = "new"
    data["frames"][2]["context_id"] = "new"
    assert analyse_tool.analyse(data)["trackers"]["tag"]["published_error"]["samples"] == 0


@pytest.mark.parametrize("change", ["scale", "geometry", "calibration"])
@pytest.mark.parametrize("age_gate", [0, 30])
def test_context_change_rejects_old_inputs_in_a_new_solve_and_old_receiver_samples(change, age_gate):
    data = bundle()
    data["contexts"]["c"]["layout"]["reading_max_age"] = age_gate
    newer = copy.deepcopy(data["contexts"]["c"])
    floor = newer["layout"]["floor"][0]
    if change == "scale":
        floor["scale"] = 20
    elif change == "geometry":
        floor["receivers"][0]["cords"]["x"] = 10
    else:
        floor["receivers"][0]["correction"] = 2
        newer["calibration"] = {"applied": {"proxy": 2}, "last_solved_at": 4}
    data["contexts"]["new"] = newer
    for frame in data["frames"][1:]:
        frame["context_id"] = "new"
        position = frame["positions"][0]
        position["updated"] = frame["time"]  # A new solve from retained sensor values.
        position["diagnostic_inputs"] = [{"receiver": "proxy", "observed": 3},
                                         {"receiver": "other", "observed": 4}]
        frame["positions"].append({"ent": "bps_group_dog", "group": True, "floor": "Yard",
            "updated": position["updated"] if age_gate == 0 else 3, "cords": position["cords"],
            "beacon_positions": [{"ent": "tag", "used": True, "floor": "Yard", "cords": position["cords"],
                "updated": position["updated"] if age_gate == 0 else 3}]})
    report = analyse_tool.analyse(data)
    for key in ("tag", "bps_group_dog"):
        assert report["trackers"][key]["published_error"]["samples"] == 0
        assert report["trackers"][key]["raw_error"]["samples"] == 0
        assert report["trackers"][key]["uncertainty_coverage_samples"] == 0
    receiver = next(row for row in report["receivers"] if row["receiver"] == "proxy")
    assert receiver["known_position_samples"] == 0
    assert receiver["status"] == {"current": 2}, 'availability reporting remains useful'
    # Once every contributing measurement belongs to the new context, score it.
    fresh = copy.deepcopy(data["frames"][2])
    for position in fresh["positions"]:
        position["updated"] = 5
        if position.get("group"):
            position["beacon_positions"][0]["updated"] = 5
        else:
            position["outdoor"]["observed"] = 5
            for reading in position["diagnostic_inputs"]:
                reading["observed"] = 5
    fresh["readings"][0]["observed"] = 5
    data["frames"].append(fresh)
    report = analyse_tool.analyse(data)
    assert report["trackers"]["tag"]["published_error"]["samples"] == 1
    assert report["trackers"]["bps_group_dog"]["published_error"]["samples"] == 1
    assert next(row for row in report["receivers"] if row["receiver"] == "proxy")["known_position_samples"] == 1


def test_legacy_solve_timestamp_alone_is_not_measurement_evidence():
    data = bundle()
    for frame in data["frames"][1:]:
        frame["positions"][0].pop("outdoor")
    assert analyse_tool.analyse(data)["trackers"]["tag"]["published_error"]["samples"] == 0
    for frame in data["frames"][1:]:
        frame["positions"][0]["diagnostic_inputs"] = [{"receiver": "proxy", "observed": 3}]
    assert analyse_tool.analyse(data)["trackers"]["tag"]["published_error"]["samples"] == 1


def test_offline_command_reads_export_and_writes_machine_readable_report(tmp_path):
    source, output = tmp_path / "capture.json", tmp_path / "report.json"
    source.write_text(json.dumps(bundle()), encoding="utf-8")
    tool = Path(analyse_tool.__file__)
    result = subprocess.run([sys.executable, str(tool), str(source), "--out", str(output)],
                            capture_output=True, text=True, check=True)
    report = json.loads(result.stdout)
    assert json.loads(output.read_text()) == report
    assert report["trackers"]["tag"]["published_error"]["median_m"] == 1
    source.write_text("[]", encoding="utf-8")
    failed = subprocess.run([sys.executable, str(tool), str(source)], capture_output=True, text=True)
    assert failed.returncode == 2 and "Expected a bps-diagnostics-v1" in failed.stderr


@pytest.mark.parametrize("member_time", [1, 3])
def test_new_original_cannot_refresh_an_older_group_for_ground_truth(member_time):
    data = bundle()
    for frame in data["frames"][1:]:
        frame["positions"][0].update({"updated": 4})
        frame["positions"][0]["outdoor"]["observed"] = 4
        frame["positions"].append({"ent": "bps_group_dog", "group": True, "floor": "Yard", "updated": member_time,
            "cords": [40, 40], "beacon_positions": [{"ent": "tag", "updated": member_time,
                "used": True, "cords": [40, 40], "floor": "Yard"}]})
    report = analyse_tool.analyse(data)
    assert report["trackers"]["bps_group_dog"]["published_error"]["samples"] == 0


@pytest.mark.parametrize("age_gate", [0, 30])
def test_matching_group_sources_are_scored_with_the_correct_fusion_clock(age_gate):
    data = bundle()
    data["contexts"]["c"]["layout"]["reading_max_age"] = age_gate
    for frame in data["frames"][1:]:
        frame["positions"][0]["updated"] = 4
        member_time = 4 if age_gate == 0 else 3
        frame["positions"].append({"ent": "bps_group_dog", "group": True, "floor": "Yard", "updated": member_time,
            "cords": [40, 40], "beacon_positions": [{"ent": "tag", "updated": member_time,
                "used": True, "cords": [40, 40], "floor": "Yard"}]})
    report = analyse_tool.analyse(data)
    assert report["trackers"]["bps_group_dog"]["published_error"]["samples"] == 1


def test_repeated_solves_of_identical_observations_are_not_independent_error_samples():
    data = bundle()
    data["frames"][2]["positions"][0]["updated"] = 5
    assert analyse_tool.analyse(data)["trackers"]["tag"]["published_error"]["samples"] == 1


def test_recorded_group_keeps_original_measurements_when_outdoor_tracking_is_disabled(hass, monkeypatch):
    async def scenario():
        data, _states = configure(hass, monkeypatch)
        rec = diag.get_recording(hass, bps._diagnostic_snapshot)
        await rec.start(["bps_group_dog"], 10)
        data["outdoor_tracking"]["enabled"] = False
        snap = bps._diagnostic_snapshot(hass, ["bps_group_dog"], 100)
        assert {row["tracker"] for row in snap["readings"]} == {"tag", "tag2"}
        assert snap["context"]["target_members"] == {"bps_group_dog": ["tag", "tag2"]}
        await rec.clear()
        assert rec.members == {} and not rec.sources
    asyncio.run(scenario())


def test_recording_freezes_members_through_group_edits_and_restarts_with_new_members(hass, monkeypatch):
    async def scenario():
        data, _states = configure(hass, monkeypatch)
        monkeypatch.setattr(bps, "_bermuda_distance_sensor_ids", lambda _h: [
            f"sensor.{tag}_distance_to_proxy" for tag in ("tag", "tag2", "tag3")])
        rec = diag.get_recording(hass, bps._diagnostic_snapshot)
        await rec.start(["bps_group_dog"], 10)
        await rec.annotate({"kind": "known_position", "target": "bps_group_dog", "floor": "Yard", "x_m": 1, "y_m": 2})
        data["tracker_groups"][0]["beacons"] = ["tag3"]
        def changed_snapshot(*args):
            snap = bps._diagnostic_snapshot(*args)
            rec.deadline = 0  # Finish after the real worker appends this frame.
            return snap
        rec.snapshot = changed_snapshot
        monkeypatch.setattr(diag, "INTERVAL_S", 0)
        await rec.task
        export = json.loads(await rec.export())
        assert len(export["frames"]) == 2
        assert rec.sources == frozenset({"tag", "tag2"})
        assert rec.members == {"bps_group_dog": ("tag", "tag2")}
        for frame in export["frames"]:
            assert {row["tracker"] for row in frame["readings"]} == {"tag", "tag2"}
            context = export["contexts"][frame["context_id"]]
            assert context["target_members"] == {"bps_group_dog": ["tag", "tag2"]}
        assert len(export["annotations"]) == 1
        assert any(context["layout"]["tracker_groups"][0]["beacons"] == ["tag3"]
                   for context in export["contexts"].values()), 'live settings changes remain visible'
        rec.snapshot = bps._diagnostic_snapshot
        await rec.start(["bps_group_dog"], 10)
        assert rec.sources == frozenset({"tag3"})
        assert rec.members == {"bps_group_dog": ("tag3",)}
        assert {row["tracker"] for row in json.loads(rec.frames[0])["readings"]} == {"tag3"}
        await rec.stop()
    asyncio.run(scenario())
