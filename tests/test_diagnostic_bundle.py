"""Full diagnostics carry captured actual points and bounded source exports."""

import asyncio
import copy
import io
import json
import zipfile
from types import SimpleNamespace as NS

import pytest

from bps import diagnostics as diag, ground_truth as gt
from bps import recent_tracking as recent
from test_snapshot_truth import marker, snapshot


class Request:
    def __init__(self, hass, body=None, query=None, owner="owner_a"):
        self.app = {"hass": hass}
        self.body = body
        self.query = query or {}
        self.owner = NS(id=owner) if owner else None

    async def json(self):
        return self.body

    def get(self, key):
        return self.owner if key == "hass_user" else None


def parsed(response):
    text = json.loads(response.body)["text"]
    return json.loads(text[text.index("{"):])


def saved(key="abcdefghijklmnop", at=100):
    captured = snapshot()
    captured["time"] = at
    return gt.prepare(captured, marker(target="bps_group_pet", label="synthetic point"), key)


def archive(response):
    with zipfile.ZipFile(io.BytesIO(response.body)) as bundle:
        return {name: bundle.read(name) for name in bundle.namelist()}


@pytest.fixture(autouse=True)
def responses(monkeypatch):
    monkeypatch.setattr(diag.web, "Response", lambda **kw: NS(status=200, **kw))


def test_full_get_loads_saved_marks_with_captured_comparison_and_no_nested_reports(hass, monkeypatch):
    async def scenario():
        records = [saved(), saved("anotherpoint123456", 101)]
        hass._store_backing[gt.STORAGE_KEY] = {"records": records, "evicted": 3}
        monkeypatch.setattr(diag.time, "time", lambda: 200)
        api = diag.BPSDiagnosticsAPI(None, None, lambda *_: snapshot())
        response = await api.get(Request(hass, query={"report": "1"}))
        history = parsed(response)["actual_location_history"]
        assert history["status"] == "available"
        assert (history["total"], history["included"], history["omitted"], history["evicted"]) == (2, 2, 0, 3)
        assert history["marks"][0]["time"] == 101
        assert history["marks"][0]["label"] == "synthetic point"
        assert history["marks"][0]["members"] == ["beacon_a", "beacon_b"]
        assert history["marks"][0]["position_errors"] == records[1]["comparison"]["markers"][0]["position_errors"]
        capture = history["marks"][0]["capture"]
        assert capture["capture_id"] == records[1]["id"]
        assert capture["captured_at_epoch_s"] == 101
        assert capture["captured_at_utc"] == "1970-01-01T00:01:41+00:00"
        assert capture["versions"]["bps"] == records[1]["report"]["bps_version"]
        assert len(capture["settings_sha256"]) == 64
        assert all("report" not in item and "comparison" not in item for item in history["marks"])
        assert len(hass._store_saves) == 0

    asyncio.run(scenario())


def test_legacy_capture_metadata_uses_its_frozen_report_only():
    record = saved()
    record.pop("capture")
    record["report"].pop("bps_version")
    record["report"]["context"]["defaults"]["reading_max_age_s"] = 11
    summary = gt.summary(record)
    assert summary["capture"]["versions"]["bps"] is None
    assert summary["capture"]["general_settings"]["defaults"]["reading_max_age_s"] == 11
    assert summary["capture"]["settings_sha256"] != saved()["capture"]["settings_sha256"]


def test_capture_fingerprint_includes_frozen_bermuda_radio_settings():
    record = saved()
    record.pop("capture")
    record["report"]["bermuda"] = {"version": "synthetic", "entries": [{
        "entry_id": "synthetic_entry", "effective_settings": {"attenuation": 2, "rssi_offsets": {"synthetic_station": -3}}}]}
    first = gt.capture_metadata(record)
    record["report"]["bermuda"]["entries"][0]["effective_settings"]["rssi_offsets"]["synthetic_station"] = -6
    second = gt.capture_metadata(record)
    assert first["settings_sha256"] != second["settings_sha256"]
    assert second["versions"]["bermuda"] == "synthetic"
    assert second["general_settings"]["bermuda_settings"][0]["effective_settings"]["rssi_offsets"]["synthetic_station"] == -6


