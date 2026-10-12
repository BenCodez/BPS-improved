"""Synthetic operation budgets for the scanner linking diagnostic snapshot."""
import copy
import json

import pytest

import bps
from bps.receiver_sources import ReceiverSources
from test_receiver_matching import configure


@pytest.mark.parametrize("identity_proven", [False, True])
def test_linking_indexes_reading_ownership_once(hass, monkeypatch, identity_proven):
    _entries, _states, _devices, add = configure(hass, monkeypatch)
    count, trackers = 160, 4
    receivers = []
    expected = {}
    for index in range(count):
        slug = f"station_{index}"
        mac = f"aa:bb:cc:00:{index // 256:02x}:{index % 256:02x}"
        receivers.append({"entity_id": slug})
        expected[slug] = {}
        for tracker in range(trackers):
            name = f"tag_{tracker}"
            eid = f"sensor.{name}_distance_to_{slug}"
            uid = f"synthetic-{name}_{mac}_range" if identity_proven else f"legacy-{index}-{tracker}"
            add(eid, "3", uid=uid)
            expected[slug][name] = eid
    # Leave one actual scanner unplaced so the diagnostic context is covered.
    add("sensor.tag_unplaced_distance_to_unplaced_station", "unavailable", uid="legacy-unplaced")
    layout = {"floor": [{"name": "Synthetic floor", "receivers": receivers}]}
    before = copy.deepcopy(layout)
    visited = 0
    indexes = []

    class CountedReadings(dict):
        def items(self):
            nonlocal visited
            for item in super().items():
                visited += 1
                yield item

    class CountedSources(ReceiverSources):
        def __init__(self, current_hass):
            super().__init__(current_hass)
            self.readings = CountedReadings(self.readings)
            indexes.append(self.readings)

    monkeypatch.setattr(bps, "ReceiverSources", CountedSources)
    result = bps._scanner_linking(hass, json.dumps(layout))

    assert len(result["placed"]) == count
    for row in result["placed"]:
        assert row["status"] == "live"
        assert row["binding_status"] == "matched"
        assert row["sensor_count"] == row["reporting_count"] == trackers
        assert row["selected_sources"] == expected[row["entity_id"]]
        assert [sensor["entity_id"] for sensor in row["sensors"]] == sorted(expected[row["entity_id"]].values())
    assert result["unplaced"] == [{"entity_id": "unplaced_station", "token": None,
                                    "sensor_count": 1, "reporting_count": 0}]
    assert layout == before and not hass._store_saves
    assert len(indexes) == 1
    # Count work, not wall time: adding placements must not repeat the global
    # scan of tracker/scanner groups on the synchronous HA event-loop path.
    assert visited == len(indexes[0])
