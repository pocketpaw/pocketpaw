# tests/ee/sites/test_site_secrets.py: the per-site secret store (sites.site_secrets),
# its routes (sites.secrets_router), the agent tools (request_site_secret /
# list_site_secrets), the deploy hand-off into secret_text bindings, and the delete
# cascade. The one rule every test here leans on: a value goes in through PUT and comes
# out only as a secret_text binding in the Worker upload. Never in a response, a tool
# result, an event, a log line, a repr or an error message.
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import bundle_deploy, site_secrets
from pocketpaw_ee.sites.bundle_deploy import ProvisionedResources, map_bindings

WS = "ws_secrets"
OTHER_WS = "ws_other"
OWNER = "user-owner"
EDITOR = "user-editor"
VALUE = "sk_live_TOPSECRET_9f8e7d6c5b4a"


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())


async def _pocket(*, workspace: str = WS, owner: str = OWNER, visibility: str = "workspace"):
    from pocketpaw_ee.cloud.models.pocket import Pocket

    doc = Pocket(
        workspace=workspace,
        name="Shop",
        type="site",
        owner=owner,
        visibility=visibility,
        engine="project",
        widgets=[],
    )
    await doc.insert()
    return str(doc.id)


async def _set(pocket_id: str, name: str = "STRIPE_KEY", value: str = VALUE):
    return await site_secrets.set_secret(
        workspace_id=WS, user_id=OWNER, pocket_id=pocket_id, name=name, value=value
    )


# ------------------------------------------------------------------ storage


@pytest.mark.asyncio
async def test_value_is_encrypted_at_rest_and_round_trips_for_deploy(beanie_test_db):
    from pocketpaw_ee.cloud._core import crypto
    from pocketpaw_ee.cloud.models.site_secret import SiteSecret

    pid = await _pocket()
    view = await _set(pid)

    row = await SiteSecret.find_one(SiteSecret.pocket_id == pid)
    assert row.encrypted_value and VALUE not in row.encrypted_value
    assert crypto.decrypt(row.encrypted_value) == VALUE
    assert view.status == "set" and VALUE not in view.model_dump_json()

    site = SimpleNamespace(workspace=WS, pocket_id=pid)
    assert await site_secrets.secrets_for_deploy(site) == {"STRIPE_KEY": VALUE}
    # A site with no pocket (a test double, a legacy row) has nothing to bind.
    assert await site_secrets.secrets_for_deploy(SimpleNamespace(workspace=WS)) == {}


@pytest.mark.asyncio
async def test_set_without_an_encryption_key_is_refused(beanie_test_db, monkeypatch):
    pid = await _pocket()
    monkeypatch.delenv("CLOUD_ENCRYPTION_KEY")
    with pytest.raises(ValidationError) as exc:
        await _set(pid)
    assert exc.value.code == "cloud.encryption_key_missing"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["lower", "1STARTS_WITH_DIGIT", "HAS-DASH", "A" * 65, ""])
async def test_names_must_be_upper_snake_and_short(beanie_test_db, name):
    pid = await _pocket()
    with pytest.raises(ValidationError) as exc:
        await _set(pid, name=name)
    assert exc.value.code == "sites.secret_name_invalid"


@pytest.mark.asyncio
async def test_request_then_set_fills_the_pending_row(beanie_test_db, _recording_bus_for_sites):
    pid = await _pocket()
    pending = await site_secrets.request_secret(
        workspace_id=WS,
        user_id=EDITOR,
        pocket_id=pid,
        name="RESEND_KEY",
        description="Resend API key for the contact form",
    )
    assert pending.status == "pending" and pending.requested_by == "agent"

    listing = await site_secrets.list_secrets(workspace_id=WS, user_id=OWNER, pocket_id=pid)
    assert [s.name for s in listing.pending] == ["RESEND_KEY"]

    await _set(pid, name="RESEND_KEY")
    listing = await site_secrets.list_secrets(workspace_id=WS, user_id=OWNER, pocket_id=pid)
    assert listing.pending == []
    assert [(s.name, s.status) for s in listing.secrets] == [("RESEND_KEY", "set")]
    assert listing.secrets[0].description == "Resend API key for the contact form"

    # Asking again for a set secret keeps its value.
    again = await site_secrets.request_secret(
        workspace_id=WS, user_id=EDITOR, pocket_id=pid, name="RESEND_KEY", description=""
    )
    assert again.status == "set"
    site = SimpleNamespace(workspace=WS, pocket_id=pid)
    assert await site_secrets.secrets_for_deploy(site) == {"RESEND_KEY": VALUE}

    events = [e for e in _recording_bus_for_sites.events if e.type.startswith("site.secret")]
    assert [e.type for e in events] == ["site.secret_requested", "site.secret_updated"]
    assert events[0].data["owner"] == OWNER and events[0].data["user_id"] == EDITOR
    assert all(VALUE not in json.dumps(e.data, default=str) for e in events)


