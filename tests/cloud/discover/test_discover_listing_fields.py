# tests/cloud/discover/test_discover_listing_fields.py — the listing fields the
# public library pages need: a slug that never moves, ``updated_at``, ``tags``
# (+ the ``tag`` filter), the opt-in ``creator`` and ``remixed_from``.
#
# Pins: a template taken private (or deleted) withdraws its listing instead of
# deleting it, so a re-publish after a rename comes back with the SAME id and
# slug, a stranger's same-titled listing never inherits the shared slug, and the
# withdrawn row holds no source content and stays off every public and staff
# read; ``updated_at`` moves on a content change and not on a no-op re-sync;
# tags are normalised on write (template save and upsert), capped at 8, refused
# outside ``[a-z0-9-]``, and filter ``GET /discover?tag=`` case-insensitively; the
# creator (name + ``avatar_url``) shows only when opted in, never falls back to
# the email, carries the avatar only when it is an absolute http(s) URL, and a
# list page resolves creators and parents in one batch read each; a new site
# template shows its creator by default, a PATCH without ``show_creator`` keeps
# the stored value, and a row saved before the field existed stays anonymous
# through a sync and a reindex; a site made with
# "use" and saved as a public template points back at the listing it came from,
# and that link goes null when the parent is hidden or withdrawn.
from __future__ import annotations

from typing import Any

import pytest
from beanie import PydanticObjectId
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.auth import service as auth_service
from pocketpaw_ee.cloud.discover import service, service_admin
from pocketpaw_ee.cloud.discover.dto import ListPublicListingsRequest, UpsertListingRequest
from pocketpaw_ee.cloud.models.discover_listing import DiscoverListing
from pocketpaw_ee.cloud.models.pocket import Pocket as PocketDoc
from pocketpaw_ee.cloud.models.site_template import SiteTemplate
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.site_templates import service as templates
from pydantic import ValidationError

pytestmark = [pytest.mark.usefixtures("mongo_db"), pytest.mark.asyncio]

WS, OTHER_WS = "w1", "w2"


async def _user(full_name: str = "", email: str = "maker@example.com", avatar: str = "") -> str:
    user = User(email=email, hashed_password="x", is_active=True, is_verified=True)
    user.full_name = full_name
    user.avatar = avatar
    await user.insert()
    return str(user.id)


