# test_audit_service.py — service-level tests for the Audit entity.
# Created: 2026-05-17 (Q1=B1) — Covers the workspace-tenancy invariant,
#   FTS / category / pocket / limit forwarding, and validation. The
#   router tests exercise the HTTP wiring; this file is direct
#   ``audit_service.agent_list_audit`` calls against a tmp store.
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from ee.cloud._core.context import RequestContext, ScopeKind
from ee.cloud.audit import service as audit_service
from ee.cloud.audit.dto import ListAuditRequest


def _ctx(workspace: str | None = "w1", user: str = "u1") -> RequestContext:
    return RequestContext(
        user_id=user,
        workspace_id=workspace,
        request_id="test",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Empty / tenancy
# ---------------------------------------------------------------------------


async def test_returns_empty_for_fresh_workspace(audit_store_tmp) -> None:
    out = await audit_service.agent_list_audit(
        _ctx(workspace="w1"),
        ListAuditRequest(),
        store=audit_store_tmp,
    )
    assert out.entries == []
    assert out.total == 0


async def test_returns_workspace_entries_only(audit_store_tmp, make_audit_entry) -> None:
    await make_audit_entry("w1", description="w1-row")
    await make_audit_entry("w2", description="w2-row")

    out = await audit_service.agent_list_audit(
        _ctx(workspace="w1"),
        ListAuditRequest(),
        store=audit_store_tmp,
    )
    assert {e.description for e in out.entries} == {"w1-row"}
    assert out.total == 1


async def test_ctx_without_workspace_returns_empty(audit_store_tmp, make_audit_entry) -> None:
    await make_audit_entry("w1", description="row")
    out = await audit_service.agent_list_audit(
        _ctx(workspace=None),
        ListAuditRequest(),
        store=audit_store_tmp,
    )
    assert out.entries == []
    assert out.total == 0


# ---------------------------------------------------------------------------
# Filter forwarding
# ---------------------------------------------------------------------------


async def test_q_param_forwards_to_search(audit_store_tmp, make_audit_entry) -> None:
    await make_audit_entry("w1", description="apple pie")
    await make_audit_entry("w1", description="banana split")

    out = await audit_service.agent_list_audit(
        _ctx(workspace="w1"),
        ListAuditRequest(q="apple"),
        store=audit_store_tmp,
    )
    assert {e.description for e in out.entries} == {"apple pie"}


async def test_category_filter_forwards(audit_store_tmp, make_audit_entry) -> None:
    await make_audit_entry("w1", category="security", description="s")
    await make_audit_entry("w1", category="decision", description="d")

    out = await audit_service.agent_list_audit(
        _ctx(workspace="w1"),
        ListAuditRequest(category="security"),
        store=audit_store_tmp,
    )
    assert [e.category for e in out.entries] == ["security"]


async def test_limit_forwards(audit_store_tmp, make_audit_entry) -> None:
    for i in range(5):
        await make_audit_entry("w1", description=f"row-{i}")

    out = await audit_service.agent_list_audit(
        _ctx(workspace="w1"),
        ListAuditRequest(limit=2),
        store=audit_store_tmp,
    )
    assert len(out.entries) == 2
    assert out.total == len(out.entries)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


async def test_invalid_category_validates() -> None:
    with pytest.raises(PydanticValidationError):
        ListAuditRequest.model_validate({"category": "bogus"})
