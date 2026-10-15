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


@pytest.mark.parametrize("silent_failure", [False, True])
def test_write_failure_retains_previous_history_and_clear_is_durable(
    hass, silent_failure
):
    async def scenario():
        api = gt.BPSGroundTruthAPI(lambda *_: snapshot())
        await api.post(request(hass, mark_body()))
        history = gt.get_history(hass)
        before = copy.deepcopy(history.records)
        save = history.store.async_save

        async def fail(_):
            if not silent_failure:
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


def record_at(key, captured_at):
    captured = snapshot()
    captured["time"] = captured_at
    return gt.prepare(captured, mark_body()["marker"], key)


def test_a_day_of_surveys_exceeding_the_old_size_limit_survives_append_and_restart(hass):
    async def scenario():
        records = [record_at(f"survey-location-{i:016d}", 100 + i) for i in range(30)]
        for record in records:
            record["report"]["synthetic_raw_evidence"] = "x" * 600_000
        stored = {"records": records, "evicted": 4}
        assert len(gt.encode(stored)) > 16 * 1024 * 1024
        hass._store_backing[gt.STORAGE_KEY] = stored
        api = gt.BPSGroundTruthAPI(lambda *_: snapshot())
        overview = json.loads((await api.get(request(hass))).body)
        assert len(overview["marks"]) == 30 and overview["evicted"] == 4
        appended = await api.post(request(hass, mark_body("later-desktop-download")))
        assert appended.status == 200 and len(appended.json_body["marks"]) == 31
        assert appended.json_body["evicted"] == 4
        hass.data["bps"].pop("_ground_truth")
        restarted = json.loads((await api.get(request(hass))).body)
        assert len(restarted["marks"]) == 31 and restarted["evicted"] == 4
        assert gt.get_history(hass).records[0]["report"]["synthetic_raw_evidence"] == "x" * 600_000

    asyncio.run(scenario())


def test_existing_marks_older_than_a_week_remain_available_for_desktop_download(hass, monkeypatch):
    async def scenario():
        record = record_at("older-survey-point", 100)
        hass._store_backing[gt.STORAGE_KEY] = {"records": [record], "evicted": 0}
        monkeypatch.setattr(gt.time, "time", lambda: 100 + 10 * 86400)
        api = gt.BPSGroundTruthAPI(lambda *_: snapshot())
        result = json.loads((await api.get(request(hass, query={"download": "1"}))).body)
        assert result["records"] == [record] and result["evicted"] == 0
        assert result["limits"] == {"max_marks": 500, "max_bytes": 128 * 1024 * 1024,
                                    "max_age_s": 30 * 86400}

    asyncio.run(scenario())


@pytest.mark.parametrize("download", [False, True])
def test_history_access_persists_expiration_and_preserves_boundary_and_future_marks(
    hass, monkeypatch, download
):
    async def scenario():
        now = 100 + gt.MAX_AGE_S + 1
        records = [
            record_at("expiredpoint123456", 100),
            record_at("boundarypoint12345", 101),
            record_at("futurepoint1234567", now + 60),
        ]
        hass._store_backing[gt.STORAGE_KEY] = {"records": records, "evicted": 4}
        monkeypatch.setattr(gt.time, "time", lambda: now)
        api = gt.BPSGroundTruthAPI(lambda *_: snapshot())

        response = await api.get(
            request(hass, query={"download": "1"} if download else {})
        )
        assert response.status == 200
        overview = json.loads(response.body)
        assert [mark["id"] for mark in overview["marks"]] == [
            record["id"] for record in records[1:]
        ]
        assert overview["evicted"] == 5
        if download:
            assert overview["records"] == records[1:]
        expected = {"records": records[1:], "evicted": 5}
        assert hass._store_backing[gt.STORAGE_KEY] == expected
        history = gt.get_history(hass)
        assert history.records == records[1:] and history.evicted == 5
        assert len(hass._store_saves) == 1

        hass.data["bps"].pop("_ground_truth")
        restarted = await api.get(request(hass, query={"download": "1"}))
        assert json.loads(restarted.body)["records"] == records[1:]
        assert json.loads(restarted.body)["evicted"] == 5
        assert len(hass._store_saves) == 1, "restart must not count expiry twice"

    asyncio.run(scenario())


