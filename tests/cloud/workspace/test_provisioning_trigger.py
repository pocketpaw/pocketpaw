# tests/cloud/workspace/test_provisioning_trigger.py — proves workspace create
# mints the LiteLLM tenant key OFF the request path, best-effort.
#
#   1. Happy path — create schedules ensure_tenant_key(workspace_id); after the
#      background mints drain, a per-tenant key row exists.
#   2. Proxy-down — a raising mint is logged, never raised into create; the
#      workspace + owner membership still land and no key row is written.
#   3. Slow proxy — create returns promptly while the mint is still sleeping;
#      the mint still runs to completion on drain.
#   4. Drain timeout — a mint that outlives drain_pending_mints' timeout leaves
#      drain returning cleanly, and the task can be cancelled.
#
# Uses the shared ``mongo_db`` + autouse ``recording_bus`` fixtures. The resolver
# is mocked (the realtime resolver isn't initialised in unit tests).

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.models.litellm_key import LiteLLMTenantKey
from pocketpaw_ee.cloud.models.user import User as _UserDoc
from pocketpaw_ee.cloud.workspace import service as workspace_service
from pocketpaw_ee.cloud.workspace.dto import CreateWorkspaceRequest

pytestmark = pytest.mark.usefixtures("mongo_db")


def _ctx(user_id: str) -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=None,
        request_id="r",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _seed_user(email: str = "owner@x.c") -> _UserDoc:
    doc = _UserDoc(
        email=email,
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="Owner",
        workspaces=[],
    )
    await doc.insert()
    return doc


@pytest.fixture(autouse=True)
def resolver_mock(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    mock = MagicMock()
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.get_resolver", lambda: mock)
    return mock


@pytest_asyncio.fixture(autouse=True)
async def stub_proxy(monkeypatch):
    """Stub the LiteLLM admin client so ensure_tenant_key never hits the network.

    By default the mint succeeds with a deterministic key; a test that wants the
    proxy-down path patches ensure_tenant_key to raise instead.
    """
    import pocketpaw_ee.cloud.llm_provisioning.service as svc

    class _FakeAdmin:
        async def generate_key(self, **kwargs):
            return {"key": f"sk-{kwargs.get('key_alias', 'x')}", **kwargs}

    monkeypatch.setattr(svc, "LiteLLMAdminClient", lambda *a, **k: _FakeAdmin())
    # Other tests create workspaces without draining; their mints sit on loops
    # that are already closed. Start each test from an empty set.
    svc._pending_mints.clear()
    yield
    # Never leak a background mint into the next test.
    for task in list(svc._pending_mints):
        task.cancel()
    await svc.drain_pending_mints(timeout=1.0)


async def test_create_workspace_provisions_tenant_key() -> None:
    import pocketpaw_ee.cloud.llm_provisioning.service as svc

    owner = await _seed_user()

    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="Acme", slug="acme")
    )
    await svc.drain_pending_mints()

    # The provisioning trigger fired: a per-tenant key row exists for the new ws.
    row = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == ws.id)
    assert row is not None
    assert row.litellm_key  # a key was minted
    assert row.key_alias == f"ws-{ws.id}"


async def test_create_workspace_survives_proxy_down(monkeypatch, caplog) -> None:
    # Simulate the proxy being unreachable: ensure_tenant_key raises.
    import pocketpaw_ee.cloud.llm_provisioning.service as svc

    async def _boom(workspace, **kwargs):
        raise RuntimeError("proxy unreachable (simulated)")

    monkeypatch.setattr(svc, "ensure_tenant_key", _boom)

    owner = await _seed_user()

    # Creation MUST NOT raise even though provisioning blew up.
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="Acme", slug="acme")
    )
    with caplog.at_level(logging.WARNING, logger=svc.__name__):
        await svc.drain_pending_mints()

    # The failure was logged, with the workspace id, not lost.
    assert any(
        "background tenant-key mint failed" in r.getMessage() and ws.id in r.getMessage()
        for r in caplog.records
    )

    # The workspace + owner membership landed (creation fully succeeded)...
    assert ws.id
    assert ws.name == "Acme"
    reloaded = await _UserDoc.find_one(_UserDoc.email == "owner@x.c")
    assert reloaded is not None
    assert any(m.workspace == ws.id and m.role == "owner" for m in reloaded.workspaces)

    # ...but no key row was provisioned (the failure was swallowed, not retried here).
    row = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == ws.id)
    assert row is None


async def test_create_workspace_does_not_wait_for_a_slow_mint(monkeypatch) -> None:
    import pocketpaw_ee.cloud.llm_provisioning.service as svc

    started = asyncio.Event()
    finished: list[str] = []

    async def _slow(workspace, **kwargs):
        started.set()
        await asyncio.sleep(2.0)
        finished.append(workspace)

    monkeypatch.setattr(svc, "ensure_tenant_key", _slow)
    owner = await _seed_user()

    t0 = time.perf_counter()
    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="Acme", slug="acme")
    )
    elapsed = time.perf_counter() - t0

    # create() returned well before the 2 s mint could have finished.
    assert elapsed < 1.5, f"create waited on the mint ({elapsed:.2f}s)"
    assert finished == []
    # The mint was scheduled and is held (not garbage-collectable).
    assert len(svc._pending_mints) == 1

    await svc.drain_pending_mints(timeout=5.0)
    assert started.is_set()
    assert finished == [ws.id]
    assert not svc._pending_mints


async def test_drain_times_out_cleanly_and_task_can_be_cancelled(monkeypatch) -> None:
    import pocketpaw_ee.cloud.llm_provisioning.service as svc

    async def _hang(workspace, **kwargs):
        await asyncio.sleep(60)

    monkeypatch.setattr(svc, "ensure_tenant_key", _hang)

    task = svc.schedule_ensure_tenant_key("ws-hang")
    assert task is not None

    t0 = time.perf_counter()
    await svc.drain_pending_mints(timeout=0.1)  # must not raise
    assert time.perf_counter() - t0 < 1.0

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    assert task not in svc._pending_mints


def test_schedule_without_a_running_loop_returns_none() -> None:
    import pocketpaw_ee.cloud.llm_provisioning.service as svc

    assert svc.schedule_ensure_tenant_key("ws-x") is None
    assert svc.schedule_ensure_tenant_key("") is None