@pytest.mark.asyncio
async def test_secret_events_reach_the_owner_and_the_actor_only():
    from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
    from pocketpaw_ee.cloud._core.realtime.events import SiteSecretRequested

    async def _everyone(_key):
        return ["u1", "u2", "u3"]

    resolver = AudienceResolver(
        group_members=_everyone,
        workspace_members=_everyone,
        workspace_admins=_everyone,
        workspace_peers=_everyone,
    )
    event = SiteSecretRequested(data={"workspace_id": WS, "owner": OWNER, "user_id": EDITOR})
    assert await resolver.audience(event) == [OWNER, EDITOR]


# ---------------------------------------------------------------------- API


class _Membership:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.role = "member"


def _app(workspace_id: str, user_id: str) -> FastAPI:
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.sites.secrets_router import router

    user = SimpleNamespace(
        id=user_id, active_workspace=workspace_id, workspaces=[_Membership(workspace_id)]
    )
    app = FastAPI()
    add_error_handler(app)
    app.include_router(router, prefix="/api/v1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id=user_id,
            workspace_id=workspace_id,
            request_id="t",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: workspace_id
    app.dependency_overrides[require_license] = lambda: None
    return app


def _client(workspace_id: str, user_id: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=_app(workspace_id, user_id)), base_url="http://t"
    )


@pytest.mark.asyncio
async def test_owner_sets_lists_and_deletes_without_ever_reading_a_value(beanie_test_db):
    pid = await _pocket()
    url = f"/api/v1/sites/by-pocket/{pid}/secrets"
    async with _client(WS, OWNER) as c:
        put = await c.put(f"{url}/STRIPE_KEY", json={"value": VALUE})
        listed = await c.get(url)
        deleted = await c.delete(f"{url}/STRIPE_KEY")
        after = await c.get(url)
        missing = await c.delete(f"{url}/STRIPE_KEY")

    assert put.status_code == 200, put.text
    assert put.json()["name"] == "STRIPE_KEY" and put.json()["status"] == "set"
    assert listed.status_code == 200
    body = listed.json()
    assert body["can_manage"] is True
    assert [(s["name"], s["status"]) for s in body["secrets"]] == [("STRIPE_KEY", "set")]
    for resp in (put, listed):
        assert VALUE not in resp.text
        assert "value" not in resp.text.lower().replace("value_", "")
    assert deleted.status_code == 204
    assert after.json()["secrets"] == []
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_editor_may_list_names_but_not_write(beanie_test_db):
    pid = await _pocket()
    await _set(pid)
    url = f"/api/v1/sites/by-pocket/{pid}/secrets"
    async with _client(WS, EDITOR) as c:
        listed = await c.get(url)
        put = await c.put(f"{url}/STRIPE_KEY", json={"value": "attacker"})
        deleted = await c.delete(f"{url}/STRIPE_KEY")

    assert listed.status_code == 200
    assert listed.json()["can_manage"] is False
    assert VALUE not in listed.text
    assert put.status_code == 403 and put.json()["error"]["code"] == "sites.secret_not_owner"
    assert deleted.status_code == 403
    site = SimpleNamespace(workspace=WS, pocket_id=pid)
    assert await site_secrets.secrets_for_deploy(site) == {"STRIPE_KEY": VALUE}


@pytest.mark.asyncio
async def test_a_private_pocket_is_closed_to_non_members(beanie_test_db):
    pid = await _pocket(visibility="private")
    async with _client(WS, EDITOR) as c:
        listed = await c.get(f"/api/v1/sites/by-pocket/{pid}/secrets")
    assert listed.status_code == 403


