"""Opt-in, bounded, memory-only diagnostic recordings. Never alters tracking."""
import asyncio
import hashlib
import json
import logging
import math
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView

LOGGER = logging.getLogger(__name__)
INTERVAL_S = 2
MAX_DURATION_S = 1800
MAX_BYTES = 16 * 1024 * 1024
MAX_CONTEXT_BYTES = 1024 * 1024
MAX_FRAME_BYTES = 256 * 1024
MAX_ANNOTATIONS = 200
MAX_FRAMES = MAX_DURATION_S // INTERVAL_S + 1
FORMAT = "bps-diagnostics-v1"
GUIDE = (
    "Coordinates in positions/raw/radii and layout are map pixels; layout floor.scale is pixels per metre. "
    "Readings are current sensor snapshots; positions contain their own solve/observation timestamps and may be older. "
    "A known-position marker means the named tracker stayed there until clear_position, another marker, or recording end. "
    "Clear the marker BEFORE moving. Group markers apply to the group and its recorded member beacons. "
    "Receiver snapshot status is descriptive, not proof it participated in the earlier solve; use outdoor.receiver_diagnostics for that. "
    "No calibration is changed. Contains property geometry, tracker identifiers and location history; share deliberately."
)


def encode(value):
    return json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")


def pack(snapshot):
    """Encode detached data off the HA loop; reject excessively large frames."""
    context = encode(snapshot.pop("context"))
    if len(context) > MAX_CONTEXT_BYTES:
        raise ValueError("Diagnostic layout/calibration context exceeds 1 MiB")
    key = hashlib.sha256(context).hexdigest()
    snapshot["context_id"] = key
    frame = encode(snapshot)
    if len(frame) > MAX_FRAME_BYTES:
        raise ValueError("Diagnostic frame exceeds 256 KiB")
    return key, context, frame


class Recording:
    def __init__(self, hass, snapshot):
        self.hass = hass
        self.snapshot = snapshot
        self.lock = asyncio.Lock()
        self.task = None
        self.active = False
        self.accepting = bool(hass.data.get("bps_initialized"))
        self.reason = "Not started"
        self.frames = []
        self.contexts = {}
        self.annotations = []
        self.targets = []
        self.sources = frozenset()
        self.members = {}
        self.started = None
        self.ended = None
        self.deadline = 0
        self.bytes = 0

    def status(self):
        return {"active": self.active, "reason": self.reason, "frames": len(self.frames),
                "bytes": self.bytes, "targets": self.targets, "started": self.started,
                "ended": self.ended, "seconds_left": max(0, int(self.deadline - time.monotonic())) if self.active else 0,
                "annotations": len(self.annotations), "available": bool(self.frames)}

    async def start(self, targets, duration):
        if not isinstance(targets, list) or not 1 <= len(targets) <= 8 or any(
                not isinstance(t, str) or not 1 <= len(t) <= 128 for t in targets):
            raise ValueError("Select 1–8 valid tracker keys")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or not 10 <= duration <= MAX_DURATION_S:
            raise ValueError("Duration must be 10–1800 seconds")
        async with self.lock:
            if not self.accepting:
                raise ValueError("BPS must be running to record diagnostics")
            if self.active:
                raise ValueError("Stop the current recording first")
            # Validate and encode before replacing an existing recording.
            started = time.time()
            targets = list(dict.fromkeys(targets))
            initial = self.snapshot(self.hass, targets, started)
            sources = frozenset(b for values in initial["context"]["target_members"].values() for b in values)
            members = {key: tuple(values) for key, values in initial["context"]["target_members"].items()}
            packed = await self.hass.async_add_executor_job(pack, initial)
            if not self.accepting:
                raise ValueError("BPS stopped while preparing the recording")
            self.targets, self.started, self.ended = targets, started, None
            self.sources = sources
            self.members = members
            self.frames, self.contexts, self.annotations, self.bytes = [], {}, [], 0
            self.deadline = time.monotonic() + duration
            self.active, self.reason = True, "Recording"
            self._append(packed)
            self.task = self.hass.async_create_task(self._run())

    def _finish(self, reason):
        self.active, self.reason, self.ended = False, reason, time.time()

    def _append(self, packed):
        key, context, frame = packed
        extra = len(frame) + (len(context) if key not in self.contexts else 0)
        if self.bytes + extra > MAX_BYTES or len(self.frames) >= MAX_FRAMES:
            self._finish("Recording limit reached")
            return
        self.contexts.setdefault(key, context)
        self.frames.append(frame)
        self.bytes += extra

    async def _run(self):
        try:
            while self.active:
                await asyncio.sleep(min(INTERVAL_S, max(0, self.deadline - time.monotonic())))
                async with self.lock:
                    if not self.active:
                        break
                    if time.monotonic() >= self.deadline:
                        self._finish("Duration complete")
                        break
                    now = time.time()
                    data = self.snapshot(self.hass, self.targets, now)
                    self._append(await self.hass.async_add_executor_job(pack, data))
        except asyncio.CancelledError:
            raise
        except Exception as err:
            LOGGER.exception("BPS diagnostic recording stopped; tracking is unaffected")
            self._finish(f"Capture failed: {str(err)[:240]}; previous frames are available")

    async def stop(self, reason="Stopped"):
        async with self.lock:
            if self.active:
                self._finish(reason)
            task, self.task = self.task, None
            if task and not task.done():
                task.cancel()
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def clear(self):
        # Finish and erase under one lock, so a concurrent start cannot be erased.
        async with self.lock:
            if self.active:
                self._finish("Cleared")
            task, self.task = self.task, None
            if task and not task.done():
                task.cancel()
            self.frames, self.contexts, self.annotations = [], {}, []
            self.bytes, self.targets, self.started, self.ended = 0, [], None, None
            self.sources, self.members = frozenset(), {}
            self.reason = "Cleared"
        if task:
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def annotate(self, body):
        async with self.lock:
            if not self.active:
                raise ValueError("Start a recording before adding a marker")
            if len(self.annotations) >= MAX_ANNOTATIONS:
                raise ValueError("Marker limit reached (200)")
            kind = body.get("kind", "note")
            label = body.get("label", "")
            if not isinstance(label, str) or len(label) > 240:
                raise ValueError("Marker label must be at most 240 characters")
            item = {"time": time.time(), "kind": kind, "label": label}
            if kind in ("known_position", "clear_position"):
                target = body.get("target")
                if target not in self.targets:
                    raise ValueError("Marker tracker must be selected in this recording")
                item["target"] = target
                if kind == "known_position":
                    floor = body.get("floor")
                    # Inspect the current saved configuration, never the panel's unsaved geometry.
                    context = self.snapshot(self.hass, self.targets, item["time"])["context"]
                    floors = context["layout"]["floor"]
                    if not any(f.get("name") == floor and isinstance(f.get("scale"), (int, float))
                               and not isinstance(f["scale"], bool) and math.isfinite(f["scale"])
                               and f["scale"] >= 1e-6 for f in floors):
                        raise ValueError("Select a saved floor with a valid metric scale")
                    for key in ("x_m", "y_m"):
                        val = body.get(key)
                        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) or abs(val) > 1e6:
                            raise ValueError("Known coordinates must be finite metres from the map origin")
                        item[key] = val
                    item["floor"] = floor
            elif kind != "note":
                raise ValueError("Unknown marker type")
            encoded = encode(item)
            if self.bytes + len(encoded) > MAX_BYTES:
                raise ValueError("Recording size limit reached")
            self.annotations.append(encoded)
            self.bytes += len(encoded)

    async def export(self):
        async with self.lock:
            if not self.frames:
                raise ValueError("No diagnostic recording is available")
            metadata = {"format": FORMAT, "guide": GUIDE, "started": self.started,
                        "ended": self.ended or time.time(), "active_at_export": self.active,
                        "targets": list(self.targets), "interval_s": INTERVAL_S,
                        "reason": self.reason}
            contexts, frames, annotations = dict(self.contexts), list(self.frames), list(self.annotations)
        def build():
            # Encoded bytes are immutable; a concurrent new recording cannot change this export.
            return (encode(metadata)[:-1] + b',"contexts":{' + b",".join(
                encode(k) + b":" + v for k, v in contexts.items()) + b'},"frames":[' +
                b",".join(frames) + b'],"annotations":[' + b",".join(annotations) + b"]}")
        return await self.hass.async_add_executor_job(build)


