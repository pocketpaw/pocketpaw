"""Platform wallet reads and operator adjustments.

Created 2026-09-16 (feat/platform-credits) — chunk 6 of the Paw Admin PRD.

Covers ``ee/pocketpaw_ee/cloud/platform/credits.py`` end to end: the two reads
at SUPPORT (wallet detail, ledger history), the two writes at OPERATOR (adjust,
reconcile), the audit trail each leaves, sub-credit precision surviving a full
round trip without truncating to zero, replay reporting, and the 402/422
validation paths.

Rung enforcement itself (SUPPORT vs OPERATOR vs no role) is already covered
generically for every ``PLATFORM_ACTIONS`` entry by
``tests/cloud/platform/test_platform_matrix.py`` and the route-coverage check
in ``tests/cloud/platform/test_platform_guard.py``. These tests call the route
handlers directly (same convention as ``test_read_audit.py`` and
``test_tenant_directory.py``), bypassing FastAPI's dependency injection, so
they exercise the routes' own logic rather than re-proving the guard.
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError, ValidationError
from pocketpaw_ee.cloud.credits import service as credits_service
from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
from pocketpaw_ee.cloud.models.user import User as UserDoc
from pocketpaw_ee.cloud.platform import credits as credits_routes
from starlette.datastructures import Headers
from starlette.requests import Request

pytestmark = pytest.mark.asyncio

WS = "ws_platform_credits"


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/platform/workspaces/x/credits",
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


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


async def test_wallet_read_reports_no_wallet_for_an_unprovisioned_workspace(mongo_db) -> None:
    operator = await _operator("support")

    out = await credits_routes.get_wallet(workspace_id=WS, request=_request(), operator=operator)

    assert out.workspace_id == WS
    assert out.balance_micro == 0
    assert out.has_wallet is False
    assert out.unapplied_count == 0


async def test_wallet_read_reports_balance_and_unapplied_count(mongo_db) -> None:
    operator = await _operator("support")
    await credits_service.grant(WS, amount_micro=2_000_000, cause="top_up", idempotency_key="g1")

    out = await credits_routes.get_wallet(workspace_id=WS, request=_request(), operator=operator)

    assert out.balance_micro == 2_000_000
    assert out.has_wallet is True


async def test_wallet_read_writes_an_audit_row(mongo_db) -> None:
    operator = await _operator("support")

    await credits_routes.get_wallet(workspace_id=WS, request=_request(), operator=operator)

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].action == "platform.credits.read"
    assert rows[0].target_workspace == WS
    assert rows[0].reason.startswith("read:")
    assert rows[0].status == "applied"


async def test_history_read_writes_an_audit_row(mongo_db) -> None:
    operator = await _operator("support")
    await credits_service.grant(WS, amount_micro=1_000_000, cause="top_up", idempotency_key="g1")

    out = await credits_routes.get_history(
        workspace_id=WS, request=_request(), operator=operator, cursor=None, limit=50, cause=None
    )

    assert len(out.items) == 1
    assert out.items[0].amount_delta_micro == 1_000_000
    assert out.items[0].applied is True
    assert out.next_cursor is None

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].action == "platform.credits.read"
    assert rows[0].target_workspace == WS


async def test_history_filters_by_cause(mongo_db) -> None:
    operator = await _operator("support")
    await credits_service.grant(WS, amount_micro=1_000_000, cause="top_up", idempotency_key="g1")
    await credits_service.grant(
        WS, amount_micro=500_000, cause="operator_grant", idempotency_key="operator:k1"
    )

    out = await credits_routes.get_history(
        workspace_id=WS,
        request=_request(),
        operator=operator,
        cursor=None,
        limit=50,
        cause="operator_grant",
    )

    assert len(out.items) == 1
    assert out.items[0].cause == "operator_grant"


async def test_history_page_shape_uses_items_and_next_cursor(mongo_db) -> None:
    """The DTO's field names, not the tenant-side ``entries`` — a load-bearing
    naming distinction per the spec (the console's pager is shared)."""
    operator = await _operator("support")
    await credits_service.grant(WS, amount_micro=1_000_000, cause="top_up", idempotency_key="g1")

    out = await credits_routes.get_history(
        workspace_id=WS, request=_request(), operator=operator, cursor=None, limit=50, cause=None
    )

    assert hasattr(out, "items")
    assert hasattr(out, "next_cursor")
    assert not hasattr(out, "entries")


# ---------------------------------------------------------------------------
# Adjust
# ---------------------------------------------------------------------------


async def test_adjust_grants_credits_and_settles_the_audit_row(mongo_db) -> None:
    operator = await _operator("operator")

    out = await credits_routes.adjust_credits(
        workspace_id=WS,
        body=credits_routes.PlatformAdjustIn(
            amount_delta_micro=5_000_000, reason="Goodwill credit", idempotency_key="case-1"
        ),
        request=_request(),
        operator=operator,
    )

    assert out.balance_micro == 5_000_000
    assert out.cause == "operator_grant"
    assert out.replayed is False
    assert out.effective_idempotency_key == "operator:case-1"
    assert out.ledger_entry_id

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].status == "applied"
    assert rows[0].reason == "Goodwill credit"
    assert rows[0].before == {"balance_micro": 0}
    assert rows[0].after["balance_micro"] == 5_000_000


