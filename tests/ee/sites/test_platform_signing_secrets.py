# tests/ee/sites/test_platform_signing_secrets.py: session-signing secrets
# (BETTER_AUTH_SECRET and friends) are minted by the platform, never asked of the
# owner. Publish mints the ones a build requests before the missing-secrets check,
# the agent's request_site_secret refuses them, and an owner-set value is never
# replaced. Values never appear in a tool result.
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.sites import draft_worker, site_secrets

WS = "ws_signing"
OWNER = "user-owner"
EDITOR = "user-editor"


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())


async def _pocket() -> str:
    from pocketpaw_ee.cloud.models.pocket import Pocket

    doc = Pocket(
        workspace=WS,
        name="Pickles",
        type="site",
        owner=OWNER,
        visibility="workspace",
        engine="project",
        widgets=[],
    )
    await doc.insert()
    return str(doc.id)


def _build(root: Path, requests: list[dict]) -> Path:
    (root / "worker").mkdir(parents=True, exist_ok=True)
    (root / "worker" / "index.js").write_text("export default { fetch(r, env) {} };\n")
    manifest = {
        "workerEntry": "worker/index.js",
        "mainModule": "index.js",
        "workerModules": ["worker/index.js"],
        "compat": {"date": "2026-09-01", "flags": []},
        "bindingRequests": requests,
    }
    (root / "paw-build.json").write_text(json.dumps(manifest))
    return root


class _CF:
    def __init__(self):
        self.puts: list[dict] = []

    async def put_worker(self, **kw):
        self.puts.append(kw)
        return True


def _secret_bindings(put: dict) -> dict[str, str]:
    return {b["name"]: b["text"] for b in put["bindings"] if b["type"] == "secret_text"}


def test_the_signing_list_is_shared_with_drafts():
    assert set(site_secrets.PLATFORM_SIGNING_SECRETS) == {
        "BETTER_AUTH_SECRET",
        "AUTH_SECRET",
        "SESSION_SECRET",
        "JWT_SECRET",
    }
    assert draft_worker.SIGNING_SECRETS == site_secrets.PLATFORM_SIGNING_SECRETS


@pytest.mark.parametrize(
    ("name", "managed"),
    [
        ("BETTER_AUTH_SECRET", True),
        ("JWT_SECRET", True),
        ("SESSION_SECRET__DRAFT", True),
        ("PAW_GATEWAY_TOKEN", True),
        ("ADMIN_PASSWORD", False),
        ("STRIPE_SECRET_KEY", False),
        ("", False),
    ],
)
def test_platform_managed_names(name, managed):
    assert site_secrets.is_platform_managed(name) is managed


@pytest.mark.asyncio
async def test_publish_mints_a_requested_signing_secret_instead_of_refusing(
    beanie_test_db, tmp_path
):
    from pocketpaw_ee.sites import service as sites_service

    pid = await _pocket()
    site = SimpleNamespace(id="site_9", workspace=WS, pocket_id=pid, d1_database_id="")
    build = _build(tmp_path, [{"type": "secret", "name": "BETTER_AUTH_SECRET", "required": True}])
    cf = _CF()

    await sites_service.deploy_bundle(site, build, cloudflare=cf)
    await sites_service.deploy_bundle(site, build, cloudflare=cf)

    first, second = (_secret_bindings(p) for p in cf.puts)
    assert set(first) == {"BETTER_AUTH_SECRET"}
    assert len(first["BETTER_AUTH_SECRET"]) >= 32
    # Minted once: a redeploy keeps the value, so sessions survive it.
    assert first == second
    # Only what the build asked for is minted.
    listed = await site_secrets.list_secrets(workspace_id=WS, user_id=OWNER, pocket_id=pid)
    assert [(s.name, s.status) for s in listed.secrets] == [("BETTER_AUTH_SECRET", "set")]
    assert listed.pending == []


@pytest.mark.asyncio
async def test_publish_keeps_an_owner_set_signing_secret(beanie_test_db, tmp_path):
    from pocketpaw_ee.sites import service as sites_service

    pid = await _pocket()
    owner_value = "an-owner-chosen-value-that-is-long-enough-0123"
    await site_secrets.set_secret(
        workspace_id=WS, user_id=OWNER, pocket_id=pid, name="SESSION_SECRET", value=owner_value
    )
    site = SimpleNamespace(id="site_9", workspace=WS, pocket_id=pid, d1_database_id="")
    cf = _CF()
    await sites_service.deploy_bundle(
        site,
        _build(tmp_path, [{"type": "secret", "name": "SESSION_SECRET", "required": True}]),
        cloudflare=cf,
    )
    assert _secret_bindings(cf.puts[0]) == {"SESSION_SECRET": owner_value}


