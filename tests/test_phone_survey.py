"""Synthetic server-authoritative phone survey and verified layout mutations."""

import asyncio
import copy
import math
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from bps.phone_survey import BPSPhoneSurveyAPI, PhoneSurvey
from bps.storage import get_bps_data
from conftest import make_hass

NOW = 1800000000.0
PHONE = "synthetic_phone"
OWNER = "synthetic_user"


def point(x=5, y=5, accuracy=2, timestamp=NOW * 1000):
    return {
        "latitude": 10 - y / 6371008.8 * 180 / math.pi,
        "longitude": 20 + x / (6371008.8 * math.cos(math.radians(10))) * 180 / math.pi,
        "accuracy_m": accuracy,
        "timestamp": timestamp,
    }


class Fixture:
    def __init__(self):
        self.now = NOW
        self.hass = make_hass()
        self.floor = {
            "name": "Synthetic floor",
            "scale": 20.0,
            "zones": [],
            "receivers": [
                {
                    "entity_id": "synthetic_receiver",
                    "scanner_mac": "aa:bb:cc:dd:ee:ff",
                    "scanner_config_entry_id": "synthetic_instance",
                    "cords": {"x": 100.0, "y": 100.0},
                    "height": 1.5,
                    "correction": 1.2,
                }
            ],
            "georeference": {
                "version": 1,
                "anchors": [
                    {
                        **{k: v for k, v in point(x, y).items() if k != "timestamp"},
                        "x": 200 + x * 20,
                        "y": 200 + y * 20,
                        "label": "Synthetic anchor",
                    }
                    for x, y in [(0, 0), (30, 0), (0, 30)]
                ],
            },
        }
        self.hass.data["bps"] = {
            "layout": {"floor": [self.floor], "tracker_height": 1.0}
        }
        self.gps = []
        self.hass.states = SimpleNamespace(
            async_all=lambda domain: [
                s for s in self.gps if s.entity_id.startswith(domain + ".")
            ]
        )
        self.binding = {
            "identity": ("synthetic_instance", "aa:bb:cc:dd:ee:ff"),
            "status": "matched",
        }
        self.row = {
            "tracker": PHONE,
            "receiver": "synthetic_receiver",
            "floor": self.floor["name"],
            "entity_id": "sensor.synthetic_phone_distance_to_synthetic_receiver",
            "status": "current",
            "unit": "m",
            "assumed_unit_m": False,
            "measured_distance_m": 0.8,
            "measurement_source": {"path": "legacy_entity"},
        }
        self.age = 0
        self.observed = None
        self.members = [PHONE]
        self.known = [PHONE]
        fixture = self

        class Sources:
            def __init__(self, _hass):
                pass

            def floor_bindings(self, floor):
                return [copy.deepcopy(fixture.binding) for _ in floor["receivers"]]

            def source(self, phone, rec, binding, *, now):
                return fixture.row["entity_id"], SimpleNamespace(state="0.8")

        self.sources = Sources
        self.manager = self.make_manager()

    def snapshot(self, _hass, targets, now):
        observed = self.observed if self.observed is not None else now - self.age
        return {
            "context": {"target_members": {PHONE: self.members}},
            "readings": [
                {**copy.deepcopy(self.row), "age_s": self.age, "observed": observed}
            ],
        }

    def inventory(self, _hass):
        return {"trackers": [{"key": key, "name": key} for key in self.known]}

    def make_manager(self):
        return PhoneSurvey(
            self.hass,
            self.snapshot,
            self.inventory,
            clock=lambda: self.now,
            sources=self.sources,
        )

    async def start(self, **extra):
        return (
            await self.manager.action(
                OWNER,
                {
                    "action": "start",
                    "phone": PHONE,
                    "floor": self.floor["name"],
                    "browser_time_ms": self.now * 1000,
                    **extra,
                },
            )
        )["session"]

    async def poll(self, token, samples=None):
        return await self.manager.action(
            OWNER,
            {
                "action": "poll",
                "session": token,
                "browser_samples": samples
                if samples is not None
                else [point(timestamp=self.now * 1000)],
            },
        )

    async def capture(self, token=None, sample=None):
        token = token or await self.start()
        last = None
        for _ in range(16):
            last = await self.poll(
                token, [sample() if sample else point(timestamp=self.now * 1000)]
            )
            self.now += 2
        return token, last


