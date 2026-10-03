# tests/cloud/discover/test_discover.py — the Discover index.
#
# Pins: the SiteTemplate Discover fields (kind, audiences, live_url) default and
# round-trip through save / PATCH; the event sync (public save lists, PATCH
# private / delete / report-hide unlist, a re-sync never unhides a listing
# Discover reports hid, and an upsert that loses the insert race retries);
# ``list_public`` (only unhidden rows, every filter, cursor paging); the public
# wire is exactly the allow-list; ``use_listing`` returns the source result and
# counts one remix (not for the owner); ``report_listing`` (one per user, owner
# refused, dismissed reporters ignored, third reporter hides here and at the
# source); staff hide / unhide / feature with their audit rows; the periodic and
# startup reindex, its idempotence and its live_url refresh; the (hidden, _id)
# index; the source registry and the listeners for both sources. Slug rules
# live in test_discover_slug.py.
#
# ``RecordingBus.subscribe`` is a no-op (tests/cloud/conftest.py), so the sync
# tests replay the recorded site-template events into the real handler. The
# plan-gate and source-registry autouse fixtures are in conftest.py.
from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound, ValidationError
from pocketpaw_ee.cloud.discover import listeners, service, service_admin, sources
from pocketpaw_ee.cloud.discover.dto import PublicListingResponse
from pocketpaw_ee.cloud.models.audit_event import AuditEvent
from pocketpaw_ee.cloud.models.discover_listing import DiscoverListing
from pocketpaw_ee.cloud.models.pocket import Pocket as PocketDoc
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.cloud.models.site_template import SiteTemplate
from pocketpaw_ee.cloud.site_templates import service as templates

pytestmark = pytest.mark.usefixtures("mongo_db")

WS = "w1"
OTHER_WS = "w2"
OWNER = "u1"
STRANGERS = ("u3", "u4", "u5")

PUBLIC_KEYS = {
    "id",
    "slug",
    "source",
    "kind",
    "title",
    "description",
    "audiences",
    "featured",
    "preview_image_url",
    "live_url",
    "remix_count",
    "created_at",
    "media_kind",
    "media_url",
}
SYNCED = {"site_template.saved", "site_template.updated", "site_template.deleted"}


async def _site(**fields: Any) -> PocketDoc:
    doc = PocketDoc(
        workspace=WS,
        type="site",
        owner=OWNER,
        name="Bakery",
        engine="svelte",
        pattern="landing",
        source={"src/routes/+page.svelte": "<h1>Bakery</h1>\n"},
        rippleSpec={"ui": {"type": "flex", "id": "root", "children": []}},
        keeps_client_bundle=True,
        **fields,
    )
    await doc.insert()
    return doc


async def _template(src: PocketDoc | None = None, **body: Any) -> dict:
    src = src or await _site()
    body.setdefault("name", "Bakery template")
    return await templates.save_template(WS, OWNER, {"pocket_id": str(src.id), **body})


async def _sync(bus) -> None:
    """Deliver the recorded site-template events to the Discover handler."""
    events, bus.events[:] = list(bus.events), []
    for event in events:
        if event.type in SYNCED:
            await listeners.on_site_template_changed(event)


async def _listing(template_id: str) -> DiscoverListing | None:
    return await DiscoverListing.find_one({"source": "site_template", "source_id": template_id})


async def _upsert(source_id: str, **fields: Any) -> str:
    fields.setdefault("workspace", WS)
    fields.setdefault("owner", OWNER)
    fields.setdefault("kind", "site")
    fields.setdefault("title", f"Listing {source_id}")
    return await service_admin.upsert_from_source("site_template", source_id, fields)


# ---------------------------------------------------------------------------
# SiteTemplate Discover fields
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_template_discover_fields_default_and_round_trip() -> None:
    plain = await _template()
    assert (plain["kind"], plain["audiences"], plain["live_url"]) == ("site", [], None)

    src = await _site()
    await Site(
        workspace=WS, pocket_id=str(src.id), owner=OWNER, deployed=True, url="https://b.example"
    ).insert()
    meta = await _template(src, kind="tool", audiences=["shop", "design"])
    assert (meta["kind"], meta["audiences"], meta["live_url"]) == (
        "tool",
        ["shop", "design"],
        "https://b.example",
    )

    patched = await templates.update_template(
        WS, OWNER, meta["id"], {"kind": "game", "audiences": ["fun"]}
    )
    assert (patched["kind"], patched["audiences"]) == ("game", ["fun"])
    doc = await SiteTemplate.get(meta["id"])
    assert (doc.kind, doc.audiences, doc.live_url) == ("game", ["fun"], "https://b.example")
    listed = (await templates.list_templates(WS, OWNER, {}))["templates"]
    assert {t["id"]: t["kind"] for t in listed}[meta["id"]] == "game"


