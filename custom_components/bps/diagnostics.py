"""Opt-in, bounded, memory-only diagnostic recordings. Never alters tracking."""
import asyncio
import hashlib
import json
import logging
import math
import io
from pathlib import Path
import sys
import time
import zipfile

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from . import recent_tracking, snapshot_truth

LOGGER = logging.getLogger(__name__)
INTERVAL_S = 2
MAX_DURATION_S = 1800
MAX_BYTES = 16 * 1024 * 1024
MAX_CONTEXT_BYTES = 1024 * 1024
MAX_FRAME_BYTES = 256 * 1024
MAX_ANNOTATIONS = 200
MAX_FRAMES = MAX_DURATION_S // INTERVAL_S + 1
FORMAT = "bps-diagnostics-v1"
MAX_REPORT_BYTES = 4 * 1024 * 1024
MAX_HISTORY_REPORT_BYTES = 1024 * 1024
MAX_BUNDLE_BYTES = 64 * 1024 * 1024
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


def format_report(snapshot):
    """Format a detached live snapshot off the HA loop, without recording."""
    import homeassistant.const as ha_const
    version = json.loads(Path(__file__).with_name("manifest.json").read_text())["version"]
    report = {"format": "bps-support-report-v1", "bps_version": version,
              "home_assistant_version": getattr(ha_const, "__version__", "unknown"),
              "python_version": sys.version.split()[0], **snapshot}
    recent = report.pop("_recent_tracking_snapshot", None)
    actual = report.pop("_actual_location_history", None)
    explicit_panel = "_panel_bytes" in report
    panel_bytes = report.pop("_panel_bytes", 256 * 1024)
    limit = MAX_REPORT_BYTES - panel_bytes
    header = ("BPS full diagnostics (live snapshot)\n"
            "Positions and current sensor readings have separate timestamps. Coordinates are map pixels; floor scale is pixels/metre.\n"
            )
    def render():
        return header + json.dumps(report, indent=2, allow_nan=False, ensure_ascii=False)
    text = render()
    if actual is not None:
        history = {"format": "bps-actual-location-summary-v1",
                   "status": actual["status"],
                   "guide": "Marks and comparisons describe only their capture instant; newest marks appear first. Position fix and source observation clocks can be older than the mark time. Full captured reports are in the actual-location history download.",
                   "marks": [], "evicted": actual.get("evicted"),
                   "total": len(actual.get("records", ())), "included": 0,
                   "omitted": len(actual.get("records", ())), "truncated": False}
        if actual["status"] == "unavailable":
            history["reason"] = actual["reason"]
        report["actual_location_history"] = history
        candidate = render()
        if len(candidate.encode("utf-8")) <= limit:
            text = candidate
            if actual["status"] == "available":
                from .ground_truth import summary
                for record in reversed(actual["records"]):
                    item = summary(record)
                    # The full captured comparison may be large. Keep the
                    # mark and position errors, bounded by the final render.
                    errors = item["position_errors"]
                    for count in range(len(errors), -1, -1):
                        item["position_errors"] = errors[:count]
                        item["position_errors_omitted"] = len(errors) - count
                        history["marks"].append(item)
                        history["included"] += 1
                        history["omitted"] -= 1
                        rendered = render()
                        if (len(rendered.encode("utf-8")) <= limit and
                                len(encode(history)) <= MAX_HISTORY_REPORT_BYTES):
                            text = rendered
                            break
                        history["marks"].pop()
                        history["included"] -= 1
                        history["omitted"] += 1
                    else:
                        continue
                history["truncated"] = history["omitted"] > 0 or any(
                    mark["position_errors_omitted"] for mark in history["marks"])
                text = render()
        else:
            report.pop("actual_location_history")
    if recent is not None:
        # Reserve space for frontend panel context and nested JSON indentation.
        budget = min(recent_tracking.EXPORT_MAX_BYTES,
                     (limit - len(text.encode("utf-8"))) // 2)
        if budget >= 2048:
            report["recent_tracking"] = recent_tracking.export_snapshot(recent, budget)
            report["recent_tracking"]["guide"] = (
                "Automatic local memory history of completed tracking batches. Sampling time is not a new BLE observation. "
                "Source updated/observed timestamps and receiver_diagnostics retain the solver input clocks. "
                "Missing group members can be stale, missing or invalid; check their original fixes and context. "
                "Grey mini icons mean stale or excluded, not known physical accuracy. "
                "Contexts are saved backend settings; unsaved panel placements are only in the live panel context. "
                "Window, byte limits and export truncation can shorten coverage. Restart/unload clears history.")
            text = render()
            # Account for the final nested representation, not just the
            # standalone history export. Preserve live data if it cannot fit.
            while len(text.encode("utf-8")) > limit and budget >= 4096:
                guide = report["recent_tracking"]["guide"]
                budget //= 2
                report["recent_tracking"] = recent_tracking.export_snapshot(recent, budget)
                report["recent_tracking"]["guide"] = guide
                text = render()
            if len(text.encode("utf-8")) > limit:
                report.pop("recent_tracking")
                text = render()
        else:
            report["recent_tracking"] = {"metadata": {"export_truncated": True},
                "reason": "Live report leaves insufficient space for recent history"}
            candidate = render()
            if len(candidate.encode("utf-8")) <= (limit if explicit_panel else MAX_REPORT_BYTES):
                text = candidate
    if len(text.encode("utf-8")) > MAX_REPORT_BYTES:
        raise ValueError("Full diagnostic report exceeds 4 MiB; reduce tracker/receiver scope before retrying")
    return text


def format_known_position_report(snapshot, markers):
    """Analyze and render one detached full report in the executor."""
    comparison = snapshot_truth.analyse(snapshot, markers)
    report = {**snapshot, "known_positions": [
        {key: marker[key] for key in ("target", "floor", "x_m", "y_m", "accuracy_m", "label")}
        for marker in comparison["markers"]], "known_position_comparison": comparison}
    text = format_report(report)
    if len(text.encode("utf-8")) + snapshot.get("_panel_bytes", 0) > MAX_REPORT_BYTES:
        raise ValueError("Full diagnostic report and panel context exceed 4 MiB")
    return {"text": text, "comparison": comparison, "read_only": True, "time": snapshot["time"]}


def build_bundle(snapshot, actual, recording, panel_context, phone_survey=None,
                 phone_omission=None, recording_omission=None):
    """Create one bounded, self-describing support archive off the HA loop."""
    recent = snapshot.get("_recent_tracking_snapshot")
    live = {key: value for key, value in snapshot.items() if not key.startswith("_")}
    import homeassistant.const as ha_const
    live.setdefault("bps_version", json.loads(Path(__file__).with_name("manifest.json").read_text())["version"])
    live.setdefault("home_assistant_version", getattr(ha_const, "__version__", "unknown"))
    live.setdefault("python_version", sys.version.split()[0])
    report_input = {**snapshot, "_panel_bytes": 0,
                    "_actual_location_history": actual}
    report_input.pop("_bermuda_full_snapshot", None)
    manifest = {"format": "bps-diagnostic-bundle-v1", "captured_at": snapshot.get("time"),
                "versions": {"bps": live["bps_version"], "home_assistant": live["home_assistant_version"],
                             "python": live["python_version"], "bermuda": live.get("bermuda", {}).get("version")},
                "limits": {"max_uncompressed_bytes": MAX_BUNDLE_BYTES,
                           "full_report_bytes": MAX_REPORT_BYTES},
                "files": {}, "omissions": {}}
    payloads = {}
    total = 0

    def add(name, value, required=False):
        nonlocal total
        data = value if isinstance(value, bytes) else encode(value)
        if total + len(data) + 32768 > MAX_BUNDLE_BYTES:
            manifest["omissions"][name] = "Bundle size limit"
            if required:
                raise ValueError("Live diagnostic snapshot exceeds the bundle size limit")
            return
        payloads[name] = data
        manifest["files"][name] = {"bytes": len(data)}
        total += len(data)

    add("live_snapshot.json", live, required=True)
    direct = snapshot.get("_bermuda_full_snapshot")
    if direct is not None:
        add("bermuda_measurements.json", direct)
    if actual["status"] in ("available", "empty"):
        from .ground_truth import GUIDE as HISTORY_GUIDE
        add("actual_location_history.json", {
            "format": "bps-ground-truth-v1", "guide": HISTORY_GUIDE,
            "records": actual["records"], "count": len(actual["records"]),
            "evicted": actual["evicted"]})
    else:
        manifest["omissions"]["actual_location_history.json"] = actual["reason"]
    if recording is not None:
        add("diagnostic_recording.json", recording)
    else:
        manifest["omissions"]["diagnostic_recording.json"] = (
            recording_omission or "No diagnostic recording is available")
    try:
        add("full_report.txt", format_report(report_input).encode("utf-8"))
    except (ValueError, TypeError) as err:
        manifest["omissions"]["full_report.txt"] = str(err)
    if recent is not None:
        try:
            add("recent_tracking.json", recent_tracking.export_snapshot(
                recent, min(recent_tracking.EXPORT_MAX_BYTES,
                            MAX_BUNDLE_BYTES - total - 32768)))
        except ValueError:
            manifest["omissions"]["recent_tracking.json"] = "Bundle size limit cannot fit recent tracking metadata"
    else:
        manifest["omissions"]["recent_tracking.json"] = "No recent tracking is available"
    layout = live.get("context", {}).get("layout")
    if layout is not None:
        add("saved_layout.json", layout)
    if panel_context is not None:
        add("panel_context.json", panel_context)
    if phone_survey is not None:
        add("phone_survey.json", phone_survey)
    else:
        manifest["omissions"]["phone_survey.json"] = phone_omission or "Authenticated owner is unavailable"
    manifest["counts"] = {"actual_location_marks": len(actual.get("records", ())),
                          "actual_location_evicted": actual.get("evicted"),
                          "actual_location_status": actual["status"],
                          "data_files": len(payloads),
                          "data_bytes_excluding_manifest": total}
    add("manifest.json", manifest, required=True)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        for name, data in payloads.items():
            entry = zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, data, compresslevel=6)
    return output.getvalue()


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
    hass.data.get("bps", {}).pop("_recent_tracking_last_attempt", None)
    recent = hass.data.get("bps", {}).pop("_recent_tracking", None)
    if recent is not None:
        recent.clear()
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

    def __init__(self, snapshot, inventory, report=None):
        self.snapshot, self.inventory = snapshot, inventory
        self.report = report
        # This view is registered once per HA instance and survives BPS reloads.
        self._snapshot_job = None
        self._bundle_job = None

    def _snapshot_finished(self, job):
        if self._snapshot_job is job:
            self._snapshot_job = None
        # A disconnected/cancelled HTTP caller may no longer retrieve failures.
        if not job.cancelled():
            job.exception()

    def _bundle_finished(self, job):
        if self._bundle_job is job:
            self._bundle_job = None
        if not job.cancelled():
            job.exception()

    async def _report_history(self, hass, now):
        from .ground_truth import report_history
        return await report_history(hass, now)

    async def get(self, request):
        hass = request.app["hass"]
        if request.query.get("report") == "1":
            try:
                if self.report is None:
                    raise ValueError("Live diagnostic report is unavailable")
                panel = request.query.get("panel_bytes")
                if panel is not None and (not isinstance(panel, str) or len(panel) > 7
                        or not panel.isascii() or not panel.isdigit() or int(panel) > MAX_REPORT_BYTES):
                    raise ValueError("Invalid diagnostic panel byte reservation")
                now = time.time()
                snapshot = self.report(hass, now)
                if panel is not None:
                    snapshot["_panel_bytes"] = int(panel)
                snapshot["_actual_location_history"] = await self._report_history(hass, now)
                text = await hass.async_add_executor_job(format_report, snapshot)
                if len(text.encode("utf-8")) + snapshot.get("_panel_bytes", 0) > MAX_REPORT_BYTES:
                    raise ValueError("Full diagnostic report and panel context exceed 4 MiB")
            except (ValueError, TypeError) as err:
                return web.json_response({"error": str(err)}, status=400)
            return web.Response(body=encode({"text": text}), content_type="application/json",
                                headers={"Cache-Control": "no-store"})
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
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Body must be a JSON object")
            action = body.get("action")
            if action == "bundle":
                if self.report is None:
                    raise ValueError("Live diagnostic report is unavailable")
                if self._bundle_job is not None and not self._bundle_job.done():
                    return web.json_response({"error": "A diagnostic bundle is already being prepared; try again after it completes"}, status=429)
                panel_context = body.get("panel_context")
                if panel_context is not None:
                    if not isinstance(panel_context, dict) or len(encode(panel_context)) > MAX_CONTEXT_BYTES:
                        raise ValueError("Diagnostic panel context must be a JSON object under 1 MiB")
                now = time.time()
                async def prepare_bundle():
                    snapshot = self.report(hass, now)
                    from .bermuda_direct import BermudaDirect
                    direct = BermudaDirect(hass, now=now, include_history=True, include_scanner_devices=True)
                    snapshot["_bermuda_full_snapshot"] = {
                        "entries": direct.statuses, "entries_truncated": direct.entries_truncated,
                        "snapshots": list(direct.snapshots.values()),
                        "guide": "All detached public direct-API data captured for this bundle, before clipboard display limits. Original source clocks and per-entry limits/omissions remain attached."}
                    actual = await self._report_history(hass, now)
                    recording, recording_omission = None, None
                    saved = hass.data.get("bps", {}).get("_diagnostics")
                    if saved is not None and saved.frames:
                        try:
                            recording = await saved.export()
                        except (ValueError, TypeError, OSError):
                            recording_omission = "Diagnostic recording became unavailable during export"
                    phone_data, phone_omission = None, None
                    owner = getattr(request.get("hass_user"), "id", None)
                    if isinstance(owner, str) and 0 < len(owner) <= 128:
                        try:
                            from .phone_survey import PhoneSurvey
                            bucket = hass.data.setdefault("bps", {})
                            manager = bucket.get("_phone_survey")
                            if manager is None:
                                manager = bucket["_phone_survey"] = PhoneSurvey(
                                    hass, self.snapshot, self.inventory)
                            phone_data = await manager.get(owner)
                        except Exception:
                            phone_omission = "Phone survey could not be read for the authenticated owner"
                    return await hass.async_add_executor_job(
                        build_bundle, snapshot, actual, recording, panel_context,
                        phone_data, phone_omission, recording_omission)

                job = asyncio.create_task(prepare_bundle())
                self._bundle_job = job
                job.add_done_callback(self._bundle_finished)
                payload = await asyncio.shield(job)
                return web.Response(body=payload, content_type="application/zip", headers={
                    "Content-Disposition": 'attachment; filename="bps-diagnostics-full.zip"',
                    "Cache-Control": "no-store"})
            if action == "snapshot":
                if self.report is None:
                    raise ValueError("Live diagnostic report is unavailable")
                panel = body.get("panel_bytes", 0)
                if isinstance(panel, bool) or not isinstance(panel, int) or not 0 <= panel <= MAX_REPORT_BYTES:
                    raise ValueError("Invalid diagnostic panel byte reservation")
                if self._snapshot_job is not None and not self._snapshot_job.done():
                    return web.json_response({"error": "A known-position snapshot is already being prepared; try again after it completes"}, status=429)
                now = time.time()

                async def prepare_snapshot():
                    snapshot = self.report(hass, now)
                    snapshot["_panel_bytes"] = panel
                    snapshot["_actual_location_history"] = await self._report_history(hass, now)
                    return await hass.async_add_executor_job(
                        format_known_position_report, snapshot, body.get("known_positions"))

                job = asyncio.create_task(prepare_snapshot())
                self._snapshot_job = job
                job.add_done_callback(self._snapshot_finished)
                # Cancelling an HTTP request cannot stop its executor thread;
                # keep the slot until actual completion, rather than queue more.
                result = await asyncio.shield(job)
                return web.Response(body=encode(result), content_type="application/json", headers={"Cache-Control": "no-store"})
            recording = get_recording(hass, self.snapshot)
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
