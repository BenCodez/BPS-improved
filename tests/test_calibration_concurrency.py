"""Manual calibration HTTP solves own inputs and reject obsolete results."""
import asyncio
import copy
import gc
import math
import threading
from collections import deque
from types import SimpleNamespace

import pytest

import bps
from bps import calibration as C
from bps.storage import save_bps_data, STORAGE_KEY_CALIB


def run(coro):
    async def bounded():
        return await asyncio.wait_for(coro, 5)
    return asyncio.run(bounded())


def request(hass, action="solve"):
    async def json():
        return {"action": action, "floor": "Yard"}
    return SimpleNamespace(app={"hass": hass}, json=json)


async def prepare(hass):
    layout = {"floor": [{"name": "Yard", "scale": 10, "receivers": [
        {"entity_id": f"p{i}", "cords": {"x": x, "y": y}}
        for i, (x, y) in enumerate(((0, 0), (100, 0), (0, 100), (100, 100)))
    ]}]}
    await save_bps_data(hass, layout)
    cal = C.get_calibration_state(hass)
    cal["receivers"] = C._build_receiver_map(layout)
    for tx, a in cal["receivers"].items():
        for rx, b in cal["receivers"].items():
            if tx != rx:
                distance = math.hypot(a["x"] - b["x"], a["y"] - b["y"]) / 10
                cal["samples"][f"{tx}|{rx}"] = deque([distance] * C.MIN_SAMPLES_PER_PAIR)
    cal["floor"] = "Yard"
    cal["results"] = {"Yard": {"floor": "Yard", "solved_at": "old", "receivers": {"p0": 0.8}}}
    cal["last_solved_at"] = "old"
    cal["error"] = "Previous diagnostic"
    return layout, cal


def test_concurrent_manual_api_solve_is_rejected_without_queueing(hass):
    async def scenario():
        _layout, cal = await prepare(hass)
        started, release = asyncio.Event(), asyncio.Event()
        jobs = 0

        async def executor(func, *args):
            nonlocal jobs
            jobs += 1
            started.set()
            await release.wait()
            return func(*args)

        hass.async_add_executor_job = executor
        api = C.BPSCalibrationAPI()
        first = asyncio.create_task(api.post(request(hass)))
        await started.wait()
        old = copy.deepcopy(cal["results"])
        for _ in range(10):
            response = await api.post(request(hass))
            assert response.status == 409
            assert response.json_body["error"] == "A calibration solve is already running"
        assert jobs == 1 and cal["results"] == old
        assert cal["error"] == "Previous diagnostic"
        release.set()
        response = await first
        assert response.status == 200
        assert cal["results"]["Yard"]["receivers"] == {f"p{i}": 1.0 for i in range(4)}
        assert cal["last_solved_at"] != "old"
        assert cal["error"] is None
        assert not cal["_manual_solve_lock"].locked()

    run(scenario())


@pytest.mark.parametrize("change", ["cancel", "stop", "mode", "task", "map", "unload", "reload", "newer_result"])
def test_pending_manual_result_is_discarded_after_state_changes(hass, change):
    async def scenario():
        layout, cal = await prepare(hass)
        started, release = asyncio.Event(), asyncio.Event()

        async def executor(func, *args):
            started.set()
            await release.wait()
            return func(*args)

        hass.async_add_executor_job = executor
        api = C.BPSCalibrationAPI()
        pending = asyncio.create_task(api.post(request(hass)))
        await started.wait()
        if change == "cancel":
            assert (await api.post(request(hass, "cancel"))).status == 200
        elif change == "stop":
            await C._stop_task(cal)
        elif change == "mode":
            cal["mode"] = "auto"
        elif change == "task":
            cal["task"] = object()
        elif change == "map":
            await save_bps_data(hass, copy.deepcopy(layout))
        elif change in {"unload", "reload"}:
            await bps._stop_tracking(hass)
            if change == "reload":
                hass.data["bps"]["_tracking_active"] = True
        else:
            cal["last_solved_at"] = "newer"
            cal["results"]["Yard"]["solved_at"] = "newer"
        before = copy.deepcopy((cal["results"], cal["last_solved_at"], cal["error"]))
        release.set()
        response = await pending
        assert response.status == 409
        assert "changed while solving" in response.json_body["error"]
        assert (cal["results"], cal["last_solved_at"], cal["error"]) == before
        assert not cal["_manual_solve_lock"].locked()

    run(scenario())


