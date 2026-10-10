"""Executor suspension, failure, and lifecycle regressions for live refreshes."""
import asyncio
import copy
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import bps
from bps.storage import save_bps_data


def run(coro):
    async def bounded():
        return await asyncio.wait_for(coro, 5)
    return asyncio.run(bounded())


@pytest.fixture(autouse=True)
def reset_tracking(monkeypatch):
    monkeypatch.setattr(bps, "apitricords", [])
    monkeypatch.setattr(bps, "tracked_entities", [])
    for name in ("_kf_position_state", "_floor_probability", "_floor_challenge", "_floor_dark_cycles"):
        monkeypatch.setattr(bps, name, {})
    for name in ("last_floor", "last_r_values"):
        monkeypatch.setattr(bps.update_trilateration_and_zone, name, {}, raising=False)


def layout():
    return {"outdoor_tracking": {"enabled": True}, "floor": [{
        "name": "Property", "scale": 10.0, "zones": [], "receivers": [
            {"entity_id": f"p{i}", "cords": {"x": x, "y": y}, "correction": 1.0}
            for i, (x, y) in enumerate(((0, 0), (100, 0), (0, 100), (100, 100)))
        ],
    }]}


def install_readings(hass):
    reading = SimpleNamespace(
        state="7.0710678118654755", attributes={"unit_of_measurement": "m"},
        last_updated=datetime.datetime.now(datetime.timezone.utc),
    )
    hass.states = SimpleNamespace(get=lambda _eid: reading)


def test_failed_tracker_is_drained_before_groups_and_next_batch(hass, monkeypatch, caplog):
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        events = []

        async def tracker(_hass, _data, entry):
            if entry["entity"] == "failed":
                await started.wait()
                raise ValueError("bad receiver")
            if entry["entity"] == "slow":
                started.set()
                await release.wait()
            events.append(entry["entity"])

        async def groups(_hass):
            events.append("groups")

        monkeypatch.setattr(bps, "process_single_entity", tracker)
        monkeypatch.setattr(bps, "update_tracker_groups", groups)
        first = asyncio.create_task(bps.process_entities(hass, [
            {"entity": "failed"}, {"entity": "slow"},
        ]))
        await started.wait()
        second = asyncio.create_task(bps.process_entities(hass, [{"entity": "next"}]))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        assert events == []
        release.set()
        await asyncio.gather(first, second)
        assert events == ["slow", "groups", "next", "groups"]

    run(scenario())
    assert "BPS tracking failed for 1 tracker(s); first failed: bad receiver" in caplog.text


def test_overlapping_batches_bound_parallel_work_and_preserve_order(hass, monkeypatch):
    async def scenario():
        release, full = asyncio.Event(), asyncio.Event()
        active = maximum = 0
        events = []

        async def tracker(_hass, _data, entry):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            if active == bps.MAX_CONCURRENT_TRACKERS:
                full.set()
            events.append(entry["entity"])
            await release.wait()
            active -= 1

        async def groups(_hass):
            assert active == 0
            events.append("groups")

        monkeypatch.setattr(bps, "process_single_entity", tracker)
        monkeypatch.setattr(bps, "update_tracker_groups", groups)
        first = asyncio.create_task(bps.process_entities(hass, [
            {"entity": f"first_{i}"} for i in range(24)
        ]))
        await full.wait()
        second = asyncio.create_task(bps.process_entities(hass, [{"entity": "second"}]))
        await asyncio.sleep(0)
        assert len(events) == bps.MAX_CONCURRENT_TRACKERS
        release.set()
        await asyncio.gather(first, second)
        assert maximum == bps.MAX_CONCURRENT_TRACKERS
        assert events[-3:] == ["groups", "second", "groups"]

    run(scenario())


def test_repeated_tracker_failures_have_bounded_diagnostics(hass, monkeypatch, caplog):
    async def tracker(*_args):
        raise ValueError("bad receiver " + "x" * 1000)

    async def groups(_hass):
        pass

    monkeypatch.setattr(bps, "process_single_entity", tracker)
    monkeypatch.setattr(bps, "update_tracker_groups", groups)

    async def scenario():
        for _ in range(10):
            await bps.process_entities(hass, [{"entity": "tracker" + "x" * 1000}])

    run(scenario())
    failures = [record for record in caplog.records if "BPS tracking failed" in record.message]
    assert len(failures) == 1
    assert len(failures[0].message) < 450


