"""Repeated live point labels persist their own captured telemetry, never calibration."""

import asyncio
import copy
import json
from types import SimpleNamespace as NS

import pytest

from bps import ground_truth as gt
from test_snapshot_truth import marker, snapshot


def request(hass, body=None, query=None):
    async def read():
        return body

    return NS(app={"hass": hass}, json=read, query=query or {})


@pytest.fixture(autouse=True)
def responses(monkeypatch):
    monkeypatch.setattr(gt.time, "time", lambda: 100.0)
    monkeypatch.setattr(gt.web, "Response", lambda **kw: NS(status=200, **kw))
    monkeypatch.setattr(
        gt.web,
        "json_response",
        lambda data, **kw: NS(json_body=data, status=kw.get("status", 200)),
    )


def mark_body(key="abcdefghijklmnop", **changes):
    return {
        "action": "mark",
        "id": key,
        "marker": marker(**{"target": "bps_group_pet", **changes}),
    }


def test_repeated_group_marks_keep_each_instant_and_restore_after_restart(
    hass, monkeypatch
):
    async def scenario():
        captured = snapshot()
        monkeypatch.setattr(gt.time, "time", lambda: captured["time"])
        before = copy.deepcopy(captured)
        calls = []

        def capture(_, now):
            calls.append(now)
            return copy.deepcopy(captured)

        api = gt.BPSGroundTruthAPI(capture)
        first = await api.post(request(hass, mark_body()))
        assert first.status == 200 and len(first.json_body["marks"]) == 1
        assert (
            first.json_body["saved"]["position_errors"][0]["target"] == "bps_group_pet"
        )
        captured["time"] = 101
        captured["positions"][0]["updated"] = 100
        captured["positions"][0]["cords"] = [40, 40]
        second = await api.post(request(hass, mark_body("anotherpoint123456", x_m=4)))
        assert second.status == 200 and len(second.json_body["marks"]) == 2
        exported = await api.get(request(hass, query={"download": "1"}))
        records = json.loads(exported.body)["records"]
        assert records[0]["report"]["positions"] == before["positions"]
        assert records[1]["report"]["positions"] == captured["positions"]
        assert records[0]["comparison"]["markers"][0]["members"] == [
            "beacon_a",
            "beacon_b",
        ]
        assert records[0]["report"]["bermuda"] == before["bermuda"]
        assert records[0]["marker"]["x_m"] == 3 and records[1]["marker"]["x_m"] == 4
        assert all(key == gt.STORAGE_KEY for key, _ in hass._store_saves)
        hass.data["bps"].pop("_ground_truth")
        restored = await api.get(request(hass, query={"download": "1"}))
        assert json.loads(restored.body)["records"] == records
        assert calls == [100, 101] and "_diagnostics" not in hass.data["bps"]
        assert (
            "only at its captured snapshot instant"
            in json.loads(restored.body)["guide"]
        )

    asyncio.run(scenario())


def test_retry_id_is_idempotent_and_cannot_label_a_different_point(hass):
    async def scenario():
        calls = []
        api = gt.BPSGroundTruthAPI(lambda *_: calls.append(True) or snapshot())
        first = await api.post(request(hass, mark_body()))
        second = await api.post(request(hass, mark_body()))
        assert first.json_body["saved"] == second.json_body["saved"] and calls == [True]
        assert len(hass._store_saves) == 1
        wrong = await api.post(request(hass, mark_body(x_m=4)))
        assert wrong.status == 400 and "different location" in wrong.json_body["error"]

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {"action": "bad"},
        {"action": "mark", "id": "too_short", "marker": marker()},
        mark_body(x_m=float("nan")),
        mark_body(floor="missing"),
        mark_body(accuracy_m=-1),
        mark_body(target="nonexistent"),
    ],
)
def test_invalid_mark_never_writes_or_changes_tracking(hass, body):
    async def scenario():
        api = gt.BPSGroundTruthAPI(lambda *_: snapshot())
        response = await api.post(request(hass, body))
        assert response.status == 400 and not hass._store_saves

    asyncio.run(scenario())


def test_write_failure_retains_previous_history_and_clear_is_durable(hass):
    async def scenario():
        api = gt.BPSGroundTruthAPI(lambda *_: snapshot())
        await api.post(request(hass, mark_body()))
        history = gt.get_history(hass)
        before = copy.deepcopy(history.records)
        save = history.store.async_save

        async def fail(_):
            raise OSError("synthetic disk failure")

        history.store.async_save = fail
        failed = await api.post(request(hass, mark_body("secondpoint123456")))
        assert failed.status == 400 and history.records == before
        cleared = await api.post(request(hass, {"action": "clear"}))
        assert cleared.status == 400 and history.records == before
        history.store.async_save = save
        cleared = await api.post(request(hass, {"action": "clear"}))
        assert cleared.status == 200 and cleared.json_body["marks"] == []
        hass.data["bps"].pop("_ground_truth")
        assert json.loads((await api.get(request(hass))).body)["marks"] == []

    asyncio.run(scenario())


def test_corrupt_store_is_preserved_and_not_overwritten(hass):
    async def scenario():
        bad = {"records": ["corrupt"], "evicted": 0}
        hass._store_backing[gt.STORAGE_KEY] = bad
        response = await gt.BPSGroundTruthAPI(lambda *_: snapshot()).post(
            request(hass, mark_body())
        )
        assert response.status == 400 and not hass._store_saves
        assert hass._store_backing[gt.STORAGE_KEY] == bad

    asyncio.run(scenario())


def test_retention_is_bounded_and_eviction_is_explicit(monkeypatch):
    records = [{"id": str(i), "report": "x" * 500} for i in range(4)]
    monkeypatch.setattr(gt, "MAX_BYTES", 1200)
    result = gt.bounded_records(records[:3], 3, 0, records[3])
    assert len(gt.encode(result)) <= 1200
    assert result["records"] == records[2:] and result["evicted"] == 2
    monkeypatch.setattr(gt, "MAX_MARKS", 1)
    result = gt.bounded_records(records[:3], 3, 0, records[3])
    assert result["records"] == records[3:] and result["evicted"] == 3


def test_cancelled_http_caller_does_not_cancel_save_or_open_another_slot(hass):
    async def scenario():
        api = gt.BPSGroundTruthAPI(lambda *_: snapshot())
        history = gt.get_history(hass)
        entered, release = asyncio.Event(), asyncio.Event()
        original_save = history.store.async_save

        async def slow(data):
            entered.set()
            await release.wait()
            await original_save(data)

        history.store.async_save = slow
        first = asyncio.create_task(api.post(request(hass, mark_body())))
        await entered.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = await api.post(request(hass, mark_body("secondpoint123456")))
        assert second.status == 429
        job = api.job
        release.set()
        await job
        await asyncio.sleep(0)
        assert api.job is None and len(history.records) == 1

    asyncio.run(scenario())
