"""Scanner-wide silence, durable identity and notification lifecycle."""
import asyncio
import copy
import sys
from types import SimpleNamespace as NS

import pytest

import bps
from bps import receiver_alerts as A
from test_bermuda_direct import prepared
from test_receiver_aliases import aliases, timestamp
from test_receiver_matching import MAC

KEY = "a" * 64


@pytest.fixture(autouse=True)
def notices(monkeypatch):
    calls = []
    module = NS(async_create=lambda hass, message, **kw: calls.append(("create", message, kw)),
                async_dismiss=lambda hass, nid: calls.append(("dismiss", nid)))
    monkeypatch.setitem(sys.modules, "homeassistant.components.persistent_notification", module)
    monkeypatch.setattr(sys.modules["homeassistant.components"], "persistent_notification", module, raising=False)
    return calls


def observed(age=None, *, proven=True, name="Synthetic station"):
    return {KEY: {"age_s": age, "scanner_activity_known": proven, "name": name,
                  "receivers": ["station"], "floors": ["Ground"]}}


def test_threshold_recovery_rearm_and_no_notification_spam(hass, notices):
    async def run():
        monitor = A.get_monitor(hass)
        await monitor.async_update(observed(0), 20000)
        notices.clear()
        await monitor.async_update(observed(), 30799)
        assert not monitor.status["active"] and not notices
        await monitor.async_update(observed(), 30800)
        assert monitor.status["active"][0]["silent_s"] == A.THRESHOLD_S
        assert notices[0][0] == "create" and "Synthetic station" in notices[0][1]
        assert notices[0][2]["notification_id"] == A.NOTIFICATION_ID
        await monitor.async_update(observed(), 30815)
        assert len(notices) == 1
        await monitor.async_update(observed(2), 30830)
        assert not monitor.status["active"] and notices[-1] == ("dismiss", A.NOTIFICATION_ID)
        await monitor.async_update(observed(), 41628)
        assert notices[-1][0] == "create"
        monitor.clear_notification()
        await monitor.async_update(observed(), 41643)
        assert notices[-1][0] == "create", "reload recreates an active notice"
        await monitor.async_update({}, 41644)
        assert notices[-1][0] == "dismiss" and not monitor.records
    asyncio.run(run())


def test_old_scanner_age_alerts_immediately_but_distance_only_cannot_arm(hass, notices):
    async def run():
        monitor = A.get_monitor(hass)
        await monitor.async_update(observed(0, proven=False), 20000)
        await monitor.async_update(observed(None, proven=False), 40000)
        assert monitor.status["monitored"] == 0
        assert monitor.status["activity_unverified"] == 1
        assert not any(call[0] == "create" for call in notices)
        await monitor.async_update(observed(20000), 40000)
        assert monitor.status["active"][0]["silent_s"] == 20000
    asyncio.run(run())


def test_history_survives_restart_flushes_and_does_not_write_each_poll(hass, notices):
    async def run():
        monitor = A.get_monitor(hass)
        await monitor.async_update(observed(1), 20000)
        saved = len(hass._store_saves)
        for now in (20015, 20030, 20299):
            await monitor.async_update(observed(1), now)
        assert len(hass._store_saves) == saved
        await monitor.async_flush()
        assert hass._store_backing[A.STORAGE_KEY]["receivers"][KEY]["last_seen"] == 20298
        restored = A.ReceiverAlerts(hass)
        await restored.async_update(observed(), 31098)
        assert restored.status["active"][0]["silent_s"] == A.THRESHOLD_S
        snapshot = A.snapshot(hass)
        snapshot["active"].clear()
        assert monitor.status["monitored"] == 1
        assert not hass._store_delay_saves
    asyncio.run(run())


@pytest.mark.parametrize("saved", [{"receivers": []}, {"receivers": {KEY: {"last_seen": True}}},
                                   {"receivers": {KEY: {"last_seen": 10**1000}}}])