def test_manual_executor_receives_detached_samples_and_receiver_records(hass):
    async def scenario():
        _layout, cal = await prepare(hass)
        started, release = asyncio.Event(), asyncio.Event()
        captured = {}

        async def executor(func, *args):
            snapshot, floor_name = args
            captured.update(copy.deepcopy(snapshot))
            assert snapshot["receivers"] is not cal["receivers"]
            assert snapshot["receivers"]["p0"] is not cal["receivers"]["p0"]
            assert snapshot["samples"]["p0|p1"] is not cal["samples"]["p0|p1"]
            started.set()
            await release.wait()
            assert snapshot == captured
            return func(snapshot, floor_name)

        hass.async_add_executor_job = executor
        pending = asyncio.create_task(C.BPSCalibrationAPI().post(request(hass)))
        await started.wait()
        cal["receivers"]["p0"]["x"] = 900
        cal["samples"]["p0|p1"].extend([99] * 20)
        cal["samples"]["new|pair"] = deque([42])
        live = copy.deepcopy((cal["samples"], cal["receivers"]))
        release.set()
        response = await pending
        assert response.status == 200
        assert cal["results"]["Yard"]["receivers"] == {f"p{i}": 1.0 for i in range(4)}
        assert (cal["samples"], cal["receivers"]) == live

    run(scenario())


def test_manual_solve_tokens_and_lock_are_not_persisted(hass):
    async def scenario():
        _layout, cal = await prepare(hass)
        assert (await C.BPSCalibrationAPI().post(request(hass))).status == 200
        await C._stop_task(cal)
        assert "_manual_solve_lock" in cal and "_manual_solve_generation" in cal
        await C.save_calibration_state(hass)
        saved = hass._store_backing[STORAGE_KEY_CALIB]
        assert set(saved) == {"saved_at", "results", "applied", "samples"}
        assert saved["results"] == cal["results"]

    run(scenario())


def test_stopped_integration_rejects_manual_solve_before_executor(hass, monkeypatch):
    async def scenario():
        _layout, cal = await prepare(hass)
        await bps._stop_tracking(hass)

        async def forbidden(*_args):
            pytest.fail("stopped integration must not queue calibration work")

        hass.async_add_executor_job = forbidden
        before = copy.deepcopy((cal["results"], cal["error"]))
        response = await C.BPSCalibrationAPI().post(request(hass))
        assert response.status == 409
        assert (cal["results"], cal["error"]) == before

    run(scenario())


@pytest.mark.parametrize("worker_fails", [False, True])
def test_cancelled_http_request_retains_admission_until_private_worker_finishes(hass, worker_fails):
    async def scenario():
        _layout, cal = await prepare(hass)
        before = copy.deepcopy((cal["results"], cal["last_solved_at"], cal["error"]))
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        jobs = 0
        abandoned_errors = []
        loop.set_exception_handler(lambda _loop, context: abandoned_errors.append(context))

        def worker(func, args):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3), "test did not release calibration worker"
            if worker_fails:
                raise ValueError("private abandoned failure")
            return func(*args)

        async def executor(func, *args):
            nonlocal jobs
            jobs += 1
            return await asyncio.to_thread(worker, func, args)

        hass.async_add_executor_job = executor
        api = C.BPSCalibrationAPI()
        request_task = asyncio.create_task(api.post(request(hass)))
        await started.wait()
        pending = cal["_manual_solve_pending"]
        try:
            request_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request_task
            assert not cal["_manual_solve_lock"].locked()
            assert not pending.cancelled() and not pending.done()
            for _ in range(10):
                assert (await api.post(request(hass))).status == 409
            assert jobs == 1
            # Session cancellation still must not erase the physical worker's
            # admission marker or permit a second solve to queue behind it.
            await C._stop_task(cal)
            assert cal["_manual_solve_pending"] is pending
            assert (await api.post(request(hass))).status == 409
            await C.save_calibration_state(hass)
            assert set(hass._store_backing[STORAGE_KEY_CALIB]) == {"saved_at", "results", "applied", "samples"}
        finally:
            release.set()
        # Waiting for task completion without retrieving its result leaves the
        # callback responsible for consuming a failed abandoned calculation.
        await asyncio.wait({pending})
        await asyncio.sleep(0)
        assert "_manual_solve_pending" not in cal
        assert (cal["results"], cal["last_solved_at"], cal["error"]) == before
        del pending
        gc.collect()
        assert abandoned_errors == []

        async def immediate(func, *args):
            return func(*args)

        hass.async_add_executor_job = immediate
        assert (await api.post(request(hass))).status == 200
        assert cal["results"]["Yard"]["receivers"] == {f"p{i}": 1.0 for i in range(4)}

    run(scenario())