def test_known_position_post_includes_prior_capture_history(hass, monkeypatch):
    async def scenario():
        hass._store_backing[gt.STORAGE_KEY] = {"records": [saved()], "evicted": 0}
        monkeypatch.setattr(diag.time, "time", lambda: 101)
        api = diag.BPSDiagnosticsAPI(None, None, lambda *_: snapshot())
        response = await api.post(Request(hass, {"action": "snapshot", "known_positions": [marker()], "panel_bytes": 0}))
        assert response.status == 200
        text = json.loads(response.body)["text"]
        report = json.loads(text[text.index("{"):])
        assert report["actual_location_history"]["marks"][0]["capture"]["capture_id"] == "abcdefghijklmnop"
        assert report["known_position_comparison"]["time"] == 100

    asyncio.run(scenario())


def test_full_get_expires_history_and_reports_unreadable_store(hass, monkeypatch):
    async def scenario():
        now = 100 + gt.MAX_AGE_S + 1
        hass._store_backing[gt.STORAGE_KEY] = {"records": [saved()], "evicted": 0}
        monkeypatch.setattr(diag.time, "time", lambda: now)
        api = diag.BPSDiagnosticsAPI(None, None, lambda *_: snapshot())
        history = parsed(await api.get(Request(hass, query={"report": "1"})))["actual_location_history"]
        assert history["status"] == "empty" and history["total"] == 0 and history["evicted"] == 1
        assert hass._store_backing[gt.STORAGE_KEY]["records"] == []
        hass.data["bps"].pop("_ground_truth")
        hass._store_backing[gt.STORAGE_KEY] = {"records": ["bad"], "evicted": 0}
        report = parsed(await api.get(Request(hass, query={"report": "1"})))
        assert report["actual_location_history"]["status"] == "unavailable"
        assert "marks" in report["actual_location_history"]
        assert hass._store_backing[gt.STORAGE_KEY]["records"] == ["bad"]

    asyncio.run(scenario())


def test_failed_expiry_marks_history_unavailable_without_replacing_live_report(hass, monkeypatch):
    async def scenario():
        stored = {"records": [saved()], "evicted": 0}
        hass._store_backing[gt.STORAGE_KEY] = stored
        monkeypatch.setattr(diag.time, "time", lambda: 100 + gt.MAX_AGE_S + 1)
        history = gt.get_history(hass)

        async def fail(_):
            raise OSError("synthetic disk failure")

        history.store.async_save = fail
        api = diag.BPSDiagnosticsAPI(None, None, lambda *_: snapshot())
        report = parsed(await api.get(Request(hass, query={"report": "1"})))
        assert report["positions"] == snapshot()["positions"]
        assert report["actual_location_history"]["status"] == "unavailable"
        assert hass._store_backing[gt.STORAGE_KEY] == stored

    asyncio.run(scenario())


def test_large_history_and_panel_reservation_keep_full_report_with_explicit_counts():
    record = saved()
    records = tuple({**record, "id": f"syntheticpoint{i:04d}", "time": i}
                    for i in range(100))
    # Full saved reports may be large. The clipboard must never copy them.
    for row in records:
        row["report"] = {"payload": "x" * 200000}
    tracking = recent.RecentTrackingHistory()
    tracking.append(recent.pack({"time": 100, "context": {}, "positions": []}))
    base = {"live": "x" * 2500000, "_panel_bytes": 400000,
            "_actual_location_history": {"status": "available", "records": records, "evicted": 7},
            "_recent_tracking_snapshot": tracking.snapshot(100)}
    text = diag.format_report(base)
    report = json.loads(text[text.index("{"):])
    history = report["actual_location_history"]
    assert len(text.encode()) + base["_panel_bytes"] <= diag.MAX_REPORT_BYTES
    assert history["total"] == history["included"] + history["omitted"]
    assert history["evicted"] == 7
    assert "payload" not in text
    assert report["recent_tracking"]["metadata"]["sample_count"] == 1