def test_queued_batch_from_replaced_layout_is_discarded(hass, monkeypatch):
    async def scenario():
        await save_bps_data(hass, layout())
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        async def tracker(_hass, _data, entry):
            calls.append(entry["entity"])
            started.set()
            await release.wait()

        async def groups(_hass):
            calls.append("groups")

        monkeypatch.setattr(bps, "process_single_entity", tracker)
        monkeypatch.setattr(bps, "update_tracker_groups", groups)
        first = asyncio.create_task(bps.process_entities(hass, [{"entity": "first"}]))
        await started.wait()
        second = asyncio.create_task(bps.process_entities(hass, [{"entity": "outdated"}]))
        await asyncio.sleep(0)
        await save_bps_data(hass, layout())
        release.set()
        await asyncio.gather(first, second)
        assert calls == ["first"]

    run(scenario())


@pytest.mark.parametrize("change", ["edit", "stop", "reload"])
def test_outdoor_result_cannot_publish_after_layout_or_lifecycle_change(hass, monkeypatch, change):
    async def scenario():
        data = layout()
        await save_bps_data(hass, data)
        install_readings(hass)
        started, release = asyncio.Event(), asyncio.Event()
        publications = []

        async def executor(func, *args):
            started.set()
            await release.wait()
            return func(*args)

        hass.async_add_executor_job = executor
        monkeypatch.setattr(bps, "update_bps_sensor_state", lambda *a, **k: publications.append(a))
        entry = {"entity": "beacon_a", "data": copy.deepcopy(data)}
        task = asyncio.create_task(bps.process_single_entity(hass, [entry], entry))
        await started.wait()
        if change == "edit":
            await save_bps_data(hass, {"floor": []})
        else:
            await bps._stop_tracking(hass)
            if change == "reload":
                hass.data["bps"]["_tracking_active"] = True
        release.set()
        await task
        assert bps.apitricords == []
        assert bps._kf_position_state == {}
        assert bps._floor_probability == {}
        assert bps.update_trilateration_and_zone.last_r_values == {}
        assert publications == []
        assert "_history" not in hass.data["bps"]

    run(scenario())


@pytest.mark.parametrize("change", ["edit", "stop", "reload"])
def test_group_executor_owns_private_layout_and_rejects_outdated_result(hass, monkeypatch, change):
    async def scenario():
        data = layout()
        data["tracker_groups"] = [{"id": "rover", "beacons": ["a", "b"]}]
        await save_bps_data(hass, data)
        originals = [{"ent": name, "floor": "Property", "cords": xy, "zone": "unknown",
                      "updated": bps.time.time(), "rms_m": 1,
                      "outdoor": {"estimated_uncertainty_m": 3, "receivers_used": 4}}
                     for name, xy in (("a", [50, 50]), ("b", [55, 50]))]
        bps.apitricords = copy.deepcopy(originals)
        started, release = asyncio.Event(), asyncio.Event()
        publications = []

        async def executor(func, *args):
            assert func is bps._assign_group_zone
            assert args[0] not in bps.apitricords
            assert args[1][0]["data"] == data
            assert args[1][0]["data"] is not data
            started.set()
            await release.wait()
            return func(*args)

        hass.async_add_executor_job = executor
        monkeypatch.setattr(bps, "update_bps_sensor_state", lambda *a, **k: publications.append(a))
        task = asyncio.create_task(bps.update_tracker_groups(hass))
        await started.wait()
        if change == "edit":
            await save_bps_data(hass, {"floor": []})
        else:
            await bps._stop_tracking(hass)
            if change == "reload":
                hass.data["bps"]["_tracking_active"] = True
        release.set()
        await task
        assert bps.apitricords == originals
        assert publications == []
        assert "_history" not in hass.data["bps"]

    run(scenario())


