"""Bounded, volatile tracking diagnostics; no HA, tasks, or disk access.

Pack detached JSON data outside the event loop. Only ``due``, ``append``,
``record_failure``, ``clear`` and ``snapshot`` touch the loop-owned buffer.
Snapshots contain immutable bytes and can safely be exported in an executor.
Sampling time is independent of any observation timestamps inside the payload.
"""
from collections import deque
from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any

WINDOW_SECONDS = 600.0
SAMPLE_INTERVAL_SECONDS = 2.0
MAX_FRAMES = 300
MAX_BYTES = 2 * 1024 * 1024
MAX_FRAME_BYTES = 64 * 1024
MAX_CONTEXT_BYTES = 256 * 1024
EXPORT_MAX_BYTES = 1024 * 1024
MAX_COUNTER = (1 << 63) - 1


class PackError(ValueError):
    """Invalid detached data, or a valid sample exceeding a per-item limit."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def _timestamp(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("sampling time must be a finite number")
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("sampling time must be a finite number")
    return value


def _encode(value: Any, *, pretty: bool = False) -> bytes:
    return json.dumps(value, allow_nan=False, ensure_ascii=False,
                      sort_keys=True, indent=2 if pretty else None,
                      separators=None if pretty else (",", ":")).encode("utf-8")


@dataclass(frozen=True)
class PackedSample:
    time: float
    context_id: str
    context_json: bytes
    frame_json: bytes


def pack(payload: dict) -> PackedSample:
    """Encode ``{time, context, positions, ...}`` without retaining input refs.

    Context keys are sorted before hashing, so equivalent settings deduplicate.
    The frame retains original nested observation timestamps verbatim.
    """
    try:
        if not isinstance(payload, dict) or not isinstance(payload.get("context"), dict):
            raise ValueError("a sample and its context must be objects")
        time = _timestamp(payload.get("time"))
        if not isinstance(payload.get("positions"), (dict, list)):
            raise ValueError("positions must be an object or list")
        context = _encode(payload["context"])
        context_id = hashlib.sha256(context).hexdigest()
        frame = dict(payload)
        del frame["context"]
        frame["context_id"] = context_id
        frame["time"] = time
        frame_json = _encode(frame)
    except (ValueError, TypeError, OverflowError, RecursionError) as err:
        raise PackError("malformed", "sample is not finite JSON data") from err
    if len(context) > MAX_CONTEXT_BYTES or len(frame_json) > MAX_FRAME_BYTES:
        raise PackError("oversize", "sample exceeds the frame or context byte limit")
    return PackedSample(time, context_id, context, frame_json)


@dataclass(frozen=True)
class HistorySnapshot:
    frames: tuple[PackedSample, ...]
    # Context bytes are shared once per version, rather than copied per frame.
    contexts: tuple[tuple[str, bytes], ...]
    metadata: tuple[tuple[str, Any], ...]


class RecentTrackingHistory:
    """Event-loop-owned rolling buffer with exact encoded-byte accounting."""

    def __init__(self):
        self._frames: deque[PackedSample] = deque()
        self._contexts: dict[str, tuple[bytes, int]] = {}
        self._bytes = 0
        self._last_time: float | None = None
        self._dropped = 0
        self._skipped = 0
        self._malformed = 0
        self._oversize = 0
        self._clock_resets = 0

    def due(self, now: float) -> bool:
        """Cheap sampling gate; does not modify counters or trim the buffer."""
        now = _timestamp(now)
        return self._last_time is None or now < self._last_time or now - self._last_time >= SAMPLE_INTERVAL_SECONDS

    def record_failure(self, reason: str = "malformed") -> None:
        """Record a failed off-loop pack without changing retained samples."""
        if reason == "oversize":
            self._oversize = min(MAX_COUNTER, self._oversize + 1)
        elif reason == "skipped":
            self._skipped = min(MAX_COUNTER, self._skipped + 1)
        else:
            self._malformed = min(MAX_COUNTER, self._malformed + 1)

    def clear(self) -> None:
        """Discard all data and counters (e.g. integration unload)."""
        self.__init__()

    def _evict(self) -> None:
        frame = self._frames.popleft()
        context, refs = self._contexts[frame.context_id]
        self._bytes -= len(frame.frame_json)
        if refs == 1:
            self._bytes -= len(context)
            del self._contexts[frame.context_id]
        else:
            self._contexts[frame.context_id] = (context, refs - 1)
        self._dropped = min(MAX_COUNTER, self._dropped + 1)

    def append(self, sample: PackedSample, now: float | None = None) -> bool:
        """Retain a successfully packed sample; reject invalid inputs atomically.

        ``now`` optionally guards a delayed executor result against future times.
        Callers should serialize their pack/append pipeline in one existing task.
        """
        if not isinstance(sample, PackedSample):
            self.record_failure()
            return False
        try:
            time = _timestamp(sample.time)
            current = time if now is None else _timestamp(now)
        except (ValueError, OverflowError):
            self.record_failure()
            return False
        if (time > current or not isinstance(sample.context_json, bytes)
                or not isinstance(sample.frame_json, bytes) or not isinstance(sample.context_id, str)):
            self.record_failure()
            return False
        if len(sample.context_json) > MAX_CONTEXT_BYTES or len(sample.frame_json) > MAX_FRAME_BYTES:
            self.record_failure("oversize")
            return False
        existing = self._contexts.get(sample.context_id)
        if existing and existing[0] != sample.context_json:
            self.record_failure()
            return False
        if time < current - WINDOW_SECONDS:
            self.record_failure("skipped")
            return False
        if self._last_time is not None and time < self._last_time:
            while self._frames:
                self._evict()
            self._last_time = None
            self._clock_resets = min(MAX_COUNTER, self._clock_resets + 1)
        if not self.due(time):
            self.record_failure("skipped")
            return False
        existing = self._contexts.get(sample.context_id)
        # Packed samples with the same hash share the canonical bytes object.
        if existing:
            context, refs = existing
            sample = PackedSample(time, sample.context_id, context, sample.frame_json)
            self._contexts[sample.context_id] = (context, refs + 1)
        else:
            self._contexts[sample.context_id] = (sample.context_json, 1)
            self._bytes += len(sample.context_json)
        self._frames.append(sample)
        self._bytes += len(sample.frame_json)
        self._last_time = time
        while self._frames and (self._frames[0].time < current - WINDOW_SECONDS
                                or len(self._frames) > MAX_FRAMES or self._bytes > MAX_BYTES):
            self._evict()
        return True

    def snapshot(self, now: float) -> HistorySnapshot:
        """Copy bounded immutable references; omit expired and future frames."""
        now = _timestamp(now)
        frames = tuple(f for f in self._frames if now - WINDOW_SECONDS <= f.time <= now)
        ids = {f.context_id for f in frames}
        contexts = tuple((key, value[0]) for key, value in self._contexts.items() if key in ids)
        metadata = {
            "window_seconds": WINDOW_SECONDS, "sample_interval_seconds": SAMPLE_INTERVAL_SECONDS,
            "max_frames": MAX_FRAMES, "max_bytes": MAX_BYTES,
            "snapshot_time": now, "dropped_frames": self._dropped,
            "skipped_samples": self._skipped, "malformed_samples": self._malformed,
            "oversize_samples": self._oversize, "clock_resets": self._clock_resets,
        }
        return HistorySnapshot(frames, contexts, tuple(metadata.items()))


def export_snapshot(snapshot: HistorySnapshot, max_bytes: int = EXPORT_MAX_BYTES) -> dict:
    """Decode off-loop, retaining the newest frames fitting pretty JSON bytes.

    No padding is included. The cap accounts for indent=2 UTF-8 JSON, including
    metadata and only referenced contexts. Export never changes the snapshot.
    """
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("export byte limit must be a positive integer")
    decoded_frames = [json.loads(f.frame_json) for f in snapshot.frames]
    decoded_contexts = {key: json.loads(value) for key, value in snapshot.contexts}
    base_metadata = dict(snapshot.metadata)

    def build(start: int) -> tuple[dict, int]:
        frames = decoded_frames[start:]
        ids = {f["context_id"] for f in frames}
        retained = snapshot.frames[start:]
        contexts = {key: value for key, value in decoded_contexts.items() if key in ids}
        meta = dict(base_metadata)
        meta.update({"sample_count": len(frames), "context_count": len(contexts),
                     "first_time": frames[0]["time"] if frames else None,
                     "last_time": frames[-1]["time"] if frames else None,
                     "encoded_bytes": sum(len(f.frame_json) for f in retained)
                     + sum(len(value) for key, value in snapshot.contexts if key in ids),
                     "export_truncated": start > 0, "export_dropped_frames": start,
                     "export_bytes": 0})
        result = {"metadata": meta, "contexts": contexts, "frames": frames}
        size = len(_encode(result, pretty=True))
        while size != meta["export_bytes"]:
            meta["export_bytes"] = size
            size = len(_encode(result, pretty=True))
        return result, size

    result, size = build(0)
    if size <= max_bytes:
        return result
    # Binary search avoids repeatedly encoding a large report for every frame.
    low, high = 1, len(decoded_frames)
    empty, empty_size = build(high)
    if empty_size > max_bytes:
        raise ValueError("export byte limit cannot fit history metadata")
    result = empty
    while low <= high:
        mid = (low + high) // 2
        candidate, size = build(mid)
        if size <= max_bytes:
            result = candidate
            high = mid - 1
        else:
            low = mid + 1
    return result