def test_bundle_carries_full_saved_reports_and_manifest_without_recursion(hass, monkeypatch):
    async def scenario():
        record = saved()
        hass._store_backing[gt.STORAGE_KEY] = {"records": [record], "evicted": 2}
        monkeypatch.setattr(diag.time, "time", lambda: 100)
        api = diag.BPSDiagnosticsAPI(None, lambda *_: {"trackers": []}, lambda *_: snapshot())
        response = await api.post(Request(hass, {"action": "bundle", "panel_context": {"note": "synthetic"}}, owner=None))
        assert response.status == 200, getattr(response, "json_body", None)
        assert response.content_type == "application/zip"
        files = archive(response)
        assert {"manifest.json", "live_snapshot.json", "full_report.txt", "actual_location_history.json", "panel_context.json", "radio_survey.json"} <= files.keys()
        survey = json.loads(files["radio_survey.json"])
        assert survey["counts"]["records"] == 1
        assert survey["counts"]["records_missing_radio"] == 0
        history = json.loads(files["actual_location_history.json"])
        assert history["records"][0]["report"] == record["report"]
        assert "actual_location_history" not in history["records"][0]["report"]
        manifest = json.loads(files["manifest.json"])
        assert manifest["omissions"]["phone_survey.json"]
        assert manifest["omissions"]["diagnostic_recording.json"]
        assert json.loads(files["panel_context.json"]) == {"note": "synthetic"}

    asyncio.run(scenario())


def test_bundle_retains_more_compressed_history_than_clipboard_with_explicit_limits():
    history = recent.RecentTrackingHistory()
    for now in range(100, 120, 2):
        history.append(recent.pack({"time": now, "context": {"version": 1},
            "positions": [{"ent": "synthetic_tag", "updated": 90.125,
                "outdoor": {"observed": 90.125, "details": "synthetic receiver details " * 10_000}}]}))
    frozen = history.snapshot(118)
    actual = {"status": "empty", "records": [], "evicted": 0}
    payload = diag.build_bundle({"time": 118, "_recent_tracking_snapshot": frozen}, actual, None, None)
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        full = json.loads(bundle.read("recent_tracking.json"))
        report = bundle.read("full_report.txt").decode()
        clipboard = json.loads(report[report.index("{"):])["recent_tracking"]
        assert full["metadata"]["sample_count"] == 10 > clipboard["metadata"]["sample_count"]
        assert full["metadata"]["export_truncated"] is False
        assert all(f["positions"][0]["outdoor"]["observed"] == 90.125 for f in full["frames"])
        assert full["metadata"]["export_bytes"] <= diag.MAX_RECENT_BUNDLE_BYTES
        assert sum(i.file_size for i in bundle.infolist()) <= diag.MAX_BUNDLE_BYTES
        manifest = json.loads(bundle.read("manifest.json"))
        assert manifest["limits"]["recent_tracking_bytes"] == diag.MAX_RECENT_BUNDLE_BYTES
    assert len(history.snapshot(118).frames) == 10


def test_larger_bundle_history_does_not_displace_map_editor_and_phone_context(monkeypatch):
    monkeypatch.setattr(diag, "MAX_BUNDLE_BYTES", 200_000)
    history = recent.RecentTrackingHistory()
    for now in range(100, 140, 2):
        history.append(recent.pack({"time": now, "context": {},
            "positions": [{"ent": "synthetic_tag", "details": "x" * 10_000}]}))
    layout = {"synthetic_map_context": "x" * 50_000}
    panel = {"synthetic_editor_context": "x" * 30_000}
    phone = {"synthetic_phone_context": "x" * 8_000}
    payload = diag.build_bundle({"time": 138, "context": {"layout": layout},
        "_recent_tracking_snapshot": history.snapshot(138)},
        {"status": "empty", "records": [], "evicted": 0}, None, panel, phone)
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        assert json.loads(bundle.read("saved_layout.json")) == layout
        assert json.loads(bundle.read("panel_context.json")) == panel
        assert json.loads(bundle.read("phone_survey.json")) == phone
        result = json.loads(bundle.read("recent_tracking.json"))
        assert 0 < result["metadata"]["sample_count"] < 20
        assert result["frames"][-1]["time"] == 138
        assert result["metadata"]["export_truncated"] is True
        assert sum(i.file_size for i in bundle.infolist()) <= diag.MAX_BUNDLE_BYTES


