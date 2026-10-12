"""All floor-local calibration maps share one fresh identity inventory."""
import copy

from bps import calibration as C
from bps.receiver_sources import ReceiverSources
from test_calibration_alias_lifecycle import fixture


def test_many_calibration_floors_share_one_index_without_mixing_geometry(hass, monkeypatch):
    data, _, _ = fixture(hass, monkeypatch, duplicate=True)
    ground = data["floor"][0]
    floors = []
    for index in range(128):
        floor = copy.deepcopy(ground)
        floor.update(name=f"Synthetic floor {index}", scale=10 + index)
        for rec in floor["receivers"]:
            rec["cords"]["x"] += index * 30
            rec["cords"]["y"] += index * 10
        if index % 2:
            floor["receivers"].insert(0, floor["receivers"].pop())
        floors.append(floor)
    # Empty floors must not add inventory scans either.
    floors += [{"name": f"Empty {index}", "scale": 10, "receivers": []} for index in range(128)]
    coords = {"floor": floors}
    original = copy.deepcopy(coords)
    expected = {floor["name"]: C._build_receiver_map({"floor": [floor]}, hass=hass)
                for floor in (floors[0], floors[1], floors[127])}
    builds = []

    class CountedSources(ReceiverSources):
        def __init__(self, current_hass):
            super().__init__(current_hass)
            builds.append(self)

    monkeypatch.setattr(C, "ReceiverSources", CountedSources)
    cal = C.get_calibration_state(hass)
    C._set_auto_receivers(hass, cal, coords)
    assert len(builds) == 1
    assert coords == original
    assert len(cal["_auto_floor_receivers"]) == 256
    for name, geometry in expected.items():
        assert cal["_auto_floor_receivers"][name] == geometry
        assert len(geometry) == 4  # Equivalent same-floor alias is deduplicated.
        assert all(row["strict_identity"] for row in geometry.values())
    assert len(cal["receivers"]) == 4
    for index in range(128):
        name = f"Synthetic floor {index}"
        shared = "shared_current" if index % 2 else "shared_old"
        assert cal["_auto_routes"][name][shared] == "shared_old"
        assert cal["_auto_floor_receivers"][name][shared]["x"] == index * 30
        assert cal["_auto_floor_receivers"][name][shared]["scale"] == 10 + index
    C._set_auto_receivers(hass, cal, coords)
    assert len(builds) == 2 and builds[0] is not builds[1]
