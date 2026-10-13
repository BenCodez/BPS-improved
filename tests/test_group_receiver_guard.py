"""Group centroids cannot claim proximity to a proven online silent scanner."""

import copy
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from bps.group_receiver_guard import receiver_constraints, constrain_group

NOW = 1000.0
KEY = ("entry", "aa:bb:cc:11:22:33")
FLOOR = {"name": "Yard", "scale": 10.0, "zones": [], "receivers": [
    {"entity_id": "station", "cords": {"x": 0, "y": 0}}]}


def state(value="unavailable", *, age=0, info=None):
    return NS(state=value, last_updated=datetime.fromtimestamp(NOW - age, timezone.utc),
              attributes={"bps_source": info or {}})


def sources(first=None, second=None, *, online_age=1, direct=True):
    pairs = {"a": state() if first is None else first, "b": state() if second is None else second}
    return NS(direct=NS(now=NOW, scanners={KEY: []} if direct else {}, scanner_age=lambda key: online_age),
        floor_bindings=lambda floor: [{"identity": KEY, "status": "matched"}],
        blocked=lambda binding: binding["status"] == "scanner_identity_conflict",
        source=lambda member, receiver, binding, now=None: ("sensor.range", pairs[member]))


def constraints(source=None, health=None, floor=None, now=NOW):
    return receiver_constraints(floor or FLOOR, ["a", "b"], source or sources(), health or {}, now, 30)


def payload():
    return {"ent": "bps_group_rover", "group": True, "floor": "Yard", "cords": [0, 0],
        "updated": NOW, "beacons_reporting": 2, "outdoor": {"estimated_uncertainty_m": 2, "confidence": "good"},
        "beacon_positions": [{"ent": ent, "cords": [x, 0], "floor": "Yard", "updated": NOW,
                             "used": True, "weight": weight} for ent, x, weight in [("a", -40, .6), ("b", 40, .4)]]}


def test_proven_online_station_without_either_member_range_blocks_the_centroid():
    p = payload()
    before = copy.deepcopy(p["beacon_positions"])
    result, report = constrain_group(p, constraints(), None, 10, NOW, 30)
    assert result["cords"] == [-40, 0] and report["state"] == "member_fix"
    assert report["receivers"][0]["missing_beacons"] == ["a", "b"]
    assert result["outdoor"]["confidence"] == "poor"
    assert result["outdoor"]["estimated_uncertainty_m"] >= 2
    assert result["beacon_positions"] == before


@pytest.mark.parametrize("reading", [state("3"), state(0), state("unknown", info={
    "path": "direct", "source_status": "current", "source_age_s": 1,
    "reason": "accepted_data_not_current"})])
def test_either_fresh_range_or_pending_raw_packet_prevents_a_false_absence(reading):
    assert constraints(sources(first=reading)) == []


def test_stale_numeric_range_is_not_fresh_contact():
    assert constraints(sources(first=state("3", age=31)))


@pytest.mark.parametrize("source,health", [
    (sources(online_age=31), {}),
    (sources(online_age=None), {}),
    (sources(direct=False), {}),
    (sources(), {"station": {"online": False, "identity": list(KEY), "captured": NOW}}),
    (sources(direct=False), {"station": {"online": True, "identity": ["other", KEY[1]], "captured": NOW}}),
    (sources(direct=False), {"station": {"online": True, "identity": list(KEY), "captured": NOW - 31}}),
])
def test_offline_unknown_and_replaced_receiver_health_is_neutral(source, health):
    assert constraints(source, health) == []


def test_recent_legacy_liveness_supports_expected_unavailable_range_entities():
    health = {"station": {"online": True, "identity": list(KEY), "captured": NOW - 1}}
    assert constraints(sources(direct=False), health)
    health["station"]["source_age_s"] = 29
    assert constraints(sources(direct=False), health) == []


def test_new_radio_packet_overrides_an_older_offline_snapshot():
    health = {"station": {"online": False, "identity": list(KEY), "captured": NOW - 5}}
    assert constraints(sources(), health)


