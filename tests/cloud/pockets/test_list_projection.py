"""Gates on the two pocket LIST reads being projected.

Both used to hydrate whole documents. A Pocket carries ``rippleSpec``, every
widget's ``spec``, and - on the svelte track - a ``source`` map holding the
full text of every file in the generated site. A gallery of a hundred pockets
pulled all of it across the wire on one event loop to render cards.

``list_pockets`` drops ``source`` and keeps everything the desktop canvas
renders from. ``agent_list`` already returned only seven small fields, so its
projection is the seven; the gap there was that the read never matched the
response, and nothing in the RESPONSE can show that - which is why these tests
watch the query as well as the result.

Mutations live in ``tests/mutations/partials.json``.
"""

from __future__ import annotations

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

import pytest_asyncio
from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service

_WORKSPACE = "ws_proj_1"
_OWNER = "u_proj_1"

#: Stands in for a generated SvelteKit site. The real ones run to hundreds of
#: kilobytes across dozens of files; the size is not what the test asserts, the
#: presence is.
_SOURCE = {
    "src/routes/+page.svelte": "<script>let x = 1;</script>\n<h1>hi</h1>\n" * 40,
    "src/app.css": "body { margin: 0 }\n" * 40,
}

_SPEC = {"ui": {"kind": "grid", "children": []}, "state": {}}


async def _seed(**overrides) -> _PocketDoc:
    fields = {
        "workspace": _WORKSPACE,
        "name": "Marketing site",
        "description": "the landing page",
        "owner": _OWNER,
        "type": "site",
        "icon": "rocket",
        "color": "#ff0000",
        "engine": "svelte",
        "source": dict(_SOURCE),
        "rippleSpec": dict(_SPEC),
        "visibility": "workspace",
    }
    fields.update(overrides)
    doc = _PocketDoc(**fields)
    await doc.insert()
    return doc