def test_bad_store_is_retried_without_overwrite(hass, notices, saved):
    hass._store_backing[A.STORAGE_KEY] = copy.deepcopy(saved)
    async def run():
        monitor = A.get_monitor(hass)
        await monitor.async_update(observed(20000), 40000)
        assert "could not be loaded" in monitor.status["error"]
        assert not hass._store_saves and not notices
        hass._store_backing[A.STORAGE_KEY] = {"receivers": {KEY: {"last_seen": 20000}}}
        await monitor.async_update(observed(), 40000)
        assert "error" not in monitor.status and notices[-1][0] == "create"
    asyncio.run(run())


def test_unacknowledged_write_warns_but_still_alerts_and_retries(hass, notices, monkeypatch):
    async def run():
        monitor = A.get_monitor(hass)
        original = monitor.store.async_save
        async def ignored(payload):
            pass  # HA can log a write failure without raising it.
        monkeypatch.setattr(monitor.store, "async_save", ignored)
        await monitor.async_update(observed(20000), 40000)
        assert "could not be saved" in monitor.status["error"]
        assert notices[-1][0] == "create"
        monkeypatch.setattr(monitor.store, "async_save", original)
        await monitor.async_update(observed(), 40015)
        assert "error" not in monitor.status
        assert hass._store_backing[A.STORAGE_KEY]["receivers"][KEY]["last_seen"] == 20000
    asyncio.run(run())


def test_cancelled_save_finishes_under_lock_and_cannot_notify_old_lifecycle(hass, notices, monkeypatch):
    async def run():
        monitor = A.get_monitor(hass)
        entered, release = asyncio.Event(), asyncio.Event()
        original = monitor.store.async_save
        async def paused(payload):
            entered.set()
            await release.wait()
            await original(payload)
        monkeypatch.setattr(monitor.store, "async_save", paused)
        task = asyncio.create_task(monitor.async_update(observed(20000), 40000))
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert monitor.lock.locked() and not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not notices and hass._store_backing[A.STORAGE_KEY]["receivers"]
        await monitor.async_update(observed(), 40015, current=lambda: False)
        assert not notices
        await monitor.async_update(observed(), 40030)
        assert notices[-1][0] == "create"
    asyncio.run(run())


@pytest.mark.parametrize("boundary", ["load", "save"])
def test_reload_during_storage_await_cannot_publish_stale_alert(hass, notices, monkeypatch, boundary):
    async def run():
        monitor = A.get_monitor(hass)
        entered, release = asyncio.Event(), asyncio.Event()
        current = [True]
        method = "async_" + boundary
        original = getattr(monitor.store, method)
        async def paused(*args):
            entered.set()
            await release.wait()
            return await original(*args)
        monkeypatch.setattr(monitor.store, method, paused)
        task = asyncio.create_task(monitor.async_update(observed(20000), 40000, current=lambda: current[0]))
        await entered.wait()
        current[0] = False
        release.set()
        await task
        assert not notices and not monitor.status["active"]
        if boundary == "load":
            assert not hass._store_saves
    asyncio.run(run())


def test_replacement_hardware_does_not_inherit_old_identity_timer(hass, notices):
    async def run():
        monitor = A.get_monitor(hass)
        await monitor.async_update(observed(20000), 40000)
        assert monitor.status["active"]
        replacement = {"b" * 64: next(iter(observed(0).values()))}
        await monitor.async_update(replacement, 40015)
        assert not monitor.status["active"] and KEY not in monitor.records
        assert notices[-1][0] == "dismiss"
    asyncio.run(run())


def direct_monitor(hass, monkeypatch):
    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    clock = [20000.]
    monkeypatch.setattr(bps.time, "time", lambda: clock[0])
    hass.data["bps"]["layout"] = data
    hass.data["bps_initialized"] = True
    payloads["entry"]["devices"].clear()  # No dog or tracked distance is required.
    def at(now, age):
        clock[0] = now
        payloads["entry"]["captured"] = now
        for scanner in payloads["entry"]["scanners"].values():
            scanner["scanner_last_seen_age"] = age
    return data, payloads, calls, at


