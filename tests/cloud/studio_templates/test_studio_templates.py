# tests/cloud/studio_templates/test_studio_templates.py — studio templates (ST-2).
#
# Created 2026-10-02 (feat/studio-templates). Pins: publish snapshots one asset of
# a succeeded generation (cover from the asset, recipe with input-image fields
# stripped, ``uses_input_images`` from ``inputImageCount``, audio -> music);
# another workspace's generation and an unknown asset are NotFound, a queued /
# failed one is a conflict; the Discover sync lists a public template with
# absolute media URLs and unlists it on PATCH -> private and DELETE; three
# Discover reports hide the listing AND the template; ``use`` returns the recipe
# and writes nothing to studio; ``reindex("studio_template")`` rebuilds listings;
# PATCH / DELETE are owner-only.
#
# ``RecordingBus.subscribe`` is a no-op, so the sync tests replay the recorded
# studio-template events into the real Discover handler.
from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud._core.errors import ConflictError, NotFound
from pocketpaw_ee.cloud.discover import listeners, service, service_admin
from pocketpaw_ee.cloud.models.discover_listing import DiscoverListing
from pocketpaw_ee.cloud.models.studio_generation import StudioGeneration
from pocketpaw_ee.cloud.models.studio_template import StudioTemplate
from pocketpaw_ee.cloud.studio_templates import service as templates

pytestmark = [pytest.mark.usefixtures("mongo_db"), pytest.mark.asyncio]

WS, OTHER_WS, OWNER = "w1", "w2", "u1"
BASE = "https://paw.example"  # conftest ``public_base`` (set with a trailing slash)
STRANGERS = ("u3", "u4", "u5")
SYNCED = {"studio_template.saved", "studio_template.updated", "studio_template.deleted"}


async def _generation(gen_id: str = "g1", **fields: Any) -> StudioGeneration:
    kind = fields.pop("kind", "image")
    params = {"kind": kind, "model": "flux", "aspectRatio": "1:1", "count": 1, "seed": 7}
    params.update(fields.pop("params", {}))
    doc = StudioGeneration(
        workspace=fields.pop("workspace", WS),
        generation_id=gen_id,
        prompt="a red fox",
        status=fields.pop("status", "succeeded"),
        kind=kind,
        model="flux",
        params=params,
        assets=fields.pop(
            "assets",
            [
                {"id": "a1", "url": "/api/v1/media/fox.png", "mime": "image/png", "width": 512},
                {"id": "a2", "url": "/api/v1/media/fox2.png", "mime": "image/png"},
            ],
        ),
        created_at_ms=1,
        **fields,
    )
    await doc.insert()
    return doc


async def _publish(gen_id: str = "g1", **body: Any) -> dict:
    body.setdefault("title", "Fox")
    return await templates.publish_template(WS, OWNER, {"generation_id": gen_id, **body})


async def _sync(bus) -> None:
    events, bus.events[:] = list(bus.events), []
    for event in events:
        if event.type in SYNCED:
            await listeners.on_studio_template_changed(event)


async def _listing(template_id: str) -> DiscoverListing | None:
    return await DiscoverListing.find_one({"source": "studio_template", "source_id": template_id})


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


async def test_publish_snapshots_the_asset_and_strips_input_images(recording_bus) -> None:
    await _generation(params={"inputImageCount": 2, "styleId": "noir"})
    meta = await _publish(asset_id="a2", audiences=["design"], visibility="workspace")

    assert meta["kind"] == "image" and meta["owner"] == OWNER
    assert meta["cover"]["url"] == "/api/v1/media/fox2.png"  # relative on the template
    assert meta["recipe"]["prompt"] == "a red fox" and meta["recipe"]["model"] == "flux"
    assert "inputImageCount" not in meta["recipe"]["params"]
    assert meta["recipe"]["params"]["styleId"] == "noir"
    assert meta["uses_input_images"] is True
    [event] = [e for e in recording_bus.events if e.type == "studio_template.saved"]
    assert (event.data["id"], event.data["user_id"]) == (meta["id"], OWNER)


async def test_strip_input_images_drops_every_input_ref_or_upload_key() -> None:
    params = {
        "inputImageCount": 1,
        "inputImageUrls": ["x"],
        "referenceImages": ["y"],
        "refs": ["z"],
        "uploadIds": ["u"],
        "seed": 3,
        "lightRig": {"k": 1},
    }
    assert templates._strip_input_images(params) == {"seed": 3, "lightRig": {"k": 1}}


async def test_audio_publishes_as_music_without_input_images() -> None:
    await _generation(
        "g2",
        kind="audio",
        assets=[{"id": "m1", "url": "/api/v1/media/song.mp3", "mime": "audio/mpeg"}],
    )
    meta = await _publish("g2")
    assert (meta["kind"], meta["recipe"]["kind"], meta["uses_input_images"]) == (
        "music",
        "audio",
        False,
    )


async def test_publish_refuses_other_workspace_unknown_asset_and_unfinished() -> None:
    await _generation("theirs", workspace=OTHER_WS)
    with pytest.raises(NotFound):
        await _publish("theirs")
    with pytest.raises(NotFound):
        await _publish("missing")
    await _generation("g1")
    with pytest.raises(NotFound):
        await _publish("g1", asset_id="nope")
    for status in ("queued", "running", "failed"):
        await _generation(f"g-{status}", status=status)
        with pytest.raises(ConflictError):
            await _publish(f"g-{status}")
    assert await StudioTemplate.count() == 0


