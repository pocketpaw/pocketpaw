"""Cross-tenant plan & entitlement overrides — chunk 7 of the Paw Admin PRD.

Calls the route handlers directly, the same pattern ``test_read_audit.py``
uses: the point under test is what the handler does (audit rows, the actual
override effect), not FastAPI's routing.

The load-bearing test here is ``test_override_actually_changes_resolve_entitlements``
— it is the direct proof of PRD errata C2's fix: an override on a field this
module exposes must change what every enforcement path in the codebase sees,
not just what the console displays.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.entitlements.service import resolve_entitlements
from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
from pocketpaw_ee.cloud.models.user import User as UserDoc
from pocketpaw_ee.cloud.models.workspace import Workspace as WorkspaceDoc
from pocketpaw_ee.cloud.platform import entitlements as routes
from starlette.datastructures import Headers
from starlette.requests import Request

pytestmark = pytest.mark.asyncio


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/platform/workspaces/x/entitlements",
            "headers": Headers(
                raw=[(b"user-agent", b"paw-admin/test"), (b"x-forwarded-for", b"203.0.113.9")]
            ).raw,
            "query_string": b"",
            "client": ("10.0.0.5", 1234),
        }
    )


async def _operator(role: str = "support") -> UserDoc:
    doc = UserDoc(
        email=f"{role}@paw.test",
        hashed_password="x",
        full_name=role.title(),
        platform_role=role,
    )
    await doc.insert()
    return doc


async def _workspace(plan: str = "free") -> WorkspaceDoc:
    ws = WorkspaceDoc(name="Acme", slug="acme", owner="u1", plan=plan)
    await ws.insert()
    return ws


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def test_read_reports_catalog_resolved_and_no_override(mongo_db) -> None:
    operator = await _operator("support")
    ws = await _workspace(plan="free")

    out = await routes.get_entitlements(workspace_id=str(ws.id), request=_request(), operator=operator)

    assert out.workspace_id == str(ws.id)
    assert out.plan == "free"
    assert out.overrides is None
    # No override yet, so catalog and resolved agree on every field.
    assert out.catalog == out.resolved
    assert out.catalog.max_seats == 0  # Free's fail-closed seat cap


async def test_read_writes_an_audit_row(mongo_db) -> None:
    operator = await _operator("support")
    ws = await _workspace()

    await routes.get_entitlements(workspace_id=str(ws.id), request=_request(), operator=operator)

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].action == "platform.entitlements.read"
    assert rows[0].target_workspace == str(ws.id)
    assert rows[0].status == "applied"


# ---------------------------------------------------------------------------
# Write — the C2 fix, proved directly against resolve_entitlements
# ---------------------------------------------------------------------------


async def test_override_actually_changes_resolve_entitlements(mongo_db) -> None:
    """The load-bearing test: an override changes what every enforcement path sees.

    Free's catalog max_seats is 0 and max_call_seconds_per_day is 0 (both
    resolver-enforced — see cloud/entitlements/domain.py). Before any override,
    resolve_entitlements must report the catalog values; after, it must report
    the overridden ones. max_call_seconds_per_day is the field PRD errata C2
    flagged as missing from Decision 7's original list despite being
    resolver-enforced.
    """
    operator = await _operator("operator")
    ws = await _workspace(plan="free")

    before = await resolve_entitlements(str(ws.id))
    assert before.max_seats == 0
    assert before.max_call_seconds_per_day == 0

    body = routes.OverridesWriteIn(
        max_seats=7,
        max_call_seconds_per_day=3600,
        reason="Comping a design-partner trial past the Free caps",
    )
    out = await routes.set_overrides(
        workspace_id=str(ws.id), body=body, request=_request(), operator=operator
    )

    assert out.overrides is not None
    assert out.overrides.max_seats == 7
    assert out.overrides.max_call_seconds_per_day == 3600
    assert out.resolved.max_seats == 7
    assert out.resolved.max_call_seconds_per_day == 3600
    # The catalog value is untouched — this is what proves the response shows
    # "what the plan gives" separately from "what is overridden".
    assert out.catalog.max_seats == 0
    assert out.catalog.max_call_seconds_per_day == 0

    after = await resolve_entitlements(str(ws.id))
    assert after.max_seats == 7
    assert after.max_call_seconds_per_day == 3600


async def test_uncapped_override_clears_the_ceiling(mongo_db) -> None:
    operator = await _operator("operator")
    ws = await _workspace(plan="free")

    body = routes.OverridesWriteIn(monthly_ceiling="uncapped", reason="Enterprise pilot, no cap")
    out = await routes.set_overrides(
        workspace_id=str(ws.id), body=body, request=_request(), operator=operator
    )

    assert out.resolved.monthly_ceiling is None
    resolved = await resolve_entitlements(str(ws.id))
    assert resolved.monthly_ceiling is None


async def test_inert_fields_are_not_exposed_on_the_write_dto() -> None:
    """PRD errata C2: these two fields cannot take effect, so they are refused a slot."""
    fields = set(routes.OverridesWriteIn.model_fields)
    assert "monthly_credit_allotment" not in fields
    assert "extra_features" not in fields
    assert "features" not in fields


async def test_write_records_an_attempted_then_applied_audit_row(mongo_db) -> None:
    operator = await _operator("operator")
    ws = await _workspace()

    body = routes.OverridesWriteIn(max_pockets=500, reason="Bulk import for a pilot")
    await routes.set_overrides(workspace_id=str(ws.id), body=body, request=_request(), operator=operator)

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    row = rows[0]
    assert row.action == "platform.entitlements.write"
    assert row.status == "applied"
    assert row.reason == "Bulk import for a pilot"
    assert row.before == {}
    assert row.after["max_pockets"] == 500


async def test_missing_reason_is_refused(mongo_db) -> None:
    operator = await _operator("operator")
    ws = await _workspace()

    body = routes.OverridesWriteIn(max_seats=10, reason="")
    with pytest.raises(ValidationError):
        await routes.set_overrides(
            workspace_id=str(ws.id), body=body, request=_request(), operator=operator
        )

    # Refused before any audit row or mutation — the reason check comes first.
    assert await PlatformAuditEvent.find_all().to_list() == []
    ws_after = await WorkspaceDoc.get(ws.id)
    assert ws_after.overrides is None


async def test_whitespace_only_reason_is_refused(mongo_db) -> None:
    operator = await _operator("operator")
    ws = await _workspace()

    body = routes.OverridesWriteIn(max_seats=10, reason="   ")
    with pytest.raises(ValidationError):
        await routes.set_overrides(
            workspace_id=str(ws.id), body=body, request=_request(), operator=operator
        )


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------


async def test_clearing_an_override_restores_the_catalog_value(mongo_db) -> None:
    operator = await _operator("operator")
    ws = await _workspace(plan="free")

    await routes.set_overrides(
        workspace_id=str(ws.id),
        body=routes.OverridesWriteIn(max_seats=9, reason="Trial extension"),
        request=_request(),
        operator=operator,
    )
    assert (await resolve_entitlements(str(ws.id))).max_seats == 9

    out = await routes.clear_overrides(
        workspace_id=str(ws.id),
        body=routes.OverridesClearIn(reason="Trial ended"),
        request=_request(),
        operator=operator,
    )

    assert out.overrides is None
    assert out.resolved.max_seats == 0
    resolved = await resolve_entitlements(str(ws.id))
    assert resolved.max_seats == 0


async def test_clear_missing_reason_is_refused(mongo_db) -> None:
    operator = await _operator("operator")
    ws = await _workspace()

    with pytest.raises(ValidationError):
        await routes.clear_overrides(
            workspace_id=str(ws.id),
            body=routes.OverridesClearIn(reason=""),
            request=_request(),
            operator=operator,
        )


async def test_clear_writes_an_audit_row_even_with_nothing_to_clear(mongo_db) -> None:
    """Clearing an already-clear workspace is still a write worth a trail."""
    operator = await _operator("operator")
    ws = await _workspace()

    await routes.clear_overrides(
        workspace_id=str(ws.id),
        body=routes.OverridesClearIn(reason="Confirming no override is active"),
        request=_request(),
        operator=operator,
    )

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].action == "platform.entitlements.write"
    assert rows[0].before == {}
    assert rows[0].after == {}


# ---------------------------------------------------------------------------
# Expiry — a whole-set property, never per-field
# ---------------------------------------------------------------------------


async def test_expired_override_reads_back_as_absent(mongo_db) -> None:
    operator = await _operator("operator")
    ws = await _workspace(plan="free")

    await routes.set_overrides(
        workspace_id=str(ws.id),
        body=routes.OverridesWriteIn(
            max_seats=20,
            reason="Time-boxed comp",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        ),
        request=_request(),
        operator=operator,
    )

    out = await routes.get_entitlements(workspace_id=str(ws.id), request=_request(), operator=operator)
    assert out.overrides is None
    assert out.resolved.max_seats == 0

    resolved = await resolve_entitlements(str(ws.id))
    assert resolved.max_seats == 0


# ---------------------------------------------------------------------------
# Not found
# ---------------------------------------------------------------------------


async def test_read_unknown_workspace_is_not_found(mongo_db) -> None:
    operator = await _operator("support")
    with pytest.raises(NotFound):
        await routes.get_entitlements(
            workspace_id="000000000000000000000000", request=_request(), operator=operator
        )