@pytest.mark.asyncio
async def test_live_url_is_none_for_an_undeployed_site() -> None:
    src = await _site()
    await Site(
        workspace=WS, pocket_id=str(src.id), owner=OWNER, deployed=False, url="https://draft"
    ).insert()
    assert (await _template(src))["live_url"] is None


# ---------------------------------------------------------------------------
# Event sync
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_follows_visibility_and_delete(recording_bus) -> None:
    private = await _template()
    await _sync(recording_bus)
    assert await _listing(private["id"]) is None

    meta = await _template(visibility="public", kind="tool", audiences=["shop"], description="d")
    await _sync(recording_bus)
    listing = await _listing(meta["id"])
    assert listing is not None
    assert (listing.title, listing.description, listing.kind, listing.audiences) == (
        "Bakery template",
        "d",
        "tool",
        ["shop"],
    )
    assert (listing.workspace, listing.owner) == (WS, OWNER)

    await templates.update_template(WS, OWNER, meta["id"], {"visibility": "private"})
    await _sync(recording_bus)
    assert await _listing(meta["id"]) is None

    await templates.update_template(WS, OWNER, meta["id"], {"visibility": "public"})
    await _sync(recording_bus)
    assert await _listing(meta["id"]) is not None

    await templates.delete_template(WS, OWNER, meta["id"])
    await _sync(recording_bus)
    assert await _listing(meta["id"]) is None


@pytest.mark.asyncio
async def test_sync_keeps_a_report_hidden_template_as_a_hidden_listing(recording_bus) -> None:
    """A /sites report-hide keeps the listing, hidden, so staff can unhide it."""
    meta = await _template(visibility="public")
    await _sync(recording_bus)
    assert await _listing(meta["id"]) is not None
    for user in STRANGERS:
        await templates.report_template(OTHER_WS, user, meta["id"], {"reason": "spam"})
    await _sync(recording_bus)
    assert (await _listing(meta["id"])).hidden is True
    assert (await service_admin.list_public())["items"] == []


async def _community_ids() -> list[str]:
    page = await templates.list_templates(OTHER_WS, "u9", {"scope": "public"})
    return [t["id"] for t in page["templates"]]


@pytest.mark.asyncio
async def test_discover_hide_reaches_the_template_and_survives_a_republish(
    recording_bus,
) -> None:
    meta = await _template(visibility="public")
    await _sync(recording_bus)
    listing_id = str((await _listing(meta["id"])).id)
    for user in STRANGERS:
        await service.report_listing(OTHER_WS, user, listing_id, {"reason": "spam"})
    await _sync(recording_bus)
    assert (await SiteTemplate.get(meta["id"])).hidden is True
    assert meta["id"] not in await _community_ids()  # hidden in the /sites tab too
    with pytest.raises(NotFound):
        await templates.use_template(OTHER_WS, "u9", meta["id"], {})

    # The owner can't launder the hide by going private and public again.
    await templates.update_template(WS, OWNER, meta["id"], {"visibility": "private"})
    await _sync(recording_bus)
    await templates.update_template(WS, OWNER, meta["id"], {"visibility": "public"})
    await _sync(recording_bus)
    assert (await SiteTemplate.get(meta["id"])).hidden is True
    assert (await service_admin.list_public())["items"] == []
    relisted = await _listing(meta["id"])
    assert relisted.hidden is True

    # Staff unhide brings both back, once, with no duplicate row.
    await service_admin.set_hidden(str(relisted.id), False)
    await _sync(recording_bus)
    template = await SiteTemplate.get(meta["id"])
    assert (template.hidden, template.reports) == (False, [])
    assert [i["id"] for i in (await service_admin.list_public())["items"]] == [str(relisted.id)]
    assert await DiscoverListing.find({"source_id": meta["id"]}).count() == 1
    assert meta["id"] in await _community_ids()


@pytest.mark.asyncio
async def test_staff_hide_reaches_the_template(recording_bus) -> None:
    meta = await _template(visibility="public")
    await _sync(recording_bus)
    listing_id = str((await _listing(meta["id"])).id)
    await service_admin.set_hidden(listing_id, True)
    await _sync(recording_bus)
    assert (await SiteTemplate.get(meta["id"])).hidden is True
    assert (await _listing(meta["id"])).hidden is True


