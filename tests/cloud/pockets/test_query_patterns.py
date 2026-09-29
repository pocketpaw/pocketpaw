"""Query-count and parity gates on the pocket read paths.

* The ``GET /pockets`` gallery resolves each pocket's ``$source`` markers. A
  ``workspace.pockets`` marker scans the workspace, so resolving it once per
  pocket made a gallery of N such pockets O(N^2) reads. One memo per page makes
  it one read, and the team lookups one query per page.
* ``visible_pocket_refs`` is the projected id+name read for callers that were
  paying for a resolved gallery to read names. Visibility is the tenancy
  boundary, so it must return exactly the pockets ``list_pockets`` returns.
* ``site_pocket_ids`` reads one field of each Site; it is projected to it.
* ``GET /pockets/builtin-widgets`` seeded on every call (6 reads + 6 writes).
"""

from __future__ import annotations

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

import pytest
import pytest_asyncio
from beanie import PydanticObjectId
from pocketpaw_ee.cloud import ripple_resolver, ripple_sources  # noqa: F401 — registers sources
from pocketpaw_ee.cloud.models.pocket import Pocket as _PocketDoc
from pocketpaw_ee.cloud.pockets import service as pockets_service

WS = "ws_qp"
ME = "u_me"
OTHER = "u_other"


