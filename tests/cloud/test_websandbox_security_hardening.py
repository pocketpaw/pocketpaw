# test_websandbox_security_hardening.py — regression tests for the 2026-07-16
# review-hardening pass on the Web Cursor cloud backend.
#
# Locks four fixes surfaced by the pre-merge self-review of PR #1730:
#   1. HIGH — the client register route must NOT let a caller bind a server-owned
#      ``sandbox_id``. authorize_sandbox trusts the row's Daytona id as the
#      ownership key, so a forgeable binding was a cross-tenant VM takeover. The
#      route now binds the repo-only ``RegisterSandboxRequest`` and there is no
#      client PATCH lifecycle route. Tested at the actual router boundary + an
#      end-to-end "the oracle still denies the attacker" proof.
#   2. MEDIUM — re-opening a repo must tear down the previously-bound VM instead
#      of orphaning it (the reaper sweeps rows, not dangling Daytona ids).
#   3. MEDIUM — a per-user cap on concurrent live sandboxes, so an authenticated
#      tenant can't cold-boot unbounded VMs by varying the repo URL.
#   4. LOW — a repo URL that embeds credentials is rejected (public-repo path).
#
# Registry runs on real Beanie over mongomock-motor (``mongo_db``); Daytona is the
# injected fake from the provision suite. No test touches real Daytona.
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import BadRequest, ConflictError, Forbidden
from pocketpaw_ee.cloud.websandbox import provision
from pocketpaw_ee.cloud.websandbox import router as websandbox_router
from pocketpaw_ee.cloud.websandbox import service as sandbox_service
from pocketpaw_ee.cloud.websandbox.dto import (
    CreateSandboxRequest,
    RegisterSandboxRequest,
)

from tests.cloud.test_websandbox_provision import _FakeDaytonaClient

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(workspace_id: str, user_id: str) -> RequestContext:
    """A minimal authed RequestContext for calling a router handler directly."""
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="test-req",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# 1. HIGH — client cannot forge a sandbox_id binding.
# ---------------------------------------------------------------------------


def test_register_request_is_repo_only() -> None:
    # The client-facing model carries ONLY repo — no server-owned fields to smuggle.
    assert set(RegisterSandboxRequest.model_fields) == {"repo"}
    # Extra fields on the wire are dropped, never bound.
    body = RegisterSandboxRequest.model_validate(
        {"repo": "https://github.com/x/y", "sandbox_id": "victim-vm", "status": "ready"}
    )
    assert not hasattr(body, "sandbox_id")


def test_no_client_patch_lifecycle_route() -> None:
    # Lifecycle is server-owned; there must be no client PATCH route through which
    # a caller could bind an arbitrary sandbox_id / status.
    methods = {
        (route.path, method)
        for route in websandbox_router.router.routes
        for method in getattr(route, "methods", set())
    }
    assert ("/websandbox/{row_id}", "PATCH") not in methods


async def test_register_route_ignores_client_supplied_sandbox_id() -> None:
    # An attacker POSTs a body that tries to bind another tenant's Daytona id.
    body = RegisterSandboxRequest.model_validate(
        {"repo": "https://github.com/x/y", "sandbox_id": "victim-vm", "status": "ready"}
    )
    view = await websandbox_router.create_sandbox(body, _ctx("attacker-ws", "attacker"))
    # The row is registered clean: no bound VM, status pending — both server-owned.
    assert view.sandboxId is None
    assert view.status == "pending"


async def test_client_register_cannot_hijack_another_tenants_vm() -> None:
    # Victim's VM is bound through the trusted provisioner path (internal command).
    await sandbox_service.create_sandbox(
        "victim-ws",
        "victim",
        CreateSandboxRequest(repo="r", status="ready", sandbox_id="victim-vm"),
    )
    # Attacker registers a row pointing at the victim's id via the CLIENT route.
    body = RegisterSandboxRequest.model_validate(
        {"repo": "r2", "sandbox_id": "victim-vm", "status": "ready"}
    )
    await websandbox_router.create_sandbox(body, _ctx("attacker-ws", "attacker"))
    # Because the attacker's row never bound the victim id, the oracle still denies
    # them — no row in the attacker's workspace references victim-vm.
    with pytest.raises(Forbidden):
        await sandbox_service.authorize_sandbox("attacker-ws", "attacker", "victim-vm")