async def _site(owner: str, workspace: str = WS, **fields: Any) -> PocketDoc:
    doc = PocketDoc(
        workspace=workspace,
        type="site",
        owner=owner,
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


async def _publish(owner: str = "u1", workspace: str = WS, **body: Any) -> dict:
    """A public site template, synced into Discover."""
    pocket_id = body.pop("pocket_id", None) or str((await _site(owner, workspace)).id)
    body.setdefault("name", "Bakery")
    meta = await templates.save_template(
        workspace, owner, {"pocket_id": pocket_id, "visibility": "public", **body}
    )
    await service_admin.sync_source("site_template", meta["id"])
    return meta


async def _row(template_id: str) -> DiscoverListing | None:
    """The raw listing row, withdrawn or not."""
    return await DiscoverListing.find_one({"source": "site_template", "source_id": template_id})


async def _upsert(source_id: str, **fields: Any) -> str:
    fields.setdefault("workspace", WS)
    fields.setdefault("owner", "u1")
    fields.setdefault("kind", "site")
    fields.setdefault("title", f"Listing {source_id}")
    return await service_admin.upsert_from_source("site_template", source_id, fields)


# ---------------------------------------------------------------------------
# Slug stability
# ---------------------------------------------------------------------------


async def test_unpublish_rename_republish_keeps_the_id_and_slug() -> None:
    meta = await _publish(name="Bakery")
    first = await service_admin.get_public("bakery")
    assert first["slug"] == "bakery"

    # Private: the listing is withdrawn, not deleted, and gone from every read.
    await templates.update_template(WS, "u1", meta["id"], {"visibility": "private"})
    await service_admin.sync_source("site_template", meta["id"])
    row = await _row(meta["id"])
    assert row is not None and row.withdrawn is True and row.slug == "bakery"
    for read in (service_admin.get_public("bakery"), service_admin.get_public(first["id"])):
        with pytest.raises(NotFound):
            await read
    assert (await service_admin.list_public())["items"] == []
    assert (await service_admin.list_all())["items"] == []
    with pytest.raises(NotFound):
        await service.use_listing(OTHER_WS, "u9", first["id"])

    # A stranger's same-titled listing meanwhile does not inherit the slug.
    stranger = await _publish(owner="u2", workspace=OTHER_WS, name="Bakery")
    assert (await _row(stranger["id"])).slug == "bakery-2"

    # Renamed and re-published: same row, same id, same slug.
    await templates.update_template(
        WS, "u1", meta["id"], {"name": "Cake shop", "visibility": "public"}
    )
    await service_admin.sync_source("site_template", meta["id"])
    again = await service_admin.get_public("bakery")
    assert (again["id"], again["slug"], again["title"]) == (first["id"], "bakery", "Cake shop")
    assert await DiscoverListing.find({"source_id": meta["id"]}).count() == 1


async def test_a_withdrawn_row_keeps_no_source_content() -> None:
    meta = await _publish(description="Fresh bread", tags=["bread"], show_creator=True)
    await templates.delete_template(WS, "u1", meta["id"])
    await service_admin.sync_source("site_template", meta["id"])

    row = await _row(meta["id"])
    assert row.withdrawn is True
    assert (row.title, row.description, row.tags, row.show_creator, row.live_url) == (
        "",
        "",
        [],
        False,
        None,
    )
    # A deleted source keeps its slug reserved: a new "Bakery" takes the next one.
    other = await _publish(name="Bakery")
    assert (await _row(other["id"])).slug == "bakery-2"
    # And withdrawing twice is a no-op.
    assert await service_admin.remove_from_source("site_template", meta["id"]) is False


async def test_reindex_withdraws_then_revives_in_place() -> None:
    meta = await _publish()
    listing_id = str((await _row(meta["id"])).id)
    await templates.update_template(WS, "u1", meta["id"], {"visibility": "private"})

    first = await service_admin.reindex("site_template")
    assert (first["created"], first["removed"]) == (0, 1)
    assert (await _row(meta["id"])).withdrawn is True
    again = await service_admin.reindex("site_template")
    assert again["removed"] == 0  # an already withdrawn row is not stale twice

    await templates.update_template(WS, "u1", meta["id"], {"visibility": "public"})
    revived = await service_admin.reindex("site_template")
    assert revived["created"] == 1
    row = await _row(meta["id"])
    assert (str(row.id), row.slug, row.withdrawn) == (listing_id, "bakery", False)


# ---------------------------------------------------------------------------
# updated_at
# ---------------------------------------------------------------------------


async def test_updated_at_moves_only_on_a_content_change() -> None:
    listing_id = await _upsert("a", title="Bakery")
    card = await service_admin.get_public(listing_id)
    assert card["updated_at"] is not None
    before = (await DiscoverListing.get(listing_id)).updatedAt

    await _upsert("a", title="Bakery")  # a re-sync that changes nothing
    assert (await DiscoverListing.get(listing_id)).updatedAt == before

    await _upsert("a", title="Bakery", description="Fresh bread")
    assert (await DiscoverListing.get(listing_id)).updatedAt > before


# ---------------------------------------------------------------------------
# tags
# ---------------------------------------------------------------------------


async def test_tags_are_normalised_on_upsert_and_on_template_save() -> None:
    listing_id = await _upsert("a", tags=["  Bread Shop ", "BAKERY", "bakery", "gluten_free", ""])
    assert (await service_admin.get_public(listing_id))["tags"] == [
        "bread-shop",
        "bakery",
        "gluten-free",
    ]

    meta = await _publish(tags=["Cakes", "cakes", "Sweet Treats"])
    assert meta["tags"] == ["cakes", "sweet-treats"]
    assert (await _row(meta["id"])).tags == ["cakes", "sweet-treats"]

    patched = await templates.update_template(WS, "u1", meta["id"], {"tags": ["Bread"]})
    await service_admin.sync_source("site_template", meta["id"])
    assert patched["tags"] == ["bread"]
    assert (await _row(meta["id"])).tags == ["bread"]


@pytest.mark.parametrize(
    "tags",
    [
        [f"t{i}" for i in range(9)],
        ["café"],
        ["a" * 33],
        ["no/slash"],
        "bakery",
        [3],
    ],
)
async def test_bad_tags_are_refused(tags: Any) -> None:
    body = {"workspace": WS, "owner": "u1", "kind": "site", "title": "T", "tags": tags}
    with pytest.raises(ValidationError):
        UpsertListingRequest.model_validate(body)
    with pytest.raises(ValidationError):
        await templates.save_template(
            WS, "u1", {"pocket_id": str((await _site("u1")).id), "name": "T", "tags": tags}
        )


async def test_tag_filter() -> None:
    await _upsert("a", title="Bakery", tags=["bread", "shop"])
    await _upsert("b", title="Arcade", tags=["game"])
    await _upsert("c", title="Hidden bread", tags=["bread"])
    await service_admin.hide_listing(str((await _row("c")).id))

    def titles(page: dict) -> list[str]:
        return [item["title"] for item in page["items"]]

    assert titles(await service_admin.list_public({"tag": "bread"})) == ["Bakery"]
    assert titles(await service_admin.list_public({"tag": " BREAD "})) == ["Bakery"]
    assert titles(await service_admin.list_public({"tag": "game"})) == ["Arcade"]
    assert titles(await service_admin.list_public({"tag": "nope"})) == []
    # Outside the dialect: matches nothing, never raises (it is a query param).
    assert ListPublicListingsRequest(tag="Café!").tag == "café!"
    assert titles(await service_admin.list_public({"tag": "Café!"})) == []


# ---------------------------------------------------------------------------
# creator
# ---------------------------------------------------------------------------


async def test_creator_is_opt_in_and_never_the_email() -> None:
    named = await _user("Ada Baker", "ada@example.com")
    unnamed = await _user("", "nameless@example.com")
    hidden_name = await _upsert("a", owner=named, show_creator=False)
    shown = await _upsert("b", owner=named, show_creator=True)
    no_name = await _upsert("c", owner=unnamed, show_creator=True)

    assert (await service_admin.get_public(hidden_name))["creator"] is None
    assert (await service_admin.get_public(shown))["creator"] == {
        "name": "Ada Baker",
        "avatar_url": None,
    }
    assert (await service_admin.get_public(no_name))["creator"] is None

    page = await service_admin.list_public()
    blob = repr(page)
    for secret in ("ada@example.com", "nameless@example.com", named, unnamed):
        assert secret not in blob
    assert await auth_service.resolve_public_identities({named, unnamed, "not-an-id"}) == {
        named: auth_service.PublicIdentity(name="Ada Baker", avatar_url=None)
    }


@pytest.mark.parametrize(
    ("avatar", "expected"),
    [
        ("https://cdn.example/ada.png", "https://cdn.example/ada.png"),
        ("http://cdn.example/ada.png", "http://cdn.example/ada.png"),
        ("  https://cdn.example/ada.png  ", "https://cdn.example/ada.png"),
        ("/uploads/avatars/ada.png", None),
        ("/api/v1/uploads/ada.png", None),
        ("data:image/png;base64,iVBORw0KGgo=", None),
        ("javascript:alert(1)", None),
        ("ftp://cdn.example/ada.png", None),
        ("https://", None),
        ("//cdn.example/ada.png", None),
        ("https://cdn.example/a da.png", None),
        ("https://cdn.example/" + "a" * 2100, None),
        ("", None),
    ],
)
async def test_creator_avatar_is_an_absolute_http_url_or_null(avatar: str, expected) -> None:
    owner = await _user("Ada Baker", avatar=avatar)
    listing_id = await _upsert("a", owner=owner, show_creator=True)
    card = await service_admin.get_public(listing_id)
    assert card["creator"] == {"name": "Ada Baker", "avatar_url": expected}


async def test_creator_avatar_is_opt_in_like_the_name() -> None:
    owner = await _user("Ada Baker", "ada@example.com", avatar="https://cdn.example/ada.png")
    hidden = await _upsert("a", owner=owner, show_creator=False)
    shown = await _upsert("b", owner=owner, show_creator=True)

    hidden_card = await service_admin.get_public(hidden)
    assert hidden_card["creator"] is None
    assert "cdn.example" not in repr(hidden_card)
    assert (await service_admin.get_public(shown))["creator"] == {
        "name": "Ada Baker",
        "avatar_url": "https://cdn.example/ada.png",
    }
    # The creator object carries the name and picture only: no id, no email.
    page = await service_admin.list_public()
    creators = [item["creator"] for item in page["items"] if item["creator"]]
    assert [set(c) for c in creators] == [{"name", "avatar_url"}]
    assert owner not in repr(page) and "ada@example.com" not in repr(page)


async def test_template_show_creator_reaches_the_listing() -> None:
    owner = await _user("Ada Baker")
    meta = await _publish(owner=owner, show_creator=True)
    assert meta["show_creator"] is True
    card = await service_admin.get_public(str((await _row(meta["id"])).id))
    assert card["creator"] == {"name": "Ada Baker", "avatar_url": None}

    await templates.update_template(WS, owner, meta["id"], {"show_creator": False})
    await service_admin.sync_source("site_template", meta["id"])
    assert (await service_admin.get_public(card["id"]))["creator"] is None


async def test_a_list_page_resolves_creators_and_parents_in_one_read_each(monkeypatch) -> None:
    owners = [
        await _user(f"Maker {i}", f"m{i}@example.com", avatar=f"https://cdn.example/{i}.png")
        for i in range(3)
    ]
    await _upsert("parent", title="Parent")
    for i, owner in enumerate(owners):
        await _upsert(f"kid{i}", owner=owner, show_creator=True, remixed_from_source_id="parent")

    calls = {"names": 0, "find": 0}
    real_names = auth_service.resolve_public_identities
    real_find = DiscoverListing.find

    async def counting_names(ids: set[str]) -> dict[str, auth_service.PublicIdentity]:
        calls["names"] += 1
        return await real_names(ids)

    def counting_find(*args: Any, **kwargs: Any):
        calls["find"] += 1
        return real_find(*args, **kwargs)

    monkeypatch.setattr(auth_service, "resolve_public_identities", counting_names)
    monkeypatch.setattr(DiscoverListing, "find", counting_find)
    page = await service_admin.list_public()

    assert len(page["items"]) == 4
    assert calls == {"names": 1, "find": 2}  # the page itself + the parents
    kids = [item for item in page["items"] if item["title"] != "Parent"]
    assert {k["creator"]["name"] for k in kids} == {"Maker 0", "Maker 1", "Maker 2"}
    assert {k["creator"]["avatar_url"] for k in kids} == {
        f"https://cdn.example/{i}.png" for i in range(3)
    }
    assert {k["remixed_from"]["slug"] for k in kids} == {"parent"}


# ---------------------------------------------------------------------------
# show_creator defaults
# ---------------------------------------------------------------------------


async def test_a_new_site_template_shows_its_creator_by_default() -> None:
    owner = await _user("Ada Baker", avatar="https://cdn.example/ada.png")
    meta = await _publish(owner=owner)
    assert meta["show_creator"] is True
    assert (await _row(meta["id"])).show_creator is True
    card = await service_admin.get_public(str((await _row(meta["id"])).id))
    assert card["creator"] == {"name": "Ada Baker", "avatar_url": "https://cdn.example/ada.png"}

    # Opting out at save time still works.
    quiet = await _publish(owner=owner, name="Quiet", show_creator=False)
    assert quiet["show_creator"] is False
    assert (await service_admin.get_public(str((await _row(quiet["id"])).id)))["creator"] is None


@pytest.mark.parametrize("stored", [True, False])
async def test_a_patch_without_show_creator_keeps_the_stored_value(stored: bool) -> None:
    owner = await _user("Ada Baker")
    meta = await _publish(owner=owner, show_creator=stored)
    patched = await templates.update_template(
        WS, owner, meta["id"], {"name": "Cake shop", "tags": ["cakes"]}
    )
    await service_admin.sync_source("site_template", meta["id"])
    assert patched["show_creator"] is stored
    assert (await _row(meta["id"])).show_creator is stored


async def test_a_template_saved_before_show_creator_stays_anonymous() -> None:
    owner = await _user("Ada Baker", avatar="https://cdn.example/ada.png")
    meta = await _publish(owner=owner)
    # Rewind both rows to before the field existed.
    await SiteTemplate.get_pymongo_collection().update_one(
        {"_id": PydanticObjectId(meta["id"])}, {"$unset": {"show_creator": ""}}
    )
    await DiscoverListing.get_pymongo_collection().update_one(
        {"source": "site_template", "source_id": meta["id"]}, {"$unset": {"show_creator": ""}}
    )
    listing_id = str((await _row(meta["id"])).id)
    assert (await service_admin.get_public(listing_id))["creator"] is None

    counts = await service_admin.reindex("site_template")
    assert (counts["created"], counts["updated"], counts["unchanged"]) == (0, 0, 1)
    await service_admin.sync_source("site_template", meta["id"])
    assert (await _row(meta["id"])).show_creator is False
    assert (await service_admin.get_public(listing_id))["creator"] is None

    # A PATCH that doesn't name the field leaves it off too.
    await templates.update_template(WS, owner, meta["id"], {"description": "Fresh bread"})
    await service_admin.sync_source("site_template", meta["id"])
    assert (await service_admin.get_public(listing_id))["creator"] is None


# ---------------------------------------------------------------------------
# remixed_from
# ---------------------------------------------------------------------------


async def test_a_remixed_site_published_again_points_at_its_parent() -> None:
    parent = await _publish(name="Bakery")
    parent_listing = str((await _row(parent["id"])).id)

    used = await service.use_listing(OTHER_WS, "u2", parent_listing, "My bakery")
    pocket_id = used["result"]["pocket_id"]
    assert (await PocketDoc.get(pocket_id)).template_id == parent["id"]

    child = await _publish(owner="u2", workspace=OTHER_WS, pocket_id=pocket_id, name="My bakery")
    child_listing = str((await _row(child["id"])).id)
    card = await service_admin.get_public(child_listing)
    assert card["remixed_from"] == {"slug": "bakery", "title": "Bakery"}
    assert parent["id"] not in repr(card)  # the source id never reaches the wire

    # An original has no parent.
    assert (await service_admin.get_public(parent_listing))["remixed_from"] is None

    # Parent hidden: null. Unhidden then withdrawn: null again.
    await service_admin.hide_listing(parent_listing)
    assert (await service_admin.get_public(child_listing))["remixed_from"] is None
    await service_admin.set_hidden(parent_listing, False)
    await service_admin.sync_source("site_template", parent["id"])
    assert (await service_admin.get_public(child_listing))["remixed_from"] is not None
    await templates.update_template(WS, "u1", parent["id"], {"visibility": "private"})
    await service_admin.sync_source("site_template", parent["id"])
    assert (await service_admin.get_public(child_listing))["remixed_from"] is None


async def test_a_template_from_an_original_site_has_no_parent() -> None:
    meta = await _publish()
    assert (await _row(meta["id"])).remixed_from_source_id is None