def test_missing_pair_proof_manual_ignore_and_invalid_clock_are_neutral():
    s = sources()
    s.source = lambda *args, **kwargs: ("sensor.disabled", None)
    assert constraints(s) == []
    floor = copy.deepcopy(FLOOR)
    floor["receivers"][0]["outdoor_policy"] = "ignore"
    assert constraints(floor=floor) == []
    assert constraints(sources(first=state(info={"path": "direct", "reason": "invalid_observation_clock"}))) == []


def test_holding_a_supported_prior_fix_never_refreshes_its_timestamp_and_expires():
    p = payload()
    for member in p["beacon_positions"]:
        member["cords"] = [10, 0]
    prior = {"floor": "Yard", "cords": [-60, 0], "updated": NOW - 1}
    result, report = constrain_group(p, constraints(), prior, 10, NOW, 30)
    assert result["cords"] == [-60, 0] and report["state"] == "held"
    assert result["updated"] == prior["updated"]
    p["updated"] = NOW + 31
    for member in p["beacon_positions"]:
        member["updated"] = NOW + 31
    p["cords"] = [0, 0]
    result, report = constrain_group(p, constraints(), prior, 10, NOW + 31, 30)
    assert result is None and report["state"] == "unavailable"
    assert report["beacons_reporting"] == 2


def test_guard_cannot_spend_elapsed_held_time_to_bypass_the_motion_budget():
    p = payload()
    p["motion_gate"] = {"state": "limited", "allowed_step_m": 10}
    p["beacon_positions"][0]["cords"] = [0, 0]
    p["beacon_positions"][1]["cords"] = [50, 0]
    prior = {"floor": "Yard", "cords": [-100, 0], "updated": NOW - 20}
    result, report = constrain_group(p, constraints(), prior, 10, NOW, 30)
    assert result["cords"] == prior["cords"] and report["state"] == "held"


def test_no_supported_point_is_unavailable_and_offline_or_detection_only_is_unchanged():
    p = payload()
    for member in p["beacon_positions"]:
        member["cords"] = [10, 0]
    result, report = constrain_group(p, constraints(), None, 10, NOW, 30)
    assert result is None and report["state"] == "unavailable"
    p = payload()
    result, report = constrain_group(p, [], None, 10, NOW, 30)
    assert result is p and report is None
    p["outdoor"]["detection_only"] = True
    result, report = constrain_group(p, constraints(), None, 10, NOW, 30)
    assert result is p and report is None


def test_group_publication_applies_the_guard_without_moving_individual_beacons(monkeypatch):
    import asyncio
    import bps
    from conftest import make_hass

    hass = make_hass()
    hass.data["bps"] = {"layout": {"outdoor_tracking": {"enabled": True},
        "floor": [copy.deepcopy(FLOOR)], "tracker_groups": [{"id": "rover", "beacons": ["a", "b"]}]}}
    original = [{"ent": ent, "floor": "Yard", "cords": [x, 0], "updated": NOW,
        "outdoor": {"observed": NOW, "estimated_uncertainty_m": 4, "receivers_used": 4}}
        for ent, x in [("a", -40), ("b", 40)]]
    before = copy.deepcopy(original)
    source = sources()
    source.direct_trackers = lambda: {"a", "b"}
    monkeypatch.setattr(bps, "ReceiverSources", lambda _: source)
    monkeypatch.setattr(bps, "apitricords", original)
    monkeypatch.setattr(bps.time, "time", lambda: NOW)
    history = NS(record=lambda *a: None, mark_gap=lambda *a: None)
    monkeypatch.setattr(bps, "get_position_history", lambda _: history)
    asyncio.run(bps.update_tracker_groups(hass))
    group = next(p for p in bps.apitricords if p.get("group"))
    assert group["cords"] in ([-40, 0], [40, 0])
    assert group["outdoor"]["receiver_guard"]["state"] == "member_fix"
    assert [p for p in bps.apitricords if not p.get("group")] == before
    assert hass.data["bps"]["group_receiver_guards"]["bps_group_rover"]["state"] == "member_fix"


