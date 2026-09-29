# Bounds on the listing endpoints that used to read whole collections, the
# indexes that back them, and the rewrites that must keep old answers exactly.
#
# Each section pins one change: the cap is applied and keeps the NEWEST rows,
# paging reaches the rest, the wire shape is unchanged, and where a query was
# rewritten (the per-mandate health loop, the folder-prefix scan) the new form
# is checked against the old one on data built to tell them apart. The mutation
# plan tests/mutations/list_bounds.json breaks each guard and names these tests.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.models.session import Session
from pocketpaw_ee.cloud.sessions import service as sessions_service
from pocketpaw_ee.cloud.sessions.dto import session_to_wire_dict

pytestmark = pytest.mark.usefixtures("mongo_db")

_T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)


async def _sess(sid: str, minutes: int, **ov) -> Session:
    fields = {
        "sessionId": sid,
        "context_type": "session",
        "workspace": "w1",
        "owner": "u1",
        "title": sid,
        "lastActivity": _T0 + timedelta(minutes=minutes),
    }
    fields.update(ov)
    doc = Session(**fields)
    await doc.insert()
    return doc


def _sessions_app(user_id: str = "u1", workspace_id: str = "w1") -> FastAPI:
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.sessions.router import router
    from pocketpaw_ee.cloud.shared.deps import current_user_id

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role="member")],
    )
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[current_user_id] = lambda: user_id
    return app


