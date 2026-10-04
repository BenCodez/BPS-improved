"""Group entity lifecycle against HA-shaped platform/registry interfaces."""
import asyncio
import importlib
import sys
import types

import pytest

from conftest import make_hass


class Registry:
    def __init__(self):
        self.entities = {}
        self.removed = []

    def async_remove(self, eid):
        self.removed.append(eid)
        del self.entities[eid]

    def async_get(self, eid):
        return self.entities.get(eid)

    def async_update_entity(self, eid, new_entity_id):
        entry = self.entities.pop(eid)
        entry.entity_id = new_entity_id
        self.entities[new_entity_id] = entry

    def add(self, eid, uid):
        self.entities[eid] = types.SimpleNamespace(entity_id=eid, unique_id=uid, platform="bps", device_id=None)


@pytest.fixture
def platform(monkeypatch):
    class Entity:
        @property
        def unique_id(self):
            return getattr(self, "_attr_unique_id", None)

        async def async_remove(self):
            self.removed = True

        async def async_added_to_hass(self):
            self.ready = True

        def async_write_ha_state(self):
            if not getattr(self, "ready", False):
                raise RuntimeError("Entity has not been registered")
            self.writes = getattr(self, "writes", 0) + 1

    sensor_mod = types.ModuleType("homeassistant.components.sensor")
    sensor_mod.SensorEntity = Entity
    sensor_mod.SensorStateClass = types.SimpleNamespace(MEASUREMENT="measurement")
    entity_mod = types.ModuleType("homeassistant.helpers.entity")
    entity_mod.DeviceInfo = dict
    monkeypatch.setitem(sys.modules, "homeassistant.components.sensor", sensor_mod)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity", entity_mod)
    registry = Registry()
    er = sys.modules["homeassistant.helpers.entity_registry"]
    dr = sys.modules["homeassistant.helpers.device_registry"]
    monkeypatch.setattr(er, "async_get", lambda _hass: registry, raising=False)
    monkeypatch.setattr(dr, "async_get", lambda _hass: types.SimpleNamespace(async_get=lambda _id: None), raising=False)
    module = importlib.import_module("bps.sensor")
    # Other suites must retain their minimal stubs after this fixture finishes.
    monkeypatch.delitem(sys.modules, "bps.sensor", raising=False)
    hass = make_hass()
    hass.states = types.SimpleNamespace(async_all=lambda: [], async_remove=lambda _eid: None)
    hass.bus = types.SimpleNamespace(async_listen=lambda *a: lambda: None)
    added = []
    def add_entities(entities, **kwargs):
        for entity in entities:
            added.append(entity)
            registry.add(entity.entity_id, entity.unique_id)
            if not getattr(hass, "defer_group_registration", False) or not str(entity.unique_id).startswith("bps_group_"):
                entity.hass = hass
                entity.ready = True
                if hasattr(entity, "_bps_group_pending"):
                    entity._bps_group_pending = False
    return hass, registry, module, add_entities, added


def test_group_registry_survives_preload_and_is_reclaimed(platform):
    hass, registry, module, add, added = platform
    eid = "sensor.bps_group_rover_bps_zone"
    registry.add(eid, "bps_group_zone_rover")
    asyncio.run(module.async_setup_entry(hass, None, add))
    assert eid in registry.entities  # Store has not loaded yet.
    sync = hass.data["bps"]["sync_group_sensors"]
    asyncio.run(sync([{"id": "rover", "name": "Rover"}]))
    assert eid in hass.data["bps_sensors"]
    assert hass.data["bps_sensors"][eid].unique_id == "bps_group_zone_rover"
    assert "sensor.bps_group_rover_bps_floor" in hass.data["bps_sensors"]


def test_group_disable_removes_owned_entities_preserves_beacons(platform):
    hass, registry, module, add, added = platform
    registry.add("sensor.beacon_a_bps_zone", "bps_zone_beacon_a")
    asyncio.run(module.async_setup_entry(hass, None, add))
    sync = hass.data["bps"]["sync_group_sensors"]
    asyncio.run(sync([{"id": "rover", "name": "Rover"}]))
    owned = [s for s in added if str(s.unique_id).startswith("bps_group_")]
    asyncio.run(sync([]))
    assert len(owned) == 2 and all(s.removed for s in owned)
    assert "sensor.beacon_a_bps_zone" in registry.entities
    assert not any("bps_group_rover" in eid for eid in hass.data["bps_sensors"])


def test_renamed_registry_entity_removed_by_unique_id(platform):
    hass, registry, module, add, added = platform
    asyncio.run(module.async_setup_entry(hass, None, add))
    sync = hass.data["bps"]["sync_group_sensors"]
    asyncio.run(sync([{"id": "rover", "name": "Rover"}]))
    sensor = hass.data["bps_sensors"]["sensor.bps_group_rover_bps_zone"]
    registry.async_update_entity(sensor.entity_id, "sensor.my_rover")
    sensor.entity_id = "sensor.my_rover"
    asyncio.run(sync([]))
    assert sensor.removed
    assert "sensor.my_rover" not in registry.entities
    assert "sensor.bps_group_rover_bps_zone" not in hass.data["bps_sensors"]


def test_group_rename_and_repeated_sync_preserve_unique_id(platform):
    hass, registry, module, add, added = platform
    asyncio.run(module.async_setup_entry(hass, None, add))
    sync = hass.data["bps"]["sync_group_sensors"]
    asyncio.run(sync([{"id": "rover", "name": "Rover"}]))
    count = len(added)
    asyncio.run(sync([{"id": "rover", "name": "Rover"}]))
    asyncio.run(sync([{"id": "rover", "name": "Rover the dog"}]))
    assert len(added) == count
    sensor = hass.data["bps_sensors"]["sensor.bps_group_rover_bps_zone"]
    assert sensor.name == "Rover the dog BPS Zone"
    assert sensor.unique_id == "bps_group_zone_rover"


def test_group_state_buffers_while_entity_addition_is_scheduled(platform):
    import bps
    hass, registry, module, add, added = platform
    asyncio.run(module.async_setup_entry(hass, None, add))
    hass.defer_group_registration = True
    sync = hass.data["bps"]["sync_group_sensors"]
    asyncio.run(sync([{"id": "rover", "name": "Rover"}]))
    eid = "sensor.bps_group_rover_bps_zone"
    sensor = hass.data["bps_sensors"][eid]
    bps.update_bps_sensor_state(hass, eid, "Yard", {"cords": [50, 50]})
    assert sensor.state == "Yard" and sensor.extra_state_attributes["cords"] == [50, 50]
    assert getattr(sensor, "writes", 0) == 0
    asyncio.run(sensor.async_added_to_hass())
    bps.update_bps_sensor_state(hass, eid, "Pasture")
    assert sensor.state == "Pasture" and sensor.writes == 1


def test_disabled_ha_group_entity_can_be_deleted_without_live_hass(platform):
    hass, registry, module, add, added = platform
    asyncio.run(module.async_setup_entry(hass, None, add))
    sync = hass.data["bps"]["sync_group_sensors"]
    asyncio.run(sync([{"id": "rover", "name": "Rover"}]))
    eid = "sensor.bps_group_rover_bps_zone"
    sensor = hass.data["bps_sensors"][eid]
    sensor.hass = None  # EntityPlatform.add_to_platform_abort for a disabled entry.
    sensor._bps_group_pending = True
    async def invalid_remove():
        raise RuntimeError("no live HA entity")
    sensor.async_remove = invalid_remove
    asyncio.run(sync([]))
    assert eid not in registry.entities
    assert eid not in hass.data["bps_sensors"]
