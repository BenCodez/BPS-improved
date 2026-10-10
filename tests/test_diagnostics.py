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
