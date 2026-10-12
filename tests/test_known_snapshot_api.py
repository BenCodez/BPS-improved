"""Read-only full snapshot API, bounded reports and preserved live context."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from bps import diagnostics as diag
from test_snapshot_truth import marker, snapshot


def request(hass, body):
    async def read_json():
        return body
    return SimpleNamespace(app={"hass": hass}, json=read_json)


@pytest.fixture
def responses(monkeypatch):
    def response(**kwargs):
        return SimpleNamespace(status=200, **kwargs)
    monkeypatch.setattr(diag.web, "Response", response)


def parse_report(text):
    return json.loads(text[text.index("{"):])


def test_snapshot_action_captures_once_executes_off_loop_and_never_gets_recording(hass, monkeypatch, responses):
    async def scenario():
        original = snapshot()
        original["readings"].append({"tracker": "other_beacon", "raw_state": "17", "unit": "m"})
        original["scanner_linking"] = {"placed": [], "unplaced": ["unplaced_receiver"]}
        original["runtime"] = {"recording": {"active": True, "frames": 7}}
        hass.data = {"bps": {"layout": {"sentinel": True}, "_diagnostics": "existing"}}
        state_before = copy.deepcopy(hass.data)
        captures, jobs = [], []
        def capture(_hass, now):
            assert _hass is hass
            captures.append(now)
            return copy.deepcopy(original)
        async def executor(fn, *args):
            jobs.append(fn)
            return fn(*args)
        hass.async_add_executor_job = executor
        monkeypatch.setattr(diag, "get_recording", lambda *_: pytest.fail("snapshot must never access recorder"))
        api = diag.BPSDiagnosticsAPI(None, None, capture)
        response = await api.post(request(hass, {"action": "snapshot", "known_positions": [marker()]}))
        assert response.status == 200 and response.headers["Cache-Control"] == "no-store"
        data = json.loads(response.body)
        report = parse_report(data["text"])
        for key, value in original.items():
            assert report[key] == value
        assert report["known_positions"] == [{**marker(), "accuracy_m": 1, "label": ""}]
        assert report["known_position_comparison"] == data["comparison"]
        assert data["read_only"] is True and data["time"] == original["time"]
        assert len(captures) == 1 and jobs == [diag.format_known_position_report]
        assert hass.data == state_before and not hass._store_saves
    asyncio.run(scenario())


def test_snapshot_does_not_create_recording_when_stopped(hass, monkeypatch, responses):
    async def scenario():
        hass.data = {}
        api = diag.BPSDiagnosticsAPI(None, None, lambda *_: snapshot())
        response = await api.post(request(hass, {"action": "snapshot", "known_positions": [marker()]}))
        assert response.status == 200 and hass.data == {}
    asyncio.run(scenario())


@pytest.mark.parametrize("panel", [-1, True, "100", 1.5, diag.MAX_REPORT_BYTES + 1])
def test_rejects_invalid_panel_before_capturing(hass, panel):
    async def scenario():
        api = diag.BPSDiagnosticsAPI(None, None, lambda *_: pytest.fail("must validate first"))
        response = await api.post(request(hass, {"action": "snapshot", "known_positions": [marker()], "panel_bytes": panel}))
        assert response.status == 400 and hass.data == {}
    asyncio.run(scenario())


@pytest.mark.parametrize("body", [[], None, {"action": "snapshot"},
    {"action": "snapshot", "known_positions": [marker(x_m="3")]},
    {"action": "snapshot", "known_positions": [marker(target="unknown")]}])
def test_rejects_bad_body_or_markers_without_recording(hass, monkeypatch, body):
    async def scenario():
        monkeypatch.setattr(diag, "get_recording", lambda *_: pytest.fail("must not create recorder"))
        api = diag.BPSDiagnosticsAPI(None, None, lambda *_: snapshot())
        response = await api.post(request(hass, body))
        assert response.status == 400 and not hass.data
    asyncio.run(scenario())


def test_missing_report_callback_is_clear_read_only_error(hass):
    async def scenario():
        response = await diag.BPSDiagnosticsAPI(None, None).post(request(hass,
            {"action": "snapshot", "known_positions": [marker()]}))
        assert response.status == 400 and "unavailable" in response.json_body["error"] and not hass.data
    asyncio.run(scenario())


def test_full_report_and_panel_byte_bound_preserves_data_or_rejects():
    data = snapshot()
    data["all_live_data"] = "é" * 1000
    original = copy.deepcopy(data)
    result = diag.format_known_position_report(data, [marker()])
    assert len(result["text"].encode()) <= diag.MAX_REPORT_BYTES
    assert parse_report(result["text"])["all_live_data"] == data["all_live_data"]
    assert data == original
    data["_panel_bytes"] = diag.MAX_REPORT_BYTES - len(result["text"].encode()) + 1
    with pytest.raises(ValueError, match="panel context"):
        diag.format_known_position_report(data, [marker()])
    data["_panel_bytes"] = 0
    data["all_live_data"] = "x" * diag.MAX_REPORT_BYTES
    with pytest.raises(ValueError, match="4 MiB"):
        diag.format_known_position_report(data, [marker()])


def test_original_get_live_report_stays_unchanged(hass, monkeypatch, responses):
    async def scenario():
        monkeypatch.setattr(diag, "get_recording", lambda *_: pytest.fail("must not create recorder"))
        original = snapshot()
        api = diag.BPSDiagnosticsAPI(None, None, lambda *_: copy.deepcopy(original))
        response = await api.get(SimpleNamespace(app={"hass": hass}, query={"report": "1"}))
        data = json.loads(response.body)
        assert set(data) == {"text"}
        assert "known_positions" not in parse_report(data["text"])
        assert not hass.data
    asyncio.run(scenario())