@pytest.mark.asyncio
async def test_a_pending_signing_request_is_filled_on_publish(beanie_test_db, tmp_path):
    from pocketpaw_ee.sites import service as sites_service

    pid = await _pocket()
    # A site built before this change: the agent left a pending request.
    await site_secrets.request_secret(
        workspace_id=WS, user_id=EDITOR, pocket_id=pid, name="BETTER_AUTH_SECRET", description="x"
    )
    site = SimpleNamespace(id="site_9", workspace=WS, pocket_id=pid, d1_database_id="")
    cf = _CF()
    await sites_service.deploy_bundle(
        site,
        _build(tmp_path, [{"type": "secret", "name": "BETTER_AUTH_SECRET", "required": True}]),
        cloudflare=cf,
    )
    assert "BETTER_AUTH_SECRET" in _secret_bindings(cf.puts[0])
    listed = await site_secrets.list_secrets(workspace_id=WS, user_id=OWNER, pocket_id=pid)
    assert listed.pending == []


@pytest.mark.asyncio
async def test_an_owner_secret_is_still_required(beanie_test_db, tmp_path):
    from pocketpaw_ee.cloud._core.errors import ValidationError
    from pocketpaw_ee.sites import service as sites_service

    pid = await _pocket()
    site = SimpleNamespace(id="site_9", workspace=WS, pocket_id=pid, d1_database_id="")
    with pytest.raises(ValidationError) as exc:
        await sites_service.deploy_bundle(
            site,
            _build(tmp_path, [{"type": "secret", "name": "ADMIN_PASSWORD", "required": True}]),
            cloudflare=_CF(),
        )
    assert exc.value.code == "sites.secrets_missing"


# ------------------------------------------------------------- the agent tool


@pytest.fixture
def _agent(monkeypatch):
    from pocketpaw_ee.agent.mcp_servers import sites_create

    monkeypatch.setattr(sites_create, "_identity", lambda: (WS, EDITOR))

    async def _no_gate(*_a, **_kw):
        return None

    monkeypatch.setattr(sites_create, "_require_sites_plan_or_error", _no_gate)
    pushed: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.agent_service.push_sse_event",
        lambda kind, data: pushed.append((kind, data)),
    )
    return sites_create, pushed


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["BETTER_AUTH_SECRET", "JWT_SECRET", "AUTH_SECRET__DRAFT"])
async def test_the_agent_cannot_ask_the_owner_for_a_signing_secret(beanie_test_db, _agent, name):
    sites_create, pushed = _agent
    pid = await _pocket()

    result = await sites_create._request_site_secret_handler(
        {"pocket_id": pid, "name": name, "description": "paste 32 random characters"}
    )

    assert result.get("is_error")
    text = result["content"][0]["text"]
    assert "automatically" in text and "Do not ask" in text
    # No card for the owner, no pending row.
    assert pushed == []
    listed = await site_secrets.list_secrets(workspace_id=WS, user_id=OWNER, pocket_id=pid)
    assert listed.pending == []


@pytest.mark.asyncio
async def test_a_refused_signing_request_mints_the_value_now(beanie_test_db, _agent):
    sites_create, _ = _agent
    pid = await _pocket()

    result = await sites_create._request_site_secret_handler(
        {"pocket_id": pid, "name": "BETTER_AUTH_SECRET", "description": ""}
    )

    listed = await site_secrets.list_secrets(workspace_id=WS, user_id=OWNER, pocket_id=pid)
    assert [(s.name, s.status) for s in listed.secrets] == [("BETTER_AUTH_SECRET", "set")]
    value = (await site_secrets.secrets_for_deploy(SimpleNamespace(workspace=WS, pocket_id=pid)))[
        "BETTER_AUTH_SECRET"
    ]
    assert value not in json.dumps(result)


@pytest.mark.asyncio
async def test_the_agent_cannot_ask_for_the_gateway_token(beanie_test_db, _agent):
    sites_create, pushed = _agent
    pid = await _pocket()
    result = await sites_create._request_site_secret_handler(
        {"pocket_id": pid, "name": "PAW_GATEWAY_TOKEN", "description": "x"}
    )
    assert result.get("is_error") and "connect_site_integration" in result["content"][0]["text"]
    assert pushed == []
    listed = await site_secrets.list_secrets(workspace_id=WS, user_id=OWNER, pocket_id=pid)
    assert listed.secrets == []


@pytest.mark.asyncio
async def test_an_owner_secret_request_still_reaches_the_owner(beanie_test_db, _agent):
    sites_create, pushed = _agent
    pid = await _pocket()
    result = await sites_create._request_site_secret_handler(
        {"pocket_id": pid, "name": "ADMIN_PASSWORD", "description": "The password for /admin"}
    )
    assert not result.get("is_error")
    assert pushed and pushed[0][0] == "site_secret_requested"
