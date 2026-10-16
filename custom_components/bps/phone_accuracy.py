"""Opt-in Android Companion App accuracy commands with server-side expiry."""

import asyncio
import re
import secrets
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.storage import Store

from .storage import save_verified_store, finish_persistence

SERVICE = re.compile(r"mobile_app_[a-z0-9_]{1,128}\Z", re.ASCII)
KEY = "bps_phone_accuracy"
LEASE_S = 90
MAX_SESSION_S = 30 * 60
MAX_PHONES = 8


class PhoneAccuracy:
    def __init__(self, hass, *, clock=time.time):
        self.hass, self.clock = hass, clock
        self.store = Store(hass, 1, KEY)
        self.records = {}
        self.loaded = False
        self.lock = asyncio.Lock()
        self.worker = None

    def services(self):
        registry = self.hass.services.async_services().get("notify", {})
        return [{"key": key, "name": "notify." + key}
                for key in sorted(registry) if SERVICE.fullmatch(key)][:128]

    def snapshot(self):
        now = self.clock()
        return {"device_state": "unverified", "lease_s": LEASE_S,
                "requests": [{"service": key, "expires_at": row["expires"], "deadline_at": row["deadline"],
                              "cleanup_pending": now >= min(row["expires"], row["deadline"])}
                             for key, row in self.records.items()]}

    def active(self):
        return (bool(self.hass.data.get("bps_initialized"))
                and self.hass.data.get("bps", {}).get("_tracking_active") is not False)

    async def persist(self, records):
        await save_verified_store(self.hass, self.store, {"records": records}, key=KEY)
        self.records = records

    async def load(self):
        if self.loaded:
            return
        saved = await self.store.async_load()
        rows = saved.get("records") if isinstance(saved, dict) else {} if saved is None else None
        if not isinstance(rows, dict) or len(rows) > MAX_PHONES or any(
                not isinstance(key, str) or not SERVICE.fullmatch(key)
                or not isinstance(row, dict)
                or not all(isinstance(row.get(k), str) and 1 <= len(row[k]) <= 128 for k in ("owner", "token"))
                for key, row in rows.items()):
            raise ValueError("Saved phone accuracy commands are unreadable; existing data was preserved")
        # An interrupted survey never resumes across HA restart. Retry OFF.
        self.records = {key: {**row, "expires": 0, "deadline": 0} for key, row in rows.items()}
        self.loaded = True
        if self.records:
            self.ensure_worker()

    def ensure_worker(self):
        if self.worker is None or self.worker.done():
            self.worker = self.hass.async_create_background_task(self.watchdog(), "BPS phone accuracy expiry")

    async def command(self, service, command):
        if service not in {row["key"] for row in self.services()}:
            raise ValueError("Select a registered Companion App notification service")
        # TTL zero prevents FCM keeping an old ON command queued for an offline phone.
        await asyncio.wait_for(self.hass.services.async_call("notify", service, {
            "message": "command_high_accuracy_mode",
            "data": {"command": command, "ttl": 0, "priority": "high"},
        }, blocking=True), timeout=8)

    async def disable(self, service):
        await self.command(service, "turn_off")
        await self.persist({key: value for key, value in self.records.items() if key != service})

    async def sweep(self):
        async with self.lock:
            await self.load()
            now = self.clock()
            for service, row in list(self.records.items()):
                if now >= min(row["expires"], row["deadline"]):
                    try:
                        await self.disable(service)
                    except (ValueError, OSError, TimeoutError):
                        pass  # Keep the durable OFF intent and retry on the next sweep.
                    except Exception:
                        pass  # Notification services can raise HA-specific delivery errors.

    async def watchdog(self):
        while self.records:
            await asyncio.sleep(15)
            await self.sweep()

    async def close(self, *, retry_pending=False):
        if self.worker and not self.worker.done():
            self.worker.cancel()
            await asyncio.gather(self.worker, return_exceptions=True)
        async with self.lock:
            await self.load()
            self.records = {key: {**row, "expires": 0} for key, row in self.records.items()}
            for service in list(self.records):
                try:
                    await self.disable(service)
                except Exception:
                    pass  # Durable intent survives shutdown for a retry after startup.
            if retry_pending and self.records:
                self.ensure_worker()

    async def action(self, owner, body):
        async with self.lock:
            await self.load()
            action, now = body.get("action"), self.clock()
            if action in ("start", "heartbeat") and not self.active():
                raise ValueError("BPS is not tracking; start another survey after it loads")
            if action == "start":
                lifecycle = self.hass.data.get("bps", {}).get("_tracking_lifecycle")
                service = body.get("service")
                if not isinstance(service, str) or service not in {row["key"] for row in self.services()}:
                    raise ValueError("Select your Android Companion App phone")
                if service in self.records:
                    raise ValueError("This phone already has an accuracy request; stop it before starting again")
                if len(self.records) >= MAX_PHONES:
                    raise ValueError("Too many active phone accuracy requests")
                row = {"owner": owner, "token": secrets.token_urlsafe(24),
                       "expires": now + LEASE_S, "deadline": now + MAX_SESSION_S}
                # Persist before ON so interruption/restart cannot lose the OFF intent.
                await self.persist({**self.records, service: row})
                self.ensure_worker()
                if not self.active() or self.hass.data.get("bps", {}).get("_tracking_lifecycle") is not lifecycle:
                    self.records[service]["expires"] = 0
                    raise ValueError("BPS stopped before the phone command; cleanup is scheduled")
                try:
                    await self.command(service, "turn_on")
                except Exception as exc:
                    self.records[service]["expires"] = 0
                    raise ValueError("Phone accuracy ON was not confirmed; an OFF retry is scheduled") from exc
                return {"session": row["token"], "status": "on_requested", "device_state": "unverified",
                        "lease_s": LEASE_S}
            token = body.get("session")
            matches = [service for service, row in self.records.items()
                       if row["owner"] == owner and (token is None or row["token"] == token)]
            if action == "stop":
                for service in matches:
                    self.records[service]["expires"] = 0
                    await self.disable(service)
                return {"status": "off_requested", "device_state": "unverified", "requested_phones": len(matches)}
            if action == "heartbeat":
                if not isinstance(token, str) or len(matches) != 1:
                    raise ValueError("Phone accuracy request expired; restart the survey")
                row = self.records[matches[0]]
                if now >= min(row["expires"], row["deadline"]):
                    raise ValueError("Phone accuracy request expired; restart the survey")
                row["expires"] = min(now + LEASE_S, row["deadline"])
                return {"status": "active", "device_state": "unverified"}
            raise ValueError("Select start, heartbeat or stop")