def get_recording(hass, snapshot):
    bucket = hass.data.setdefault("bps", {})
    if "_diagnostics" not in bucket:
        bucket["_diagnostics"] = Recording(hass, snapshot)
    return bucket["_diagnostics"]


async def shutdown(hass):
    recording = hass.data.get("bps", {}).get("_diagnostics")
    if recording:
        recording.accepting = False
        await recording.stop("Integration stopped")


def activate(hass):
    recording = hass.data.get("bps", {}).get("_diagnostics")
    if recording:
        recording.accepting = True


class BPSDiagnosticsAPI(HomeAssistantView):
    url = "/api/bps/diagnostics"
    name = "api:bps:diagnostics"
    requires_auth = True

    def __init__(self, snapshot, inventory):
        self.snapshot, self.inventory = snapshot, inventory

    async def get(self, request):
        hass = request.app["hass"]
        recording = get_recording(hass, self.snapshot)
        if request.query.get("download") == "1":
            try:
                data = await recording.export()
            except ValueError as err:
                return web.json_response({"error": str(err)}, status=400)
            return web.Response(body=data, content_type="application/json", headers={
                "Content-Disposition": 'attachment; filename="bps-diagnostics.json"', "Cache-Control": "no-store"})
        return web.json_response({**recording.status(), **self.inventory(hass)})

    async def post(self, request):
        hass = request.app["hass"]
        recording = get_recording(hass, self.snapshot)
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Body must be a JSON object")
            action = body.get("action")
            if action == "start":
                updater = hass.data.get("bps_update_task")
                if not hass.data.get("bps_initialized") or updater is None or updater.done():
                    raise ValueError("BPS must be running to record diagnostics")
                targets = body.get("targets")
                known = {item["key"] for item in self.inventory(hass)["trackers"]}
                if not isinstance(targets, list) or any(not isinstance(t, str) or t not in known for t in targets):
                    raise ValueError("Select an existing Bermuda beacon or enabled tracker group")
                await recording.start(targets, body.get("duration_s", 600))
            elif action == "stop":
                await recording.stop()
            elif action == "annotate":
                await recording.annotate(body)
            elif action == "clear":
                await recording.clear()
            else:
                raise ValueError("Unknown diagnostic action")
        except (ValueError, TypeError) as err:
            return web.json_response({"error": str(err)}, status=400)
        return web.json_response(recording.status())