def run(coro):
    return asyncio.run(coro)


def test_authenticated_and_server_membership_rejects_logical_or_wrong_phone():
    assert BPSPhoneSurveyAPI.requires_auth is True
    f = Fixture()
    f.known = ["bps_group_synthetic"]
    with pytest.raises(ValueError, match="actual Bermuda"):
        run(f.start(phone="bps_group_synthetic"))
    f.known = [PHONE]

    async def check():
        token = await f.start()
        f.members = ["synthetic_other_phone"]
        with pytest.raises(ValueError, match="membership changed"):
            await f.poll(token)
        f.members = [PHONE]
        f.known = []
        with pytest.raises(ValueError, match="no longer available"):
            await f.poll(token)

    run(check())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: setattr(f, "age", 5.1),
        lambda f: setattr(f, "observed", NOW - 6),
        lambda f: f.row.update(status="stale"),
        lambda f: f.row.update(measured_distance_m=1.01),
        lambda f: f.row.update(assumed_unit_m=True),
        lambda f: f.row.update(unit="unknown"),
        lambda f: f.row.update(tracker="synthetic_other_phone"),
        lambda f: f.binding.update(identity=None),
        lambda f: f.binding.update(status="ambiguous_scanner_identity"),
        lambda f: f.row.update(
            measurement_source={
                "path": "direct",
                "entry_id": "wrong_instance",
                "scanner_mac": "aa:bb:cc:dd:ee:ff",
            }
        ),
    ],
)
def test_stale_unknown_or_unproven_range_never_creates_candidate(mutate):
    f = Fixture()
    mutate(f)
    _, result = run(f.capture())
    assert result["capturing"] is False
    assert f.manager.candidates == []
    assert f.hass.data["bps"]["layout"]["floor"][0]["receivers"][0]["cords"] == {
        "x": 100.0,
        "y": 100.0,
    }


def test_averages_gps_retains_raw_clocks_and_only_saves_after_explicit_verification():
    f = Fixture()

    async def check():
        original = copy.deepcopy(get_bps_data(f.hass))
        token, result = await f.capture(
            sample=lambda: point(x=4 if f.now < NOW + 16 else 6, timestamp=f.now * 1000)
        )
        candidate = result["candidate"]
        assert get_bps_data(f.hass) == original
        assert candidate["status"] == "pending"
        assert candidate["gps"]["sample_count"] == 16
        assert candidate["gps"]["accuracy_m"] >= 2
        assert candidate["gps"]["spread_m"] == pytest.approx(1, abs=1e-6)
        assert candidate["position"]["x"] == pytest.approx(300, abs=1e-6)
        assert candidate["position"]["y"] == pytest.approx(300, abs=1e-6)
        assert candidate["gps"]["samples"][0]["timestamp"] == NOW * 1000
        assert candidate["ble_proof"][0]["observed"] == NOW
        assert "bps_phone_survey" in f.hass._store_backing
        # An unchanged stationary session cannot repeatedly overwrite its candidate.
        for _ in range(20):
            f.now += 2
            assert "candidate" not in await f.poll(token)
        assert len(f.manager.candidates) == 1
        # Concurrent non-coordinate changes survive verified read-modify-write.
        floor = get_bps_data(f.hass)["floor"][0]
        floor["receivers"][0].update(height=2.0, correction=1.7)
        floor["zones"].append({"name": "Concurrent synthetic zone"})
        f.now += 1000  # Verification deliberately reviews a retained candidate later.
        saved = await f.manager.action(
            OWNER, {"action": "verify", "candidate_id": candidate["id"]}
        )
        assert saved["saved"] and saved["position"] == candidate["position"]
        floor = get_bps_data(f.hass)["floor"][0]
        assert floor["receivers"][0]["height"] == 2.0
        assert floor["receivers"][0]["correction"] == 1.7
        assert floor["zones"] == [{"name": "Concurrent synthetic zone"}]
        assert floor["scale"] == original["floor"][0]["scale"]
        assert floor["georeference"] == original["floor"][0]["georeference"]
        assert floor["receivers"][0]["cords"] == candidate["position"]
        assert f.manager.candidates[0]["status"] == "verified"

    run(check())


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f.floor.update(scale=21),
        lambda f: f.floor["georeference"]["anchors"][0].update(x=201),
        lambda f: f.floor["receivers"][0]["cords"].update(x=101),
        lambda f: f.floor["receivers"][0].update(scanner_config_entry_id="replacement"),
    ],
)
def test_capture_cancels_when_saved_coordinate_context_changes(mutate):
    f = Fixture()

    async def check():
        token = await f.start()
        await f.poll(token)
        mutate(f)
        f.now += 2
        with pytest.raises(ValueError, match="changed"):
            await f.poll(token)
        assert not f.manager.sessions and not f.manager.candidates

    run(check())


