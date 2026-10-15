"""Private, bounded history of instantaneous user-labelled diagnostic snapshots."""

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import re
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.helpers.storage import Store

from .diagnostics import encode, format_known_position_report
from .storage import finish_persistence, save_verified_store

STORAGE_KEY = "bps_ground_truth"
MAX_MARKS = 100
MAX_BYTES = 16 * 1024 * 1024
MAX_AGE_S = 7 * 86400
REQUEST_ID = re.compile(r"[a-zA-Z0-9_-]{16,80}\Z", re.ASCII)
GUIDE = (
    "Each actual-location mark applies only at its captured snapshot instant. "
    "Do not interpolate between marks or treat the dog as stationary afterward. "
    "Groups use membership captured with that mark. Current BLE readings and earlier "
    "position solves retain separate clocks. GPS coordinates, map geometry and identifiers "
    "are private; this history stays in Home Assistant storage. No tracking or calibration is changed."
)


def prepare(snapshot, marker, request_id):
    result = format_known_position_report(snapshot, [marker])
    report = json.loads(result["text"][result["text"].index("{") :])
    mark = report["known_positions"][0]
    record = {
        "id": request_id,
        "time": snapshot["time"],
        "marker": mark,
        "comparison": result["comparison"],
        "report": report,
    }
    record["capture"] = capture_metadata(record)
    return record


def capture_metadata(record):
    """Derive provenance only from the report frozen with this mark.

    Older records may lack fields. Never fill them from today's HA state.
    """
    report = record.get("report") if isinstance(record.get("report"), dict) else {}
    context = report.get("context") if isinstance(report.get("context"), dict) else {}
    runtime = report.get("runtime") if isinstance(report.get("runtime"), dict) else {}
    bermuda = report.get("bermuda") if isinstance(report.get("bermuda"), dict) else {}
    bermuda_settings = [{key: entry.get(key) for key in ("entry_id", "configured_settings", "effective_settings")}
                        for entry in bermuda.get("entries", []) if isinstance(entry, dict)]
    settings = {"context": context, "bermuda_settings": bermuda_settings or None,
                "calibration_status": report.get("calibration_status"),
                "effective_history_settings": runtime.get("effective_history_settings")}
    has_settings = any(value is not None and value != {} for value in settings.values())
    canonical = json.dumps(settings, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).encode("utf-8") if has_settings else None
    at = record.get("time")
    try:
        utc = datetime.fromtimestamp(at, timezone.utc).isoformat() if isinstance(at, (int, float)) and not isinstance(at, bool) else None
    except (OverflowError, OSError, ValueError):
        utc = None
    return {"capture_id": record.get("id"), "captured_at_epoch_s": at,
            "captured_at_utc": utc,
            "clock": "home_assistant_server_utc_epoch", "label_scope": "instantaneous",
            "versions": {"bps": report.get("bps_version"),
                         "home_assistant": report.get("home_assistant_version"),
                         "python": report.get("python_version"),
                         "bermuda": bermuda.get("version")},
            "settings_sha256": hashlib.sha256(canonical).hexdigest() if canonical is not None else None,
            "settings_in_saved_report": [name for name, value in settings.items()
                                         if value is not None and value != {}],
            "general_settings": {"defaults": context.get("defaults"),
                                 "bermuda_settings": bermuda_settings or None,
                                 "effective_history_settings": runtime.get("effective_history_settings")}}


def summary(record):
    marker = record["marker"]
    comparison = record["comparison"]["markers"][0]
    return {
        "id": record["id"],
        "time": record["time"],
        **marker,
        "members": comparison["members"],
        "position_errors": comparison["position_errors"],
        "summary": comparison["summary"],
        "capture": record.get("capture") or capture_metadata(record),
    }


def validate_store(data):
    if data is not None and (
        not isinstance(data, dict)
        or not isinstance(data.get("records"), list)
        or not isinstance(data.get("evicted"), int)
        or isinstance(data["evicted"], bool)
        or data["evicted"] < 0
    ):
        raise ValueError(
            "Actual-location history is unreadable; existing data was preserved"
        )
    records = data["records"] if data else []
    if len(records) > MAX_MARKS or len(encode(data or {})) > MAX_BYTES:
        raise ValueError("Actual-location history exceeds its storage limits")
    ids = set()
    summaries = {}
    for record in records:
        if (
            not isinstance(record, dict)
            or not REQUEST_ID.fullmatch(str(record.get("id", "")))
            or not isinstance(record.get("time"), (int, float))
            or isinstance(record["time"], bool)
            or record["id"] in ids
        ):
            raise ValueError(
                "Actual-location history is unreadable; existing data was preserved"
            )
        try:
            summaries[record["id"]] = summary(record)
            encode(record)
        except (KeyError, IndexError, TypeError, ValueError) as err:
            raise ValueError(
                "Actual-location history is unreadable; existing data was preserved"
            ) from err
        ids.add(record["id"])
    return records, data["evicted"] if data else 0, summaries


def bounded_records(records, previous_count, evicted, record):
    records = [*records, record]
    evicted += previous_count + 1 - len(records)
    sizes = [len(encode(r)) for r in records]
    total = sum(sizes)

    def size():
        return (
            len(encode({"records": [], "evicted": evicted}))
            + total
            + max(0, len(records) - 1)
        )

    if len(encode({"records": [record], "evicted": evicted})) > MAX_BYTES:
        raise ValueError("Actual-location snapshot exceeds the history storage limit")
    while len(records) > MAX_MARKS or size() > MAX_BYTES:
        records.pop(0)
        total -= sizes.pop(0)
        evicted += 1
    return {"records": records, "evicted": evicted}