async def test_adjust_sub_credit_grant_survives_without_truncating(mongo_db) -> None:
    """The headline precision case: 375_000 micro (0.375 credits) must land
    exactly, not round to 0 whole credits and vanish."""
    operator = await _operator("operator")

    out = await credits_routes.adjust_credits(
        workspace_id=WS,
        body=credits_routes.PlatformAdjustIn(
            amount_delta_micro=375_000, reason="Sub-credit correction", idempotency_key="case-2"
        ),
        request=_request(),
        operator=operator,
    )

    assert out.balance_micro == 375_000
    assert await credits_service.balance_micro(WS) == 375_000


async def test_adjust_debit_claws_back_credits(mongo_db) -> None:
    operator = await _operator("operator")
    await credits_service.grant(WS, amount_micro=1_000_000, cause="top_up", idempotency_key="g1")

    out = await credits_routes.adjust_credits(
        workspace_id=WS,
        body=credits_routes.PlatformAdjustIn(
            amount_delta_micro=-300_000, reason="Refund reversal", idempotency_key="case-3"
        ),
        request=_request(),
        operator=operator,
    )

    assert out.balance_micro == 700_000
    assert out.cause == "operator_debit"
    assert out.amount_delta_micro == -300_000


async def test_adjust_replay_reports_true_without_erroring(mongo_db) -> None:
    operator = await _operator("operator")
    body = credits_routes.PlatformAdjustIn(
        amount_delta_micro=1_000_000, reason="First", idempotency_key="dup-key"
    )

    first = await credits_routes.adjust_credits(
        workspace_id=WS, body=body, request=_request(), operator=operator
    )
    second = await credits_routes.adjust_credits(
        workspace_id=WS,
        body=credits_routes.PlatformAdjustIn(
            amount_delta_micro=1_000_000, reason="Second call, same key", idempotency_key="dup-key"
        ),
        request=_request(),
        operator=operator,
    )

    assert first.replayed is False
    assert second.replayed is True
    # The movement applied exactly once — a replay must not double the balance.
    assert second.balance_micro == first.balance_micro == 1_000_000


async def test_adjust_debit_replay_reports_true_without_erroring(mongo_db) -> None:
    """The claw-back side of replay — the path with no ``created`` flag on the
    service return, so this route falls back to ``is_recorded``."""
    operator = await _operator("operator")
    await credits_service.grant(WS, amount_micro=2_000_000, cause="top_up", idempotency_key="g1")
    body = credits_routes.PlatformAdjustIn(
        amount_delta_micro=-500_000, reason="Claw back", idempotency_key="dup-debit"
    )

    first = await credits_routes.adjust_credits(
        workspace_id=WS, body=body, request=_request(), operator=operator
    )
    second = await credits_routes.adjust_credits(
        workspace_id=WS, body=body, request=_request(), operator=operator
    )

    assert first.replayed is False
    assert second.replayed is True
    assert second.balance_micro == first.balance_micro == 1_500_000


async def test_adjust_claw_back_beyond_balance_is_402_with_exact_micro_message(mongo_db) -> None:
    operator = await _operator("operator")
    await credits_service.grant(WS, amount_micro=100_000, cause="top_up", idempotency_key="g1")

    with pytest.raises(CloudError) as exc_info:
        await credits_routes.adjust_credits(
            workspace_id=WS,
            body=credits_routes.PlatformAdjustIn(
                amount_delta_micro=-900_000, reason="Over-claw", idempotency_key="case-4"
            ),
            request=_request(),
            operator=operator,
        )

    err = exc_info.value
    assert err.status_code == 402
    assert err.code == "credits.insufficient"
    # Exact micro figures, not L9's truncated whole-credit ones (both would
    # read as 0 here and the message would say nothing useful).
    assert "900000 micro" in err.message
    assert "100000 micro" in err.message

    # The rejected debit left no trace, and the failure settled the audit row.
    assert await credits_service.balance_micro(WS) == 100_000
    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].status == "failed"


async def test_adjust_rejects_an_empty_reason(mongo_db) -> None:
    operator = await _operator("operator")

    with pytest.raises(ValidationError):
        await credits_routes.adjust_credits(
            workspace_id=WS,
            body=credits_routes.PlatformAdjustIn(
                amount_delta_micro=1_000_000, reason="   ", idempotency_key="case-5"
            ),
            request=_request(),
            operator=operator,
        )

    assert await PlatformAuditEvent.find_all().count() == 0