def test_guard_hold_cannot_reuse_consumed_motion_time_without_a_motion_gate(monkeypatch):
    import asyncio
    import bps
    from conftest import make_hass

    hass = make_hass()
    floor = copy.deepcopy(FLOOR)
    floor["scale"] = 1
    floor["receivers"][0]["cords"]["x"] = 8
    hass.data["bps"] = {"layout": {"outdoor_tracking": {"enabled": True}, "floor": [floor],
        "tracker_groups": [{"id": "rover", "beacons": ["a", "b"]}]}}
    source = sources()
    source.direct_trackers = lambda: {"a", "b"}
    monkeypatch.setattr(bps, "ReceiverSources", lambda _: source)
    monkeypatch.setattr(bps, "get_position_history", lambda _: NS(record=lambda *a: None, mark_gap=lambda *a: None))
    monkeypatch.setattr(bps, "apitricords", [])
    clock = NOW
    monkeypatch.setattr(bps.time, "time", lambda: clock)
    for step, coordinates in enumerate(((0, 0), (7, 9), (8, 12))):
        clock = NOW + step
        previous = [p for p in bps.apitricords if p.get("group")]
        bps.apitricords = [{"ent": ent, "floor": "Yard", "cords": [x, 0], "updated": clock,
            "outdoor": {"observed": clock, "estimated_uncertainty_m": 4, "receivers_used": 4}}
            for ent, x in zip(("a", "b"), coordinates)] + previous
        asyncio.run(bps.update_tracker_groups(hass))
        group = next(p for p in bps.apitricords if p.get("group"))
        assert group["cords"] == [0, 0]
        assert group["updated"] == NOW
    assert "motion_gate" not in group
    assert group["outdoor"]["receiver_guard"]["state"] == "held"
    assert hass.data["bps"]["group_stability"]["bps_group_rover"]["motion_epoch"] == NOW + 2


@pytest.mark.parametrize("outdoor", [False, True])
def test_removed_groups_clear_receiver_guard_diagnostics(monkeypatch, outdoor):
    import asyncio
    import bps
    from conftest import make_hass

    hass = make_hass()
    hass.data["bps"] = {"layout": {"outdoor_tracking": {"enabled": outdoor},
        "floor": [copy.deepcopy(FLOOR)], "tracker_groups": []},
        "group_receiver_guards": {"bps_group_rover": {"state": "unavailable"}},
        "group_guard_baselines": {"bps_group_rover": {"floor": "Yard", "cords": [0, 0], "updated": NOW}}}
    monkeypatch.setattr(bps, "apitricords", [])
    asyncio.run(bps.update_tracker_groups(hass))
    assert "group_receiver_guards" not in hass.data["bps"]
    assert "group_guard_baselines" not in hass.data["bps"]


