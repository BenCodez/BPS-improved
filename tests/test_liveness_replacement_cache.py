"""Cached scanner ages cannot follow a reused name onto replacement hardware."""
import asyncio
from types import SimpleNamespace as NS

import pytest

import bps
from test_receiver_aliases import aliases, timestamp
from test_receiver_matching import MAC


@pytest.mark.parametrize("old_age", [0, 99])
@pytest.mark.parametrize("evidence", ["on", "off", "fresh", "none"])
def test_failed_dump_after_explicit_same_name_replacement_uses_current_evidence(hass, monkeypatch, old_age, evidence):
    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    rec = data["floor"][0]["receivers"][0]
    rec.update(entity_id="current_station", scanner_mac=MAC, scanner_config_entry_id="entry")
    hass.data["bps"]["layout"] = data
    calls = 0

    async def dump(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("Synthetic temporary dump failure")
        return {
            MAC: {"_is_scanner": True, "name": "Current station", "address": MAC, "last_seen": 100 - old_age},
            "clock": {"_is_scanner": False, "last_seen": 100},
        }

    hass.services.async_call = dump

    async def scenario():
        await bps.update_receiver_liveness(hass)
        dom = hass.data["bps"]
        assert dom["rl_ages"]["current_station"] == old_age
        assert dom["rl_mac_ages"][MAC] == old_age
        assert ("current_station" in dom["rl_offline"]) is (old_age == 99)
        replacement = "dd:ee:ff:10:20:00"
        devices["station"].address = replacement
        for eid in (old, new):
            entries[eid].unique_id = f"synthetic-tracker_{replacement}_range"
            states[eid].state = "unavailable"
        rec["scanner_mac"] = replacement
        if evidence in ("on", "off"):
            states["binary_sensor.current_station_status"] = NS(state=evidence, attributes={"device_class": "connectivity"})
        elif evidence == "fresh":
            states[new].state = "3"
            states[new].last_updated = timestamp(999)
        await bps.update_receiver_liveness(hass)
        assert calls == 2 and replacement not in dom["rl_mac_ages"]
        assert dom["rl_ages"]["current_station"] == old_age
        assert ("current_station" in dom["rl_offline"]) is (evidence == "off")
        assert ("current_station" in dom["rl_unknown"]) is (evidence == "none")

    asyncio.run(scenario())
