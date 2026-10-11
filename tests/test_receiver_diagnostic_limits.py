"""Bound receiver diagnostics work and reject oversized editor saves atomically."""
import asyncio
import json
from types import SimpleNamespace as NS

import pytest

import bps
from bps.outdoor_config import MAX_RECEIVERS_PER_FLOOR, MAX_RECEIVER_PLACEMENTS
from bps.storage import save_bps_data
from test_receiver_matching import configure


def test_large_legacy_layout_diagnostics_do_not_rescan_every_placement(hass, monkeypatch):
    _entries, _states, _devices, add = configure(hass, monkeypatch)
    add("sensor.tag_distance_to_workshop_112233")
    count, comparisons = 1000, 0
    class FloorName(str):
        __hash__ = str.__hash__
        def __eq__(self, other):
            nonlocal comparisons
            comparisons += 1
            return str.__eq__(self, other)
    placed = bps._placed_receivers
    monkeypatch.setattr(bps, "_placed_receivers", lambda content: [
        (FloorName(name), slug, uid, mac) for name, slug, uid, mac in placed(content)])
    content = json.dumps({"floor": [{"name": "Ground", "receivers": [
        {"entity_id": f"unmatched_{index}"} for index in range(count)]}]})
    result = bps._scanner_diagnostics(hass, content)
    assert len(result["unmatched_receivers"]) == count
    # A comparison budget detects quadratic rescans without timing CI workers.
    assert comparisons < count * 10


@pytest.mark.parametrize("same_floor", [False, True])
def test_suggestion_excludes_candidates_only_on_the_same_floor(hass, monkeypatch, same_floor):
    _entries, _states, _devices, add = configure(hass, monkeypatch)
    add("sensor.tag_distance_to_workshop_112233")
    floors = [{"name": "Ground", "receivers": [{"entity_id": "old_112233"}]}]
    if same_floor:
        floors[0]["receivers"].append({"entity_id": "workshop_112233"})
    else:
        floors.append({"name": "Upstairs", "receivers": [{"entity_id": "workshop_112233"}]})
    result = bps._scanner_diagnostics(hass, json.dumps({"floor": floors}))
    assert result["unmatched_receivers"] == [{"entity_id": "old_112233", "floor": "Ground",
        "suggested": None if same_floor else "workshop_112233"}]


def test_malformed_floor_name_and_token_do_not_break_diagnostics(hass, monkeypatch):
    configure(hass, monkeypatch)[3]("sensor.tag_distance_to_workshop_112233")
    content = json.dumps({"floor": [{"name": ["invalid"], "receivers": [
        {"entity_id": "old_station", "scanner_uid": {"invalid": True}}]}]})
    assert bps._scanner_diagnostics(hass, content)["unmatched_receivers"][0]["suggested"] is None


@pytest.mark.parametrize("counts,accepted", [
    ([MAX_RECEIVERS_PER_FLOOR] * (MAX_RECEIVER_PLACEMENTS // MAX_RECEIVERS_PER_FLOOR), True),
    ([MAX_RECEIVERS_PER_FLOOR + 1], False),
    ([MAX_RECEIVERS_PER_FLOOR] * (MAX_RECEIVER_PLACEMENTS // MAX_RECEIVERS_PER_FLOOR) + [1], False),
])
def test_save_receiver_limits_before_layout_or_map_mutation(hass, tmp_path, counts, accepted):
    async def scenario():
        original = {"floor": [{"name": "Existing", "receivers": []}]}
        await save_bps_data(hass, original)
        map_path = tmp_path / "existing.png"
        map_path.write_bytes(b"synthetic-map")
        layout = {"floor": [{"name": f"Floor {index}", "receivers": [
            {"entity_id": f"station_{index}_{receiver}"} for receiver in range(count)]}
            for index, count in enumerate(counts)]}
        async def post():
            return {"coordinates": json.dumps(layout), "remove": "existing.png"}
        hass.config.path = lambda _path: str(tmp_path)
        response = await bps.BPSSaveAPIText().post(NS(app={"hass": hass}, post=post))
        assert response.status == (200 if accepted else 400)
        assert hass.data["bps"]["layout"] == (layout if accepted else original)
        assert hass._store_backing["bps"] == (layout if accepted else original)
        assert map_path.exists() is not accepted
    asyncio.run(scenario())
