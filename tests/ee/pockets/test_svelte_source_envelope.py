# tests/ee/pockets/test_svelte_source_envelope.py
# Created: 2026-06-21 (feat/dsv-5-svelte-dynamic-brain) — DSV-5 write-side: the
# loosened ``Pocket.source`` (``dict[str, str]`` -> ``dict[str, Any]``) must
# persist + read back a DYNAMIC svelte site's ``source`` CONTENT ENVELOPE, which
# carries the live-data bindings (``objects``/``sources``/``actions``/``auth``) as
# SIBLING keys alongside the ``{path: contents}`` SvelteKit files on the same dict.
# DSV-2b's read resolves ``objects`` off exactly this envelope, so the round-trip
# is the write-side half of that contract.
#
# Ground truth: insert a real (mongomock) Beanie ``Pocket`` doc and read it back —
# proving the binding siblings survive Mongo serialization with their NON-string
# types intact (objects/sources/actions = lists of dicts, auth = bool). A STATIC
# svelte pocket (str->str source map) must still round-trip unchanged (the looser
# ``Any`` is a superset).
from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from bson import ObjectId  # noqa: E402

# A dynamic svelte source ENVELOPE: the §4.3 SvelteKit files PLUS the live-data
# bindings as sibling keys on the same dict (the DSV-5 contract).
_DYNAMIC_ENVELOPE: dict = {
    # ── the {path: contents} SvelteKit files (string values) ──
    "src/routes/+page.svelte": (
        "<script>import Guestbook from '$lib/components/Guestbook.svelte';</script><Guestbook />"
    ),
    "src/routes/+layout.svelte": "<script>import '../app.css';</script>",
    "src/routes/+page.ts": "export const prerender = true;",
    "src/app.css": ":root{}",
    "src/lib/components/Guestbook.svelte": "<section><h2>Guestbook</h2></section>",
    # ── the live-data bindings, siblings on the SAME envelope (NON-string) ──
    "objects": [
        {
            "name": "entry",
            "fields": {"id": "text", "name": "text", "message": "text"},
            "primaryKey": "id",
        }
    ],
    "sources": [{"name": "entries", "kind": "data", "object": "entry", "refresh": "pocket_open"}],
    "actions": [{"name": "sign", "object": "entry", "op": "insert"}],
    "auth": True,
}

# A static svelte source map: only the str->str file entries, no bindings.
_STATIC_MAP: dict = {
    "src/routes/+page.svelte": (
        "<script>import Hero from '$lib/components/Hero.svelte';</script><Hero />"
    ),
    "src/routes/+layout.svelte": "<script>import '../app.css';</script>",
    "src/routes/+page.ts": "export const prerender = true;",
    "src/app.css": ":root{}",
    "src/lib/components/Hero.svelte": "<section><h1>hi</h1></section>",
}


@pytest.mark.asyncio
async def test_dynamic_source_envelope_round_trips(beanie_test_db) -> None:
    """A dynamic svelte pocket persists its ``source`` envelope — files AND the
    binding siblings — and reads back BYTE-FOR-BYTE, with the non-string binding
    types (lists/bool) intact. This is the DSV-5 write-side half of the contract
    DSV-2b's read assumes."""
    from pocketpaw_ee.cloud.models.pocket import Pocket

    doc = Pocket(
        workspace=str(ObjectId()),
        name="Svelte Guestbook",
        owner=str(ObjectId()),
        type="site",
        pattern="dynamic",
        engine="svelte",
        source=_DYNAMIC_ENVELOPE,
    )
    await doc.insert()

    # Ground truth: re-read straight from Mongo (not the in-memory object).
    read = await Pocket.get(doc.id)
    assert read is not None
    assert read.engine == "svelte"
    assert read.pattern == "dynamic"
    # The whole envelope survived — files + bindings.
    assert read.source == _DYNAMIC_ENVELOPE
    # The binding siblings kept their NON-string types.
    assert isinstance(read.source["objects"], list)
    assert read.source["objects"][0]["name"] == "entry"
    assert isinstance(read.source["sources"], list)
    assert isinstance(read.source["actions"], list)
    assert read.source["auth"] is True
    # A file entry is still a content string.
    assert isinstance(read.source["src/routes/+page.svelte"], str)


@pytest.mark.asyncio
async def test_static_svelte_source_map_still_round_trips(beanie_test_db) -> None:
    """A STATIC svelte pocket (str->str source map, no bindings) round-trips
    unchanged under the loosened ``dict[str, Any]`` type — the looser type is a
    superset, so the pre-DSV-5 behaviour is preserved exactly."""
    from pocketpaw_ee.cloud.models.pocket import Pocket

    doc = Pocket(
        workspace=str(ObjectId()),
        name="Static Svelte",
        owner=str(ObjectId()),
        type="site",
        pattern="landing",
        engine="svelte",
        source=_STATIC_MAP,
    )
    await doc.insert()

    read = await Pocket.get(doc.id)
    assert read is not None
    assert read.source == _STATIC_MAP
    # Every value is still a content string (no bindings present).
    assert all(isinstance(v, str) for v in read.source.values())
    assert read.pattern == "landing"


@pytest.mark.asyncio
async def test_model_dump_preserves_binding_envelope(beanie_test_db) -> None:
    """A JSON model_dump (the wire path the agent view / DTO ride) preserves the
    binding envelope intact — the loosened type does not coerce or drop the
    non-string siblings."""
    from pocketpaw_ee.cloud.models.pocket import Pocket

    doc = Pocket(
        workspace=str(ObjectId()),
        name="Svelte Guestbook",
        owner=str(ObjectId()),
        type="site",
        pattern="dynamic",
        engine="svelte",
        source=_DYNAMIC_ENVELOPE,
    )
    dumped = doc.model_dump(mode="json", by_alias=True)
    assert dumped["source"] == _DYNAMIC_ENVELOPE
    assert dumped["source"]["auth"] is True