@pytest.mark.asyncio
async def test_another_workspace_sees_a_404_even_as_the_pocket_owner(beanie_test_db):
    pid = await _pocket()
    url = f"/api/v1/sites/by-pocket/{pid}/secrets"
    async with _client(OTHER_WS, OWNER) as c:
        listed = await c.get(url)
        put = await c.put(f"{url}/STRIPE_KEY", json={"value": VALUE})
        deleted = await c.delete(f"{url}/STRIPE_KEY")
    assert (listed.status_code, put.status_code, deleted.status_code) == (404, 404, 404)


@pytest.mark.asyncio
async def test_value_size_cap_and_empty_value(beanie_test_db):
    pid = await _pocket()
    url = f"/api/v1/sites/by-pocket/{pid}/secrets/BIG"
    async with _client(WS, OWNER) as c:
        too_big = await c.put(url, json={"value": "x" * (8 * 1024 + 1)})
        at_cap = await c.put(url, json={"value": "x" * (8 * 1024)})
        empty = await c.put(url, json={"value": "  "})
        bad_name = await c.put(f"/api/v1/sites/by-pocket/{pid}/secrets/lower", json={"value": "v"})
    assert too_big.status_code == 413
    assert "x" * 100 not in too_big.text
    assert at_cap.status_code == 200
    assert empty.status_code == 422
    assert bad_name.status_code == 422


# -------------------------------------------------------------------- tools


@pytest.fixture
def _agent(monkeypatch):
    from pocketpaw_ee.agent.mcp_servers import sites_create

    monkeypatch.setattr(sites_create, "_identity", lambda: (WS, EDITOR))
    pushed: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        "pocketpaw_ee.cloud.chat.agent_service.push_sse_event",
        lambda kind, data: pushed.append((kind, data)),
    )
    return sites_create, pushed


def _payload(result: dict) -> dict:
    assert not result.get("is_error"), result
    return json.loads(result["content"][0]["text"])


@pytest.mark.asyncio
async def test_request_tool_creates_a_pending_request_and_never_takes_a_value(
    beanie_test_db, _agent
):
    sites_create, pushed = _agent
    pid = await _pocket()

    out = _payload(
        await sites_create._request_site_secret_handler(
            {
                "pocket_id": pid,
                "name": "STRIPE_KEY",
                "description": "Stripe secret key (Dashboard > Developers > API keys)",
                "value": VALUE,  # the schema forbids it; the handler ignores it too
            }
        )
    )
    assert out["secret"]["status"] == "pending"
    assert "owner will fill it in the builder" in out["message"]
    assert "value" not in out["secret"]
    assert pushed and pushed[0][0] == "site_secret_requested"
    assert await site_secrets.secrets_for_deploy(SimpleNamespace(workspace=WS, pocket_id=pid)) == {}

    listed = _payload(await sites_create._list_site_secrets_handler({"pocket_id": pid}))
    assert listed["secrets"] == [
        {
            "name": "STRIPE_KEY",
            "status": "pending",
            "description": "Stripe secret key (Dashboard > Developers > API keys)",
        }
    ]


@pytest.mark.asyncio
async def test_tools_report_a_set_secret_without_its_value(beanie_test_db, _agent):
    sites_create, _ = _agent
    pid = await _pocket()
    await _set(pid)

    requested = await sites_create._request_site_secret_handler(
        {"pocket_id": pid, "name": "STRIPE_KEY", "description": "again"}
    )
    listed = await sites_create._list_site_secrets_handler({"pocket_id": pid})
    for result in (requested, listed):
        assert VALUE not in json.dumps(result)
    assert _payload(requested)["secret"]["status"] == "set"
    assert _payload(listed)["secrets"][0]["status"] == "set"


@pytest.mark.asyncio
async def test_tools_refuse_bad_input_and_foreign_pockets(beanie_test_db, _agent):
    sites_create, _ = _agent
    foreign = await _pocket(workspace=OTHER_WS)
    bad_name = await sites_create._request_site_secret_handler(
        {"pocket_id": await _pocket(), "name": "lower-case", "description": "x"}
    )
    no_pocket = await sites_create._list_site_secrets_handler({})
    other = await sites_create._list_site_secrets_handler({"pocket_id": foreign})
    assert bad_name["is_error"] and "secret_name_invalid" in bad_name["content"][0]["text"]
    assert no_pocket["is_error"]
    assert other["is_error"] and "not_found" in other["content"][0]["text"]