async def _get(app: FastAPI, url: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get(url)


# --- GET /sessions -----------------------------------------------------------


async def test_list_for_owner_keeps_the_newest_rows_up_to_the_limit() -> None:
    for i in range(5):
        await _sess(f"s{i}", i)
    ctx = sessions_service.legacy_ctx("u1", "w1")
    rows = await sessions_service.list_for_owner(ctx, "w1", limit=3)
    assert [r.sessionId for r in rows] == ["s4", "s3", "s2"]


def test_service_listings_default_to_a_cap() -> None:
    assert sessions_service.LIST_LIMIT == 200
    for fn in (
        sessions_service.list_for_owner,
        sessions_service.list_by_agent,
        sessions_service.list_for_pocket,
    ):
        assert fn.__kwdefaults__["limit"] == sessions_service.LIST_LIMIT, fn


async def test_route_caps_pages_by_cursor_and_keeps_the_bare_list_shape() -> None:
    docs = [await _sess(f"s{i}", i) for i in range(5)]
    app = _sessions_app()

    first = await _get(app, "/api/v1/sessions?limit=2")
    assert first.status_code == 200, first.text
    body = first.json()
    assert isinstance(body, list)
    assert [r["sessionId"] for r in body] == ["s4", "s3"]
    # Same wire dict as before the cap: session_to_wire_dict of the domain row.
    stored = [await Session.get(d.id) for d in docs[::-1][:2]]
    expected = [session_to_wire_dict(sessions_service._to_domain(d)) for d in stored]
    assert body == expected
    cursor = first.headers["x-next-cursor"]

    second = await _get(app, f"/api/v1/sessions?limit=2&cursor={cursor}")
    assert [r["sessionId"] for r in second.json()] == ["s2", "s1"]
    third = await _get(app, f"/api/v1/sessions?limit=2&cursor={second.headers['x-next-cursor']}")
    assert [r["sessionId"] for r in third.json()] == ["s0"]
    assert "x-next-cursor" not in third.headers


async def test_route_default_limit_is_200_and_upper_bound_is_enforced() -> None:
    for i in range(203):
        await _sess(f"s{i:03d}", i)
    app = _sessions_app()
    res = await _get(app, "/api/v1/sessions")
    assert len(res.json()) == 200
    assert res.headers.get("x-next-cursor")
    assert (await _get(app, "/api/v1/sessions?limit=501")).status_code == 422


async def test_route_agent_filter_is_capped_newest_first() -> None:
    for i in range(4):
        await _sess(f"a{i}", i, agent="agent-A")
    await _sess("b0", 10, agent="agent-B")
    res = await _get(_sessions_app(), "/api/v1/sessions?agent_id=agent-A&limit=2")
    assert [r["sessionId"] for r in res.json()] == ["a3", "a2"]


# --- POST /sessions/by-agents -------------------------------------------------


async def test_by_agents_caps_each_agent_independently() -> None:
    for i in range(5):
        await _sess(f"a{i}", i, agent="agent-A")
    await _sess("b0", 100, agent="agent-B")
    await _sess("a-foreign", 50, agent="agent-A", owner="u2")
    ctx = sessions_service.legacy_ctx("u1", "w1")

    grouped = await sessions_service.list_by_agents(
        ctx, "w1", ["agent-A", "agent-B", "agent-Z"], per_agent=2
    )

    # A hot agent cannot starve another: each gets its own newest two.
    assert [s.sessionId for s in grouped["agent-A"]] == ["a4", "a3"]
    assert [s.sessionId for s in grouped["agent-B"]] == ["b0"]
    assert grouped["agent-Z"] == []
    per_agent = await sessions_service.list_by_agent(ctx, "w1", "agent-A", limit=2)
    assert [s.id for s in grouped["agent-A"]] == [s.id for s in per_agent]


async def test_by_agents_default_cap_is_the_agent_limit() -> None:
    for i in range(3):
        await _sess(f"a{i}", i, agent="agent-A")
    ctx = sessions_service.legacy_ctx("u1", "w1")
    assert sessions_service.list_by_agents.__kwdefaults__["per_agent"] == 100
    grouped = await sessions_service.list_by_agents(ctx, "w1", ["agent-A"])
    assert [s.sessionId for s in grouped["agent-A"]] == ["a2", "a1", "a0"]


# --- GET /pockets/{id}/sessions ------------------------------------------------


async def _pocket(workspace: str = "w1", visibility: str = "private") -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket

    doc = Pocket(workspace=workspace, name="p", owner="u_owner", type="site", visibility=visibility)
    await doc.insert()
    return str(doc.id)


async def test_pocket_list_is_capped_newest_first(monkeypatch) -> None:
    async def _member(workspace_id: str, user_id: str) -> bool:
        return workspace_id == "w1"

    monkeypatch.setattr(sessions_service, "_is_workspace_member", _member)
    pid = await _pocket(visibility="workspace")
    for i in range(4):
        await _sess(f"p{i}", i, context_type="pocket", pocket=pid, owner="u_owner")
    rows = await sessions_service.list_for_pocket(
        sessions_service.legacy_ctx("u_reader"), pid, limit=3
    )
    assert [r.sessionId for r in rows] == ["p3", "p2", "p1"]


async def test_non_reader_pocket_list_is_pinned_to_the_pockets_workspace() -> None:
    # A private pocket the caller cannot read: they get their own threads only,
    # and only the ones in the pocket's workspace. A row stamped with this pocket
    # from another workspace (possible only for rows written before
    # ``_refuse_foreign_scope_ids``) is no longer listed.
    pid = await _pocket(workspace="w1", visibility="private")
    await _sess("mine", 1, context_type="pocket", pocket=pid, owner="u1")
    await _sess("mine-foreign-ws", 2, context_type="pocket", pocket=pid, owner="u1", workspace="w2")
    await _sess("theirs", 3, context_type="pocket", pocket=pid, owner="u2")

    rows = await sessions_service.list_for_pocket(sessions_service.legacy_ctx("u1"), pid)

    assert [r.sessionId for r in rows] == ["mine"]


async def test_missing_pocket_keeps_the_owner_only_list() -> None:
    await _sess("orphan", 1, context_type="pocket", pocket="deadbeefdeadbeefdeadbeef", owner="u1")
    rows = await sessions_service.list_for_pocket(
        sessions_service.legacy_ctx("u1"), "deadbeefdeadbeefdeadbeef"
    )
    assert [r.sessionId for r in rows] == ["orphan"]


# --- chat preamble count ---------------------------------------------------------


async def test_count_for_user_matches_the_listing_per_surface() -> None:
    await _sess("c1", 1, surface="chat")
    await _sess("legacy", 2)  # surface None reads as chat
    await _sess("f1", 3, surface="files")
    await _sess("gone", 4, surface="chat", deleted_at=_T0)
    await _sess("other-owner", 5, surface="chat", owner="u2")
    for surface in ("chat", "files", None):
        listed = await sessions_service.list_for_user("w1", "u1", surface=surface)
        assert await sessions_service.count_for_user("w1", "u1", surface=surface) == len(listed)
    assert await sessions_service.count_for_user("w1", "u1", surface="chat") == 2


# --- GET /sessions/runtime ---------------------------------------------------------


async def test_runtime_index_limit_keeps_newest_and_total_counts_all() -> None:
    from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore

    for i in range(4):
        await _sess(f"websocket_{i}", i, context_type="pocket")
    await _sess("websocket_x", 9, context_type="pocket", owner="u2")
    store = MongoMemoryStore()

    index = await store._load_session_index_async(workspace_id="w1", owner_id="u1", limit=2)
    assert list(index) == ["websocket_3", "websocket_2"]
    assert await store._count_session_index_async(workspace_id="w1", owner_id="u1") == 4
    full = await store._load_session_index_async(workspace_id="w1", owner_id="u1")
    assert len(full) == 4


# --- indexes ------------------------------------------------------------------------


def _keys(model) -> list[list[tuple[str, int]]]:
    out = []
    for idx in model.Settings.indexes:
        keys = idx if isinstance(idx, list) else idx.document["key"].items()
        out.append([tuple(k) for k in keys])
    return out


def test_new_indexes_are_declared_on_real_fields() -> None:
    from pocketpaw_ee.cloud.models.task import Task
    from pocketpaw_ee.cloud.uploads.models import FileUpload

    session_idx = _keys(Session)
    assert [("workspace", 1), ("owner", 1), ("agent", 1), ("lastActivity", -1)] in session_idx
    assert [("owner", 1), ("lastActivity", -1)] in session_idx
    assert [("workspace_id", 1), ("createdAt", -1)] in _keys(Task)
    assert [
        ("workspace", 1),
        ("pocket_id", 1),
        ("deleted_at", 1),
        ("createdAt", -1),
    ] in _keys(FileUpload)
    for model in (Session, Task, FileUpload):
        for keys in _keys(model):
            for field, _ in keys:
                assert field in model.model_fields or field == "_id", (model, field)


# --- GET /belt/mandates ----------------------------------------------------------------


async def _old_list_health(workspace_id: str) -> list[dict]:
    """The per-mandate loop this listing used before the aggregation, as the
    oracle the new output must equal."""
    from pocketpaw_ee.cloud.mandates.domain import MandateDoc, ShiftDoc, SightingDoc

    docs = await MandateDoc.find(MandateDoc.workspace == workspace_id).sort("-createdAt").to_list()
    out = []
    for doc in docs:
        mid = str(doc.id)
        last = (
            await ShiftDoc.find(ShiftDoc.workspace == workspace_id, ShiftDoc.mandate_id == mid)
            .sort("-no")
            .first_or_none()
        )
        gates = await ShiftDoc.find(
            ShiftDoc.workspace == workspace_id,
            ShiftDoc.mandate_id == mid,
            ShiftDoc.state == "in_gate",
        ).count()
        sightings = await SightingDoc.find(
            SightingDoc.workspace == workspace_id, SightingDoc.mandate_id == mid
        ).count()
        out.append(
            {
                "id": mid,
                "last_shift_state": last.state if last else None,
                "open_gate_count": gates,
                "sighting_count": sightings,
            }
        )
    return out


async def test_mandate_health_matches_the_old_per_mandate_queries() -> None:
    from pocketpaw_ee.cloud.mandates import service as mandates_service
    from pocketpaw_ee.cloud.mandates.domain import (
        Charter,
        MandateDoc,
        ShiftDoc,
        SightingDoc,
        Surface,
    )

    async def mandate(name: str, ws: str = "w1") -> str:
        doc = MandateDoc(
            workspace=ws, name=name, surface=Surface(repo_id="r"), charter=Charter(goal="g")
        )
        await doc.insert()
        return str(doc.id)

    busy = await mandate("busy")
    idle = await mandate("idle")
    foreign = await mandate("foreign", ws="w2")
    # Inserted out of ``no`` order, so "last" must come from the sort, not
    # insertion order: shift 3 is the newest and is in the gate.
    for no, state in ((2, "done"), (3, "in_gate"), (1, "in_gate")):
        await ShiftDoc(workspace="w1", mandate_id=busy, no=no, state=state).insert()
    for _ in range(3):
        await SightingDoc(
            workspace="w1", mandate_id=busy, patrol="deps", severity=1, summary="s"
        ).insert()
    # Rows in another workspace for the same mandate id must not count.
    await ShiftDoc(workspace="w2", mandate_id=busy, no=9, state="done").insert()
    await SightingDoc(
        workspace="w2", mandate_id=busy, patrol="deps", severity=1, summary="x"
    ).insert()
    await ShiftDoc(workspace="w2", mandate_id=foreign, no=1, state="in_gate").insert()

    got = (await mandates_service.list_mandates("w1", "u1"))["mandates"]
    old = await _old_list_health("w1")

    fields = ("last_shift_state", "open_gate_count", "sighting_count")
    assert [{"id": m["id"], **{k: m["health"][k] for k in fields}} for m in got] == old
    by_id = {m["id"]: m["health"] for m in got}
    assert by_id[busy] == {"last_shift_state": "in_gate", "open_gate_count": 2, "sighting_count": 3}
    assert by_id[idle] == {"last_shift_state": None, "open_gate_count": 0, "sighting_count": 0}


# --- GET /agents -------------------------------------------------------------------------


async def test_agent_list_limit_and_route_cap(monkeypatch) -> None:
    from pocketpaw_ee.cloud.agents import service as agents_service
    from pocketpaw_ee.cloud.agents.router import list_agents as list_route
    from pocketpaw_ee.cloud.models.agent import Agent

    for i in range(4):
        await Agent(
            workspace="w1", name=f"a{i}", slug=f"a{i}", owner="u1", visibility="workspace"
        ).insert()

    assert len(await agents_service.list_agents("w1", limit=2)) == 2
    # Internal callers (planner, kb) omit the limit and still see every agent.
    assert len(await agents_service.list_agents("w1")) == 4

    monkeypatch.setattr(agents_service, "LIST_LIMIT", 3)
    rows = await list_route(workspace_id="w1", user_id="u1", query=None)
    assert len(rows) == 3


# --- DELETE /uploads/folders/{id} ------------------------------------------------------------


async def _file(fid: str, folder_path, *, ws: str = "w1", deleted_at=None):
    from pocketpaw_ee.cloud.uploads.models import FileUpload

    doc = FileUpload(
        file_id=fid,
        storage_key=f"k/{fid}",
        filename=f"{fid}.txt",
        mime="text/plain",
        size=1,
        workspace=ws,
        owner="u1",
        folder_path=folder_path,
        deleted_at=deleted_at,
    )
    await doc.insert()
    return doc


def _old_under(fp, prefix: str) -> bool:
    fp = fp or "/"
    return fp == prefix or (prefix != "/" and fp.startswith(prefix + "/"))


_FOLDER_ROWS = [
    ("at", "/a.b(c)+d"),
    ("child", "/a.b(c)+d/sub"),
    ("grandchild", "/a.b(c)+d/sub/deeper"),
    ("sibling-regex", "/aXb(c)+d"),  # '.' must not match any char
    ("regex-bait", "/aXbccd/sub"),  # what an unescaped "^/a.b(c)+d/" would match
    ("sibling-suffix", "/a.b(c)+dx"),
    ("sibling-name", "/a.b(c)+d x/sub"),
    ("root", "/"),
    ("null", None),
    ("empty", ""),
    ("other", "/z"),
]


@pytest.mark.parametrize("prefix", ["/a.b(c)+d", "/", "/z", "/a.b(c)+d/sub"])
async def test_folder_prefix_matches_the_old_python_test(prefix: str) -> None:
    from pocketpaw_ee.cloud.uploads.models import FileUpload
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    for fid, fp in _FOLDER_ROWS:
        await _file(fid, fp)
        await _file(f"{fid}-w2", fp, ws="w2")
    already = datetime(2026, 1, 1, tzinfo=UTC)
    await _file("was-deleted", prefix, deleted_at=already)
    expected = {fid for fid, fp in _FOLDER_ROWS if _old_under(fp, prefix)}
    store = MongoFileStore()

    assert await store.count_under_prefix("w1", prefix) == len(expected)
    before = {d.file_id: d.updatedAt for d in await FileUpload.find_all().to_list()}
    assert await store.soft_delete_under_prefix("w1", prefix) == len(expected)

    rows = {d.file_id: d for d in await FileUpload.find_all().to_list()}
    assert {fid for fid, d in rows.items() if d.deleted_at and fid != "was-deleted"} == expected
    # Another workspace is untouched; an already-deleted row keeps its stamp.
    assert all(d.deleted_at is None for fid, d in rows.items() if fid.endswith("-w2"))
    assert rows["was-deleted"].deleted_at.replace(tzinfo=UTC) == already
    # Only deleted_at is written, as the per-row saves did.
    assert {fid: d.updatedAt for fid, d in rows.items()} == before
    assert await store.count_under_prefix("w1", prefix) == 0


async def test_rewrite_folder_prefix_still_moves_only_the_subtree() -> None:
    from pocketpaw_ee.cloud.uploads.models import FileUpload
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    for fid, fp in _FOLDER_ROWS:
        await _file(fid, fp)
    moved = await MongoFileStore().rewrite_folder_prefix("w1", "/a.b(c)+d", "/n")
    rows = {d.file_id: d.folder_path for d in await FileUpload.find_all().to_list()}
    assert moved == 3
    assert rows["at"] == "/n"
    assert rows["child"] == "/n/sub"
    assert rows["grandchild"] == "/n/sub/deeper"
    assert rows["sibling-regex"] == "/aXb(c)+d"
    assert rows["sibling-suffix"] == "/a.b(c)+dx"
