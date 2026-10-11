"""Synthetic telemetry tests for volatile retrospective diagnostics."""
import asyncio
from dataclasses import FrozenInstanceError, replace
import json
import math

import pytest

from bps import recent_tracking as R


def sample(time, context=None, **extra):
    return R.pack({"time": time, "context": context or {"version": 1},
                   "positions": {"synthetic_device": {"x": 1, "y": 2}}, **extra})


def exported(history, now):
    return R.export_snapshot(history.snapshot(now))


def pretty_size(value):
    return len(json.dumps(value, allow_nan=False, ensure_ascii=False,
                          sort_keys=True, indent=2).encode("utf-8"))


def assert_accounting(history):
    assert history._bytes == (sum(len(f.frame_json) for f in history._frames)
                              + sum(len(c[0]) for c in history._contexts.values()))
    assert sum(c[1] for c in history._contexts.values()) == len(history._frames)
    assert set(history._contexts) == {f.context_id for f in history._frames}
    assert history._bytes <= R.MAX_BYTES
    assert len(history._frames) <= R.MAX_FRAMES


def test_pack_detaches_and_deduplicates_context_key_order():
    source = {"time": 100, "context": {"a": 1, "b": 2},
              "positions": {"synthetic_device": {"observation_time": 90}}}
    packed = R.pack(source)
    source["context"]["a"] = 7
    source["positions"]["synthetic_device"]["observation_time"] = 100
    same = sample(102, {"b": 2, "a": 1})
    assert packed.context_id == same.context_id
    assert json.loads(packed.frame_json)["positions"]["synthetic_device"]["observation_time"] == 90
    with pytest.raises(FrozenInstanceError):
        packed.time = 101


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf, object(), {1, 2}])
def test_invalid_nested_json_preserves_previous_buffer(bad):
    history = R.RecentTrackingHistory()
    history.append(sample(100))
    before = history.snapshot(100)
    with pytest.raises(R.PackError) as err:
        sample(102, telemetry={"reading": bad})
    assert err.value.reason == "malformed"
    history.record_failure(err.value.reason)
    after = history.snapshot(100)
    assert before.frames == after.frames and before.contexts == after.contexts
    assert dict(after.metadata)["malformed_samples"] == 1


@pytest.mark.parametrize("time", [True, None, "100", math.nan, math.inf, 10 ** 1000])
def test_rejects_invalid_sampling_time(time):
    with pytest.raises(R.PackError):
        sample(time)


def test_sampling_gate_and_cached_observation_timestamps():
    history = R.RecentTrackingHistory()
    assert history.due(100)
    for time in (100, 102, 104):
        assert history.append(sample(time, readings={"observation_time": 90, "cached": True}))
    assert not history.due(105.999)
    assert history.due(106)
    assert not history.append(sample(105))
    result = exported(history, 106)
    assert [f["time"] for f in result["frames"]] == [100, 102, 104]
    assert [f["readings"]["observation_time"] for f in result["frames"]] == [90] * 3
    assert result["metadata"]["skipped_samples"] == 1


