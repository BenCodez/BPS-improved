"""Batch-scoped identity indexing with live readings and registry edit guards."""
import asyncio
import copy
import pytest

import bps
from bps.receiver_sources import ReceiverSources
from test_receiver_aliases import aliases, timestamp
from test_receiver_matching import MAC


def test_real_tracking_batch_indexes_once_and_reads_each_tracker_fresh(hass, monkeypatch):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch, outdoor=True)
    rows = []
    for i in range(24):
        entity = f"tag_{i}"
        eid = f"sensor.{entity}_distance_to_current_station"
        add(eid, str(i + 3), uid=f"synthetic-tag-{i}_{MAC}_range")
        states[eid].last_updated = timestamp(998)
        rows.append({"entity": entity, "data": copy.deepcopy(data)})
    builds = []
    original = bps.ReceiverSources

    def index(hass):
        value = original(hass)
        builds.append(value)
        return value

    async def solve(_hass, _rows, _entity):
        await asyncio.sleep(0)

    async def groups(_hass):
        pass

    monkeypatch.setattr(bps, "ReceiverSources", index)
    monkeypatch.setattr(bps, "update_trilateration_and_zone", solve)
    monkeypatch.setattr(bps, "update_tracker_groups", groups)

    async def scenario():
        await bps.process_entities(hass, rows)
        assert len(builds) == 1
        for i, row in enumerate(rows):
            rec = row["data"]["floor"][0]["receivers"][0]
            assert rec["distance"] == 2 * (i + 3)
            assert rec["_outdoor_reading"]["entity_id"] == f"sensor.tag_{i}_distance_to_current_station"
            assert rec["_outdoor_reading"]["observed"] == 998
            assert rec["custom"] == {"keep": True} and rec["height"] == 2
        states["sensor.tag_0_distance_to_current_station"].state = "9"
        states["sensor.tag_0_distance_to_current_station"].last_updated = timestamp(999)
        await bps.process_entities(hass, rows)
        assert len(builds) == 2 and builds[0] is not builds[1]
        rec = rows[0]["data"]["floor"][0]["receivers"][0]
        assert rec["distance"] == 18 and rec["_outdoor_reading"]["observed"] == 999

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["mac", "tracker", "owner", "disabled", "platform", "removed", "reading_only", "disabled_anchor_tracker", "disabled_anchor_owner", "new_exact"])
def test_shared_index_rechecks_source_proof_after_executor_suspension(hass, monkeypatch, change):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch, outdoor=True)
    if change.startswith("disabled_anchor"):
        entries[old].disabled_by = "user"
    elif change == "new_exact":
        del entries[old]
        data["floor"][0]["receivers"][0].update(scanner_mac=MAC, scanner_config_entry_id="entry")
    source = ReceiverSources(hass)
    rec = data["floor"][0]["receivers"][0]

    async def scenario():
        await bps.update_receiver_radii(hass, {"entity": "tag", "data": data}, source)
        assert rec["distance"] == 8
        # Registry and states can change while another solver is off-loop.
        await asyncio.sleep(0)
        if change == "mac":
            entries[new].unique_id = "synthetic-tracker_aa:bb:cc:44:55:66_range"
        elif change == "tracker":
            entries[new].unique_id = f"replacement-tag_{MAC}_range"
        elif change == "owner":
            entries[new].config_entry_id = "replacement-owner"
        elif change == "disabled":
            entries[new].disabled_by = "user"
        elif change == "platform":
            entries[new].platform = "esphome"
        elif change == "removed":
            del entries[new]
        elif change == "disabled_anchor_tracker":
            entries[old].unique_id = f"replacement-tag_{MAC}_range"
        elif change == "disabled_anchor_owner":
            entries[old].config_entry_id = "replacement-owner"
        elif change == "new_exact":
            add(old, uid=f"replacement-tag_{MAC}_range")
        states[new].state = "9"
        states[new].last_updated = timestamp(999)
        await bps.update_receiver_radii(hass, {"entity": "tag", "data": data}, source)
        if change == "reading_only":
            assert rec["distance"] == 18 and rec["_outdoor_reading"]["observed"] == 999
        else:
            assert "distance" not in rec and "r" not in rec["cords"]
            assert "_outdoor_reading" not in rec

    asyncio.run(scenario())
