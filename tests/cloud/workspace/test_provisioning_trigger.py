# tests/cloud/workspace/test_provisioning_trigger.py — proves the WU-F workspace
# provisioning trigger is BEST-EFFORT and NON-BLOCKING.
#
#   1. Happy path — creating a workspace fires ensure_tenant_key(workspace_id) so
#      a per-tenant LiteLLM key is provisioned for the new workspace.
#   2. Proxy-down — if ensure_tenant_key RAISES (proxy unreachable / mint failure),
#      workspace creation STILL SUCCEEDS (the workspace + owner membership land);
#      the provisioning error is swallowed + logged, never fatal.
#
# Uses the shared ``mongo_db`` + autouse ``recording_bus`` fixtures. The resolver
# is mocked (the realtime resolver isn't initialised in unit tests).
#
# Created 2026-06-26 (feat/litellm-billing-cutover, WU-F): new test module.

from __future__ import annotations

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
    yield


async def test_create_workspace_provisions_tenant_key() -> None:
    owner = await _seed_user()

    ws = await workspace_service.create(
        _ctx(str(owner.id)), CreateWorkspaceRequest(name="Acme", slug="acme")
    )

    # The provisioning trigger fired: a per-tenant key row exists for the new ws.
    row = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == ws.id)
    assert row is not None
    assert row.litellm_key  # a key was minted
    assert row.key_alias == f"ws-{ws.id}"


async def test_create_workspace_survives_proxy_down(monkeypatch) -> None:
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

    # The workspace + owner membership landed (creation fully succeeded)...
    assert ws.id
    assert ws.name == "Acme"
    reloaded = await _UserDoc.find_one(_UserDoc.email == "owner@x.c")
    assert reloaded is not None
    assert any(m.workspace == ws.id and m.role == "owner" for m in reloaded.workspaces)

    # ...but no key row was provisioned (the failure was swallowed, not retried here).
    row = await LiteLLMTenantKey.find_one(LiteLLMTenantKey.workspace == ws.id)
    assert row is None