@pytest.mark.parametrize("recovery", ["new_observations", "expiry", "membership"])
def test_unavailable_guard_cannot_restart_motion_on_cached_observations(monkeypatch, recovery):
    import asyncio
    import bps
    from conftest import make_hass

    hass = make_hass()
    floor = copy.deepcopy(FLOOR)
    floor["scale"] = 1
    layout = {"outdoor_tracking": {"enabled": True}, "reading_max_age": 30, "floor": [floor],
        "tracker_groups": [{"id": "rover", "beacons": ["a", "b"]}]}
    hass.data["bps"] = {"layout": layout}
    reading = state("2")
    source = sources()
    source.source = lambda *args, **kwargs: ("sensor.range", reading)
    source.direct_trackers = lambda: {"a", "b"}
    monkeypatch.setattr(bps, "ReceiverSources", lambda _: source)
    monkeypatch.setattr(bps, "get_position_history", lambda _: NS(record=lambda *a: None, mark_gap=lambda *a: None))
    clock = NOW
    monkeypatch.setattr(bps.time, "time", lambda: clock)

    def fixes(x, stamp):
        return [{"ent": ent, "floor": "Yard", "cords": [x, 0], "updated": stamp,
            "outdoor": {"observed": stamp, "estimated_uncertainty_m": 4, "receivers_used": 4}} for ent in ("a", "b")]

    monkeypatch.setattr(bps, "apitricords", fixes(0, NOW))
    asyncio.run(bps.update_tracker_groups(hass))
    original = next(p for p in bps.apitricords if p.get("group"))
    reading = state("unavailable")
    clock = NOW + 1
    bps.apitricords = fixes(30, clock) + [original]
    for poll in (NOW + 1, NOW + 1, NOW + 2):
        clock = poll
        asyncio.run(bps.update_tracker_groups(hass))
        assert not any(p.get("group") for p in bps.apitricords)
        baseline = hass.data["bps"]["group_guard_baselines"]["bps_group_rover"]
        assert baseline["cords"] == [0, 0] and baseline["updated"] == NOW
        assert hass.data["bps"]["group_stability"]["bps_group_rover"]["motion_epoch"] == NOW + 1
    if recovery == "new_observations":
        for update in (2, 3, 4):
            clock = NOW + update
            bps.apitricords = fixes(30, clock)
            asyncio.run(bps.update_tracker_groups(hass))
            if update < 4:
                assert not any(p.get("group") for p in bps.apitricords)
        current = next(p for p in bps.apitricords if p.get("group"))
        assert 3 <= current["cords"][0] <= 10
        assert not hass.data["bps"]["group_guard_baselines"]
        return
    if recovery == "membership":
        layout["tracker_groups"][0]["beacons"] = ["c", "d"]
        source.source = lambda *args, **kwargs: ("sensor.range", reading)
        clock = NOW + 3
        bps.apitricords = [{**p, "ent": ent} for p, ent in zip(fixes(0, clock), ("c", "d"))]
        asyncio.run(bps.update_tracker_groups(hass))
        assert not any(p.get("group") for p in bps.apitricords)
        assert not hass.data["bps"]["group_guard_baselines"]
        return
    # The hidden baseline expires by its original source time, allowing a fresh
    # fix to establish a new track rather than holding an old point forever.
    clock = NOW + 31
    bps.apitricords = fixes(30, clock)
    asyncio.run(bps.update_tracker_groups(hass))
    current = next(p for p in bps.apitricords if p.get("group"))
    assert current["cords"] == [30, 0]
    assert not hass.data["bps"]["group_guard_baselines"]


@pytest.mark.parametrize("case,expected", [
    ("complete_absence", True), ("stale_pair", True), ("ambiguous", False),
    ("api_truncated", False), ("bridge_truncated", False), ("api_missing_counts", False),
    ("missing_age", False), ("negative_age", False), ("nonfinite_age", False),
    ("fresh_raw_stale_status", False),
])
def test_real_direct_adapters_require_complete_unambiguous_contact_evidence(hass, monkeypatch, case, expected):
    from test_bermuda_direct import prepared, measurement, MAC, OTHER
    from bps import bermuda_direct
    from bps.receiver_sources import ReceiverSources

    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    snapshot = payloads["entry"]
    device = snapshot["devices"]["synthetic-tracker"]
    if case in ("complete_absence", "api_truncated", "api_missing_counts"):
        device["scanners"] = {}
    if case == "api_truncated":
        snapshot["omitted"]["observations"] = 1
    elif case == "api_missing_counts":
        snapshot.pop("omitted")
    elif case == "bridge_truncated":
        monkeypatch.setattr(bermuda_direct, "MAX_OBSERVATIONS", 0)
    elif case == "ambiguous":
        device["scanners"][OTHER] = measurement(OTHER, retained=MAC)
    elif case in ("missing_age", "negative_age", "nonfinite_age"):
        device["scanners"][MAC]["age"] = {"missing_age": None, "negative_age": -1,
                                           "nonfinite_age": float("nan")}[case]
    elif case in ("stale_pair", "fresh_raw_stale_status"):
        ad = device["scanners"][MAC]
        ad["status"] = "stale"
        if case == "stale_pair":
            ad["age"] = 31
            ad["observed"] = NOW - 31
    source = ReceiverSources(hass)
    result = receiver_constraints(data["floor"][0], ["tag"], source, {}, NOW, 30)
    assert bool(result) is expected
    if result:
        assert result[0]["missing_beacons"] == ["tag"]