def test_candidate_verification_rejects_replacement_hardware_or_changed_anchors():
    for mutate in (
        lambda f: f.binding.update(identity=("replacement", "aa:bb:cc:dd:ee:ff")),
        lambda f: f.floor["georeference"]["anchors"][0].update(x=201),
    ):
        f = Fixture()

        async def check():
            _, result = await f.capture()
            mutate(f)
            with pytest.raises(ValueError, match="changed"):
                await f.manager.action(
                    OWNER,
                    {"action": "verify", "candidate_id": result["candidate"]["id"]},
                )
            assert f.floor["receivers"][0]["cords"] == {"x": 100.0, "y": 100.0}

        run(check())


def test_poll_gap_restarts_capture_and_stop_keeps_persisted_candidates():
    f = Fixture()

    async def check():
        token = await f.start()
        await f.poll(token)
        f.now += 20
        result = await f.poll(token)
        assert result["elapsed_s"] == 0
        await f.manager.action(OWNER, {"action": "stop", "session": token})
        with pytest.raises(ValueError, match="stopped"):
            await f.poll(token)
        token, result = await f.capture()
        await f.manager.action(OWNER, {"action": "stop", "session": token})
        restarted = f.make_manager()
        assert (await restarted.get(OWNER))["candidates"][0]["id"] == result[
            "candidate"
        ]["id"]
        assert (await restarted.get("synthetic_other_user"))["candidates"] == []

    run(check())


def test_cached_gps_and_cached_ble_cannot_be_renewed_by_polling():
    f = Fixture()
    _, result = run(f.capture(sample=lambda: point(timestamp=NOW * 1000)))
    assert "candidate" not in result and not f.manager.candidates
    f = Fixture()
    f.row["measurement_source"] = {
        "path": "direct",
        "entry_id": "synthetic_instance",
        "scanner_mac": "aa:bb:cc:dd:ee:ff",
        "source_observed": 1.0,
    }
    _, result = run(f.capture())
    assert "candidate" not in result and not f.manager.candidates


def test_movement_and_future_or_pre_capture_fixes_do_not_produce_eligible_average():
    for sample in (
        lambda f: point(
            x=0 if f.now < NOW + 16 else 40, accuracy=1, timestamp=f.now * 1000
        ),
        lambda f: point(timestamp=(f.now + 100) * 1000),
        lambda f: point(timestamp=(NOW - 1) * 1000),
    ):
        f = Fixture()
        _, result = run(f.capture(sample=lambda: sample(f)))
        assert "candidate" not in result and not f.manager.candidates


def test_clock_skew_is_aligned_once_without_changing_original_browser_timestamps():
    f = Fixture()

    async def check():
        token = await f.start(browser_time_ms=(NOW - 120) * 1000)
        last = None
        for _ in range(16):
            last = await f.poll(token, [point(timestamp=(f.now - 120) * 1000)])
            f.now += 2
        gps = last["candidate"]["gps"]
        assert gps["samples"][0]["timestamp"] == (NOW - 120) * 1000
        assert gps["samples"][0]["observed"] == NOW

    run(check())