def test_stop_drains_async_batch_while_executor_thread_finishes_privately(hass, monkeypatch):
    async def scenario():
        data = layout()
        await save_bps_data(hass, data)
        install_readings(hass)
        started = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()
        loop = asyncio.get_running_loop()
        publications = []

        def worker(func, args):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(3), "test did not release executor worker"
            try:
                return func(*args)
            finally:
                finished.set()

        async def executor(func, *args):
            return await asyncio.to_thread(worker, func, args)

        hass.async_add_executor_job = executor
        monkeypatch.setattr(bps, "update_bps_sensor_state", lambda *a, **k: publications.append(a))
        entry = {"entity": "beacon_a", "data": copy.deepcopy(data)}
        task = asyncio.create_task(bps.process_entities(hass, [entry]))
        hass.data["bps_update_task"] = task
        await started.wait()
        try:
            await bps._stop_tracking(hass)
            assert task.cancelled()
            assert not finished.is_set()
        finally:
            release.set()
        assert await asyncio.to_thread(finished.wait, 3)
        assert bps.apitricords == []
        assert publications == []
        assert "bps_update_task" not in hass.data

    run(scenario())


def test_direct_outdoor_tracking_without_runtime_task_remains_supported(hass):
    async def scenario():
        data = layout()
        await save_bps_data(hass, data)
        install_readings(hass)
        entry = {"entity": "beacon_a", "data": copy.deepcopy(data)}
        await bps.process_single_entity(hass, [entry], entry)
        assert bps.apitricords[0]["ent"] == "beacon_a"
        assert bps.apitricords[0]["cords"] == pytest.approx([50, 50])
    run(scenario())


@pytest.mark.parametrize("fail_abandoned", [False, True])
def test_repeated_reload_retains_physical_worker_limit(hass, monkeypatch, fail_abandoned):
    async def scenario():
        loop = asyncio.get_running_loop()
        # Exceed the intended limit in the underlying executor so the test
        # cannot pass solely because HA/asyncio's pool happens to be small.
        loop.set_default_executor(ThreadPoolExecutor(max_workers=24))
        release = threading.Event()
        full = asyncio.Event()
        lock = threading.Lock()
        active = maximum = started = 0
        abandoned_errors = []
        loop.set_exception_handler(lambda _loop, context: abandoned_errors.append(context))

        def worker():
            nonlocal active, maximum, started
            with lock:
                active += 1
                started += 1
                maximum = max(maximum, active)
                if started == bps.MAX_CONCURRENT_TRACKERS:
                    loop.call_soon_threadsafe(full.set)
            try:
                assert release.wait(3)
                if fail_abandoned:
                    raise ValueError("abandoned private worker")
            finally:
                with lock:
                    active -= 1

        async def executor(func, *args):
            return await asyncio.to_thread(func, *args)

        async def tracker(_hass, _data, entry):
            await bps._tracking_executor_job(hass, worker)

        async def groups(_hass):
            pass

        hass.async_add_executor_job = executor
        monkeypatch.setattr(bps, "process_single_entity", tracker)
        monkeypatch.setattr(bps, "update_tracker_groups", groups)
        entries = [{"entity": str(i)} for i in range(bps.MAX_CONCURRENT_TRACKERS)]
        bucket = hass.data.setdefault("bps", {})
        first = asyncio.create_task(bps.process_entities(hass, entries))
        hass.data["bps_update_task"] = first
        await full.wait()
        try:
            await bps._stop_tracking(hass)
            assert first.cancelled()
            for _ in range(3):
                bucket["_tracking_active"] = True
                next_batch = asyncio.create_task(bps.process_entities(hass, entries))
                hass.data["bps_update_task"] = next_batch
                # Let the fresh batch reach worker admission without any
                # physical thread being allowed to complete yet.
                for _ in range(10):
                    await asyncio.sleep(0)
                assert started == bps.MAX_CONCURRENT_TRACKERS
                assert len(bucket["_tracking_executor_jobs"]) == bps.MAX_CONCURRENT_TRACKERS
                await bps._stop_tracking(hass)
                assert next_batch.cancelled()
            bucket["_tracking_active"] = True
            recovery = asyncio.create_task(bps.process_entities(hass, entries))
            hass.data["bps_update_task"] = recovery
            release.set()
            await recovery
            assert started == 2 * bps.MAX_CONCURRENT_TRACKERS
            assert maximum == bps.MAX_CONCURRENT_TRACKERS
            # New jobs may finish before the last abandoned old job: they
            # only need an available slot, not the whole prior batch. Drain
            # those owned jobs before asserting final physical quiescence.
            await asyncio.gather(*tuple(bucket["_tracking_executor_jobs"]))
            assert active == 0
            assert bucket["_tracking_executor_jobs"] == set()
            assert abandoned_errors == []
        finally:
            release.set()
            await bps._stop_tracking(hass)

    run(scenario())


