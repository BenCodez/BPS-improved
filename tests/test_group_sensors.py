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


class Devices:
    def __init__(self):
        self.entries = {}
        self.updates = []
        self.removed = []

    def async_get(self, device_id):
        return self.entries.get(device_id)

    def async_get_device(self, *, identifiers):
        return next((device for device in self.entries.values() if device.identifiers & identifiers), None)

    def register(self, info):
        if self.async_get_device(identifiers=info["identifiers"]) is None:
            device_id = str(len(self.entries))
            self.entries[device_id] = types.SimpleNamespace(
                id=device_id, identifiers=info["identifiers"], name=info["name"], name_by_user=None)
        return self.async_get_device(identifiers=info["identifiers"])

    def async_update_device(self, device_id, *, name):
        self.updates.append((device_id, name))
        self.entries[device_id].name = name

    def async_remove_device(self, device_id):
        self.removed.append(device_id)
        del self.entries[device_id]


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
            self.published_names = [*getattr(self, "published_names", []), getattr(self, "_attr_name", None)]

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
    devices = Devices()
    monkeypatch.setattr(dr, "async_get", lambda _hass: devices, raising=False)
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
            info = getattr(entity, "_attr_device_info", None)
            if info:
                registry.entities[entity.entity_id].device_id = devices.register(info).id
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
    assert len(owned) == 3 and all(s.removed for s in owned)
    assert "sensor.beacon_a_bps_zone" in registry.entities
    assert not any("bps_group_rover" in eid for eid in hass.data["bps_sensors"])


def test_discovered_colliding_beacon_reclaims_group_zone_and_floor_sensors(platform, monkeypatch):
    import bps
    hass, registry, module, add, added = platform
    listeners = {}
    def listen(name, callback):
        listeners[name] = callback
        return lambda: None
    hass.bus.async_listen = listen
    asyncio.run(module.async_setup_entry(hass, None, add))
    sync = hass.data["bps"]["sync_group_sensors"]
    asyncio.run(sync([{"id": "rover", "name": "Rover"}]))
    old_group_sensors = [sensor for sensor in added if isinstance(sensor, module.BPSGroupSensor)]
    devices = module.dr.async_get(hass)
    group_device = devices.async_get_device(identifiers={("bps", "bps_group_rover")})
    distance_id = "sensor.bps_group_rover_distance_to_proxy"
    registry.entities[distance_id] = types.SimpleNamespace(
        entity_id=distance_id, unique_id="bermuda_distance", platform="bermuda", device_id=None)
    hass.states.async_all = lambda: [types.SimpleNamespace(entity_id=distance_id)]
    listeners["state_changed"](types.SimpleNamespace(data={"entity_id": distance_id, "old_state": None}))
    # Discovery cannot create ordinary zone/floor sensors until the group owner retires.
    assert hass.data["bps_sensors"]["sensor.bps_group_rover_bps_zone"].unique_id == "bps_group_zone_rover"
    hass.data["bps"]["apitricords"] = [{"ent": "bps_group_rover", "zone": "Yard", "floor": "Property"}]
    asyncio.run(sync([]))
    assert all(sensor.removed for sensor in old_group_sensors)
    for suffix, _label in module.SENSOR_KINDS:
        eid = f"sensor.bps_group_rover_{suffix}"
        sensor = hass.data["bps_sensors"][eid]
        assert not isinstance(sensor, module.BPSGroupSensor)
        assert sensor.unique_id == f"{suffix}_bps_group_rover"
        assert registry.entities[eid].unique_id == sensor.unique_id
    zone = hass.data["bps_sensors"]["sensor.bps_group_rover_bps_zone"]
    assert zone.state == "Yard" and getattr(zone, "writes", 0) == 0
    assert hass.data["bps_sensors"]["sensor.bps_group_rover_bps_floor"].state == "Property"
    assert devices.async_get(group_device.id) is group_device
    assert devices.removed == []
    bps.update_bps_sensor_state(hass, zone.entity_id, "Yard")
    assert zone.state == "Yard" and zone.writes == 1
    # Ordinary value changes and unchanged group sync avoid another registry scan.
    monkeypatch.setattr(module, "get_filtered_entities", lambda _hass: pytest.fail("unexpected rescan"))
    listeners["state_changed"](types.SimpleNamespace(data={"entity_id": distance_id, "old_state": object()}))
    asyncio.run(sync([]))
    assert hass.data["bps_sensors"][zone.entity_id] is zone