def test_tool_schemas_do_not_accept_a_value():
    from pocketpaw_ee.agent.mcp_servers import sites_create

    captured = {}

    def _tool(name, _desc, schema):
        captured[name] = schema
        return lambda fn: fn

    sites_create.make_request_site_secret_tool(_tool)
    sites_create.make_list_site_secrets_tool(_tool)
    for schema in captured.values():
        assert "value" not in schema["properties"]
        assert schema["additionalProperties"] is False


# ------------------------------------------------------------------- deploy


def test_every_set_secret_binds_as_secret_text_requested_or_not():
    bindings, warnings = map_bindings(
        [{"type": "secret", "name": "API_KEY", "required": True}],
        [],
        ProvisionedResources(secrets={"API_KEY": "a", "WEBHOOK_SECRET": "b"}),
        has_assets=False,
    )
    assert bindings == [
        {"type": "secret_text", "name": "API_KEY", "text": "a"},
        {"type": "secret_text", "name": "WEBHOOK_SECRET", "text": "b"},
    ]
    assert warnings == []


def test_missing_required_secrets_refuse_with_a_builder_message():
    with pytest.raises(ValidationError) as exc:
        map_bindings(
            [{"type": "secret", "name": "API_KEY", "required": True}],
            [],
            ProvisionedResources(secrets={"OTHER": VALUE}),
            has_assets=False,
            required_secrets=["RESEND_KEY", "OTHER"],
        )
    assert exc.value.code == "sites.secrets_missing"
    assert "API_KEY, RESEND_KEY" in exc.value.message
    assert "builder" in exc.value.message
    assert VALUE not in exc.value.message


def test_a_secret_cannot_shadow_another_binding():
    with pytest.raises(ValidationError) as exc:
        map_bindings(
            [{"type": "d1", "name": "DB"}],
            [],
            ProvisionedResources(d1_database_id="d1", secrets={"DB": VALUE}),
            has_assets=False,
        )
    assert VALUE not in exc.value.message


def test_reprs_never_carry_a_value():
    res = ProvisionedResources(secrets={"API_KEY": VALUE})
    bundle = bundle_deploy.PawBundle(
        main_module="w.js",
        modules=[],
        assets={},
        assets_config={},
        compatibility_date="2026-09-01",
        compatibility_flags=[],
        bindings=[{"type": "secret_text", "name": "API_KEY", "text": VALUE}],
    )
    assert VALUE not in repr(res) and VALUE not in repr(bundle)


ACCT = "acct_1"


class _FakeCloudflare:
    def __init__(self):
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        ok = {"success": True, "errors": [], "messages": []}
        return httpx.Response(200, json={**ok, "result": {"id": "site_1"}})

    def client(self):
        from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

        return CloudflareClient(
            account_id=ACCT,
            api_token="tok_1",
            zone_id="zone_1",
            dispatch_namespace="paw-sites",
            _transport=httpx.MockTransport(self.handler),
        )


def _build(root: Path, **extra) -> Path:
    (root / "worker").mkdir(parents=True, exist_ok=True)
    (root / "worker" / "index.js").write_text("export default { fetch(r, env) {} };\n")
    manifest = {
        "workerEntry": "worker/index.js",
        "mainModule": "index.js",
        "workerModules": ["worker/index.js"],
        "compat": {"date": "2026-09-01", "flags": []},
        "bindingRequests": [{"type": "secret", "name": "STRIPE_KEY", "required": True}],
        **extra,
    }
    (root / "paw-build.json").write_text(json.dumps(manifest))
    return root


def _metadata(request: httpx.Request) -> tuple[dict, bytes]:
    """The upload's metadata JSON, and the body with that part's bindings blanked."""
    import re

    ct = request.headers["content-type"]
    boundary = re.search(r"boundary=(.+)$", ct).group(1).strip('"').encode()
    for chunk in request.content.split(b"--" + boundary)[1:-1]:
        head, _, body = chunk[2:].partition(b"\r\n\r\n")
        if b'name="metadata"' in head:
            meta = json.loads(body[:-2])
            return meta, request.content.replace(body[:-2], b"")
    raise AssertionError("no metadata part")