@pytest.mark.parametrize("conflicting_geometry", [False, True])
def test_direct_activity_without_dogs_alias_dedup_and_identity_rename(hass, notices, monkeypatch, conflicting_geometry):
    data, payloads, calls, at = direct_monitor(hass, monkeypatch)
    clone = copy.deepcopy(data["floor"][0]["receivers"][0])
    clone["entity_id"] = "current_station"
    if conflicting_geometry:
        clone["cords"]["x"] += 100
    data["floor"][0]["receivers"].append(clone)
    async def run():
        at(20000, 2)
        await bps.update_receiver_liveness(hass)
        first = copy.deepcopy(A.get_monitor(hass).records)
        assert len(first) == 1 and len(calls) == 1
        at(40000, 1)
        await bps.update_receiver_liveness(hass)
        assert A.snapshot(hass)["monitored"] == 1 and not A.snapshot(hass)["active"]
        data["floor"][0]["receivers"] = [clone]
        at(50800, 10801)
        await bps.update_receiver_liveness(hass)
        active = A.snapshot(hass)["active"]
        assert len(active) == 1 and active[0]["receivers"] == ["current_station"]
        assert set(first) == set(A.get_monitor(hass).records)
        response = await bps.BPSReceiverStatusAPI().get(NS(app={"hass": hass}))
        assert response.json_body["receiver_alerts"] == A.snapshot(hass)
        assert bps._diagnostic_report_snapshot(hass, 50800)["receiver_alerts"] == A.snapshot(hass)
        at(50815, 0)
        await bps.update_receiver_liveness(hass)
        assert not A.snapshot(hass)["active"] and notices[-1][0] == "dismiss"
    asyncio.run(run())


def test_network_online_does_not_mask_missing_ble_after_direct_api_loss(hass, notices, monkeypatch):
    data, payloads, calls, at = direct_monitor(hass, monkeypatch)
    async def run():
        at(20000, 0)
        await bps.update_receiver_liveness(hass)
        payloads["entry"] = RuntimeError("synthetic Bermuda outage")
        hass.states.get("sensor.tag_distance_to_current_station").state = "unavailable"
        hass.states.get("sensor.tag_distance_to_old_station").state = "unavailable"
        # Network connectivity is independent of radio reception.
        prior_get = hass.states.get
        hass.states.get = lambda eid: NS(state="on", attributes={"device_class": "connectivity"}) if eid == "binary_sensor.old_station_status" else prior_get(eid)
        monkeypatch.setattr(bps.time, "time", lambda: 30800.)
        await bps.update_receiver_liveness(hass)
        assert "old_station" not in hass.data["bps"]["rl_offline"]
        assert len(A.snapshot(hass)["active"]) == 1
    asyncio.run(run())


def test_legacy_scanner_clock_not_distance_absence_and_unmatched_excluded(hass, notices, monkeypatch):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    hass.data["bps"]["layout"] = data
    hass.data["bps_initialized"] = True
    clock = [20000.]
    monkeypatch.setattr(bps.time, "time", lambda: clock[0])
    monkeypatch.setattr(A.time, "monotonic", lambda: clock[0] - 19000)
    dump = {}
    async def call(*args, **kwargs):
        return dump
    hass.services.async_call = call
    async def run():
        states[new].last_updated = timestamp(19999)
        await bps.update_receiver_liveness(hass)
        assert A.snapshot(hass)["monitored"] == 0, "distance alone must not arm scanner silence"
        dump[MAC] = {"_is_scanner": True, "address": MAC, "name": "Current station", "last_seen": 1000}
        await bps.update_receiver_liveness(hass)
        assert A.snapshot(hass)["monitored"] == 1
        clock[0] = 30800
        await bps.update_receiver_liveness(hass)
        assert len(A.snapshot(hass)["active"]) == 1
        data["floor"][0]["receivers"][0]["scanner_config_entry_id"] = "wrong-owner"
        data["floor"][0]["receivers"][0]["scanner_mac"] = MAC
        await bps.update_receiver_liveness(hass)
        assert A.snapshot(hass)["monitored"] == 0 and not A.snapshot(hass)["active"]
    asyncio.run(run())


