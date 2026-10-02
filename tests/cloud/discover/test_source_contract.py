# tests/cloud/discover/test_source_contract.py — the generic DiscoverSource contract.
#
# Created 2026-10-02 (feat/discover-source-contract). A fake source backed by an
# in-memory dict proves the index is source-generic: ``sync_source`` lists a
# public row (a hidden one as a hidden listing) and removes a private or missing
# one; ``reindex`` reports created / updated / unchanged / removed; a row whose
# kind the source doesn't declare is refused; an unknown source or one without
# ``iter_public`` is ``discover.reindex_unsupported``; and the periodic pass
# reindexes every registered source, carrying on past one that raises.
#
# Updated 2026-10-02 (feat/studio-templates): the builtin sources now include
# ``studio_template``.
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.discover import listeners, service_admin, sources
from pocketpaw_ee.cloud.models.discover_listing import DiscoverListing

pytestmark = pytest.mark.usefixtures("mongo_db")

FAKE = "fake"


def _row(source_id: str, **fields: Any) -> dict[str, Any]:
    return {
        "id": source_id,
        "public": True,
        "hidden": False,
        "workspace": "w1",
        "owner": "u1",
        "kind": "tool",
        "title": f"Item {source_id}",
        "description": "",
        "audiences": [],
        "preview_image_url": None,
        "live_url": None,
        **fields,
    }


async def _use(*_args: Any) -> dict:
    return {}


@pytest.fixture
def rows(monkeypatch, builtin_sources) -> dict[str, dict[str, Any]]:
    """A registered ``fake`` source over this dict; the registry is restored after."""
    monkeypatch.setattr(sources, "_SOURCES", dict(sources._SOURCES))
    store: dict[str, dict[str, Any]] = {}

    async def _get(source_id: str) -> dict[str, Any] | None:
        return store.get(source_id)

    async def _iter() -> AsyncIterator[dict[str, Any]]:
        for row in list(store.values()):
            yield row

    sources.register_source(
        sources.DiscoverSource(FAKE, frozenset({"tool"}), _use, get_public=_get, iter_public=_iter)
    )
    return store


async def _listing(source_id: str, source: str = FAKE) -> DiscoverListing | None:
    return await DiscoverListing.find_one({"source": source, "source_id": source_id})


@pytest.mark.asyncio
async def test_sync_source_lists_public_rows_and_removes_the_rest(rows) -> None:
    rows["a"] = _row("a")
    await service_admin.sync_source(FAKE, "a")
    listing = await _listing("a")
    assert (listing.title, listing.kind, listing.hidden) == ("Item a", "tool", False)

    rows["a"] = _row("a", hidden=True)
    await service_admin.sync_source(FAKE, "a")
    assert (await _listing("a")).hidden is True

    rows["a"] = _row("a", public=False)
    await service_admin.sync_source(FAKE, "a")
    assert await _listing("a") is None

    rows["b"] = _row("b")
    await service_admin.sync_source(FAKE, "b")
    del rows["b"]
    await service_admin.sync_source(FAKE, "b")
    assert await _listing("b") is None


@pytest.mark.asyncio
async def test_sync_source_refuses_an_undeclared_kind(rows) -> None:
    rows["a"] = _row("a", kind="game")
    with pytest.raises(ValidationError):
        await service_admin.sync_source(FAKE, "a")
    assert await _listing("a") is None


@pytest.mark.asyncio
async def test_reindex_counts(rows) -> None:
    rows["a"], rows["b"] = _row("a"), _row("b")
    rows["private"] = _row("private", public=False)
    first = await service_admin.reindex(FAKE)
    assert first == {"source": FAKE, "created": 2, "updated": 0, "unchanged": 0, "removed": 0}
    assert await _listing("private") is None

    rows["a"] = _row("a", title="Renamed")
    del rows["b"]
    second = await service_admin.reindex(FAKE)
    assert second == {"source": FAKE, "created": 0, "updated": 1, "unchanged": 0, "removed": 1}
    assert (await _listing("a")).title == "Renamed"
    assert await _listing("b") is None

    third = await service_admin.reindex(FAKE)
    assert (third["updated"], third["unchanged"]) == (0, 1)


@pytest.mark.asyncio
async def test_reindex_refuses_unknown_and_unsupported_sources(rows) -> None:
    sources.register_source(sources.DiscoverSource("use_only", frozenset({"tool"}), _use))
    for name in ("nope", "use_only"):
        with pytest.raises(ValidationError) as exc:
            await service_admin.reindex(name)
        assert exc.value.code == "discover.reindex_unsupported"


@pytest.mark.asyncio
async def test_the_pass_reindexes_every_source_past_a_failing_one(rows, caplog) -> None:
    async def _boom() -> AsyncIterator[dict[str, Any]]:
        raise RuntimeError("boom")
        yield {}  # pragma: no cover - makes this an async generator

    # Registered before ``fake`` is re-registered so it runs first.
    sources.register_source(
        sources.DiscoverSource("boom", frozenset({"tool"}), _use, iter_public=_boom)
    )
    fake = sources._SOURCES.pop(FAKE)
    sources.register_source(fake)
    rows["a"] = _row("a")

    await listeners._reindex_once()

    assert [s.name for s in sources.registered_sources()] == [
        "site_template",
        "studio_template",
        "boom",
        FAKE,
    ]
    assert await _listing("a") is not None
    assert "discover: reindex failed for boom" in caplog.text