@pytest.mark.asyncio
async def test_the_value_appears_only_in_the_secret_text_binding(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    fake = _FakeCloudflare()
    build = _build(tmp_path)

    async def _provision(_requests):
        return ProvisionedResources(secrets={"STRIPE_KEY": VALUE})

    await bundle_deploy.deploy_bundle(
        fake.client(), script_name="site_1", build_dir=build, salt="ws", provision=_provision
    )

    (put,) = fake.requests
    meta, rest = _metadata(put)
    assert meta["bindings"] == [{"type": "secret_text", "name": "STRIPE_KEY", "text": VALUE}]
    # Exactly one occurrence across everything sent, and it is the binding above.
    assert put.content.count(VALUE.encode()) == 1
    assert VALUE.encode() not in rest
    assert VALUE not in caplog.text
    assert VALUE not in (build / "paw-build.json").read_text()


@pytest.mark.asyncio
async def test_publish_refuses_before_any_upload_when_a_required_secret_is_missing(
    tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    fake = _FakeCloudflare()
    build = _build(tmp_path, requiredSecrets=["RESEND_KEY"])

    async def _provision(_requests):
        return ProvisionedResources(secrets={"RESEND_KEY": VALUE})

    with pytest.raises(ValidationError) as exc:
        await bundle_deploy.deploy_bundle(
            fake.client(), script_name="site_1", build_dir=build, salt="ws", provision=_provision
        )
    assert exc.value.code == "sites.secrets_missing"
    assert "STRIPE_KEY" in exc.value.message and VALUE not in exc.value.message
    assert fake.requests == []
    assert VALUE not in caplog.text


@pytest.mark.asyncio
async def test_service_deploy_reads_the_owners_secrets_from_the_store(beanie_test_db, tmp_path):
    from pocketpaw_ee.sites import service as sites_service

    pid = await _pocket()
    await _set(pid)
    site = SimpleNamespace(id="site_9", workspace=WS, pocket_id=pid, d1_database_id="")

    class _CF:
        def __init__(self):
            self.put = None

        async def put_worker(self, **kw):
            self.put = kw
            return True

    cf = _CF()
    await sites_service.deploy_bundle(site, _build(tmp_path), cloudflare=cf)
    assert cf.put["bindings"] == [{"type": "secret_text", "name": "STRIPE_KEY", "text": VALUE}]


# ------------------------------------------------------------------ cascade


@pytest.mark.asyncio
async def test_site_delete_cascade_removes_the_secrets(beanie_test_db):
    from pocketpaw_ee.cloud.models.site_secret import SiteSecret
    from pocketpaw_ee.sites import delete_cascade

    pid = await _pocket()
    keep = await _pocket()
    await _set(pid)
    await site_secrets.request_secret(
        workspace_id=WS, user_id=OWNER, pocket_id=pid, name="PENDING_ONE", description=""
    )
    await _set(keep)

    purged: list[str] = []

    class _Deps:
        async def purge_records(self, *, workspace_id, site_id):
            purged.append(site_id)

    site = SimpleNamespace(id="site_1", workspace=WS, pocket_id=pid)
    assert await delete_cascade._purge_records(site=site, deps=_Deps()) == "done"
    assert purged == ["site_1"]
    assert await SiteSecret.find(SiteSecret.pocket_id == pid).count() == 0
    assert await SiteSecret.find(SiteSecret.pocket_id == keep).count() == 1


@pytest.mark.asyncio
async def test_a_failed_secret_purge_is_logged_and_does_not_stop_the_cascade(monkeypatch, caplog):
    from pocketpaw_ee.sites import delete_cascade

    async def _boom(**_kw):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(site_secrets, "purge_for_pocket", _boom)

    class _Deps:
        async def purge_records(self, *, workspace_id, site_id):
            return None

    site = SimpleNamespace(id="site_1", workspace=WS, pocket_id="pk")
    with caplog.at_level(logging.WARNING):
        assert await delete_cascade._purge_records(site=site, deps=_Deps()) == "done"
    assert "could not remove its secrets" in caplog.text