def test_direct_api_fallback_to_entirely_stale_legacy_dump_preserves_silence(hass, notices, monkeypatch):
    data, payloads, calls, at = direct_monitor(hass, monkeypatch)
    async def call(*args, **kwargs):
        return {MAC: {"_is_scanner": True, "address": MAC, "name": "Current station", "last_seen": 1000}}
    hass.services.async_call = call
    monkeypatch.setattr(A.time, "monotonic", lambda: 21015.)
    async def run():
        at(40000, 20000)
        await bps.update_receiver_liveness(hass)
        assert A.snapshot(hass)["active"][0]["last_seen"] == 20000
        payloads["entry"] = RuntimeError("synthetic direct outage")
        monkeypatch.setattr(bps.time, "time", lambda: 40015.)
        await bps.update_receiver_liveness(hass)
        assert A.snapshot(hass)["active"][0]["last_seen"] == 20000
        assert not any(call[0] == "dismiss" for call in notices), "fallback cannot clear the notice"
    asyncio.run(run())


@pytest.mark.parametrize("source", [MAC, "dd:ee:ff:11:22:33"])
def test_ha_radio_clock_not_receiver_beacon_reception_and_missing_scanner(hass, notices, monkeypatch, source):
    data, payloads, calls, at = direct_monitor(hass, monkeypatch)
    if source != MAC:
        from test_bermuda_direct import snapshot
        payloads["entry"] = snapshot(sid=source, retained=MAC)
        payloads["entry"]["devices"].clear()
    radio_age = [0.]
    scanner = NS(source=source.upper(), time_since_last_detection=lambda: radio_age[0])
    scanners = {source: scanner}
    module = NS(async_scanner_by_source=lambda hass, address: scanners.get(address.lower()))
    monkeypatch.setitem(sys.modules, "homeassistant.components.bluetooth", module)
    async def run():
        at(20000, 0)
        if source != MAC:
            payloads["entry"]["scanners"][source]["scanner_last_seen_age"] = 0
        await bps.update_receiver_liveness(hass)
        radio_age[0] = 10800
        monkeypatch.setattr(bps.time, "time", lambda: 30800.)
        # Bermuda's device-level last_seen is fresh because a different
        # scanner can still hear this station's transmitted beacon.
        await bps.update_receiver_liveness(hass)
        assert A.snapshot(hass)["active"][0]["silent_s"] == 10800
        scanners.clear()
        monkeypatch.setattr(bps.time, "time", lambda: 30815.)
        await bps.update_receiver_liveness(hass)
        assert len(A.snapshot(hass)["active"]) == 1
        scanners[source] = scanner
        radio_age[0] = 0
        await bps.update_receiver_liveness(hass)
        assert not A.snapshot(hass)["active"] and notices[-1][0] == "dismiss"
    asyncio.run(run())


def test_registered_radio_without_first_packet_gets_three_hour_grace(hass, notices, monkeypatch):
    data, payloads, calls, at = direct_monitor(hass, monkeypatch)
    scanner = NS(source=MAC.upper(), time_since_last_detection=lambda: float("inf"))
    module = NS(async_scanner_by_source=lambda hass, address: scanner if address.lower() == MAC else None)
    monkeypatch.setitem(sys.modules, "homeassistant.components.bluetooth", module)
    async def run():
        at(20000, None)
        await bps.update_receiver_liveness(hass)
        assert A.snapshot(hass)["monitored"] == 1 and not A.snapshot(hass)["active"]
        at(30799, None)
        await bps.update_receiver_liveness(hass)
        assert not A.snapshot(hass)["active"]
        at(30800, None)
        await bps.update_receiver_liveness(hass)
        assert A.snapshot(hass)["active"][0]["silent_s"] == 10800
    asyncio.run(run())