def test_mark_access_expires_history_before_duplicate_id_lookup(hass, monkeypatch):
    async def scenario():
        expired = record_at("abcdefghijklmnop", 100)
        hass._store_backing[gt.STORAGE_KEY] = {"records": [expired], "evicted": 0}
        now = 100 + gt.MAX_AGE_S + 1
        monkeypatch.setattr(gt.time, "time", lambda: now)
        calls = []

        def capture(_, captured_at):
            calls.append(captured_at)
            return {**snapshot(), "time": captured_at}

        api = gt.BPSGroundTruthAPI(capture)
        response = await api.post(request(hass, mark_body(x_m=4)))
        assert response.status == 200
        assert calls == [now]
        assert response.json_body["saved"]["time"] == now
        assert response.json_body["saved"]["x_m"] == 4
        assert len(response.json_body["marks"]) == 1
        assert response.json_body["evicted"] == 1
        assert hass._store_backing[gt.STORAGE_KEY]["records"][0]["time"] == now
        assert hass._store_backing[gt.STORAGE_KEY]["evicted"] == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("silent_drop", [False, True])
def test_failed_expiry_preserves_records_and_clear_bypasses_expiry(
    hass, monkeypatch, silent_drop
):
    async def scenario():
        records = [record_at("expiredpoint123456", 100)]
        stored = {"records": records, "evicted": 3}
        hass._store_backing[gt.STORAGE_KEY] = copy.deepcopy(stored)
        monkeypatch.setattr(gt.time, "time", lambda: 100 + gt.MAX_AGE_S + 1)
        history = gt.get_history(hass)
        await history.load()
        save = history.store.async_save

        async def fail_expiry(data):
            if data["evicted"]:
                if silent_drop:
                    return
                raise OSError("synthetic expiry write failure")
            await save(data)

        history.store.async_save = fail_expiry
        api = gt.BPSGroundTruthAPI(lambda *_: snapshot())
        failed = await api.get(request(hass, query={"download": "1"}))
        assert failed.status == 400 and failed.json_body["error"]
        assert history.records == records and history.evicted == 3
        assert hass._store_backing[gt.STORAGE_KEY] == stored

        cleared = await api.post(request(hass, {"action": "clear"}))
        assert cleared.status == 200
        assert cleared.json_body["marks"] == [] and cleared.json_body["evicted"] == 0
        assert history.records == [] and history.evicted == 0
        assert hass._store_backing[gt.STORAGE_KEY] == {"records": [], "evicted": 0}
        hass.data["bps"].pop("_ground_truth")
        restored = await api.get(request(hass, query={"download": "1"}))
        assert json.loads(restored.body)["records"] == []
        assert json.loads(restored.body)["evicted"] == 0

    asyncio.run(scenario())


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


@pytest.mark.parametrize("download", [False, True])
def test_disconnected_history_get_retains_expiry_lock_before_new_mark(hass, monkeypatch, download):
    async def scenario():
        expired = record_at("expiredpoint123456", 100)
        hass._store_backing[gt.STORAGE_KEY] = {"records": [expired], "evicted": 0}
        now = 100 + gt.MAX_AGE_S + 1
        monkeypatch.setattr(gt.time, "time", lambda: now)
        history = gt.get_history(hass)
        await history.load()
        entered, release = asyncio.Event(), asyncio.Event()
        original_save = history.store.async_save
        calls = 0

        async def delayed_expiry(data):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            await original_save(data)

        history.store.async_save = delayed_expiry
        api = gt.BPSGroundTruthAPI(lambda *_: {**snapshot(), "time": now})
        reading = asyncio.create_task(api.get(request(hass, query={"download": "1"} if download else {})))
        await entered.wait()
        reading.cancel()
        await asyncio.sleep(0)
        assert history.lock.locked() and not reading.done()
        marking = asyncio.create_task(api.post(request(hass, mark_body("newpoint123456789"))))
        await asyncio.sleep(0)
        assert not marking.done()
        reading.cancel()
        await asyncio.sleep(0)
        assert history.lock.locked() and not reading.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await reading
        marked = await marking
        assert marked.status == 200 and marked.json_body["saved"]["id"] == "newpoint123456789"
        assert history.evicted == 1 and len(history.records) == 1
        expected = {"records": history.records, "evicted": 1}
        assert hass._store_backing[gt.STORAGE_KEY] == expected
        assert not history.lock.locked()
        hass.data["bps"].pop("_ground_truth")
        restarted = await api.get(request(hass, query={"download": "1"}))
        assert json.loads(restarted.body)["records"] == expected["records"]
        assert json.loads(restarted.body)["evicted"] == 1

    asyncio.run(scenario())