def test_legacy_distance_liveness_retains_the_original_reading_age(hass, monkeypatch):
    import asyncio
    import bps
    from test_receiver_aliases import aliases, timestamp
    from test_bermuda_direct import install
    from bps.receiver_sources import ReceiverSources

    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    install(monkeypatch, {"entry": None})
    states[new].last_updated = timestamp(NOW - 29)
    asyncio.run(bps.update_receiver_liveness(hass))
    health = hass.data["bps"]["rl_status_snapshot"]
    assert health["old_station"]["online"] is True
    assert health["old_station"]["source_age_s"] == 29
    monkeypatch.setattr(bps.time, "time", lambda: NOW + 2)
    source = ReceiverSources(hass)
    assert receiver_constraints(data["floor"][0], ["tag"], source, health, NOW + 2, 30) == []
    asyncio.run(bps.update_receiver_liveness(hass))
    assert hass.data["bps"]["rl_status_snapshot"]["old_station"]["online"] is None


def test_direct_scanner_liveness_age_survives_an_api_fallback(hass, monkeypatch):
    import asyncio
    import bps
    from test_bermuda_direct import prepared, MAC
    from bps.receiver_sources import ReceiverSources

    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch)
    payloads["entry"]["scanners"][MAC]["scanner_last_seen_age"] = 29
    asyncio.run(bps.update_receiver_liveness(hass))
    health = hass.data["bps"]["rl_status_snapshot"]
    assert health["old_station"]["online"] is True
    assert health["old_station"]["source_age_s"] == 29
    payloads["entry"] = None
    states[new].state = "unavailable"
    monkeypatch.setattr(bps.time, "time", lambda: NOW + 2)
    source = ReceiverSources(hass)
    assert receiver_constraints(data["floor"][0], ["tag"], source, health, NOW + 2, 30) == []


@pytest.mark.parametrize("change", ["membership", "scale"])
@pytest.mark.parametrize("supported_member", [False, True])
def test_changed_group_signature_cannot_hold_a_fix_from_the_old_group(monkeypatch, change, supported_member):
    import asyncio
    import bps
    from conftest import make_hass

    hass = make_hass()
    floor = copy.deepcopy(FLOOR)
    floor["scale"] = 1
    floor["receivers"][0]["cords"]["x"] = 8
    group_config = {"id": "rover", "beacons": ["a", "b"]}
    hass.data["bps"] = {"layout": {"outdoor_tracking": {"enabled": True}, "floor": [floor],
        "tracker_groups": [group_config]}}
    source = sources()
    source.direct_trackers = lambda: {"a", "b", "c", "d"}
    source.source = lambda *args, **kwargs: ("sensor.range", state())
    monkeypatch.setattr(bps, "ReceiverSources", lambda _: source)
    monkeypatch.setattr(bps, "get_position_history", lambda _: NS(record=lambda *a: None, mark_gap=lambda *a: None))
    monkeypatch.setattr(bps, "apitricords", [])
    clock = NOW
    monkeypatch.setattr(bps.time, "time", lambda: clock)

    def fixes(coordinates):
        return [{"ent": ent, "floor": "Yard", "cords": [x, 0], "updated": clock,
            "outdoor": {"observed": clock, "estimated_uncertainty_m": 4, "receivers_used": 4}}
            for ent, x in zip(group_config["beacons"], coordinates)]

    bps.apitricords = fixes((0, 0))
    asyncio.run(bps.update_tracker_groups(hass))
    previous = next(p for p in bps.apitricords if p.get("group"))
    assert previous["cords"] == [0, 0]
    clock += 1
    if change == "membership":
        group_config["beacons"] = ["c", "d"]
    else:
        floor["scale"] = 2
    member_x = 8 + 3 * floor["scale"] + 1 if supported_member else 8
    bps.apitricords = fixes((8, member_x)) + [previous]
    asyncio.run(bps.update_tracker_groups(hass))
    report = hass.data["bps"]["group_receiver_guards"]["bps_group_rover"]
    if supported_member:
        current = next(p for p in bps.apitricords if p.get("group"))
        assert current["cords"] == [member_x, 0]
        assert current["updated"] == clock
        assert report["state"] == "member_fix"
    else:
        assert not any(p.get("group") for p in bps.apitricords)
        assert report["state"] == "unavailable"


