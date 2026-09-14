"""Cross-tenant reads leave a trail.

The gap this closes: the read routes shipped with no audit call at all, so a
support operator could enumerate every tenant and pull every member's email with
nothing written down. ``request_logs`` records the route TEMPLATE, not the query
string — so the part that says WHOSE data was read appeared in no log anywhere.

These call the route handlers directly rather than over HTTP, because the point
under test is "does the handler write a row", not the routing.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
from pocketpaw_ee.cloud.models.user import User as UserDoc
from pocketpaw_ee.cloud.models.workspace import Workspace as WorkspaceDoc
from pocketpaw_ee.cloud.platform import users as users_routes
from pocketpaw_ee.cloud.platform import workspaces as workspace_routes
from starlette.datastructures import Headers
from starlette.requests import Request

pytestmark = pytest.mark.asyncio


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/platform/workspaces",
            "headers": Headers(
                raw=[(b"user-agent", b"paw-admin/test"), (b"x-forwarded-for", b"203.0.113.9")]
            ).raw,
            "query_string": b"",
            "client": ("10.0.0.5", 1234),
        }
    )


async def _operator() -> UserDoc:
    doc = UserDoc(
        email="ops@paw.test",
        hashed_password="x",
        full_name="Ops",
        platform_role="support",
    )
    await doc.insert()
    return doc


async def test_searching_tenants_writes_an_audit_row(mongo_db) -> None:
    operator = await _operator()
    await WorkspaceDoc(name="Acme", slug="acme", owner="u1").insert()

    await workspace_routes.search_workspaces(
        request=_request(),
        operator=operator,
        q="acme",
        plan=None,
        include_deleted=False,
        cursor=None,
        limit=50,
    )

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "platform.workspace.read"
    assert row.actor_id == str(operator.id)
    assert row.actor_platform_role == "support"
    # The SEARCH TERM is the sensitive part and must survive into the row.
    assert "acme" in row.reason
    assert row.status == "applied"


async def test_reading_a_tenant_records_which_one(mongo_db) -> None:
    operator = await _operator()
    ws = WorkspaceDoc(name="Acme", slug="acme", owner="u1")
    await ws.insert()

    await workspace_routes.get_workspace(
        workspace_id=str(ws.id),
        request=_request(),
        operator=operator,
    )

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    # Indexed on target_workspace, so "what was done to this tenant" is answerable.
    assert rows[0].target_workspace == str(ws.id)


async def test_member_list_read_is_recorded(mongo_db) -> None:
    operator = await _operator()
    ws = WorkspaceDoc(name="Acme", slug="acme", owner="u1")
    await ws.insert()

    await workspace_routes.list_members(
        workspace_id=str(ws.id),
        request=_request(),
        operator=operator,
    )

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].action == "platform.member.read"
    assert rows[0].target_workspace == str(ws.id)


async def test_user_search_records_the_email_fragment(mongo_db) -> None:
    """Searching for "ceo@competitor" is the action worth reviewing later."""
    operator = await _operator()

    await users_routes.find_users(
        request=_request(),
        operator=operator,
        email="ceo@competitor.test",
        limit=25,
    )

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].action == "platform.user.read"
    assert "ceo@competitor.test" in rows[0].reason


async def test_the_audit_row_carries_caller_evidence(mongo_db) -> None:
    """IP and user-agent, for reconstructing a compromised-account timeline."""
    operator = await _operator()

    await users_routes.find_users(
        request=_request(), operator=operator, email="someone@x.test", limit=25
    )

    row = (await PlatformAuditEvent.find_all().to_list())[0]
    assert row.ip == "203.0.113.9"
    assert row.user_agent == "paw-admin/test"


async def test_reads_are_distinguishable_from_writes(mongo_db) -> None:
    """A read row's reason is machine-generated; a write row's is a human's.

    Both share a collection, so something has to tell them apart when the
    console renders the trail.
    """
    operator = await _operator()

    await users_routes.find_users(request=_request(), operator=operator, email="a@b.test", limit=25)

    row = (await PlatformAuditEvent.find_all().to_list())[0]
    assert row.reason.startswith("read:")
    assert row.action.endswith(".read")
