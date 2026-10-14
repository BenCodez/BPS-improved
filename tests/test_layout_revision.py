"""Synthetic cross-client layout conflicts, including queued survey overwrites."""

import asyncio
import copy
import json
from types import SimpleNamespace as NS

import pytest

import bps
from bps import phone_survey
from bps.storage import BPS_FILE_LOCK, get_bps_data, layout_revision, save_bps_data
from conftest import make_hass
from test_phone_survey import Fixture, OWNER


def request(hass, layout, revision, **extra):
    async def post():
        data = {"coordinates": json.dumps(layout), **extra}
        if revision is not None:
            data["layout_revision"] = revision
        return data

    return NS(app={"hass": hass}, post=post)


def test_read_revision_matches_layout_and_successful_saves_advance_it(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        bps, "_scanner_diagnostics", lambda *_: {"matching": {"options": []}}
    )

    async def check():
        hass = make_hass()
        hass.config.path = lambda _path: str(tmp_path)
        response = await bps.BPSReadAPIText().get(NS(app={"hass": hass}))
        baseline = response.json_body["layout_revision"]
        assert baseline == layout_revision(get_bps_data(hass))
        layout = {"floor": [{"name": "Synthetic floor", "receivers": []}]}
        saved = await bps.BPSSaveAPIText().post(request(hass, layout, baseline))
        assert saved.status == 200
        revision = saved.json_body["layout_revision"]
        assert revision == layout_revision(layout) and revision != baseline
        assert saved.headers["X-BPS-Layout-Revision"] == revision
        # Consecutive fresh saves remain valid; JSON key order is immaterial.
        layout["floor"][0]["scale"] = 20
        assert (
            await bps.BPSSaveAPIText().post(request(hass, layout, revision))
        ).status == 200

    asyncio.run(check())


@pytest.mark.parametrize("revision", [None, "wrong"])
def test_missing_or_stale_revision_rejects_layout_and_map_mutation(tmp_path, revision):
    async def check():
        hass = make_hass()
        hass.config.path = lambda _path: str(tmp_path)
        original = {"floor": [{"name": "Synthetic floor", "receivers": []}]}
        await save_bps_data(hass, original)
        image = tmp_path / "existing.png"
        image.write_bytes(b"synthetic map")
        response = await bps.BPSSaveAPIText().post(
            request(hass, {"floor": []}, revision, remove="existing.png")
        )
        assert response.status == 409
        assert get_bps_data(hass) == original and image.read_bytes() == b"synthetic map"
        assert hass._store_backing["bps"] == original

    asyncio.run(check())


def test_full_save_queued_behind_verification_cannot_undo_its_committed_position(
    tmp_path, monkeypatch
):
    async def check():
        f = Fixture()
        _, result = await f.capture()
        candidate = result["candidate"]
        f.hass.config.path = lambda _path: str(tmp_path)
        old_layout = copy.deepcopy(get_bps_data(f.hass))
        baseline = layout_revision(old_layout)
        entered, release, queued = asyncio.Event(), asyncio.Event(), asyncio.Event()
        actual_save = phone_survey.save_bps_data

        async def hold_verification(hass, layout):
            entered.set()
            await release.wait()
            await actual_save(hass, layout)

        monkeypatch.setattr(phone_survey, "save_bps_data", hold_verification)
        verifying = asyncio.create_task(
            f.manager.action(
                OWNER, {"action": "verify", "candidate_id": candidate["id"]}
            )
        )
        await entered.wait()
        stale_request = request(f.hass, old_layout, baseline)
        post = stale_request.post

        async def posted():
            queued.set()
            return await post()

        stale_request.post = posted
        stale_save = asyncio.create_task(bps.BPSSaveAPIText().post(stale_request))
        await queued.wait()
        assert not stale_save.done()
        release.set()
        verification = await verifying
        assert (await stale_save).status == 409
        assert verification["previous_layout_revision"] == baseline
        assert verification["layout_revision"] == layout_revision(get_bps_data(f.hass))
        assert (
            get_bps_data(f.hass)["floor"][0]["receivers"][0]["cords"]
            == candidate["position"]
        )
        assert f.manager.candidates[0]["status"] == "verified"

    asyncio.run(check())


def test_retry_revision_cannot_hide_other_clients_layout_changes():
    async def check():
        f = Fixture()
        _, result = await f.capture()
        body = {"action": "verify", "candidate_id": result["candidate"]["id"]}
        baseline = layout_revision(get_bps_data(f.hass))
        first = await f.manager.action(OWNER, body)
        retry = await f.manager.action(OWNER, body)
        assert (
            first["previous_layout_revision"]
            == retry["previous_layout_revision"]
            == baseline
        )
        assert first["layout_revision"] == retry["layout_revision"]
        async with BPS_FILE_LOCK:
            changed = copy.deepcopy(get_bps_data(f.hass))
            changed["floor"][0]["receivers"][0]["height"] = 3
            await save_bps_data(f.hass, changed)
        retry = await f.manager.action(OWNER, body)
        assert retry["previous_layout_revision"] != baseline
        assert retry["layout_revision"] == layout_revision(changed)
        assert get_bps_data(f.hass) == changed

    asyncio.run(check())