class GroundTruthHistory:
    def __init__(self, hass):
        self.hass = hass
        self.store = Store(hass, 1, STORAGE_KEY)
        self.lock = asyncio.Lock()
        self.loaded = False
        self.records = []
        self.evicted = 0
        self._summary_cache = {}

    async def load(self):
        if self.loaded:
            return
        try:
            data = await self.store.async_load()
        except Exception as err:
            raise ValueError(
                "Actual-location history could not be read; existing data was preserved"
            ) from err
        if data is None:
            self.records, self.evicted = [], 0
        else:
            self.records, self.evicted, self._summary_cache = await self.hass.async_add_executor_job(
                validate_store, data
            )
        self.loaded = True

    def recent(self, now=None):
        now = time.time() if now is None else now
        return [r for r in self.records if now - r["time"] <= MAX_AGE_S]

    async def expire(self, now=None):
        records = self.recent(now)
        expired = len(self.records) - len(records)
        if not expired:
            return
        data = {"records": records, "evicted": self.evicted + expired}
        await self.persist(data)

    async def persist(self, data):
        # Retain the caller's history lock through disk and cache publication,
        # including GET expiry when HA cancels a disconnected HTTP handler.
        async def commit():
            await save_verified_store(self.hass, self.store, data, key=STORAGE_KEY)
            self.records, self.evicted = data["records"], data["evicted"]
            retained = {record["id"] for record in self.records}
            self._summary_cache = {key: value for key, value in self._summary_cache.items()
                                   if key in retained}

        await finish_persistence(commit())

    def overview(self, now=None):
        records = self.recent(now)
        return {
            "format": "bps-ground-truth-v1",
            "guide": GUIDE,
            "marks": [self._summary_cache.get(r["id"]) or summary(r) for r in records],
            "evicted": self.evicted,
            "limits": {
                "max_marks": MAX_MARKS,
                "max_bytes": MAX_BYTES,
                "max_age_s": MAX_AGE_S,
            },
        }

    async def report_snapshot(self, now=None):
        """Detach record references while serialized with mark/expiry writes.

        Published records are never modified in place. Formatting the selected
        fields happens later in the diagnostic executor, outside this lock.
        """
        async with self.lock:
            await self.load()
            await self.expire(now)
            records = tuple(self.recent(now))
            return {"status": "available" if records else "empty", "records": records,
                    "evicted": self.evicted}

    async def append(self, record):
        data = await self.hass.async_add_executor_job(
            bounded_records, self.recent(), len(self.records), self.evicted, record
        )
        await self.persist(data)

    async def clear(self):
        await self.persist({"records": [], "evicted": 0})


def get_history(hass):
    bucket = hass.data.setdefault("bps", {})
    if "_ground_truth" not in bucket:
        bucket["_ground_truth"] = GroundTruthHistory(hass)
    return bucket["_ground_truth"]


async def report_history(hass, now=None):
    """Keep a usable live report even when the separate history is unreadable."""
    try:
        return await get_history(hass).report_snapshot(now)
    except Exception:
        return {"status": "unavailable", "reason":
                "Saved actual-location history could not be read or expired; check the actual-location history endpoint"}


class BPSGroundTruthAPI(HomeAssistantView):
    url = "/api/bps/ground_truth"
    name = "api:bps:ground_truth"
    requires_auth = True

    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.job = None

    def finished(self, job):
        if self.job is job:
            self.job = None
        if not job.cancelled():
            job.exception()

    async def get(self, request):
        history = get_history(request.app["hass"])
        try:
            async with history.lock:
                await history.load()
                now = time.time()
                await history.expire(now)
                data = history.overview(now)
                if request.query.get("download") == "1":
                    data["records"] = list(history.recent(now))
            payload = await history.hass.async_add_executor_job(encode, data)
            return web.Response(
                body=payload,
                content_type="application/json",
                headers={"Cache-Control": "no-store"},
            )
        except (ValueError, TypeError, OSError) as err:
            return web.json_response({"error": str(err)}, status=400)

    async def post(self, request):
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get("action") not in (
                "mark",
                "clear",
            ):
                raise ValueError("Choose mark or clear for actual-location history")
            if self.job is not None and not self.job.done():
                return web.json_response(
                    {
                        "error": "An actual-location mark is being saved; try again shortly"
                    },
                    status=429,
                )
            if body["action"] == "mark":
                request_id = body.get("id")
                if not isinstance(request_id, str) or not REQUEST_ID.fullmatch(
                    request_id
                ):
                    raise ValueError("Supply a unique actual-location mark ID")
                if not isinstance(body.get("marker"), dict):
                    raise ValueError("Supply an actual-location marker")
            history = get_history(request.app["hass"])

            async def save():
                async with history.lock:
                    await history.load()
                    if body["action"] == "clear":
                        await history.clear()
                        return history.overview()
                    now = time.time()
                    await history.expire(now)
                    existing = next(
                        (r for r in history.records if r["id"] == body["id"]), None
                    )
                    if existing:
                        expected = {"accuracy_m": 1.0, "label": "", **body["marker"]}
                        if existing["marker"] != expected:
                            raise ValueError(
                                "A mark ID cannot be reused for a different location"
                            )
                        return {**history.overview(now), "saved": summary(existing)}
                    snapshot = self.snapshot(history.hass, now)
                    record = await history.hass.async_add_executor_job(
                        prepare, snapshot, body["marker"], body["id"]
                    )
                    await history.append(record)
                    return {**history.overview(now), "saved": summary(record)}

            self.job = asyncio.create_task(save())
            self.job.add_done_callback(self.finished)
            result = await asyncio.shield(self.job)
            return web.json_response(result, headers={"Cache-Control": "no-store"})
        except (ValueError, TypeError, OSError) as err:
            return web.json_response({"error": str(err)}, status=400)
