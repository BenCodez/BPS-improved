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
