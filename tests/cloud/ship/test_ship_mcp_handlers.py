# tests/cloud/ship/test_ship_mcp_handlers.py — the /ship AGENT surface
# (``pocketpaw_ship`` MCP handlers), exercised end to end.
#
# WHY THIS EXISTS. Every handler in ``agent/mcp_servers/ship.py`` shipped with
# ZERO test coverage, and four of them raised AttributeError on their SUCCESS
# path while the whole ship suite stayed green:
#
#   * ``_box_wire`` read ``view.box_id`` and ``_app_wire`` read ``view.app_id``
#     — the views expose ``id``, so ship_list_boxes / ship_provision_box /
#     ship_list_apps / ship_create_app crashed as soon as one box or app existed.
#   * ``_add_domain_handler`` returned ``view.url``; ``DomainView`` has no such
#     field, so the tool crashed AFTER routing the domain and issuing a real
#     ACME certificate.
#   * ``_deploy_app_handler`` tested ``isinstance(result, dict)`` against a
#     frozen dataclass, so the PROD-deploy branch was dead code and a gated
#     deploy fell through to ``result.id`` and raised — breaking the exact
#     "relay proposed, never claim deployed" contract the gate exists for.
#
# All four raised OUTSIDE the handlers' try/except, so the agent saw a raw
# traceback rather than the error envelope. The common thread is that these are
# the seams between the agent surface and the service views — the fakes used
# elsewhere in the ship suite never cross them. These tests drive the REAL
# handlers against the REAL service, with only the chat-session identity faked.
#
# Created 2026-07-29 (fix/ship-review-p0): new module.

from __future__ import annotations

import pytest
from pocketpaw_ee.agent.mcp_servers import ship as ship_mcp

from tests.cloud.ship.conftest import APP, DOMAIN, IMAGE, install_fake_engine

WS = "w1"
USER = "u1"


@pytest.fixture(autouse=True)
def _identity(monkeypatch):
    """Give the handlers a resolvable cloud-chat identity, and silence audit."""
    monkeypatch.setattr(ship_mcp, "_identity", lambda: (WS, USER))
    monkeypatch.setattr(ship_mcp, "_audit", lambda *a, **k: None)


def _body(resp: dict) -> dict:
    """Unwrap the MCP envelope, failing loudly on an error response."""
    import json

    assert not resp.get("is_error"), f"handler returned an error: {resp}"
    return json.loads(resp["content"][0]["text"])


async def _ready_box_id() -> str:
    from pocketpaw_ee.cloud.ship import store

    box = await store.create_provisioning_box(
        workspace_id=WS,
        provider="hcloud",
        server_type="cx22",
        region="fsn1",
        ssh_private_key="FAKE-KEY",
        ssh_public_key="ssh-ed25519 AAAAFAKE test",
    )
    await store.mark_ready(box, server_id="srv-1", ip="203.0.113.9", price_monthly=8.25)
    return str(box.id)


async def _an_app(box_id: str, *, prod: bool = False) -> str:
    from pocketpaw_ee.cloud.ship import store

    app = await store.create_app(
        workspace_id=WS,
        box_id=box_id,
        name=APP,
        build_path="dockerfile",
        git_ref="",
        image=IMAGE,
        env_refs=[],
        prod=prod,
    )
    return str(app.id)


# ---------------------------------------------------------------------------
# The wire mappers — a NON-EMPTY result is what broke; empty lists passed fine.
# ---------------------------------------------------------------------------


async def test_list_boxes_renders_a_real_box(mongo_db, enc_key):  # noqa: ARG001
    await _ready_box_id()
    body = _body(await ship_mcp._list_boxes_handler({}))
    assert len(body["boxes"]) == 1
    box = body["boxes"][0]
    # ``id`` is the contract field — the mapper used to read ``box_id`` and raise.
    assert box["id"] and box["provider"] == "hcloud" and box["status"] == "ready"


async def test_list_apps_renders_a_real_app(mongo_db, enc_key):  # noqa: ARG001
    await _an_app(await _ready_box_id())
    body = _body(await ship_mcp._list_apps_handler({}))
    assert len(body["apps"]) == 1
    assert body["apps"][0]["id"] and body["apps"][0]["name"] == APP