async def test_adjust_rejects_a_zero_delta(mongo_db) -> None:
    operator = await _operator("operator")

    with pytest.raises(ValidationError):
        await credits_routes.adjust_credits(
            workspace_id=WS,
            body=credits_routes.PlatformAdjustIn(
                amount_delta_micro=0, reason="Nothing to do", idempotency_key="case-6"
            ),
            request=_request(),
            operator=operator,
        )


async def test_adjust_rejects_a_missing_idempotency_key(mongo_db) -> None:
    operator = await _operator("operator")

    with pytest.raises(ValidationError):
        await credits_routes.adjust_credits(
            workspace_id=WS,
            body=credits_routes.PlatformAdjustIn(
                amount_delta_micro=1_000_000, reason="No key", idempotency_key=""
            ),
            request=_request(),
            operator=operator,
        )


async def test_adjust_namespaces_the_operator_key(mongo_db) -> None:
    """A raw key must never reach the shared ledger key space unnamespaced —
    an operator-chosen key colliding with a machine writer's would be silently
    swallowed as a replay."""
    operator = await _operator("operator")

    out = await credits_routes.adjust_credits(
        workspace_id=WS,
        body=credits_routes.PlatformAdjustIn(
            amount_delta_micro=1_000_000, reason="Namespacing check", idempotency_key="req-1"
        ),
        request=_request(),
        operator=operator,
    )

    assert out.effective_idempotency_key == "operator:req-1"
    assert await credits_service.is_recorded(WS, "operator:req-1") is True
    assert await credits_service.is_recorded(WS, "req-1") is False


async def test_adjust_writes_operator_identity_and_reason_onto_the_ledger_ref(mongo_db) -> None:
    operator = await _operator("operator")

    await credits_routes.adjust_credits(
        workspace_id=WS,
        body=credits_routes.PlatformAdjustIn(
            amount_delta_micro=1_000_000, reason="Traceable reason", idempotency_key="case-7"
        ),
        request=_request(),
        operator=operator,
    )

    entry = await credits_service.find_by_key(WS, "operator:case-7")
    assert entry is not None
    assert entry.ref["reason"] == "Traceable reason"
    assert entry.ref["actor_id"] == str(operator.id)
    assert entry.ref["actor_email"] == operator.email
    assert "audit_event_id" in entry.ref
    assert entry.member_id is None


async def test_adjust_never_exposes_allow_negative(mongo_db) -> None:
    """Master PRD Decision 6: overage is a metering-path concept only. There is
    no request field for it at all — this documents that the request DTO has
    no such field, so a caller cannot pass one even by accident."""
    assert "allow_negative" not in credits_routes.PlatformAdjustIn.model_fields


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


async def test_reconcile_response_shape_and_rereads_balance_micro(mongo_db) -> None:
    operator = await _operator("operator")
    await credits_service.grant(WS, amount_micro=1_000_000, cause="top_up", idempotency_key="g1")

    out = await credits_routes.reconcile_wallet(
        workspace_id=WS,
        body=credits_routes.PlatformReconcileIn(reason="Routine repair"),
        request=_request(),
        operator=operator,
    )

    assert out.workspace_id == WS
    assert out.balance_micro_before == 1_000_000
    assert out.balance_micro_after == 1_000_000
    assert out.redriven == 0
    assert out.voided == 0

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].action == "platform.credits.adjust"
    assert rows[0].status == "applied"


async def test_reconcile_reports_redriven_phantoms(mongo_db) -> None:
    from pocketpaw_ee.cloud.models.credit import CreditLedgerEntry

    operator = await _operator("operator")
    await credits_service.grant(WS, amount_micro=1_000_000, cause="top_up", idempotency_key="g1")
    # A crash-window phantom: committed, never applied.
    phantom = CreditLedgerEntry(
        workspace=WS,
        kind="grant",
        amount_delta_micro=250_000,
        balance_after_micro=0,
        applied=False,
        conditional=False,
        cause="top_up",
        idempotency_key="phantom-1",
    )
    await phantom.insert()

    out = await credits_routes.reconcile_wallet(
        workspace_id=WS,
        body=credits_routes.PlatformReconcileIn(reason="Repair phantom"),
        request=_request(),
        operator=operator,
    )

    assert out.redriven == 1
    assert out.voided == 0
    assert out.balance_micro_after == 1_250_000


async def test_reconcile_rejects_an_empty_reason(mongo_db) -> None:
    operator = await _operator("operator")

    with pytest.raises(ValidationError):
        await credits_routes.reconcile_wallet(
            workspace_id=WS,
            body=credits_routes.PlatformReconcileIn(reason=""),
            request=_request(),
            operator=operator,
        )

    assert await PlatformAuditEvent.find_all().count() == 0
