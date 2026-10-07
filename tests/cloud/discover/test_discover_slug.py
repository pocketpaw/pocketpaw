# tests/cloud/discover/test_discover_slug.py — Discover listing slugs.
#
# Pins the slug rules: derivation from the title (unicode folded, punctuation
# and spaces collapsed; a title with no ASCII left, CJK or Devanagari, falls
# back to the source_id, never a constant), a source-proposed slug is slugified
# too, collisions suffix ``-2``, ``-3`` across every source, a slug never
# changes on a rename or when two syncs of the same row both read "no slug",
# a sync that loses the slug race retries (a sibling row: next suffix; the same
# row: keep its slug), ``get_public`` finds a listing by id or by slug (hidden
# is 404), reindex backfills a pre-slug row once honouring a proposed slug (a
# row without a slug serves its id meanwhile), and ``UpsertListingRequest``
# still refuses unknown fields.
from __future__ import annotations

from typing import Any

import pytest
from pocketpaw_ee.cloud._core.errors import NotFound
from pocketpaw_ee.cloud.discover import service_admin, sources
from pocketpaw_ee.cloud.discover.dto import UpsertListingRequest
from pocketpaw_ee.cloud.discover.service_admin import slugify
from pocketpaw_ee.cloud.models.discover_listing import DiscoverListing
from pydantic import ValidationError

pytestmark = pytest.mark.usefixtures("mongo_db")

WS, OWNER = "w1", "u1"


async def _upsert(source_id: str, source: str = "site_template", **fields: Any) -> str:
    fields.setdefault("workspace", WS)
    fields.setdefault("owner", OWNER)
    fields.setdefault("kind", "image" if source == "studio_template" else "site")
    fields.setdefault("title", "Bakery")
    return await service_admin.upsert_from_source(source, source_id, fields)


async def _slug(listing_id: str) -> str | None:
    return (await DiscoverListing.get(listing_id)).slug


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        ("Café Crème & Co!", "cafe-creme-co"),
        ("  Hello   World  ", "hello-world"),
        ("--Bakery--", "bakery"),
        ("Ünïcödé Tëst 2", "unicode-test-2"),
        ("日本語", ""),
    ],
)
def test_slugify(title: str, slug: str) -> None:
    assert slugify(title) == slug


@pytest.mark.asyncio
@pytest.mark.parametrize("title", ["日本語のパン屋", "नमस्ते दुनिया", "!!!"])
async def test_a_title_with_no_ascii_falls_back_to_the_source_id(title: str) -> None:
    assert await _slug(await _upsert("Shop_42", title=title)) == "shop-42"


@pytest.mark.asyncio
async def test_upsert_derives_the_slug_from_the_title_or_the_proposed_slug() -> None:
    assert await _slug(await _upsert("a", title="Café Crème & Co!")) == "cafe-creme-co"
    assert await _slug(await _upsert("b", title="Other", slug="  My Shop! ")) == "my-shop"


@pytest.mark.asyncio
async def test_same_title_suffixes_across_sources() -> None:
    ids = [await _upsert(f"t{i}") for i in range(3)]
    assert [await _slug(i) for i in ids] == ["bakery", "bakery-2", "bakery-3"]
    assert await _slug(await _upsert("s1", source="studio_template")) == "bakery-4"


@pytest.mark.asyncio
async def test_a_rename_keeps_the_slug() -> None:
    listing_id = await _upsert("a", title="Bakery")
    assert await _upsert("a", title="Renamed shop", slug="ignored") == listing_id
    doc = await DiscoverListing.get(listing_id)
    assert (doc.title, doc.slug) == ("Renamed shop", "bakery")


@pytest.mark.asyncio
async def test_two_syncs_of_one_row_that_both_read_no_slug_keep_one_slug(monkeypatch) -> None:
    """The lost read: sync B read the row before sync A wrote ``bakery``, so B
    derives ``bakery-2`` for the same row. It must not land."""
    real = DiscoverListing.get_pymongo_collection

    class _StaleRead:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def find_one(self, *args: Any, **kwargs: Any) -> Any:
            doc = await self._inner.find_one(*args, **kwargs)
            if doc is not None and args[1:] == ({"slug": 1},):  # the upsert's pre-read
                return {"_id": doc["_id"]}
            return doc

    listing_id = await _upsert("a")
    monkeypatch.setattr(
        DiscoverListing, "get_pymongo_collection", classmethod(lambda cls: _StaleRead(real()))
    )
    assert await _upsert("a", title="Bakery again") == listing_id
    assert await _slug(listing_id) == "bakery"
    assert await DiscoverListing.find({"slug": "bakery-2"}).count() == 0