# ---------------------------------------------------------------------------
# 2. MEDIUM — re-open tears down the orphaned VM.
# ---------------------------------------------------------------------------


async def test_reopen_same_repo_deletes_previous_vm() -> None:
    fake = _FakeDaytonaClient()
    repo = "https://github.com/octocat/Hello-World.git"

    first = await provision.open_sandbox("w1", "u1", {"repo": repo}, client=fake)
    assert first.sandbox_id == "dtn-1"

    # Re-open the same repo: a new VM is provisioned and the row rebinds to it.
    second = await provision.open_sandbox("w1", "u1", {"repo": repo}, client=fake)
    assert second.id == first.id  # same registry row (idempotent key)
    assert second.sandbox_id == "dtn-2"

    # The now-orphaned first VM was stopped + deleted instead of leaking.
    assert "dtn-1" in fake.delete_calls
    assert "dtn-2" not in fake.delete_calls


# ---------------------------------------------------------------------------
# 3. MEDIUM — per-user concurrent-sandbox cap.
# ---------------------------------------------------------------------------


async def test_open_rejects_past_per_user_cap(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_WEBSANDBOX_MAX_PER_USER", "2")
    fake = _FakeDaytonaClient()

    await provision.open_sandbox("w1", "u1", {"repo": "https://github.com/a/one.git"}, client=fake)
    await provision.open_sandbox("w1", "u1", {"repo": "https://github.com/a/two.git"}, client=fake)

    # A THIRD distinct repo is over the cap of 2 → clean ConflictError, no VM booted.
    calls_before = len(fake.create_calls)
    with pytest.raises(ConflictError) as exc:
        await provision.open_sandbox(
            "w1", "u1", {"repo": "https://github.com/a/three.git"}, client=fake
        )
    assert exc.value.code == "websandbox.too_many"
    assert len(fake.create_calls) == calls_before  # short-circuited before provisioning


async def test_reopen_existing_repo_not_blocked_by_cap(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_WEBSANDBOX_MAX_PER_USER", "1")
    fake = _FakeDaytonaClient()
    repo = "https://github.com/a/one.git"

    await provision.open_sandbox("w1", "u1", {"repo": repo}, client=fake)
    # Re-opening the SAME repo reuses its row — not a new slot, so the cap of 1
    # does not reject it.
    again = await provision.open_sandbox("w1", "u1", {"repo": repo}, client=fake)
    assert again.status == "ready"


async def test_cap_is_per_user_not_per_workspace(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_WEBSANDBOX_MAX_PER_USER", "1")
    fake = _FakeDaytonaClient()

    await provision.open_sandbox("w1", "u1", {"repo": "https://github.com/a/one.git"}, client=fake)
    # A different user in the same workspace has their own budget.
    other = await provision.open_sandbox(
        "w1", "u2", {"repo": "https://github.com/a/two.git"}, client=fake
    )
    assert other.status == "ready"


# ---------------------------------------------------------------------------
# 4. LOW — credential-bearing repo URLs are rejected.
# ---------------------------------------------------------------------------


async def test_open_rejects_url_with_embedded_credentials() -> None:
    fake = _FakeDaytonaClient()
    with pytest.raises(BadRequest) as exc:
        await provision.open_sandbox(
            "w1",
            "u1",
            {"repo": "https://user:token@github.com/octocat/Hello-World.git"},
            client=fake,
        )
    assert exc.value.code == "websandbox.invalid_repo"
    assert fake.create_calls == []  # rejected before any VM was provisioned