def test_optional_phone_gps_command_requires_real_explicit_service_and_is_rate_limited():
    f = Fixture()
    calls = []

    async def call(domain, service, data, blocking):
        calls.append((domain, service, data, blocking))

    f.hass.services = SimpleNamespace(
        async_services=lambda: {
            "notify": {"mobile_app_synthetic_phone": {}, "persistent_notification": {}}
        },
        async_call=call,
    )

    async def check():
        assert (await f.manager.get(OWNER))["notify_services"] == [
            {
                "key": "mobile_app_synthetic_phone",
                "name": "notify.mobile_app_synthetic_phone",
            }
        ]
        with pytest.raises(ValueError, match="Explicitly select"):
            await f.start(request_gps=True, notify_service="arbitrary_service")
        token = await f.start()
        await f.poll(token)
        assert calls == []
        await f.manager.action(OWNER, {"action": "stop", "session": token})
        token = await f.start(
            request_gps=True, notify_service="mobile_app_synthetic_phone"
        )
        response = await f.poll(token)
        assert response["gps_request"]["status"] == "requested"
        assert response["gps_request"]["gps_response"] == "unknown"
        f.now += 2
        await f.poll(token)
        assert len(calls) == 1 and calls[0] == (
            "notify",
            "mobile_app_synthetic_phone",
            {"message": "request_location_update"},
            True,
        )
        await f.manager.action(OWNER, {"action": "stop", "session": token})
        token = await f.start(
            request_gps=True, notify_service="mobile_app_synthetic_phone"
        )
        await f.poll(token)
        assert len(calls) == 1, "restart cannot bypass per-phone rate limit"

    run(check())


def test_stale_real_ha_gps_can_be_selected_for_recovery_but_cached_states_are_not_fixes():
    f = Fixture()
    f.gps = [
        SimpleNamespace(
            entity_id="device_tracker.synthetic_gps_phone",
            state="home",
            attributes={
                "latitude": 10.0,
                "longitude": 20.0,
                "gps_accuracy": 2.0,
                "source_type": "gps",
            },
            last_updated=datetime.fromtimestamp(NOW - 100, timezone.utc),
        )
    ]

    async def check():
        token = await f.start(gps_entity=f.gps[0].entity_id)
        result = await f.poll(token, [])
        assert result["sample_count"] == 0
        f.gps[0].last_updated = datetime.fromtimestamp(NOW, timezone.utc)
        result = await f.poll(token, [])
        assert result["sample_count"] == 1
        f.now += 2
        f.gps[0].last_updated = datetime.fromtimestamp(f.now, timezone.utc)
        result = await f.poll(token, [])
        assert result["sample_count"] == 1, (
            "unrelated HA attribute updates cannot refresh a cached coordinate tuple"
        )
        f.gps[0].attributes["latitude"] += 0.000001
        result = await f.poll(token, [])
        assert result["sample_count"] == 2

    run(check())


def test_committed_position_is_reported_when_audit_save_fails():
    f = Fixture()

    async def check():
        _, result = await f.capture()

        async def fail(_payload):
            raise OSError("synthetic audit failure")

        f.manager.store.async_save = fail
        response = await f.manager.action(
            OWNER, {"action": "verify", "candidate_id": result["candidate"]["id"]}
        )
        assert (
            response["saved"] and "record could not be persisted" in response["warning"]
        )
        assert (
            get_bps_data(f.hass)["floor"][0]["receivers"][0]["cords"]
            == response["position"]
        )

    run(check())


