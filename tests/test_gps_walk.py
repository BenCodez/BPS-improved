"""Moving GPS labels retain independent clocks and never become calibration."""

import asyncio
import copy
import json
import math

import pytest

from bps import ground_truth as gt
from bps.gps_walk import prepare_reference
from test_ground_truth import request
from test_snapshot_truth import snapshot


def point(x=0, y=0):
    return {"latitude": 10 - y / 6371008.8 * 180 / math.pi,
            "longitude": 20 + x / (6371008.8 * math.cos(math.radians(10))) * 180 / math.pi}


def capture():
    data = snapshot()
    data["context"]["layout_revision"] = "c" * 64
    floor = data["context"]["layout"]["floor"][0]
    floor["georeference"] = {"version": 1, "anchors": [
        {**point(x, y), "x": x * 10, "y": y * 10, "accuracy_m": 2, "label": "Synthetic"}
        for x, y in [(0, 0), (30, 0), (0, 30)]
    ]}
    return data


def body(data=None):
    data = data or capture()
    floor = data["context"]["layout"]["floor"][0]
    return {"action": "gps_mark", "id": "moving_gps_mark_1234",
            "marker": {"target": "bps_group_pet", "floor": "Garden", "label": "Walk"},
            "gps": {**point(3, 4), "timestamp_ms": 99000, "accuracy_m": 3,
                    "mode": "walk", "dog_distance_m": 5, "session_id": "gps_session_123456",
                    "layout_revision": data["context"]["layout_revision"],
                    "speed_m_s": 1, "heading_deg": 80,
                    "alignment": {"scale": floor["scale"], "georeference": copy.deepcopy(floor["georeference"])}}}


def test_projection_includes_gps_map_separation_motion_and_preserves_input():
    data, b = capture(), body()
    before = copy.deepcopy((data, b))
    marker, ref = prepare_reference(data, b["marker"], b["gps"])
    assert marker["x_m"] == pytest.approx(3)
    assert marker["y_m"] == pytest.approx(4)
    assert marker["accuracy_m"] == sum(ref["tolerance_components"].values())
    assert ref["tolerance_components"]["gps_age_motion_m"] == pytest.approx(3)
    assert ref["tolerance_components"]["alignment_m"] > 2
    assert ref["gps_observed_at_epoch_s"] == 99
    assert ref["ble_snapshot_at_epoch_s"] == 100
    assert not ref["stationary_calibration_eligible"]
    assert ref["request"]["gps"] == b["gps"]
    assert (data, b) == before
    ride = copy.deepcopy(b["gps"]); ride["mode"] = "ride"
    _, ride_ref = prepare_reference(data, b["marker"], ride)
    assert ride_ref["tolerance_components"]["gps_age_motion_m"] == pytest.approx(10)


@pytest.mark.parametrize("changes", [
    {"timestamp_ms": 96000}, {"timestamp_ms": 102000}, {"accuracy_m": 21},
    {"accuracy_m": float("nan")}, {"latitude": True}, {"longitude": 190},
    {"dog_distance_m": -1}, {"dog_distance_m": 31}, {"speed_m_s": 4},
    {"mode": "fast"}, {"session_id": "short"}, {"heading_deg": 361},
    {"alignment": {}}, {"extra": "field"}, {"mode": []}, {"layout_revision": "d" * 64},
])
def test_invalid_or_stale_gps_is_not_a_reference(changes):
    b = body(); b["gps"].update(changes)
    with pytest.raises(ValueError):
        prepare_reference(capture(), b["marker"], b["gps"])


def test_mismatched_metric_scale_converts_reference_accuracy_too():
    data = capture(); data["context"]["layout"]["floor"][0]["scale"] = 5
    b = body(data)
    marker, ref = prepare_reference(data, b["marker"], b["gps"])
    assert marker["x_m"] == pytest.approx(6)
    assert ref["tolerance_components"]["gps_m"] == pytest.approx(6)
    assert ref["tolerance_components"]["dog_distance_m"] == pytest.approx(10)


def test_saved_gps_capture_idempotence_restoration_and_export(hass, monkeypatch):
    monkeypatch.setattr(gt.time, "time", lambda: 100)
    from types import SimpleNamespace as NS
    monkeypatch.setattr(gt.web, "json_response", lambda data, **kw: NS(json_body=data, status=kw.get("status", 200)))
    monkeypatch.setattr(gt.web, "Response", lambda **kw: NS(status=200, **kw))

    async def scenario():
        calls = []
        api = gt.BPSGroundTruthAPI(lambda *_: calls.append(True) or capture())
        b = body()
        result = await api.post(request(hass, b))
        assert result.status == 200
        assert result.json_body["saved"]["gps_reference"]["gps_age_s"] == 1
        monkeypatch.setattr(gt.time, "time", lambda: 110)
        retry = await api.post(request(hass, b))
        assert retry.status == 200 and calls == [True]
        bad = copy.deepcopy(b); bad["gps"]["dog_distance_m"] = 10
        assert (await api.post(request(hass, bad))).status == 400
        hass.data["bps"].pop("_ground_truth")
        records = json.loads((await api.get(request(hass, query={"download": "1"}))).body)["records"]
        ref = records[0]["gps_reference"]
        assert ref == records[0]["report"]["gps_reference"]
        assert ref["request"]["gps"] == b["gps"]
        assert records[0]["capture"]["settings_sha256"]
        assert all(key == gt.STORAGE_KEY for key, _ in hass._store_saves)
    asyncio.run(scenario())


def test_slow_expiry_does_not_refresh_an_old_gps_fix(hass, monkeypatch):
    from types import SimpleNamespace as NS
    clock = [100.0]
    monkeypatch.setattr(gt.time, "time", lambda: clock[0])
    monkeypatch.setattr(gt.web, "json_response", lambda data, **kw: NS(json_body=data, status=kw.get("status", 200)))

    async def scenario():
        history = gt.get_history(hass)
        expire = history.expire

        async def slow_expire(now):
            await expire(now)
            await asyncio.sleep(0)
            clock[0] = 110.0

        history.expire = slow_expire
        times = []

        def fresh_snapshot(_, now):
            times.append(now)
            result = capture()
            result["time"] = now
            return result

        api = gt.BPSGroundTruthAPI(fresh_snapshot)
        result = await api.post(request(hass, body()))
        assert result.status == 400
        assert "stale" in result.json_body["error"]
        assert times == [110.0]
        assert not history.records and not hass._store_saves
    asyncio.run(scenario())