@pytest.mark.parametrize("clock", ["missing", "malformed", "future", "nonfinite", "negative"])
def test_real_legacy_adapter_unavailability_with_an_invalid_clock_is_neutral(hass, monkeypatch, clock):
    import asyncio
    import bps
    from test_receiver_aliases import aliases, timestamp
    from test_bermuda_direct import install
    from bps.receiver_sources import ReceiverSources

    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch)
    install(monkeypatch, {"entry": None})
    states[new].state = "unavailable"
    states[old].state = "unknown"
    states[old].last_updated = {
        "missing": None, "malformed": NS(timestamp=lambda: "bad timestamp"),
        "future": timestamp(NOW + 1), "nonfinite": NS(timestamp=lambda: float("nan")),
        "negative": timestamp(-1),
    }[clock]
    states["binary_sensor.current_station_status"] = NS(state="on", attributes={"device_class": "connectivity"})
    asyncio.run(bps.update_receiver_liveness(hass))
    health = hass.data["bps"]["rl_status_snapshot"]
    assert health["old_station"]["online"] is True
    source = ReceiverSources(hass)
    assert receiver_constraints(data["floor"][0], ["tag"], source, health, NOW, 30) == []


@pytest.mark.parametrize("cutoff,age,accepted", [(120, 40, True), (120, 120, False),
                                                (120, 121, False), (0, 400, True)])
def test_real_legacy_ranges_honor_configured_freshness(hass, monkeypatch, cutoff, age, accepted):
    import asyncio
    import bps
    from test_receiver_aliases import aliases, timestamp
    from test_bermuda_direct import install
    from bps.receiver_sources import ReceiverSources

    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch, outdoor=True)
    install(monkeypatch, {"entry": None})
    data["reading_max_age"] = cutoff
    hass.data["bps"]["layout"] = data
    states[new].last_updated = timestamp(NOW - age)
    states["binary_sensor.current_station_status"] = NS(state="on", attributes={"device_class": "connectivity"})
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    receiver = data["floor"][0]["receivers"][0]
    assert ("distance" in receiver) is accepted
    asyncio.run(bps.update_receiver_liveness(hass))
    source = ReceiverSources(hass)
    result = receiver_constraints(data["floor"][0], ["tag"], source,
        hass.data["bps"]["rl_status_snapshot"], NOW, cutoff or 180, reading_max_age_s=cutoff)
    assert bool(result) is (not accepted)


@pytest.mark.parametrize("cutoff,age", [(120, 40), (0, 400)])
def test_group_publication_passes_the_configured_range_age_gate(monkeypatch, cutoff, age):
    import asyncio
    import bps
    from conftest import make_hass

    hass = make_hass()
    hass.data["bps"] = {"layout": {"outdoor_tracking": {"enabled": True},
        "reading_max_age": cutoff, "floor": [copy.deepcopy(FLOOR)],
        "tracker_groups": [{"id": "rover", "beacons": ["a", "b"]}]}}
    source = sources(first=state("3", age=age), second=state("4", age=age))
    source.direct_trackers = lambda: {"a", "b"}
    monkeypatch.setattr(bps, "ReceiverSources", lambda _: source)
    monkeypatch.setattr(bps, "get_position_history", lambda _: NS(record=lambda *a: None, mark_gap=lambda *a: None))
    monkeypatch.setattr(bps.time, "time", lambda: NOW)
    monkeypatch.setattr(bps, "apitricords", [{"ent": ent, "floor": "Yard", "cords": [0, 0], "updated": NOW,
        "outdoor": {"observed": NOW, "estimated_uncertainty_m": 4, "receivers_used": 4}} for ent in ("a", "b")])
    asyncio.run(bps.update_tracker_groups(hass))
    group = next(p for p in bps.apitricords if p.get("group"))
    assert group["cords"] == [0, 0]
    assert not hass.data["bps"]["group_receiver_guards"]