class _FindSpy:
    """Wraps a pymongo collection and counts ``find`` calls, keeping projections."""

    def __init__(self, inner) -> None:  # noqa: ANN001
        self._inner = inner
        self.finds = 0
        self.projections: list[object] = []

    def find(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.finds += 1
        given = args[1] if len(args) > 1 else kwargs.get("projection")
        self.projections.append(dict(given) if isinstance(given, dict) else given)
        return self._inner.find(*args, **kwargs)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


@pytest_asyncio.fixture
async def pocket_spy(mongo_db, monkeypatch):  # noqa: ARG001
    spy = _FindSpy(_PocketDoc.get_pymongo_collection())
    monkeypatch.setattr(_PocketDoc, "get_pymongo_collection", classmethod(lambda cls: spy))  # noqa: ARG005
    return spy


async def _seed(**fields) -> str:
    base = {"workspace": WS, "name": "p", "owner": ME, "visibility": "workspace"}
    base.update(fields)
    doc = _PocketDoc(**base)
    await doc.insert()
    return str(doc.id)


_POCKETS_MARKER = {"$source": "workspace.pockets"}


def _spec_with_markers() -> dict:
    # The same marker twice in one spec, plus the gallery-wide repetition.
    return {
        "ui": {"kind": "grid", "children": []},
        "state": {"a": dict(_POCKETS_MARKER), "b": [dict(_POCKETS_MARKER)]},
    }


# ---------------------------------------------------------------------------
# #7 — the gallery no longer does O(N^2) source reads
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("n", [3, 8])
async def test_gallery_source_reads_do_not_grow_with_pocket_count(pocket_spy, n):
    """Mutation: drop ``memo=resolve_memo`` from the ResolveCtx in ``_wire_dict``.
    Each pocket then resolves its own marker and the find count becomes 1 + N.
    """
    for i in range(n):
        await _seed(name=f"p{i}", rippleSpec=_spec_with_markers())
    pocket_spy.finds = 0

    rows = await pockets_service.list_pockets(WS, ME)

    assert len(rows) == n
    # One find for the list itself, one for the single shared marker resolution.
    assert pocket_spy.finds == 2
    for row in rows:
        listed = row["rippleSpec"]["state"]["a"]
        assert {p["name"] for p in listed} == {f"p{i}" for i in range(n)}
        assert row["rippleSpec"]["state"]["b"][0] == listed


async def test_shared_source_results_are_not_aliased_across_pockets(pocket_spy):
    """A memoized result is deep-copied into each spec, so one pocket's wire dict
    can be edited without changing another's."""
    await _seed(name="a", rippleSpec=_spec_with_markers())
    await _seed(name="b", rippleSpec=_spec_with_markers())

    rows = await pockets_service.list_pockets(WS, ME)
    rows[0]["rippleSpec"]["state"]["a"].clear()

    assert len(rows[1]["rippleSpec"]["state"]["a"]) == 2
    assert len(rows[0]["rippleSpec"]["state"]["b"][0]) == 2


async def test_gallery_resolves_every_team_in_one_user_query(mongo_db, monkeypatch):  # noqa: ARG001
    """Mutation: pass ``team_users=None`` in ``list_pockets``; the lookup then
    runs once per pocket."""
    calls: list[list[str]] = []
    real = pockets_service._resolve_user_ids

    async def _counting(ids):
        calls.append(list(ids))
        return await real(ids)

    monkeypatch.setattr(pockets_service, "_resolve_user_ids", _counting)
    for i in range(4):
        await _seed(name=f"t{i}", team=[str(PydanticObjectId()), ME])

    rows = await pockets_service.list_pockets(WS, ME)

    assert len(calls) == 1
    assert len(rows) == 4
    # An id with no User row still renders the "Unknown" fallback.
    assert all(t["fullName"] == "Unknown" for r in rows for t in r["team"])


async def test_one_spec_resolves_a_repeated_marker_once(mongo_db, monkeypatch):  # noqa: ARG001
    """Within a single resolve (``GET /pockets/{id}``), identical markers share
    one read and distinct markers resolve concurrently."""
    calls: list[dict] = []

    async def _fake(ctx, args):
        calls.append(args)
        return ["x"]

    monkeypatch.setitem(ripple_resolver._REGISTRY, "test.fake", _fake)
    spec = {
        "a": {"$source": "test.fake", "k": 1},
        "b": [{"$source": "test.fake", "k": 1}, {"$source": "test.fake", "k": 2}],
    }
    ctx = ripple_resolver.ResolveCtx(workspace_id=WS, user_id=ME, pocket_id="p")

    out = await ripple_resolver.resolve_ripple_spec(spec, ctx)

    assert out == {"a": ["x"], "b": [["x"], ["x"]]}
    assert sorted(c["k"] for c in calls) == [1, 2]
    assert spec["a"] == {"$source": "test.fake", "k": 1}  # input not mutated


# ---------------------------------------------------------------------------
# #10/#11 — visible_pocket_refs has list_pockets' visibility, exactly
# ---------------------------------------------------------------------------


async def _seed_visibility_matrix() -> dict[str, str]:
    return {
        "mine_private": await _seed(name="mine_private", visibility="private"),
        "mine_workspace": await _seed(name="mine_workspace"),
        "other_private": await _seed(name="other_private", owner=OTHER, visibility="private"),
        "other_private_shared": await _seed(
            name="other_private_shared", owner=OTHER, visibility="private", shared_with=[ME]
        ),
        "other_private_team": await _seed(
            name="other_private_team", owner=OTHER, visibility="private", team=[ME]
        ),
        "other_workspace": await _seed(name="other_workspace", owner=OTHER),
        "foreign_ws": await _seed(name="foreign_ws", workspace="ws_elsewhere"),
    }


async def test_refs_exclude_another_users_private_pocket(mongo_db):  # noqa: ARG001
    """Mutation: widen the visibility branch to ``{"$ne": None}``. The private
    pocket then leaks into mission control, kb scopes and the surface preamble.
    (Plan: tests/mutations/pocket_query_patterns.json.)"""
    ids = await _seed_visibility_matrix()

    refs = {r["_id"] for r in await pockets_service.visible_pocket_refs(WS, ME)}

    assert ids["other_private"] not in refs
    assert ids["foreign_ws"] not in refs


async def test_refs_include_shared_with_and_team_pockets(mongo_db):  # noqa: ARG001
    ids = await _seed_visibility_matrix()

    refs = {r["_id"] for r in await pockets_service.visible_pocket_refs(WS, ME)}

    assert ids["other_private_shared"] in refs
    assert ids["other_private_team"] in refs


@pytest.mark.parametrize("viewer", [ME, OTHER, "u_stranger"])
async def test_refs_match_list_pockets_exactly(mongo_db, viewer):  # noqa: ARG001
    """The whole matrix, per viewer, in the same order."""
    await _seed_visibility_matrix()

    full = await pockets_service.list_pockets(WS, viewer)
    refs = await pockets_service.visible_pocket_refs(WS, viewer)

    assert [r["_id"] for r in refs] == [p["_id"] for p in full]
    assert [r["name"] for r in refs] == [p["name"] for p in full]


async def test_refs_honour_the_project_filter_like_list_pockets(mongo_db):  # noqa: ARG001
    await _seed(name="in_a", project_id="proj-a")
    await _seed(name="loose")

    for project_id in ("proj-a", ""):
        full = await pockets_service.list_pockets(WS, ME, project_id=project_id)
        refs = await pockets_service.visible_pocket_refs(WS, ME, project_id=project_id)
        assert [r["_id"] for r in refs] == [p["_id"] for p in full]
        assert len(refs) == 1


async def test_refs_are_one_projected_query_with_counts(pocket_spy):
    await _seed(
        name="w",
        type="dashboard",
        widgets=[{"name": "a", "spec": {"big": "x" * 500}}, {"name": "b"}],
        agents=["ag1"],
        rippleSpec=_spec_with_markers(),
    )
    pocket_spy.finds = 0

    refs = await pockets_service.visible_pocket_refs(WS, ME)

    assert pocket_spy.finds == 1  # no $source resolution
    assert "rippleSpec" not in pocket_spy.projections[0]
    assert refs == [
        {
            "_id": refs[0]["_id"],
            "name": "w",
            "type": "dashboard",
            "widget_count": 2,
            "agent_count": 1,
        }
    ]


# ---------------------------------------------------------------------------
# #9 — site_pocket_ids is projected
# ---------------------------------------------------------------------------


async def test_site_pocket_ids_reads_only_pocket_id(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.sites import service as sites_service

    inner = Site.get_pymongo_collection()
    await inner.insert_many(
        [
            {"workspace": WS, "pocket_id": "pk1"},
            {"workspace": WS, "pocket_id": "pk2", "archived": False},
            {"workspace": WS, "pocket_id": "pk3", "archived": True},
            {"workspace": "ws_elsewhere", "pocket_id": "pk4"},
        ]
    )
    spy = _FindSpy(inner)
    monkeypatch.setattr(Site, "get_pymongo_collection", classmethod(lambda cls: spy))  # noqa: ARG005

    assert await sites_service.site_pocket_ids(WS) == {"pk1", "pk2"}
    assert spy.projections == [{"_id": 0, "pocket_id": 1}]


# ---------------------------------------------------------------------------
# #26 — GET /pockets/builtin-widgets does not write once seeded
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def builtin_doc(mongo_db, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud.models.builtin_widget import BuiltInWidget

    monkeypatch.setattr(pockets_service, "_builtin_seeded_collection", None)
    return BuiltInWidget


def _forbid_writes(monkeypatch, doc_cls) -> None:
    async def _no_write(self, *a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("builtin-widgets GET wrote to the database")

    monkeypatch.setattr(doc_cls, "insert", _no_write)
    monkeypatch.setattr(doc_cls, "save", _no_write)


async def test_builtin_widgets_get_does_not_write_after_the_first_seed(builtin_doc, monkeypatch):
    """Mutation: delete the ``collection is _builtin_seeded_collection`` early
    return AND the change check; every GET then saves six rows again."""
    first = await pockets_service.list_builtin_widgets()
    assert len(first) == 6

    _forbid_writes(monkeypatch, builtin_doc)
    assert await pockets_service.list_builtin_widgets() == first


async def test_builtin_widgets_seeded_db_costs_no_write_in_a_new_process(builtin_doc, monkeypatch):
    """A new process (flag reset) against an already-seeded, unchanged DB reads
    and does not rewrite. Mutation: drop the ``if any(... != v ...)`` guard."""
    await pockets_service.list_builtin_widgets()
    monkeypatch.setattr(pockets_service, "_builtin_seeded_collection", None)

    _forbid_writes(monkeypatch, builtin_doc)
    assert len(await pockets_service.list_builtin_widgets()) == 6


async def test_builtin_widgets_still_refresh_a_drifted_row(builtin_doc):
    await pockets_service.list_builtin_widgets()
    row = await builtin_doc.find_one({"slug": "mission-tray"})
    row.color = "#000000"
    await row.save()
    pockets_service._builtin_seeded_collection = None

    await pockets_service.list_builtin_widgets()

    assert (await builtin_doc.find_one({"slug": "mission-tray"})).color == "#FCD34D"