@pytest.mark.asyncio
async def test_upsert_that_loses_the_insert_race_updates_the_winner(monkeypatch) -> None:
    from pymongo.errors import DuplicateKeyError

    winner_id = await _upsert("race", title="Winner")
    real = DiscoverListing.get_pymongo_collection
    raised: list[bool] = []

    class _Racing:
        """The first upsert=True call raises as if a sibling inserted first."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def find_one_and_update(self, *args: Any, **kwargs: Any) -> Any:
            if kwargs.get("upsert") and not raised:
                raised.append(True)
                raise DuplicateKeyError("E11000 duplicate key")
            return await self._inner.find_one_and_update(*args, **kwargs)

    monkeypatch.setattr(
        DiscoverListing, "get_pymongo_collection", classmethod(lambda cls: _Racing(real()))
    )
    assert await _upsert("race", title="Loser") == winner_id
    assert raised == [True]
    assert (await DiscoverListing.get(winner_id)).title == "Loser"
    assert await DiscoverListing.find({"source_id": "race"}).count() == 1


@pytest.mark.asyncio
async def test_resync_keeps_discover_owned_state(recording_bus) -> None:
    meta = await _template(visibility="public")
    await _sync(recording_bus)
    listing = await _listing(meta["id"])
    for user in STRANGERS:
        await service.report_listing(OTHER_WS, user, str(listing.id), {"reason": "spam"})
    await service_admin.set_featured(str(listing.id), True)

    await templates.update_template(WS, OWNER, meta["id"], {"name": "Renamed"})
    await _sync(recording_bus)
    after = await _listing(meta["id"])
    assert after.title == "Renamed"
    assert (after.hidden, after.featured, len(after.reports)) == (True, True, 3)


@pytest.mark.asyncio
async def test_a_failing_sync_is_swallowed(monkeypatch) -> None:
    async def _boom(_source: str, _template_id: str) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(service_admin, "sync_source", _boom)
    event = type("E", (), {"type": "site_template.saved", "data": {"id": "x"}})()
    await listeners.on_site_template_changed(event)  # no raise


def test_register_discover_listeners_subscribes_the_three_events(monkeypatch) -> None:
    subscribed: list[str] = []

    class _Bus:
        def subscribe(self, event_type: str, _handler) -> None:
            subscribed.append(event_type)

    monkeypatch.setattr(listeners, "get_bus", lambda: _Bus())
    listeners.register_discover_listeners()
    studio = {e.replace("site_", "studio_") for e in SYNCED}
    assert set(subscribed) == SYNCED | studio


@pytest.mark.asyncio
async def test_periodic_reindex_starts_once_runs_and_stops(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    ran = asyncio.Event()

    async def _reindex(source: str) -> dict:
        assert source == "site_template"
        ran.set()
        return {}

    monkeypatch.setattr(listeners, "REINDEX_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(service_admin, "reindex", _reindex)
    app = SimpleNamespace(state=SimpleNamespace())
    await listeners.start_discover_reindex(app)
    task = app.state.discover_reindex_task
    await listeners.start_discover_reindex(app)
    assert app.state.discover_reindex_task is task  # second start is a no-op
    await asyncio.wait_for(ran.wait(), 1)
    await listeners.stop_discover_reindex(app)
    assert app.state.discover_reindex_task is None and task.cancelled()


@pytest.mark.asyncio
async def test_reindex_loop_runs_its_first_pass_at_once(monkeypatch) -> None:
    import asyncio
    from types import SimpleNamespace

    ran = asyncio.Event()

    async def _reindex(source: str) -> dict:
        ran.set()
        return {}

    monkeypatch.setattr(listeners, "REINDEX_INTERVAL_SECONDS", 3600)
    monkeypatch.setattr(service_admin, "reindex", _reindex)
    app = SimpleNamespace(state=SimpleNamespace())
    await listeners.start_discover_reindex(app)
    try:
        await asyncio.wait_for(ran.wait(), 1)  # no 30-minute wait first
    finally:
        await listeners.stop_discover_reindex(app)


@pytest.mark.asyncio
async def test_startup_backfill_runs_one_pass_and_logs_a_failure(monkeypatch, caplog) -> None:
    import asyncio
    from types import SimpleNamespace

    calls: list[str] = []

    async def _reindex(source: str) -> dict:
        calls.append(source)
        raise RuntimeError("boom")

    monkeypatch.setattr(service_admin, "reindex", _reindex)
    app = SimpleNamespace(state=SimpleNamespace())
    await listeners.start_discover_backfill(app)  # returns before the pass runs
    await asyncio.wait_for(app.state.discover_backfill_task, 1)
    assert calls == ["site_template", "studio_template"]
    assert "discover: reindex failed" in caplog.text
    await listeners.stop_discover_backfill(app)  # already done: a no-op


def test_mount_cloud_backfills_when_the_scheduler_is_off() -> None:
    import inspect

    from pocketpaw_ee import cloud

    source = inspect.getsource(cloud.mount_cloud)
    scheduled = source.index("async def _stop_discover_reindex")
    backfill = source.index("async def _start_discover_backfill")
    assert scheduled < source.index("else:", scheduled) < backfill


# ---------------------------------------------------------------------------
# Public reads
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_public_filters_and_hides() -> None:
    await _upsert("a", kind="site", title="Corner Bakery", audiences=["shop"])
    b = await _upsert("b", kind="tool", title="Color tool", audiences=["design", "everyone"])
    await _upsert("c", kind="game", title="Snake", description="A BAKERY game", audiences=["fun"])
    hidden = await _upsert("d", kind="site", title="Hidden bakery")
    await service_admin.set_hidden(hidden, True)
    await service_admin.set_featured(b, True)

    async def titles(**query: Any) -> list[str]:
        return [i["title"] for i in (await service_admin.list_public(query))["items"]]

    assert await titles() == ["Snake", "Color tool", "Corner Bakery"]
    assert await titles(kind="tool") == ["Color tool"]
    assert await titles(audience="design") == ["Color tool"]
    assert await titles(q="bakery") == ["Snake", "Corner Bakery"]
    assert await titles(q="b.k") == []  # regex metacharacters are escaped
    assert await titles(featured=True) == ["Color tool"]
    assert await titles(source="other") == []
    with pytest.raises(NotFound):
        await service_admin.get_public(hidden)


@pytest.mark.asyncio
async def test_list_public_cursor_pages() -> None:
    for i in range(5):
        await _upsert(f"p{i}", title=f"T{i}")
    first = await service_admin.list_public({"limit": 2})
    second = await service_admin.list_public({"limit": 2, "cursor": first["next_cursor"]})
    third = await service_admin.list_public({"limit": 2, "cursor": second["next_cursor"]})
    seen = [i["title"] for page in (first, second, third) for i in page["items"]]
    assert seen == ["T4", "T3", "T2", "T1", "T0"]
    assert third["next_cursor"] is None
    with pytest.raises(ValidationError):
        await service_admin.list_public({"cursor": "nope"})


@pytest.mark.asyncio
async def test_public_wire_is_exactly_the_allow_list() -> None:
    listing_id = await _upsert("a", audiences=["shop"], live_url="https://x")
    card = await service_admin.get_public(listing_id)
    assert set(card) == PUBLIC_KEYS == set(PublicListingResponse.model_fields)
    assert (await service_admin.list_public())["items"] == [card]


# ---------------------------------------------------------------------------
# use / report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_use_listing_returns_the_pocket_and_counts_once(recording_bus) -> None:
    meta = await _template(visibility="public")
    await _sync(recording_bus)
    listing_id = str((await _listing(meta["id"])).id)

    used = await service.use_listing(OTHER_WS, "u3", listing_id, name="My bakery")
    assert used["source"] == "site_template"
    pocket = await PocketDoc.get(used["result"]["pocket_id"])
    assert (pocket.workspace, pocket.owner, pocket.name) == (OTHER_WS, "u3", "My bakery")
    assert (await DiscoverListing.get(listing_id)).remix_count == 1
    assert [e.type for e in recording_bus.events].count("discover.listing.used") == 1

    await service_admin.set_hidden(listing_id, True)
    with pytest.raises(NotFound):
        await service.use_listing(OTHER_WS, "u3", listing_id)
    assert (await DiscoverListing.get(listing_id)).remix_count == 1


@pytest.mark.asyncio
async def test_using_your_own_listing_does_not_count_a_remix() -> None:
    meta = await _template(visibility="public")
    await service_admin.sync_source("site_template", meta["id"])
    listing_id = str((await _listing(meta["id"])).id)

    used = await service.use_listing(WS, OWNER, listing_id)
    assert used["result"]["pocket_id"]
    assert (await DiscoverListing.get(listing_id)).remix_count == 0
    audit = await AuditEvent.find({"action": "discover.listing_used"}).to_list()
    assert [(a.workspace, a.actor_id) for a in audit] == [(WS, OWNER)]


@pytest.mark.asyncio
async def test_failed_use_does_not_count() -> None:
    # A listing whose template is gone (the sync has not caught up yet).
    listing_id = await _upsert("650000000000000000000000")
    with pytest.raises(NotFound):
        await service.use_listing(OTHER_WS, "u3", listing_id)
    assert (await DiscoverListing.get(listing_id)).remix_count == 0


@pytest.mark.asyncio
async def test_report_listing_hides_at_three_distinct_reporters() -> None:
    listing_id = await _upsert("a")
    with pytest.raises(Forbidden):
        await service.report_listing(WS, OWNER, listing_id, {"reason": "mine"})

    await service.report_listing(OTHER_WS, "u3", listing_id, {"reason": "spam"})
    await service.report_listing(OTHER_WS, "u3", listing_id, {"reason": "again"})
    await service.report_listing(OTHER_WS, "u4", listing_id, {"reason": "spam"})
    doc = await DiscoverListing.get(listing_id)
    assert ([r["user"] for r in doc.reports], doc.hidden) == (["u3", "u4"], False)

    await service.report_listing(OTHER_WS, "u5", listing_id, {"reason": "spam"})
    assert (await DiscoverListing.get(listing_id)).hidden is True
    with pytest.raises(NotFound):
        await service.report_listing(OTHER_WS, "u6", listing_id, {"reason": "late"})


@pytest.mark.asyncio
async def test_unhide_clears_reports_and_hide_keeps_them() -> None:
    listing_id = await _upsert("a")
    for user in STRANGERS:
        await service.report_listing(OTHER_WS, user, listing_id, {"reason": "spam"})
    doc = await DiscoverListing.get(listing_id)
    assert (doc.hidden, len(doc.reports)) == (True, 3)

    await service_admin.set_hidden(listing_id, True)
    assert len((await DiscoverListing.get(listing_id)).reports) == 3  # hiding keeps them

    await service_admin.set_hidden(listing_id, False)
    doc = await DiscoverListing.get(listing_id)
    assert (doc.hidden, doc.reports) == (False, [])

    # One fresh report no longer re-hides it.
    await service.report_listing(OTHER_WS, "u6", listing_id, {"reason": "spam"})
    assert (await DiscoverListing.get(listing_id)).hidden is False


@pytest.mark.asyncio
async def test_dismissed_reporters_cannot_re_hide_after_an_unhide() -> None:
    listing_id = await _upsert("a")
    for user in STRANGERS:
        await service.report_listing(OTHER_WS, user, listing_id, {"reason": "spam"})
    await service_admin.set_hidden(listing_id, False)
    doc = await DiscoverListing.get(listing_id)
    assert (doc.reports, sorted(doc.dismissed_reporters)) == ([], list(STRANGERS))

    for user in STRANGERS:  # the same accounts again: ignored
        await service.report_listing(OTHER_WS, user, listing_id, {"reason": "spam"})
    doc = await DiscoverListing.get(listing_id)
    assert (doc.hidden, doc.reports) == (False, [])

    await service.report_listing(OTHER_WS, "u6", listing_id, {"reason": "spam"})
    assert [r["user"] for r in (await DiscoverListing.get(listing_id)).reports] == ["u6"]


@pytest.mark.asyncio
async def test_moderation_and_use_write_audit_rows(recording_bus) -> None:
    meta = await _template(visibility="public")
    await _sync(recording_bus)
    listing_id = str((await _listing(meta["id"])).id)
    used = await service.use_listing(OTHER_WS, "u3", listing_id)
    await service_admin.set_featured(listing_id, True)
    await service_admin.set_hidden(listing_id, True)
    await service_admin.set_hidden(listing_id, False)

    for action, ws, actor, metadata in [
        ("discover.listing_used", OTHER_WS, "u3", {"source": "site_template"}),
        ("discover.listing_featured", WS, "staff", {"featured": "True"}),
        ("discover.listing_hidden", WS, "staff", {"hidden": "True"}),
        ("discover.listing_unhidden", WS, "staff", {"hidden": "False"}),
    ]:
        rows = await AuditEvent.find(AuditEvent.action == action).to_list()
        assert len(rows) == 1, action
        row = rows[0]
        assert (row.workspace, row.actor_id, row.target_type, row.target_id) == (
            ws,
            actor,
            "discover_listing",
            listing_id,
        ), action
        assert row.metadata == metadata, action
    assert used["result"]["pocket_id"]


def test_public_list_sort_has_a_hidden_id_index() -> None:
    keys = [list(index.document["key"].items()) for index in DiscoverListing.Settings.indexes]
    assert [("hidden", 1), ("_id", -1)] in keys


# ---------------------------------------------------------------------------
# reindex / registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reindex_is_idempotent() -> None:
    public = await _template(visibility="public", kind="game")
    await _template(name="Private one")
    await _upsert("650000000000000000000000")  # a template that no longer exists

    first = await service_admin.reindex("site_template")
    rows = await DiscoverListing.find_all().to_list()
    second = await service_admin.reindex("site_template")
    again = await DiscoverListing.find_all().to_list()

    assert first == {
        "source": "site_template",
        "created": 1,
        "updated": 0,
        "unchanged": 0,
        "removed": 1,
    }
    assert (second["created"], second["updated"], second["unchanged"], second["removed"]) == (
        0,
        0,
        1,
        0,
    )
    assert [(r.source_id, r.kind) for r in rows] == [(public["id"], "game")]
    assert [(r.id, r.source_id) for r in again] == [(rows[0].id, public["id"])]
    with pytest.raises(ValidationError):
        await service_admin.reindex("nope")


@pytest.mark.asyncio
async def test_a_no_op_reindex_writes_and_emits_nothing(recording_bus) -> None:
    meta = await _template(visibility="public")
    await service_admin.reindex("site_template")
    before = await _listing(meta["id"])
    recording_bus.events[:] = []

    result = await service_admin.reindex("site_template")
    assert (result["updated"], result["unchanged"]) == (0, 1)
    assert recording_bus.events == []
    assert (await _listing(meta["id"])).updatedAt == before.updatedAt

    await (await SiteTemplate.get(meta["id"])).set({"description": "Fresh bread"})
    result = await service_admin.reindex("site_template")
    assert (result["updated"], result["unchanged"]) == (1, 0)
    assert (await _listing(meta["id"])).description == "Fresh bread"
    assert [e.type for e in recording_bus.events] == ["discover.listing.upserted"]


@pytest.mark.asyncio
async def test_reindex_keeps_a_hidden_public_template_as_a_hidden_listing() -> None:
    meta = await _template(visibility="public")
    doc = await SiteTemplate.get(meta["id"])
    await doc.set({"hidden": True})

    await service_admin.reindex("site_template")
    listing = await _listing(meta["id"])
    assert listing is not None and listing.hidden is True
    assert (await service_admin.list_public())["items"] == []


@pytest.mark.asyncio
async def test_reindex_refreshes_a_stale_live_url() -> None:
    src = await _site()
    site = Site(workspace=WS, pocket_id=str(src.id), owner=OWNER, deployed=True, url="https://a")
    await site.insert()
    meta = await _template(src, visibility="public")
    await service_admin.reindex("site_template")
    assert (await _listing(meta["id"])).live_url == "https://a"

    await site.set({"url": "https://renamed"})  # a slug change emits no event
    await service_admin.reindex("site_template")
    assert (await _listing(meta["id"])).live_url == "https://renamed"
    assert (await SiteTemplate.get(meta["id"])).live_url == "https://renamed"

    await site.set({"deployed": False})  # unpublished
    await service_admin.reindex("site_template")
    assert (await _listing(meta["id"])).live_url is None
    assert (await SiteTemplate.get(meta["id"])).live_url is None


@pytest.mark.asyncio
async def test_registry(monkeypatch) -> None:
    monkeypatch.setattr(sources, "_SOURCES", dict(sources._SOURCES))
    with pytest.raises(NotFound):
        sources.get_source("nope")

    async def _use(*_args: Any) -> dict:
        return {"x": 1}

    sources.register_source(sources.DiscoverSource("probe", frozenset({"tool"}), _use))
    sources.register_source(sources.DiscoverSource("probe", frozenset({"game"}), _use))
    assert sources.get_source("probe").kinds == frozenset({"game"})
    assert [s.name for s in sources.registered_sources()].count("probe") == 1
    with pytest.raises(ValidationError):
        await _upsert("a", kind="widget")
