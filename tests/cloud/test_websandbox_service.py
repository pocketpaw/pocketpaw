# test_websandbox_service.py — service-level tests for the Web Cursor Sandbox
# Registry (WC-1). Created 2026-07-15 (feat/websandbox-registry).
#
# Covers the security core: a create->get round-trip, fail-closed
# ``authorize_sandbox`` for a non-owning caller (cross-workspace AND
# cross-user-same-workspace), the audit event that a denial writes, the
# owner-scoped ``list_sandboxes``, and the Rule-3 construction-time tenancy
# guard on the domain value object. Uses the ``mongo_db`` fixture (real Beanie
# over mongomock-motor) so the tenant-filtered query paths are exercised, not a
# Protocol fake.
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.errors import Forbidden, NotFound
from pocketpaw_ee.cloud.models.audit_event import AuditEvent
from pocketpaw_ee.cloud.websandbox import service as sandbox_service
from pocketpaw_ee.cloud.websandbox.domain import WebSandboxView
from pocketpaw_ee.cloud.websandbox.dto import CreateSandboxRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


# ---------------------------------------------------------------------------
# create -> get round-trip
# ---------------------------------------------------------------------------


async def test_create_then_get_round_trips() -> None:
    view = await sandbox_service.create_sandbox(
        "w1", "u1", CreateSandboxRequest(repo="github.com/acme/api", sandbox_id="sbx-1")
    )
    assert view.workspace_id == "w1"
    assert view.user_id == "u1"
    assert view.repo == "github.com/acme/api"
    assert view.sandbox_id == "sbx-1"
    assert view.status == "pending"

    fetched = await sandbox_service.get_sandbox("w1", "u1", view.id)
    assert fetched.id == view.id
    assert fetched.repo == "github.com/acme/api"


async def test_create_is_idempotent_on_registry_key() -> None:
    first = await sandbox_service.create_sandbox(
        "w1", "u1", CreateSandboxRequest(repo="r1", status="pending")
    )
    second = await sandbox_service.create_sandbox(
        "w1", "u1", CreateSandboxRequest(repo="r1", status="ready", sandbox_id="sbx-9")
    )
    # Same row (one sandbox per (workspace, user, repo)), updated in place.
    assert second.id == first.id
    assert second.status == "ready"
    assert second.sandbox_id == "sbx-9"

    rows = await sandbox_service.list_sandboxes("w1", "u1")
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# authorize_sandbox — fail closed
# ---------------------------------------------------------------------------


async def test_authorize_allows_owner() -> None:
    await sandbox_service.create_sandbox(
        "w1", "u1", CreateSandboxRequest(repo="r1", sandbox_id="sbx-1")
    )
    view = await sandbox_service.authorize_sandbox("w1", "u1", "sbx-1")
    assert view.sandbox_id == "sbx-1"
    assert view.workspace_id == "w1"


async def test_authorize_denies_cross_workspace() -> None:
    await sandbox_service.create_sandbox(
        "w1", "u1", CreateSandboxRequest(repo="r1", sandbox_id="sbx-1")
    )
    # Same sandbox id, a caller in a DIFFERENT workspace. Tenant filter returns
    # nothing -> fail closed.
    with pytest.raises(Forbidden) as exc:
        await sandbox_service.authorize_sandbox("w2", "u1", "sbx-1")
    assert exc.value.status_code == 403


async def test_authorize_denies_cross_user_same_workspace() -> None:
    await sandbox_service.create_sandbox(
        "w1", "u1", CreateSandboxRequest(repo="r1", sandbox_id="sbx-1")
    )
    # Same workspace, a DIFFERENT user. Row is found by the tenant filter but
    # the owner check fails -> fail closed with the same opaque Forbidden.
    with pytest.raises(Forbidden) as exc:
        await sandbox_service.authorize_sandbox("w1", "u2", "sbx-1")
    assert exc.value.status_code == 403


async def test_authorize_denies_unknown_sandbox() -> None:
    with pytest.raises(Forbidden):
        await sandbox_service.authorize_sandbox("w1", "u1", "does-not-exist")


# ---------------------------------------------------------------------------
# A denial WRITES a high-severity audit event
# ---------------------------------------------------------------------------


async def test_denial_writes_cross_tenant_audit_event() -> None:
    await sandbox_service.create_sandbox(
        "w1", "u1", CreateSandboxRequest(repo="r1", sandbox_id="sbx-1")
    )

    before = await AuditEvent.find({"action": "vm.cross_tenant_denied"}).to_list()
    assert before == []

    with pytest.raises(Forbidden):
        await sandbox_service.authorize_sandbox("w1", "u2", "sbx-1")

    rows = await AuditEvent.find({"action": "vm.cross_tenant_denied"}).to_list()
    assert len(rows) == 1
    ev = rows[0]
    assert ev.workspace == "w1"
    assert ev.actor_id == "u2"
    assert ev.target_type == "web_sandbox"
    assert ev.target_id == "sbx-1"
    assert ev.metadata.get("reason") == "wrong_owner"


async def test_allowed_authorize_writes_no_denial_event() -> None:
    await sandbox_service.create_sandbox(
        "w1", "u1", CreateSandboxRequest(repo="r1", sandbox_id="sbx-1")
    )
    await sandbox_service.authorize_sandbox("w1", "u1", "sbx-1")

    rows = await AuditEvent.find({"action": "vm.cross_tenant_denied"}).to_list()
    assert rows == []


# ---------------------------------------------------------------------------
# list_sandboxes — tenant + owner scoped
# ---------------------------------------------------------------------------


async def test_list_returns_only_callers_rows() -> None:
    await sandbox_service.create_sandbox("w1", "u1", CreateSandboxRequest(repo="a"))
    await sandbox_service.create_sandbox("w1", "u1", CreateSandboxRequest(repo="b"))
    # Same workspace, different user — must not appear in u1's list.
    await sandbox_service.create_sandbox("w1", "u2", CreateSandboxRequest(repo="c"))
    # Different workspace — must not appear either.
    await sandbox_service.create_sandbox("w2", "u1", CreateSandboxRequest(repo="d"))

    rows = await sandbox_service.list_sandboxes("w1", "u1")
    assert {r.repo for r in rows} == {"a", "b"}


async def test_get_denies_cross_tenant_row() -> None:
    other = await sandbox_service.create_sandbox("w2", "u1", CreateSandboxRequest(repo="r1"))
    # A caller in w1 must not be able to read a w2 row by id — indistinguishable
    # from a missing row.
    with pytest.raises(NotFound):
        await sandbox_service.get_sandbox("w1", "u1", other.id)


# ---------------------------------------------------------------------------
# Rule 3 — domain enforces tenancy at construction
# ---------------------------------------------------------------------------


def test_domain_requires_tenancy_fields() -> None:
    # Omitting workspace_id / user_id is a construction-time TypeError — the
    # frozen dataclass has no defaults for the tenancy fields.
    with pytest.raises(TypeError):
        WebSandboxView(  # type: ignore[call-arg]
            id="x",
            repo="r1",
            status="pending",
            sandbox_id=None,
            installation_id=None,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )


def test_domain_is_frozen() -> None:
    view = WebSandboxView(
        id="x",
        workspace_id="w1",
        user_id="u1",
        repo="r1",
        status="pending",
        sandbox_id=None,
        installation_id=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        view.workspace_id = "w2"  # type: ignore[misc]
