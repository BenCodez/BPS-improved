"""Phone commands need explicit targets and survive failed cleanup/restart."""

import asyncio
from types import SimpleNamespace as NS

import pytest

from bps import phone_accuracy as pa

PHONE = "mobile_app_synthetic_android"


def fixture(hass):
    calls, clock = [], [100.0]
    fail = set()

    async def command(domain, service, body, blocking):
        assert domain == "notify" and service == PHONE and blocking
        calls.append(body["data"]["command"])
        if body["data"]["command"] in fail:
            raise OSError("synthetic delivery failure")
        assert body["message"] == "command_high_accuracy_mode"
        assert body["data"]["ttl"] == 0

    hass.services = NS(async_services=lambda: {"notify": {PHONE: {}, "persistent_notification": {}}}, async_call=command)
    hass.async_create_task = asyncio.create_task
    hass.async_create_background_task = lambda coro, name: asyncio.create_task(coro, name=name)
    hass.bus = NS(async_listen_once=lambda *_: lambda: None)
    manager = pa.PhoneAccuracy(hass, clock=lambda: clock[0])
    return manager, calls, clock, fail


def test_live_heartbeat_stop_and_expiry(hass):
    async def scenario():
        manager, calls, clock, _ = fixture(hass)
        result = await manager.action("owner", {"action": "start", "service": PHONE})
        assert calls == ["turn_on"] and PHONE in hass._store_backing[pa.KEY]["records"]
        clock[0] = 180
        await manager.action("owner", {"action": "heartbeat", "session": result["session"]})
        clock[0] = 200
        await manager.sweep(); assert calls == ["turn_on"]
        clock[0] = 271
        await manager.sweep(); assert calls == ["turn_on", "turn_off"]
        assert manager.records == {} and hass._store_backing[pa.KEY]["records"] == {}
        await manager.close()
    asyncio.run(scenario())


def test_stop_is_owner_scoped_and_same_phone_has_only_one_session(hass):
    async def scenario():
        manager, calls, _, _ = fixture(hass)
        result = await manager.action("owner", {"action": "start", "service": PHONE})
        await manager.action("other", {"action": "stop", "session": result["session"]})
        assert calls == ["turn_on"]
        with pytest.raises(ValueError, match="already"):
            await manager.action("other", {"action": "start", "service": PHONE})
        await manager.action("owner", {"action": "stop"})
        assert calls == ["turn_on", "turn_off"]
        await manager.close()
    asyncio.run(scenario())


def test_failed_off_keeps_durable_intent_and_recovery_retries_after_restart(hass):
    async def scenario():
        manager, calls, _, fail = fixture(hass)
        await manager.action("owner", {"action": "start", "service": PHONE})
        fail.add("turn_off")
        await manager.close()
        assert PHONE in hass._store_backing[pa.KEY]["records"]
        fail.clear()
        restarted = pa.PhoneAccuracy(hass, clock=lambda: 101)
        await restarted.sweep()
        assert calls == ["turn_on", "turn_off", "turn_off"]
        assert not restarted.records
        await restarted.close()
    asyncio.run(scenario())


def test_hard_deadline_cannot_be_extended_by_heartbeats(hass):
    async def scenario():
        manager, calls, clock, _ = fixture(hass)
        result = await manager.action("owner", {"action": "start", "service": PHONE})
        for _ in range(29):
            clock[0] += 60
            await manager.action("owner", {"action": "heartbeat", "session": result["session"]})
        clock[0] += 61
        with pytest.raises(ValueError, match="expired"):
            await manager.action("owner", {"action": "heartbeat", "session": result["session"]})
        await manager.sweep()
        assert calls[-1] == "turn_off"
        await manager.close()
    asyncio.run(scenario())


def test_no_on_command_is_sent_when_durable_intent_cannot_be_saved(hass):
    async def scenario():
        manager, calls, _, _ = fixture(hass)
        async def fail(_):
            raise OSError("storage failure")
        manager.store.async_save = fail
        with pytest.raises(OSError):
            await manager.action("owner", {"action": "start", "service": PHONE})
        assert calls == [] and manager.records == {}
    asyncio.run(scenario())


def test_failed_on_expires_promptly_for_cleanup(hass):
    async def scenario():
        manager, calls, _, fail = fixture(hass)
        fail.add("turn_on")
        with pytest.raises(ValueError, match="not confirmed"):
            await manager.action("owner", {"action": "start", "service": PHONE})
        await manager.sweep()
        assert calls == ["turn_on", "turn_off"]
        await manager.close()
    asyncio.run(scenario())


def test_api_rejects_non_admin_without_loading_or_sending_commands(hass):
    async def scenario():
        _, calls, _, _ = fixture(hass)
        api = pa.BPSPhoneAccuracyAPI()
        request = {"hass_user": NS(id="owner", is_admin=False)}
        response = await api._handle(request, {"action": "start", "service": PHONE})
        assert response.status == 403 and not calls
        assert "_phone_accuracy" not in hass.data.get("bps", {})
    asyncio.run(scenario())


def test_disconnect_during_on_retains_server_cleanup_and_diagnostics_omit_tokens(hass):
    async def scenario():
        manager, calls, _, _ = fixture(hass)
        hass.data.update(bps_initialized=True, bps={"_phone_accuracy": manager})
        entered, release = asyncio.Event(), asyncio.Event()
        original = hass.services.async_call

        async def delayed(*args, **kwargs):
            await original(*args, **kwargs)
            if args[2]["data"]["command"] == "turn_on":
                entered.set()
                await release.wait()

        hass.services.async_call = delayed
        class Request(dict):
            app = {"hass": hass}
        request = Request(hass_user=NS(id="owner", is_admin=True))
        client = asyncio.create_task(pa.BPSPhoneAccuracyAPI()._handle(request, {"action": "start", "service": PHONE}))
        await entered.wait()
        client.cancel()
        await asyncio.sleep(0)
        assert not client.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await client
        assert PHONE in manager.records
        exported = manager.snapshot()
        assert exported["device_state"] == "unverified"
        assert all("token" not in row and "owner" not in row for row in exported["requests"])
        await manager.close()
        assert calls == ["turn_on", "turn_off"]
    asyncio.run(scenario())