def test_unload_drains_tracking_before_platform_and_history_teardown(hass, monkeypatch):
    async def scenario():
        started, drained = asyncio.Event(), asyncio.Event()
        events = []

        async def tracker(*_args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                drained.set()

        async def teardown(_hass, **_kwargs):
            assert drained.is_set()
            assert hass.data["bps"]["_tracking_active"] is False
            events.append("teardown")

        async def unload_platforms(_entry, _platforms):
            assert drained.is_set()
            events.append("platforms")
            return True

        monkeypatch.setattr(bps, "process_single_entity", tracker)
        monkeypatch.setattr(bps, "shutdown_diagnostics", teardown)
        monkeypatch.setattr(bps, "flush_position_history", teardown)
        monkeypatch.setattr(bps, "async_shutdown_calibration", teardown)
        monkeypatch.setattr(bps, "cleanup_legacy_bps_registry_and_states", lambda _hass: None)
        monkeypatch.setattr(bps.er, "async_get", lambda _hass: SimpleNamespace(entities={}), raising=False)
        monkeypatch.setattr(bps, "async_remove_panel", lambda *a, **k: None)
        hass.states = SimpleNamespace(async_all=lambda: [])
        hass.config_entries = SimpleNamespace(async_unload_platforms=unload_platforms)
        hass.data["bps_initialized"] = True
        task = asyncio.create_task(bps.process_entities(hass, [{"entity": "a"}]))
        hass.data["bps_update_task"] = task
        await started.wait()
        assert await bps.async_unload_entry(hass, SimpleNamespace(entry_id="entry"))
        assert task.cancelled()
        assert events == ["teardown", "teardown", "platforms", "teardown"]
        assert "bps_initialized" not in hass.data

    run(scenario())


def test_deferred_startup_cannot_resurrect_stopped_integration(hass, monkeypatch):
    async def scenario():
        callbacks = {}
        hass.is_running = False
        hass.bus = SimpleNamespace(async_listen_once=lambda event, callback: callbacks.update({event: callback}))
        assert await bps.async_setup(hass, {})
        assert "homeassistant_started" in callbacks
        await bps._stop_tracking(hass)
        # No HTTP/runtime plumbing is installed: reaching initialization after
        # teardown would fail as well as incorrectly restarting the task.
        await callbacks["homeassistant_started"](None)
        assert "bps_update_task" not in hass.data
        assert hass.data["bps"]["_tracking_active"] is False

    run(scenario())


@pytest.mark.parametrize("platform_raises", [False, True])
@pytest.mark.parametrize("startup_phase", ["waiting", "event_during_unload", "initializing"])
def test_failed_unload_preserves_real_deferred_initialization(hass, monkeypatch, tmp_path,
                                                              platform_raises, startup_phase):
    async def scenario():
        callbacks = {}
        initialized = []
        started = asyncio.Event()
        migration_started, finish_migration = asyncio.Event(), asyncio.Event()
        initial_task = None

        async def noop(*_args, **_kwargs):
            pass

        async def migrate(_hass):
            initialized.append(True)
            if startup_phase == "initializing":
                migration_started.set()
                await finish_migration.wait()

        async def tracking(_hass):
            started.set()
            await asyncio.Event().wait()

        async def unload(_entry, _platforms):
            assert "_tracking_setup" not in hass.data["bps"]
            if startup_phase == "event_during_unload":
                hass.is_running = True
                await callbacks["homeassistant_started"](None)
                assert "bps_update_task" not in hass.data
            finish_migration.set()
            if platform_raises:
                raise RuntimeError("platform could not unload")
            return False

        hass.is_running = False
        hass.async_create_task = asyncio.create_task
        hass.bus = SimpleNamespace(async_listen_once=lambda event, callback: callbacks.update({event: callback}))
        hass.config = SimpleNamespace(path=lambda: str(tmp_path))
        hass.config_entries = SimpleNamespace(async_unload_platforms=unload)
        hass.data["bps_views_registered"] = True
        entry = SimpleNamespace(entry_id="entry", options={bps.OPTION_SHOW_SIDEBAR_PANEL: False})
        monkeypatch.setattr(bps.aiofiles.os, "makedirs", noop)
        monkeypatch.setattr(bps, "migrate_legacy", migrate)
        monkeypatch.setattr(bps, "load_bps_data", noop)
        monkeypatch.setattr(bps, "restore_position_history", noop)
        monkeypatch.setattr(bps, "async_restore_calibration_state", noop)
        monkeypatch.setattr(bps, "async_start_auto_if_enabled", noop)
        monkeypatch.setattr(bps, "shutdown_diagnostics", noop)
        monkeypatch.setattr(bps, "flush_position_history", noop)
        monkeypatch.setattr(bps, "activate_diagnostics", lambda _hass: None)
        monkeypatch.setattr(bps, "update_tracked_entities", tracking)
        monkeypatch.setattr(bps, "cleanup_legacy_bps_registry_and_states",
                            lambda _hass: pytest.fail("cleanup after failed platform unload"))
        assert await bps.async_setup(hass, entry)
        token = hass.data["bps"]["_tracking_setup"]
        initializer = hass.data["bps"]["_tracking_initialize"]
        try:
            if startup_phase == "initializing":
                hass.is_running = True
                initial_task = asyncio.create_task(callbacks["homeassistant_started"](None))
                await migration_started.wait()
            assert not await bps.async_unload_entry(hass, entry)
            assert hass.data["bps"]["_tracking_setup"] is token
            assert hass.data["bps"]["_tracking_initialize"] is initializer
            if startup_phase == "waiting":
                assert "bps_update_task" not in hass.data
                hass.is_running = True
                await callbacks["homeassistant_started"](None)
            await started.wait()
            task = hass.data["bps_update_task"]
            assert hass.data["bps"]["_tracking_active"] is True
            # A retry or late event must not duplicate initialization/loops.
            await callbacks["homeassistant_started"](None)
            assert hass.data["bps_update_task"] is task
            assert initialized == [True]
        finally:
            finish_migration.set()
            if initial_task is not None:
                await initial_task
            await bps._stop_tracking(hass)

    run(scenario())


@pytest.mark.parametrize("failure", ["platform_false", "platform_exception"])
def test_failed_unload_resumes_tracking_with_fresh_generation(hass, monkeypatch, failure):
    async def scenario():
        started = asyncio.Event()

        async def tracking(_hass):
            started.set()
            await asyncio.Event().wait()

        async def noop(_hass, **_kwargs):
            pass

        async def unload_platforms(_entry, _platforms):
            if failure == "platform_exception":
                raise RuntimeError("platform could not unload")
            return failure != "platform_false"

        def remove_panel(*_args, **_kwargs):
            pytest.fail("frontend cleanup must wait for successful platform unload")

        def cleanup(_hass):
            pytest.fail("registry cleanup must wait for successful platform unload")

        monkeypatch.setattr(bps, "update_tracked_entities", tracking)
        monkeypatch.setattr(bps, "shutdown_diagnostics", noop)
        monkeypatch.setattr(bps, "flush_position_history", noop)
        monkeypatch.setattr(bps, "cleanup_legacy_bps_registry_and_states", cleanup)
        monkeypatch.setattr(bps.er, "async_get", lambda _hass: SimpleNamespace(entities={}), raising=False)
        monkeypatch.setattr(bps, "async_remove_panel", remove_panel)
        hass.async_create_task = asyncio.create_task
        hass.config_entries = SimpleNamespace(async_unload_platforms=unload_platforms)
        hass.data["bps_initialized"] = True
        sensors = hass.data["bps_sensors"] = {"sensor.dog_bps_zone": object()}
        listener = hass.data["bps_state_listener_unsub"] = lambda: pytest.fail("live listener removed")
        bucket = hass.data.setdefault("bps", {})
        generation = bucket["_tracking_lifecycle"] = object()
        reconciled = []

        def reconcile():
            assert bucket["_tracking_active"] is True
            reconciled.append(True)

        bucket["resume_sensor_discovery"] = reconcile
        old = asyncio.create_task(tracking(hass))
        hass.data["bps_update_task"] = old
        await started.wait()
        started.clear()
        try:
            assert not await bps.async_unload_entry(hass, SimpleNamespace(entry_id="entry"))
            new = hass.data["bps_update_task"]
            assert old.cancelled() and new is not old
            assert bucket["_tracking_active"] is True
            assert bucket["_tracking_lifecycle"] is not generation
            assert hass.data["bps_initialized"] is True
            assert hass.data["bps_sensors"] is sensors
            assert hass.data["bps_state_listener_unsub"] is listener
            assert reconciled == [True]
            await started.wait()
        finally:
            await bps._stop_tracking(hass)

    run(scenario())


@pytest.mark.parametrize("panel_error", [False, True])
def test_successful_platform_unload_completes_even_if_panel_removal_fails(hass, monkeypatch, panel_error):
    async def scenario():
        started = asyncio.Event()
        events = []

        async def tracking(_hass):
            started.set()
            await asyncio.Event().wait()

        async def noop(_hass, **_kwargs):
            pass

        async def unload(_entry, _platforms):
            assert "bps_state_listener_unsub" in hass.data
            events.append("platform")
            return True

        def remove_panel(*_args, **_kwargs):
            events.append("panel")
            if panel_error:
                raise RuntimeError("frontend cleanup failed")

        async def stop_calibration(_hass):
            events.append("calibration")

        monkeypatch.setattr(bps, "update_tracked_entities", tracking)
        monkeypatch.setattr(bps, "shutdown_diagnostics", noop)
        monkeypatch.setattr(bps, "flush_position_history", noop)
        monkeypatch.setattr(bps, "async_shutdown_calibration", stop_calibration)
        monkeypatch.setattr(bps, "cleanup_legacy_bps_registry_and_states", lambda _hass: events.append("registry"))
        monkeypatch.setattr(bps.er, "async_get", lambda _hass: SimpleNamespace(entities={}), raising=False)
        monkeypatch.setattr(bps, "async_remove_panel", remove_panel)
        hass.states = SimpleNamespace(async_all=lambda: [])
        hass.async_create_task = asyncio.create_task
        hass.config_entries = SimpleNamespace(async_unload_platforms=unload)
        hass.data["bps_initialized"] = True
        hass.data["bps_sensors"] = {"sensor.dog_bps_zone": object()}
        hass.data["bps_state_listener_unsub"] = lambda: events.append("listener")
        bucket = hass.data.setdefault("bps", {})
        bucket["sync_group_sensors"] = object()
        bucket["resume_sensor_discovery"] = lambda: pytest.fail("unloaded platform resumed")
        old = asyncio.create_task(tracking(hass))
        hass.data["bps_update_task"] = old
        await started.wait()
        assert await bps.async_unload_entry(hass, SimpleNamespace(entry_id="entry"))
        assert old.cancelled()
        assert events == ["platform", "listener", "registry", "panel", "calibration"]
        assert bucket["_tracking_active"] is False
        assert not {"bps_update_task", "bps_initialized", "bps_sensors", "bps_state_listener_unsub"} & hass.data.keys()
        assert "sync_group_sensors" not in bucket
        assert "resume_sensor_discovery" not in bucket

    run(scenario())