def test_bundle_retains_direct_devices_omitted_by_clipboard_limits(hass, monkeypatch):
    from bps.bermuda_direct import BermudaDirect
    from test_bermuda_direct import prepared

    _layout, _entries, _states, _add, _old, _new, payloads, _calls = prepared(hass, monkeypatch)
    template = copy.deepcopy(next(iter(payloads["entry"]["devices"].values())))
    devices = {}
    for index in range(128):
        did = f"synthetic_device_{index}"
        row = copy.deepcopy(template)
        row.update(device_id=did, unique_id=did)
        for advert in row["scanners"].values():
            advert["device_id"] = did
            advert["history"] = [{"rssi": -80, "stamp": 998, "note": "x" * 512} for _ in range(5)]
        devices[did] = row
    payloads["entry"]["devices"] = devices

    async def scenario():
        monkeypatch.setattr(diag.time, "time", lambda: 1000)

        def report(_hass, now):
            direct = BermudaDirect(hass, now=now, include_history=True)
            return {**snapshot(), "time": now, "bermuda": {"direct": direct.diagnostics()}}

        api = diag.BPSDiagnosticsAPI(None, None, report)
        response = await api.post(Request(hass, {"action": "bundle"}, owner=None))
        assert response.status == 200
        files = archive(response)
        displayed = json.loads(files["live_snapshot.json"])["bermuda"]["direct"]
        full = json.loads(files["bermuda_measurements.json"])
        assert len(displayed["snapshots"][0]["devices"]) < 128
        assert len(full["snapshots"][0]["devices"]) == 128
        assert full["snapshots"][0]["devices"]["synthetic_device_127"]["tracked"] is True
        assert json.loads(files["manifest.json"])["versions"]["bps"]

    asyncio.run(scenario())


def test_bundle_single_flight_survives_cancelled_http_caller(hass, monkeypatch):
    async def scenario():
        api = diag.BPSDiagnosticsAPI(None, None, lambda *_: snapshot())
        entered, release = asyncio.Event(), asyncio.Event()
        original = api._report_history

        async def slow(*args):
            entered.set()
            await release.wait()
            return await original(*args)

        api._report_history = slow
        request = Request(hass, {"action": "bundle"}, owner=None)
        first = asyncio.create_task(api.post(request))
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = await api.post(request)
        assert second.status == 429
        job = api._bundle_job
        release.set()
        await job
        await asyncio.sleep(0)
        assert api._bundle_job is None

    asyncio.run(scenario())


def test_bundle_uses_only_authenticated_owner_phone_survey_and_available_recording(hass):
    async def scenario():
        owners = []

        class Survey:
            async def get(self, owner):
                owners.append(owner)
                return {"candidates": [{"owner": owner, "id": "synthetic"}]}

        class Recording:
            frames = [b"available"]

            async def export(self):
                return b'{"format":"bps-diagnostics-v1","frames":[]}'

        hass.data["bps"] = {"_phone_survey": Survey(), "_diagnostics": Recording()}
        api = diag.BPSDiagnosticsAPI(None, None, lambda *_: snapshot())
        response = await api.post(Request(hass, {"action": "bundle"}, owner="owner_a"))
        files = archive(response)
        assert owners == ["owner_a"]
        assert json.loads(files["phone_survey.json"])["candidates"] == [
            {"owner": "owner_a", "id": "synthetic"}]
        assert json.loads(files["diagnostic_recording.json"])["frames"] == []

    asyncio.run(scenario())


def test_bundle_limit_is_explicit_and_never_emits_partial_zip(monkeypatch):
    monkeypatch.setattr(diag, "MAX_BUNDLE_BYTES", 100)
    with pytest.raises(ValueError, match="Live diagnostic snapshot exceeds"):
        diag.build_bundle(snapshot(), {"status": "available", "records": (), "evicted": 0}, None, None)