def test_count_limit_age_boundary_and_context_reference_eviction():
    history = R.RecentTrackingHistory()
    for index in range(301):
        history.append(sample(index * 2, {"version": index // 100}))
        assert_accounting(history)
    result = exported(history, 600)
    assert result["metadata"]["sample_count"] == 300
    assert result["metadata"]["first_time"] == 2
    assert result["metadata"]["dropped_frames"] == 1
    history.append(sample(1200, {"version": 4}))
    result = exported(history, 1200)
    # Exactly 600 seconds old is retained; older contexts are purged.
    assert [f["time"] for f in result["frames"]] == [600, 1200]
    assert {c["version"] for c in result["contexts"].values()} == {3, 4}
    history.append(sample(1202, {"version": 4}))
    assert {c["version"] for c in exported(history, 1202)["contexts"].values()} == {4}
    assert_accounting(history)


def test_byte_limit_counts_versioned_contexts_and_purges_orphans():
    history = R.RecentTrackingHistory()
    for index in range(35):
        history.append(sample(index * 2, {"version": index, "map": "x" * 200_000},
                              telemetry="y" * 50_000))
        assert_accounting(history)
    result = exported(history, 68)
    assert len(history._frames) == 8
    assert len(history._contexts) == 8
    assert dict(history.snapshot(68).metadata)["dropped_frames"] == 27
    # The 1 MiB report cap may keep fewer frames than the in-memory limit.
    assert pretty_size(result) <= R.EXPORT_MAX_BYTES


def test_context_dedup_shares_bytes_and_counts_context_only_once():
    history = R.RecentTrackingHistory()
    a, b = sample(100), sample(102)
    history.append(a)
    history.append(b)
    assert history._frames[0].context_json is history._frames[1].context_json
    assert history._bytes == len(a.context_json) + len(a.frame_json) + len(b.frame_json)
    assert_accounting(history)


def test_frame_and_context_exact_size_boundaries():
    base = sample(100, padding="")
    exact = sample(100, padding="x" * (R.MAX_FRAME_BYTES - len(base.frame_json)))
    assert len(exact.frame_json) == R.MAX_FRAME_BYTES
    with pytest.raises(R.PackError) as err:
        sample(100, padding="x" * (R.MAX_FRAME_BYTES - len(base.frame_json) + 1))
    assert err.value.reason == "oversize"
    empty_context = sample(100, {"padding": ""})
    exact_context = sample(100, {"padding": "x" * (R.MAX_CONTEXT_BYTES - len(empty_context.context_json))})
    assert len(exact_context.context_json) == R.MAX_CONTEXT_BYTES
    with pytest.raises(R.PackError) as err:
        sample(100, {"padding": "x" * (R.MAX_CONTEXT_BYTES - len(empty_context.context_json) + 1)})
    assert err.value.reason == "oversize"


def test_clock_rollback_and_future_stale_snapshot_guards():
    history = R.RecentTrackingHistory()
    history.append(sample(100))
    assert history.snapshot(99).frames == ()
    assert history.snapshot(701).frames == ()
    assert history.due(98)
    assert not history.append(sample(102), now=101)
    assert exported(history, 100)["frames"][0]["time"] == 100
    assert history.append(sample(98))
    result = exported(history, 98)
    assert [f["time"] for f in result["frames"]] == [98]
    assert result["metadata"]["clock_resets"] == 1
    assert_accounting(history)


def test_snapshot_is_immutable_and_export_survives_append_clear():
    history = R.RecentTrackingHistory()
    history.append(sample(100))
    snapshot = history.snapshot(100)
    with pytest.raises(FrozenInstanceError):
        snapshot.frames = ()
    history.append(sample(102, {"version": 2}))
    history.clear()
    assert R.export_snapshot(snapshot)["metadata"]["sample_count"] == 1
    assert exported(history, 102)["metadata"]["sample_count"] == 0
    assert_accounting(history)


def test_invalid_or_expired_append_does_not_reset_existing_history():
    history = R.RecentTrackingHistory()
    original = sample(100)
    history.append(original)
    before = history.snapshot(100)
    assert not history.append(None)
    # Even an invalid packet with a rollback time cannot clear the buffer.
    assert not history.append(replace(original, time=98, context_json=b"{}"))
    assert not history.append(sample(98), now=800)
    assert history.snapshot(100).frames == before.frames
    assert dict(history.snapshot(100).metadata)["clock_resets"] == 0


def test_counters_saturate_without_wrapping():
    history = R.RecentTrackingHistory()
    history._skipped = R.MAX_COUNTER
    history._malformed = R.MAX_COUNTER
    history._oversize = R.MAX_COUNTER
    history.record_failure("skipped")
    history.record_failure("malformed")
    history.record_failure("oversize")
    assert history._skipped == history._malformed == history._oversize == R.MAX_COUNTER


def test_pretty_json_export_cap_keeps_newest_frames_and_actual_range():
    history = R.RecentTrackingHistory()
    # Many tiny keys expand substantially when pretty-printed.
    for index in range(30):
        history.append(sample(index * 2, {"version": index},
                              telemetry=[{"a": 1} for _ in range(1000)]))
    snapshot = history.snapshot(58)
    result = R.export_snapshot(snapshot, max_bytes=100_000)
    meta = result["metadata"]
    assert meta["export_truncated"] is True
    assert meta["export_bytes"] == pretty_size(result) <= 100_000
    assert meta["last_time"] == 58
    assert meta["first_time"] == result["frames"][0]["time"]
    assert meta["export_dropped_frames"] + len(result["frames"]) == 30
    assert set(result["contexts"]) == {f["context_id"] for f in result["frames"]}
    assert len(snapshot.frames) == 30
    assert R.export_snapshot(snapshot)["metadata"]["sample_count"] > len(result["frames"])


def test_export_empty_and_impossibly_small_limit():
    snapshot = R.RecentTrackingHistory().snapshot(100)
    result = R.export_snapshot(snapshot)
    assert result["metadata"]["first_time"] is None
    assert result["metadata"]["last_time"] is None
    assert result["contexts"] == {} and result["frames"] == []
    with pytest.raises(ValueError):
        R.export_snapshot(snapshot, max_bytes=1)


def test_loop_snapshot_does_no_json_work_and_export_can_run_off_loop(monkeypatch):
    history = R.RecentTrackingHistory()
    for index in range(300):
        history.append(sample(index * 2))
    original_loads, original_dumps = R.json.loads, R.json.dumps

    def forbidden(*args, **kwargs):
        raise AssertionError("JSON work on event-loop snapshot path")

    monkeypatch.setattr(R.json, "loads", forbidden)
    monkeypatch.setattr(R.json, "dumps", forbidden)
    snapshot = history.snapshot(600)
    assert len(snapshot.frames) == 300
    monkeypatch.setattr(R.json, "loads", original_loads)
    monkeypatch.setattr(R.json, "dumps", original_dumps)

    async def scenario():
        task = asyncio.create_task(asyncio.to_thread(R.export_snapshot, snapshot))
        history.clear()
        result = await task
        assert result["metadata"]["sample_count"] == 300
        assert history.snapshot(600).frames == ()

    asyncio.run(scenario())