async def test_patch_and_delete_are_owner_only() -> None:
    await _generation()
    meta = await _publish()
    with pytest.raises(NotFound):
        await templates.update_template(WS, "u2", meta["id"], {"title": "Mine now"})
    with pytest.raises(NotFound):
        await templates.delete_template(OTHER_WS, OWNER, meta["id"])
    patched = await templates.update_template(WS, OWNER, meta["id"], {"title": "Fox 2"})
    assert patched["title"] == "Fox 2"
    assert [t["id"] for t in (await templates.list_templates(WS, OWNER))["templates"]] == [
        meta["id"]
    ]
    assert (await templates.list_templates(WS, "u2"))["templates"] == []


# ---------------------------------------------------------------------------
# Discover sync
# ---------------------------------------------------------------------------


async def test_public_lists_with_absolute_urls_private_and_delete_unlist(recording_bus) -> None:
    await _generation()
    private = await _publish()
    await _sync(recording_bus)
    assert await _listing(private["id"]) is None

    meta = await _publish(visibility="public", description="d", audiences=["fun"])
    await _sync(recording_bus)
    listing = await _listing(meta["id"])
    assert listing is not None
    assert (listing.kind, listing.title, listing.audiences, listing.live_url) == (
        "image",
        "Fox",
        ["fun"],
        None,
    )
    assert listing.preview_image_url == f"{BASE}/api/v1/media/fox.png"
    assert (listing.media_kind, listing.media_url) == ("image", f"{BASE}/api/v1/media/fox.png")
    card = (await service_admin.list_public({"source": "studio_template"}))["items"][0]
    assert card["media_url"] == f"{BASE}/api/v1/media/fox.png"

    await templates.update_template(WS, OWNER, meta["id"], {"visibility": "private"})
    await _sync(recording_bus)
    assert await _listing(meta["id"]) is None

    await templates.update_template(WS, OWNER, meta["id"], {"visibility": "public"})
    await _sync(recording_bus)
    assert await _listing(meta["id"]) is not None
    await templates.delete_template(WS, OWNER, meta["id"])
    await _sync(recording_bus)
    assert await _listing(meta["id"]) is None


async def test_video_preview_is_the_poster_and_music_has_none(recording_bus) -> None:
    await _generation(
        "v",
        kind="video",
        assets=[
            {
                "id": "v1",
                "url": "/api/v1/media/clip.mp4",
                "mime": "video/mp4",
                "posterUrl": "/api/v1/media/clip.jpg",
            }
        ],
    )
    await _generation(
        "m", kind="audio", assets=[{"id": "m1", "url": "/api/v1/media/s.mp3", "mime": "audio/mpeg"}]
    )
    video = await _publish("v", visibility="public")
    music = await _publish("m", visibility="public")
    await _sync(recording_bus)
    v, m = await _listing(video["id"]), await _listing(music["id"])
    assert (v.preview_image_url, v.media_kind, v.media_url) == (
        f"{BASE}/api/v1/media/clip.jpg",
        "video",
        f"{BASE}/api/v1/media/clip.mp4",
    )
    assert (m.kind, m.preview_image_url, m.media_kind, m.media_url) == (
        "music",
        None,
        "audio",
        f"{BASE}/api/v1/media/s.mp3",
    )


async def test_three_discover_reports_hide_listing_and_template(recording_bus) -> None:
    await _generation()
    meta = await _publish(visibility="public")
    await _sync(recording_bus)
    listing_id = str((await _listing(meta["id"])).id)
    for user in STRANGERS:
        await service.report_listing(OTHER_WS, user, listing_id, {"reason": "spam"})
    await _sync(recording_bus)

    assert (await _listing(meta["id"])).hidden is True
    assert (await StudioTemplate.get(meta["id"])).hidden is True
    assert (await service_admin.list_public())["items"] == []
    # A private -> public round trip doesn't launder the hide.
    await templates.update_template(WS, OWNER, meta["id"], {"visibility": "private"})
    await templates.update_template(WS, OWNER, meta["id"], {"visibility": "public"})
    await _sync(recording_bus)
    assert (await _listing(meta["id"])).hidden is True


async def test_use_returns_the_recipe_and_writes_nothing_to_studio(recording_bus) -> None:
    await _generation(params={"inputImageCount": 1})
    meta = await _publish(visibility="public")
    await _sync(recording_bus)
    listing_id = str((await _listing(meta["id"])).id)
    before = (await StudioGeneration.count(), await StudioTemplate.count())

    out = await service.use_listing(OTHER_WS, "u3", listing_id)

    assert out["source"] == "studio_template"
    assert out["result"]["uses_input_images"] is True
    assert out["result"]["recipe"]["prompt"] == "a red fox"
    assert "inputImageCount" not in out["result"]["recipe"]["params"]
    assert (await StudioGeneration.count(), await StudioTemplate.count()) == before
    assert await StudioGeneration.find({"workspace": OTHER_WS}).count() == 0


async def test_use_of_a_hidden_template_is_not_found() -> None:
    await _generation()
    meta = await _publish(visibility="public")
    await service_admin.sync_source("studio_template", meta["id"])
    listing_id = str((await _listing(meta["id"])).id)
    await StudioTemplate.get_pymongo_collection().update_one(
        {"_id": (await StudioTemplate.get(meta["id"])).id}, {"$set": {"hidden": True}}
    )
    with pytest.raises(NotFound):
        await service.use_listing(OTHER_WS, "u3", listing_id)


async def test_reindex_rebuilds_studio_listings() -> None:
    await _generation()
    public = await _publish(visibility="public")
    await _publish()  # private: never listed
    await DiscoverListing.find({"source": "studio_template"}).delete()

    first = await service_admin.reindex("studio_template")
    assert (first["created"], first["removed"]) == (1, 0)
    assert (await _listing(public["id"])).media_url == f"{BASE}/api/v1/media/fox.png"
    again = await service_admin.reindex("studio_template")
    assert (again["created"], again["updated"], again["unchanged"]) == (0, 0, 1)