def test_multiple_pending_receivers_can_be_verified_in_sequence():
    f = Fixture()
    f.floor["receivers"].append(
        {
            "entity_id": "synthetic_receiver_two",
            "scanner_mac": "aa:bb:cc:dd:ee:02",
            "scanner_config_entry_id": "synthetic_instance",
            "cords": {"x": 120.0, "y": 100.0},
            "height": 2.0,
        }
    )

    class Sources:
        def __init__(self, _hass):
            pass

        def floor_bindings(self, floor):
            return [
                {
                    "status": "matched",
                    "identity": (rec["scanner_config_entry_id"], rec["scanner_mac"]),
                }
                for rec in floor["receivers"]
            ]

        def source(self, phone, rec, binding, *, now):
            return "sensor." + phone + "_distance_to_" + rec[
                "entity_id"
            ], SimpleNamespace(state="0.8")

    f.sources = Sources
    f.manager = f.make_manager()

    async def check():
        token, first = await f.capture()
        f.row.update(
            receiver="synthetic_receiver_two",
            entity_id="sensor.synthetic_phone_distance_to_synthetic_receiver_two",
        )
        _, second = await f.capture(token)
        assert len(f.manager.candidates) == 2
        for response in (first, second):
            saved = await f.manager.action(
                OWNER, {"action": "verify", "candidate_id": response["candidate"]["id"]}
            )
            assert saved["saved"]
        assert all(c["status"] == "verified" for c in f.manager.candidates)
        assert all(
            rec["cords"]["x"] == pytest.approx(300, abs=1e-6)
            for rec in get_bps_data(f.hass)["floor"][0]["receivers"]
        )

    run(check())


def test_verification_retry_never_reverts_a_later_saved_receiver_move():
    f = Fixture()

    async def check():
        _, result = await f.capture()
        action = {"action": "verify", "candidate_id": result["candidate"]["id"]}
        await f.manager.action(OWNER, action)
        live = get_bps_data(f.hass)["floor"][0]["receivers"][0]
        live["cords"] = {"x": 450.0, "y": 470.0}
        saves = len(f.hass._store_saves)
        retry = await f.manager.action(OWNER, action)
        assert retry["position"] == live["cords"]
        assert len(f.hass._store_saves) == saves
        assert live["cords"] == {"x": 450.0, "y": 470.0}

    run(check())


def test_explicit_saved_receiver_resolves_colocated_ambiguity_but_still_requires_its_fresh_proof():
    f = Fixture()
    f.floor["receivers"].append(
        {
            "entity_id": "synthetic_receiver_two",
            "scanner_mac": "aa:bb:cc:dd:ee:02",
            "scanner_config_entry_id": "synthetic_instance",
            "cords": {"x": 100.0, "y": 100.0},
        }
    )
    original = f.snapshot
    selected_stale = False

    def snapshot(hass, targets, now):
        result = original(hass, targets, now)
        row = copy.deepcopy(result["readings"][0])
        row.update(
            receiver="synthetic_receiver_two",
            entity_id="sensor.synthetic_phone_distance_to_synthetic_receiver_two",
        )
        if selected_stale:
            row.update(age_s=10.0, observed=now - 10)
        result["readings"].append(row)
        return result

    class Sources:
        def __init__(self, _hass):
            pass

        def floor_bindings(self, floor):
            return [
                {
                    "status": "matched",
                    "identity": (rec["scanner_config_entry_id"], rec["scanner_mac"]),
                }
                for rec in floor["receivers"]
            ]

        def source(self, phone, rec, binding, *, now):
            return "sensor." + phone + "_distance_to_" + rec[
                "entity_id"
            ], SimpleNamespace(state="0.8")

    f.sources, f.snapshot = Sources, snapshot
    f.manager = f.make_manager()

    async def check():
        nonlocal selected_stale
        token = await f.start()
        response = await f.poll(token)
        assert (
            response["capturing"] is False and "Several receivers" in response["status"]
        )
        with pytest.raises(ValueError, match="existing saved receiver"):
            await f.start(receiver="synthetic_forged_receiver")
        await f.manager.action(OWNER, {"action": "stop", "session": token})
        token = await f.start(receiver="synthetic_receiver_two")
        selected_stale = True
        response = await f.poll(token)
        assert response["capturing"] is False, (
            "fresh nearby first receiver cannot authorize the selected stale receiver"
        )
        selected_stale = False
        _, response = await f.capture(token)
        assert response["candidate"]["receiver"] == "synthetic_receiver_two"
        assert response["candidate"]["scanner_identity"] == [
            "synthetic_instance",
            "aa:bb:cc:dd:ee:02",
        ]

    run(check())
