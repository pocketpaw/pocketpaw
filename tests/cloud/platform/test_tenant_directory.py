"""Cross-tenant tenant directory: search, pagination and detail reads.

Runs against the mongomock-motor DB, so these exercise the real queries rather
than mocks of them. The import boundary around these helpers is asserted
separately in test_platform_boundary.py.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.models.user import User as UserDoc
from pocketpaw_ee.cloud.models.user import WorkspaceMembership
from pocketpaw_ee.cloud.models.workspace import Workspace as WorkspaceDoc
from pocketpaw_ee.cloud.workspace import service as workspace_service

pytestmark = pytest.mark.asyncio


async def _workspace(name: str, slug: str, owner: str, plan: str = "free") -> WorkspaceDoc:
    doc = WorkspaceDoc(name=name, slug=slug, owner=owner, plan=plan)
    await doc.insert()
    return doc


async def _user(email: str, workspaces: list[tuple[str, str]] | None = None) -> UserDoc:
    doc = UserDoc(
        email=email,
        hashed_password="x",
        full_name=email.split("@")[0],
        workspaces=[
            WorkspaceMembership(workspace=wid, role=role) for wid, role in (workspaces or [])
        ],
    )
    await doc.insert()
    return doc


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


async def test_search_returns_every_tenant(mongo_db) -> None:
    await _workspace("Acme", "acme", "u1")
    await _workspace("Globex", "globex", "u2")

    found, cursor = await workspace_service.platform_search_workspaces()

    assert {w.slug for w in found} == {"acme", "globex"}
    assert cursor is None


async def test_search_matches_slug_and_name(mongo_db) -> None:
    await _workspace("Acme Corporation", "acme", "u1")
    await _workspace("Globex", "globex", "u2")

    by_slug, _ = await workspace_service.platform_search_workspaces(q="acme")
    by_name, _ = await workspace_service.platform_search_workspaces(q="Corporation")

    assert [w.slug for w in by_slug] == ["acme"]
    assert [w.slug for w in by_name] == ["acme"]


async def test_search_matches_the_owner_email(mongo_db) -> None:
    """The support case: an email arrives with no workspace attached."""
    owner = await _user("founder@acme.test")
    await _workspace("Acme", "acme", str(owner.id))
    await _workspace("Globex", "globex", "someone-else")

    found, _ = await workspace_service.platform_search_workspaces(q="founder@acme.test")

    assert [w.slug for w in found] == ["acme"]


async def test_search_is_case_insensitive(mongo_db) -> None:
    await _workspace("Acme", "acme", "u1")
    found, _ = await workspace_service.platform_search_workspaces(q="ACME")
    assert [w.slug for w in found] == ["acme"]


async def test_search_escapes_regex_metacharacters(mongo_db) -> None:
    """A search box is not a regex engine.

    Without escaping, ``.*`` matches every tenant — a caller typing it would get
    the whole table back and would have no way to know the filter was ignored.
    """
    await _workspace("Acme", "acme", "u1")
    await _workspace("Globex", "globex", "u2")

    found, _ = await workspace_service.platform_search_workspaces(q=".*")

    assert found == []


async def test_soft_deleted_tenants_are_excluded_by_default(mongo_db) -> None:
    from datetime import UTC, datetime

    live = await _workspace("Live", "live", "u1")
    gone = await _workspace("Gone", "gone", "u2")
    gone.deleted_at = datetime.now(UTC)
    await gone.save()

    default, _ = await workspace_service.platform_search_workspaces()
    including, _ = await workspace_service.platform_search_workspaces(include_deleted=True)

    assert [w.slug for w in default] == [live.slug]
    assert {w.slug for w in including} == {"live", "gone"}


async def test_plan_filter(mongo_db) -> None:
    await _workspace("A", "a", "u1", plan="free")
    await _workspace("B", "b", "u2", plan="pro")

    found, _ = await workspace_service.platform_search_workspaces(plan="pro")

    assert [w.slug for w in found] == ["b"]


async def test_member_counts_are_populated(mongo_db) -> None:
    ws = await _workspace("Acme", "acme", "u1")
    await _user("a@acme.test", [(str(ws.id), "owner")])
    await _user("b@acme.test", [(str(ws.id), "member")])
    await _user("elsewhere@other.test", [("some-other-workspace", "owner")])

    found, _ = await workspace_service.platform_search_workspaces()

    assert [w.member_count for w in found] == [2]


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def test_pagination_walks_every_row_exactly_once(mongo_db) -> None:
    """The property that matters: no row skipped, no row seen twice.

    Cursoring on _id rather than a timestamp is what buys this. Rows created in
    the same clock tick share a createdAt, and a strict timestamp cursor would
    drop whichever one landed on the boundary.
    """
    for i in range(7):
        await _workspace(f"WS{i}", f"ws{i}", "u1")

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # bounded, so a cursor bug cannot hang the suite
        page, cursor = await workspace_service.platform_search_workspaces(limit=2, cursor=cursor)
        seen.extend(w.slug for w in page)
        if cursor is None:
            break

    assert len(seen) == 7, seen
    assert len(set(seen)) == 7, "a row was returned on two different pages"
    assert cursor is None


async def test_last_page_reports_no_cursor(mongo_db) -> None:
    await _workspace("Only", "only", "u1")
    page, cursor = await workspace_service.platform_search_workspaces(limit=50)
    assert len(page) == 1
    assert cursor is None


# ---------------------------------------------------------------------------
# Detail
# ---------------------------------------------------------------------------


async def test_get_workspace_returns_a_soft_deleted_tenant(mongo_db) -> None:
    """Unlike every tenant-scoped read, which treats deleted as missing.

    An operator opening this page is usually asking why the account is gone.
    """
    from datetime import UTC, datetime

    ws = await _workspace("Gone", "gone", "u1")
    ws.deleted_at = datetime.now(UTC)
    await ws.save()

    found = await workspace_service.platform_get_workspace(str(ws.id))

    assert found.slug == "gone"
    assert found.deleted_at is not None


async def test_get_workspace_raises_for_an_unknown_id(mongo_db) -> None:
    from pocketpaw_ee.cloud._core.errors import NotFound

    with pytest.raises(NotFound):
        await workspace_service.platform_get_workspace("507f1f77bcf86cd799439011")


async def test_get_workspace_raises_for_a_malformed_id(mongo_db) -> None:
    """A bad id is a 404, not a 500 from ObjectId parsing."""
    from pocketpaw_ee.cloud._core.errors import NotFound

    with pytest.raises(NotFound):
        await workspace_service.platform_get_workspace("not-an-object-id")


async def test_list_members_needs_no_membership(mongo_db) -> None:
    """The whole point: the caller is not in this workspace."""
    ws = await _workspace("Acme", "acme", "u1")
    await _user("a@acme.test", [(str(ws.id), "owner")])
    await _user("b@acme.test", [(str(ws.id), "member")])

    members = await workspace_service.platform_list_members(str(ws.id))

    assert {m.email for m in members} == {"a@acme.test", "b@acme.test"}
    assert {m.role for m in members} == {"owner", "member"}


# ---------------------------------------------------------------------------
# User lookup
# ---------------------------------------------------------------------------


async def test_find_users_returns_memberships(mongo_db) -> None:
    ws = await _workspace("Acme", "acme", "u1")
    await _user("person@acme.test", [(str(ws.id), "admin")])

    rows = await workspace_service.platform_find_users(email="person@acme.test")

    assert len(rows) == 1
    _user_id, email, _name, memberships = rows[0]
    assert email == "person@acme.test"
    assert memberships == [(str(ws.id), "admin")]


async def test_find_users_matches_a_substring(mongo_db) -> None:
    """Support requests arrive with the wrong case or a partial address."""
    await _user("Someone@Acme.TEST")
    rows = await workspace_service.platform_find_users(email="acme")
    assert len(rows) == 1


async def test_find_users_escapes_regex(mongo_db) -> None:
    await _user("a@acme.test")
    rows = await workspace_service.platform_find_users(email=".*")
    assert rows == []