def test_retained_discovery_listener_pauses_during_unload_and_resumes(platform, monkeypatch):
    hass, registry, module, add, added = platform
    listeners = {}
    hass.bus.async_listen = lambda name, callback: listeners.update({name: callback}) or (lambda: None)
    asyncio.run(module.async_setup_entry(hass, None, add))
    distance_id = "sensor.dog_distance_to_proxy"
    registry.entities[distance_id] = types.SimpleNamespace(
        entity_id=distance_id, unique_id="bermuda_distance", platform="bermuda", device_id=None)
    hass.states.async_all = lambda: [types.SimpleNamespace(entity_id=distance_id)]
    event = types.SimpleNamespace(data={"entity_id": distance_id, "old_state": None})
    hass.data["bps"]["_tracking_active"] = False
    before = len(added)
    with monkeypatch.context() as paused:
        paused.setattr(module, "get_filtered_entities", lambda _hass: pytest.fail("discovery during unload"))
        listeners["state_changed"](event)
    assert len(added) == before
    hass.data["bps"]["_tracking_active"] = True
    # The creation event will never recur; ordinary updates have old_state.
    # Lifecycle recovery must explicitly reconcile the retained platform.
    hass.data["bps"]["resume_sensor_discovery"]()
    assert "sensor.dog_bps_zone" in hass.data["bps_sensors"]


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


@pytest.mark.parametrize("registered_device", [True, False])
def test_group_rename_and_repeated_sync_preserve_unique_id(platform, registered_device):
    hass, registry, module, add, added = platform
    asyncio.run(module.async_setup_entry(hass, None, add))
    sync = hass.data["bps"]["sync_group_sensors"]
    asyncio.run(sync([{"id": "rover", "name": "Rover"}]))
    count = len(added)
    devices = module.dr.async_get(hass)
    device = devices.async_get_device(identifiers={("bps", "bps_group_rover")})
    assert device.name == "Rover (BPS group)"
    device.name_by_user = "My custom collar"
    if not registered_device:
        devices.entries.clear()  # Registration can still be pending during rename.
    asyncio.run(sync([{"id": "rover", "name": "Rover"}]))
    assert devices.updates == []
    asyncio.run(sync([{"id": "rover", "name": "Rover the dog"}]))
    assert len(added) == count
    sensor = hass.data["bps_sensors"]["sensor.bps_group_rover_bps_zone"]
    assert sensor.name == "Rover the dog BPS Zone"
    assert sensor.unique_id == "bps_group_zone_rover"
    for kind in ("zone", "floor"):
        entity = hass.data["bps_sensors"][f"sensor.bps_group_rover_bps_{kind}"]
        assert entity._attr_device_info["name"] == "Rover the dog (BPS group)"
    if registered_device:
        assert devices.async_get_device(identifiers={("bps", "bps_group_rover")}) is device
        assert device.name == "Rover the dog (BPS group)"
        assert device.name_by_user == "My custom collar"
        assert devices.updates == [(device.id, "Rover the dog (BPS group)")]
    else:
        assert devices.updates == []
    asyncio.run(sync([{"id": "rover", "name": "Rover the dog"}]))
    assert len(devices.updates) == int(registered_device)


@pytest.mark.parametrize("pending", [False, True])
def test_group_rename_publishes_labels_without_fix_and_respects_pending_registration(platform, monkeypatch, pending):
    import bps
    hass, registry, module, add, added = platform
    hass.defer_group_registration = pending
    monkeypatch.setattr(bps, "apitricords", [])
    monkeypatch.setattr(bps, "tracked_entities", [])
    hass.data.setdefault("bps", {})["layout"] = {"floor": [], "outdoor_tracking": {"enabled": True},
        "tracker_groups": [{"id": "rover", "name": "Rover", "beacons": ["tag"]}]}
    asyncio.run(module.async_setup_entry(hass, None, add))
    asyncio.run(bps.update_tracker_groups(hass))
    entities = [hass.data["bps_sensors"][f"sensor.bps_group_rover_bps_{kind}"] for kind in ("zone", "floor")]
    assert all(entity._state == "unknown" and entity._attrs == {} for entity in entities)
    hass.data["bps"]["layout"]["tracker_groups"][0]["name"] = "Rover renamed"
    asyncio.run(bps.update_tracker_groups(hass))
    for kind, entity in zip(("zone", "floor"), entities):
        label = f"Rover renamed BPS {kind.title()}"
        assert entity.name == label
        assert getattr(entity, "published_names", []) == ([] if pending else [label])
        if pending:
            # HA's eventual initial publication adopts the updated label.
            entity.hass = hass
            asyncio.run(entity.async_added_to_hass())
            entity.async_write_ha_state()
            assert entity.published_names == [label]
    asyncio.run(bps.update_tracker_groups(hass))
    assert all(entity.writes == 1 for entity in entities)


@pytest.mark.parametrize("retired_id", ["rover", "bps_group_floor_rover", "bps_group_zone_rover"])
def test_retired_group_devices_are_removed_without_touching_active_devices(platform, retired_id):
    hass, registry, module, add, added = platform
    asyncio.run(module.async_setup_entry(hass, None, add))
    sync = hass.data["bps"]["sync_group_sensors"]
    rover, spot = {"id": retired_id, "name": "Rover"}, {"id": "spot", "name": "Spot"}
    asyncio.run(sync([rover, spot]))
    devices = module.dr.async_get(hass)
    rover_device = devices.async_get_device(identifiers={("bps", "bps_group_" + retired_id)})
    spot_device = devices.async_get_device(identifiers={("bps", "bps_group_spot")})
    asyncio.run(sync([spot]))
    assert devices.async_get(rover_device.id) is None
    assert devices.async_get(spot_device.id) is spot_device
    assert devices.removed == [rover_device.id]
    asyncio.run(sync([]))
    assert devices.async_get(spot_device.id) is None
    assert devices.removed == [rover_device.id, spot_device.id]
    asyncio.run(sync([]))
    assert len(devices.removed) == 2