@pytest.mark.parametrize("cutoff,age", [(120, 40), (0, 400)])
def test_real_direct_ranges_honor_configured_freshness(hass, monkeypatch, cutoff, age):
    import asyncio
    import bps
    from test_bermuda_direct import prepared, MAC
    from bps.receiver_sources import ReceiverSources

    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch, outdoor=True)
    data["reading_max_age"] = cutoff
    hass.data["bps"]["layout"] = data
    payloads["entry"]["devices"]["synthetic-tracker"]["scanners"][MAC]["age"] = age
    asyncio.run(bps.update_receiver_radii(hass, {"entity": "tag", "data": data}))
    assert "distance" in data["floor"][0]["receivers"][0]
    source = ReceiverSources(hass)
    assert receiver_constraints(data["floor"][0], ["tag"], source, {}, NOW,
        cutoff or 180, reading_max_age_s=cutoff) == []


def test_short_range_age_gate_preserves_recent_direct_receiver_health(hass, monkeypatch):
    import asyncio
    import bps
    from test_bermuda_direct import prepared, MAC

    data, entries, states, add, old, new, payloads, calls = prepared(hass, monkeypatch, outdoor=True)
    data["reading_max_age"] = 5
    data["tracker_groups"] = [{"id": "rover", "beacons": ["tag"]}]
    hass.data["bps"]["layout"] = data
    payloads["entry"]["scanners"][MAC]["scanner_last_seen_age"] = 10
    payloads["entry"]["devices"]["synthetic-tracker"]["scanners"] = {}
    asyncio.run(bps.update_receiver_liveness(hass))
    assert hass.data["bps"]["rl_status_snapshot"]["old_station"]["online"] is True
    monkeypatch.setattr(bps, "get_position_history", lambda _: NS(record=lambda *a: None, mark_gap=lambda *a: None))
    monkeypatch.setattr(bps, "apitricords", [{"ent": "tag", "floor": "Ground", "cords": [10, 20], "updated": NOW,
        "outdoor": {"observed": NOW, "estimated_uncertainty_m": 4, "receivers_used": 4}}])
    asyncio.run(bps.update_tracker_groups(hass))
    assert not any(p.get("group") for p in bps.apitricords)
    assert hass.data["bps"]["group_receiver_guards"]["bps_group_rover"]["state"] == "unavailable"


def test_short_range_age_gate_preserves_recent_legacy_health_between_polls(hass, monkeypatch):
    import asyncio
    import bps
    from test_receiver_aliases import aliases
    from test_bermuda_direct import install
    from bps.receiver_sources import ReceiverSources

    data, entries, states, devices, add, old, new = aliases(hass, monkeypatch, outdoor=True)
    install(monkeypatch, {"entry": None})
    states[new].state = "unavailable"
    states["binary_sensor.current_station_status"] = NS(state="on", attributes={"device_class": "connectivity"})
    clock = NOW - 6
    monkeypatch.setattr(bps.time, "time", lambda: clock)
    asyncio.run(bps.update_receiver_liveness(hass))
    health = hass.data["bps"]["rl_status_snapshot"]
    assert health["old_station"]["online"] is True
    clock = NOW
    result = receiver_constraints(data["floor"][0], ["tag"], ReceiverSources(hass),
        health, NOW, 5, reading_max_age_s=5)
    assert result and result[0]["missing_beacons"] == ["tag"]