async def test_create_app_returns_the_created_app(mongo_db, enc_key):  # noqa: ARG001
    box_id = await _ready_box_id()
    body = _body(await ship_mcp._create_app_handler({"name": APP, "box_id": box_id}))
    assert body["id"] and body["name"] == APP and body["box_id"] == box_id


async def test_add_domain_returns_the_wire_shape(mongo_db, enc_key, monkeypatch):  # noqa: ARG001
    install_fake_engine(monkeypatch)
    app_id = await _an_app(await _ready_box_id())
    body = _body(await ship_mcp._add_domain_handler({"app_id": app_id, "domain": DOMAIN}))
    # DomainView has no ``url``; reading one crashed AFTER the cert was issued.
    assert body["domain"] == DOMAIN and body["tls_enabled"] is True


# ---------------------------------------------------------------------------
# The gate — a PROD app must come back as "proposed", never as a deploy
# ---------------------------------------------------------------------------


async def test_prod_deploy_relays_proposed_and_never_claims_deployed(
    mongo_db, enc_key, arq_pool, monkeypatch
):  # noqa: ARG001
    install_fake_engine(monkeypatch)
    app_id = await _an_app(await _ready_box_id(), prod=True)

    body = _body(await ship_mcp._deploy_app_handler({"app_id": app_id}))

    assert body["status"] == "proposed", "a PROD deploy must not run"
    assert body["proposal_id"], "the agent needs the proposal id to relay"
    assert "deploy_id" not in body, "a proposed deploy has not deployed anything"


async def test_non_prod_deploy_runs_and_reports_a_deploy_id(
    mongo_db, enc_key, arq_pool, monkeypatch
):  # noqa: ARG001
    install_fake_engine(monkeypatch)
    app_id = await _an_app(await _ready_box_id(), prod=False)

    body = _body(await ship_mcp._deploy_app_handler({"app_id": app_id}))

    assert body["deploy_id"] and body["status"] != "proposed"


# ---------------------------------------------------------------------------
# Wave 2 / 3 verbs reach the service and come back with the wire fields
# ---------------------------------------------------------------------------


async def test_wave2_and_wave3_handlers_round_trip(mongo_db, enc_key, monkeypatch):  # noqa: ARG001
    install_fake_engine(monkeypatch)
    app_id = await _an_app(await _ready_box_id())

    # The scale/resource values match the recorded transcripts (conftest maps the
    # EXACT command strings), so these assertions also pin the command surface.
    scale = _body(
        await ship_mcp._set_scale_handler({"app_id": app_id, "scale": {"web": 2, "worker": 1}})
    )
    assert scale["scale"] == {"web": 2, "worker": 1}

    checks = _body(await ship_mcp._set_checks_handler({"app_id": app_id, "zero_downtime": True}))
    assert checks["zero_downtime"] is True

    res = _body(
        await ship_mcp._set_resources_handler({"app_id": app_id, "cpu": 1000, "memory_mb": 512})
    )
    assert res["memory_limit_mb"] == 512 and res["cpu_limit"] == 1000

    vol = _body(await ship_mcp._create_volume_handler({"app_id": app_id, "mount_path": "/data"}))
    assert vol["volumes"][0]["mount_path"] == "/data"

    assert _body(await ship_mcp._restart_handler({"app_id": app_id}))["action"] == "restart"


# ---------------------------------------------------------------------------
# Identity + secrecy invariants
# ---------------------------------------------------------------------------


async def test_handlers_refuse_without_a_chat_identity(monkeypatch):
    monkeypatch.setattr(ship_mcp, "_identity", lambda: (None, None))
    resp = await ship_mcp._list_boxes_handler({})
    assert resp["is_error"] is True


async def test_create_db_reports_the_env_var_name_never_the_dsn(mongo_db, enc_key, monkeypatch):  # noqa: ARG001
    install_fake_engine(monkeypatch)
    app_id = await _an_app(await _ready_box_id())

    resp = await ship_mcp._create_db_handler({"app_id": app_id, "db_type": "postgres"})

    assert _body(resp)["env_var"] == "DATABASE_URL"
    assert "s3cr3tpass" not in str(resp), "the connection string must never reach the agent"