def test_retired_group_device_with_another_entity_is_preserved(platform):
    hass, registry, module, add, added = platform
    asyncio.run(module.async_setup_entry(hass, None, add))
    sync = hass.data["bps"]["sync_group_sensors"]
    asyncio.run(sync([{"id": "rover", "name": "Rover"}]))
    devices = module.dr.async_get(hass)
    device = devices.async_get_device(identifiers={("bps", "bps_group_rover")})
    registry.entities["sensor.shared"] = types.SimpleNamespace(entity_id="sensor.shared", unique_id="shared",
        platform="other", device_id=device.id)
    asyncio.run(sync([]))
    assert devices.async_get(device.id) is device
    assert devices.removed == []


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


def test_missing_prefixed_beacon_keeps_group_registry_identity(platform, monkeypatch):
    import bps
    hass, registry, module, add, added = platform
    monkeypatch.setattr(bps, "apitricords", [])
    monkeypatch.setattr(bps, "tracked_entities", [])
    hass.data.setdefault("bps", {})["layout"] = {"floor": [], "outdoor_tracking": {"enabled": True},
        "tracker_groups": [{"id": "rover", "name": "Rover", "beacons": ["bps_group_beacon"]}]}
    asyncio.run(module.async_setup_entry(hass, None, add))
    entities = {key: value for key, value in registry.entities.items() if key.startswith("sensor.bps_group_")}
    initial_additions = len(added)
    assert len(entities) == 3
    entities["sensor.bps_group_rover_bps_zone"].name = "My dog's zone"
    for sensors in (["sensor.bps_group_beacon_distance_to_p0"], [],
                    ["sensor.bps_group_beacon_distance_to_p0"]):
        monkeypatch.setattr(bps, "tracked_entities", sensors)
        asyncio.run(bps.update_tracker_groups(hass))
        assert all(registry.entities[key] is entity for key, entity in entities.items())
    assert registry.entities["sensor.bps_group_rover_bps_zone"].name == "My dog's zone"
    assert registry.removed == [] and len(added) == initial_additions


def test_primary_group_entity_is_selectable_without_fix_and_rename_safe(platform):
    hass, registry, module, add, added = platform
    asyncio.run(module.async_setup_entry(hass, None, add))
    sync = hass.data['bps']['sync_group_sensors']
    asyncio.run(sync([{'id': 'rover', 'name': 'Rover'}]))
    key = 'sensor.bps_group_rover_bps_position'
    primary = hass.data['bps_sensors'][key]
    assert primary.unique_id == 'bps_group_position_rover'
    assert primary._attrs == {'group': True, 'tracker_key': 'bps_group_rover', 'name': 'Rover'}
    assert primary.name == 'Rover'
    count = len(added)
    registry.async_update_entity(key, 'sensor.my_dog')
    primary.entity_id = 'sensor.my_dog'
    asyncio.run(sync([{'id': 'rover', 'name': 'Rover renamed'}]))
    assert len(added) == count
    assert primary._attrs['name'] == 'Rover renamed'
    assert primary._attrs['tracker_key'] == 'bps_group_rover'
    asyncio.run(sync([]))
    assert 'sensor.my_dog' not in registry.entities
    assert primary.removed


@pytest.mark.parametrize('kind', ['position', 'zone', 'floor'])
def test_renamed_group_entities_survive_another_group_registration(platform, kind):
    hass, registry, module, add, added = platform
    asyncio.run(module.async_setup_entry(hass, None, add))
    sync = hass.data['bps']['sync_group_sensors']
    rover = {'id': 'rover', 'name': 'Rover'}
    asyncio.run(sync([rover]))
    key = f'sensor.bps_group_rover_bps_{kind}'
    primary = hass.data['bps_sensors'][key]
    renamed = f'sensor.my_dog_{kind}'
    registry.async_update_entity(key, renamed)
    primary.entity_id = renamed
    asyncio.run(sync([rover, {'id': 'fido', 'name': 'Fido'}]))
    assert renamed in registry.entities
    assert key not in registry.entities
    assert hass.data['bps_sensors'][key] is primary
    # Adding ordinary beacon sensors also invokes the legacy post-add migration.
    additions = []
    module.ensure_sensors_for_entity(hass, 'beacon_new', hass.data['bps_sensors'], additions)
    add(additions)
    module.normalize_bps_registry_entity_ids_from_cache(hass)
    assert renamed in registry.entities
    asyncio.run(sync([]))
    assert renamed not in registry.entities