@pytest.mark.asyncio
async def test_a_pre_slug_row_is_backfilled_once_even_when_two_syncs_race(monkeypatch) -> None:
    """Both syncs of a legacy row read "no slug". A's backfill lands between B's
    upsert and B's backfill; B's guarded backfill must then be a no-op."""
    real = DiscoverListing.get_pymongo_collection
    inserted = await real().insert_one(
        {
            "source": "site_template",
            "source_id": "legacy",
            "workspace": WS,
            "owner": OWNER,
            "kind": "site",
            "title": "Bakery",
        }
    )

    class _SiblingBackfills:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def find_one_and_update(self, *args: Any, **kwargs: Any) -> Any:
            raw = await self._inner.find_one_and_update(*args, **kwargs)
            await self._inner.update_one({"_id": raw["_id"]}, {"$set": {"slug": "bakery"}})
            return raw  # as B saw it: still no slug

    monkeypatch.setattr(
        DiscoverListing,
        "get_pymongo_collection",
        classmethod(lambda cls: _SiblingBackfills(real())),
    )
    # B (a rename arrived meanwhile) derives ``renamed`` and must not overwrite A's.
    assert await _upsert("legacy", title="Renamed") == str(inserted.inserted_id)
    assert await _slug(str(inserted.inserted_id)) == "bakery"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("winner", "expected"),
    [("sibling", "bakery-2"), ("a", "bakery")],
    ids=["sibling-row-took-the-slug", "same-row-was-inserted-first"],
)
async def test_losing_the_slug_race_retries(monkeypatch, winner: str, expected: str) -> None:
    from pymongo.errors import DuplicateKeyError

    real = DiscoverListing.get_pymongo_collection
    raised: list[bool] = []

    class _Racing:
        """On the first upsert a concurrent sync inserts ``winner`` with ``bakery``."""

        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        async def find_one_and_update(self, *args: Any, **kwargs: Any) -> Any:
            if kwargs.get("upsert") and not raised:
                raised.append(True)
                await self._inner.insert_one(
                    {
                        "source": "site_template",
                        "source_id": winner,
                        "slug": "bakery",
                        "workspace": WS,
                        "owner": OWNER,
                        "kind": "site",
                        "title": "Bakery",
                    }
                )
                raise DuplicateKeyError("E11000 duplicate key")
            return await self._inner.find_one_and_update(*args, **kwargs)

    monkeypatch.setattr(
        DiscoverListing, "get_pymongo_collection", classmethod(lambda cls: _Racing(real()))
    )
    assert await _slug(await _upsert("a")) == expected
    assert raised == [True]
    assert await DiscoverListing.find({"source_id": "a"}).count() == 1


@pytest.mark.asyncio
async def test_get_public_by_id_or_slug() -> None:
    site = await _upsert("a")
    studio = await _upsert("s1", source="studio_template")
    by_id = await service_admin.get_public(site)
    assert by_id["slug"] == "bakery"
    assert await service_admin.get_public("bakery") == by_id
    assert (await service_admin.get_public("bakery-2"))["id"] == studio
    with pytest.raises(NotFound):
        await service_admin.get_public("no-such-slug")


@pytest.mark.asyncio
async def test_a_hidden_listing_is_not_found_by_slug() -> None:
    listing_id = await _upsert("a")
    await service_admin.hide_listing(listing_id)
    with pytest.raises(NotFound):
        await service_admin.get_public("bakery")


@pytest.mark.asyncio
async def test_reindex_backfills_a_pre_slug_row_once(monkeypatch) -> None:
    row = {
        "id": "legacy",
        "public": True,
        "hidden": False,
        "workspace": WS,
        "owner": OWNER,
        "kind": "site",
        "title": "Old Bakery",
        "description": "",
        "audiences": [],
        "preview_image_url": None,
        "live_url": None,
        "media_kind": None,
        "media_url": None,
        "slug": "Proposed Handle",  # honoured on backfill, as on sync
    }

    async def _iter():
        yield row

    legacy = sources.DiscoverSource(
        name="legacy_source", kinds=frozenset({"site"}), use=None, iter_public=_iter
    )
    monkeypatch.setitem(sources._SOURCES, "legacy_source", legacy)  # scoped to this test
    fields = {k: v for k, v in row.items() if k not in ("id", "public", "hidden", "slug")}
    inserted = await DiscoverListing.get_pymongo_collection().insert_one(
        {"source": "legacy_source", "source_id": "legacy", **fields}
    )
    listing_id = str(inserted.inserted_id)
    # Until the backfill, the card serves its id as the slug (a valid lookup key).
    assert (await service_admin.get_public(listing_id))["slug"] == listing_id

    first = await service_admin.reindex("legacy_source")
    assert (first["updated"], first["created"]) == (1, 0)
    assert await _slug(listing_id) == "proposed-handle"
    assert (await service_admin.get_public("proposed-handle"))["id"] == listing_id
    second = await service_admin.reindex("legacy_source")
    assert (second["unchanged"], second["updated"]) == (1, 0)


def test_upsert_request_still_forbids_unknown_fields() -> None:
    base = {"workspace": WS, "owner": OWNER, "kind": "site", "title": "x"}
    assert UpsertListingRequest.model_validate({**base, "slug": "ok"}).slug == "ok"
    with pytest.raises(ValidationError):
        UpsertListingRequest.model_validate({**base, "featured": True})