class _ProjectionSpy:
    """Wraps the real pymongo collection, recording every ``find`` projection.

    Watching the query is the only way to see ``agent_list``'s fix: it never
    returned ``source``, so a revert to an unprojected read is invisible in
    the response and completely visible here.
    """

    def __init__(self, inner) -> None:  # noqa: ANN001
        self._inner = inner
        self.projections: list[object] = []

    def find(self, *args, **kwargs):  # noqa: ANN002, ANN003
        # Copied, not referenced: the driver normalizes the projection dict in
        # place (adding an explicit ``_id``), so holding the caller's object
        # would record what the driver made of it rather than what was asked.
        given = args[1] if len(args) > 1 else kwargs.get("projection")
        self.projections.append(dict(given) if isinstance(given, dict) else given)
        return self._inner.find(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


@pytest_asyncio.fixture
async def spy(mongo_db, monkeypatch):  # noqa: ARG001
    inner = _PocketDoc.get_pymongo_collection()
    watcher = _ProjectionSpy(inner)
    monkeypatch.setattr(
        _PocketDoc,
        "get_pymongo_collection",
        classmethod(lambda cls: watcher),  # noqa: ARG005
    )
    return watcher


async def test_the_gallery_does_not_carry_the_site_source(spy):
    """Mutation: revert list_pockets to an unprojected find.

    ``source`` is the single biggest field on the document and no consumer of
    THIS response reads it. It must come back as the model default rather than
    the stored map.
    """
    await _seed()

    rows = await pockets_service.list_pockets(_WORKSPACE, _OWNER)

    assert len(rows) == 1
    assert rows[0]["source"] is None


async def test_the_gallery_still_carries_what_the_canvas_renders(spy):
    """The other direction, and the reason this is a projection not a rewrite.

    Dropping ``rippleSpec`` or ``widgets`` would be a bigger saving and would
    break the desktop client, which renders the canvas straight off this list.
    """
    await _seed()

    rows = await pockets_service.list_pockets(_WORKSPACE, _OWNER)

    # Compared on the ``ui`` subtree, not the whole spec: the wire dict runs
    # the spec through the normalizer and the $source resolver, both of which
    # legitimately add keys. The subtree is the part the canvas draws.
    assert rows[0]["rippleSpec"]["ui"] == _SPEC["ui"]
    assert rows[0]["name"] == "Marketing site"
    assert rows[0]["engine"] == "svelte"
    assert rows[0]["icon"] == "rocket"
    assert "widgets" in rows[0]


async def test_the_gallery_read_asks_mongo_to_exclude_source(spy):
    """Mutation: widen the projection to include source.

    Asserted on the query, not the response: a projection that still fetched
    the field and dropped it afterwards would pass the test above and save
    nothing at all, which is the entire cost this finding is about.
    """
    await _seed()

    await pockets_service.list_pockets(_WORKSPACE, _OWNER)

    assert spy.projections, "list_pockets did not go through a projected read"
    assert {"source": 0} in spy.projections, spy.projections


async def test_a_legacy_document_still_reads_back_its_model_defaults(spy):
    """No mutation: this pins behaviour rather than guarding a line.

    The regression it describes - hand-building the wire dict from raw BSON,
    the way the two projected helpers in this codebase already do - is a
    rewrite, not an edit, so no find-and-replace expresses it and it is not
    in the mutation plan. It is here as the reason the projected read still
    goes through the model at all.

    The projected read must still go through pydantic. A pocket written before
    ``engine`` existed has no such key in Mongo, and skipping validation hands
    back None where an unprojected find gives "ripple" - which downstream code
    treats as a track it does not know. The id is asserted for the same
    reason: ``_id`` only becomes ``id`` through the model's alias.
    """
    doc = await _seed()
    await _PocketDoc.get_pymongo_collection().update_one(
        {"_id": doc.id}, {"$unset": {"engine": "", "icon": "", "color": ""}}
    )

    rows = await pockets_service.list_pockets(_WORKSPACE, _OWNER)

    assert rows[0]["_id"] == str(doc.id)
    assert rows[0]["engine"] == "ripple"
    assert rows[0]["icon"] == ""
    assert rows[0]["color"] == ""


async def test_the_agent_list_read_is_projected_to_what_it_returns(spy):
    """Mutation: revert agent_list to an unprojected find.

    This one runs on every creation flow as the "have we already got one of
    these?" check, so it is the hottest of the two.
    """
    await _seed()

    rows = await pockets_service.agent_list(_WORKSPACE, _OWNER)

    assert spy.projections, "agent_list did not go through a projected read"
    projection = spy.projections[-1]
    assert isinstance(projection, dict)
    assert "source" not in projection
    assert "rippleSpec" not in projection
    assert "widgets" not in projection
    assert set(projection) == {"_id", "name", "description", "type", "icon", "color", "owner"}
    assert rows == [
        {
            "id": str((await _PocketDoc.find_one({"workspace": _WORKSPACE})).id),
            "name": "Marketing site",
            "description": "the landing page",
            "type": "site",
            "icon": "rocket",
            "color": "#ff0000",
            "owner": _OWNER,
        }
    ]


async def test_agent_list_substitutes_a_default_for_a_missing_field(spy):
    """Mutation: drop the ``or ""`` fallbacks.

    Raw BSON does not get the model's defaults, and these values go straight
    into prompt rows where callers call string methods on them. A None here is
    an AttributeError in the surface handler, a long way from this file.
    """
    doc = await _seed()
    await _PocketDoc.get_pymongo_collection().update_one(
        {"_id": doc.id}, {"$unset": {"description": "", "icon": "", "color": ""}}
    )

    rows = await pockets_service.agent_list(_WORKSPACE, _OWNER)

    assert rows[0]["description"] == ""
    assert rows[0]["icon"] == ""
    assert rows[0]["color"] == ""


async def test_both_reads_stay_scoped_to_the_workspace(spy):
    """A projection must not quietly widen the tenancy filter."""
    await _seed()
    await _seed(workspace="ws_other", name="Someone else's")

    listed = await pockets_service.list_pockets(_WORKSPACE, _OWNER)
    agent_rows = await pockets_service.agent_list(_WORKSPACE, _OWNER)

    assert [r["name"] for r in listed] == ["Marketing site"]
    assert [r["name"] for r in agent_rows] == ["Marketing site"]


async def test_a_private_pocket_owned_by_someone_else_stays_hidden(spy):
    """The $or visibility union survives the rewrite."""
    await _seed(owner="someone_else", visibility="private", name="Private")
    await _seed(name="Mine")

    listed = await pockets_service.list_pockets(_WORKSPACE, _OWNER)
    agent_rows = await pockets_service.agent_list(_WORKSPACE, _OWNER)

    assert [r["name"] for r in listed] == ["Mine"]
    assert [r["name"] for r in agent_rows] == ["Mine"]