async def activate(hass):
    bucket = hass.data.setdefault("bps", {})
    if "_phone_accuracy" not in bucket:
        bucket["_phone_accuracy"] = PhoneAccuracy(hass)
    manager = bucket["_phone_accuracy"]
    async with manager.lock:
        await manager.load()
        if manager.records:
            manager.ensure_worker()
    if not bucket.get("_phone_accuracy_stop_listener"):
        async def on_stop(_event):
            await manager.close()
        bucket["_phone_accuracy_stop_listener"] = hass.bus.async_listen_once("homeassistant_stop", on_stop)
    return manager


class BPSPhoneAccuracyAPI(HomeAssistantView):
    url = "/api/bps/phone_accuracy"
    name = "api:bps:phone_accuracy"
    requires_auth = True

    async def _handle(self, request, body=None):
        user = request.get("hass_user")
        if user is None or getattr(user, "is_admin", False) is not True:
            return web.json_response({"error": "Administrator access is required for phone commands"}, status=403)
        try:
            if body and body.get("action") in ("start", "heartbeat") and not request.app["hass"].data.get("bps_initialized"):
                raise ValueError("BPS is not loaded; start another survey after it loads")
            manager = await activate(request.app["hass"])
            if body is None:
                data = {"services": manager.services(), "device_state": "unverified"}
            else:
                data = await finish_persistence(manager.action(user.id, body))
            return web.json_response(data, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError, OSError, TimeoutError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception:
            return web.json_response({"error": "Phone command failed; pending OFF commands will be retried"}, status=503)

    async def get(self, request):
        return await self._handle(request)

    async def post(self, request):
        if request.content_length is None or request.content_length > 4096:
            return web.json_response({"error": "Phone accuracy request is too large"}, status=400)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Supply a phone accuracy action")
        except (ValueError, TypeError):
            return web.json_response({"error": "Invalid phone accuracy action"}, status=400)
        return await self._handle(request, body)
