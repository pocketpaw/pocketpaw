# tests/cloud/ship/test_ship_rbac.py — the /ship REST surface's role gate.
#
# WHY THIS EXISTS. SHIP-4 registered ``ship.read`` (MEMBER) and ``ship.manage``
# (ADMIN) in guards/actions.py and then never consulted either on any request
# path. The router's only dependency was ``require_license``, and
# ``RequestContext`` carries no role — it only answers "which workspace". So the
# ADMIN tier the action registry advertised was documentation-only: any MEMBER
# (the LOWEST workspace role) could POST /ship/boxes to provision a billable
# Hetzner server, PUT .../env to write secrets, and deploy / restart / rebuild /
# tear down production. The executor's post-approval ``ship.manage`` re-check is
# not a substitute — it guards the one path that already required a human, not
# the unattended ones that spend money.
#
# These tests pin both halves: a MEMBER may READ but is refused on every
# mutating route, and an ADMIN may do both.
#
# Created 2026-07-29 (fix/ship-review-p0): new module.

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from tests.cloud.ship.conftest import APP, IMAGE, _app_on_box, _build_app, _ready_box

# (method, path suffix, payload) for every MUTATING route on an app.
_APP_WRITES = [
    ("post", "/deploy", None),
    ("post", "/domains", {"domain": "demo.paw.example"}),
    ("post", "/db", {"db_type": "postgres"}),
    ("put", "/scale", {"scale": {"web": 1}}),
    ("put", "/checks", {"zero_downtime": True}),
    ("put", "/resources", {"cpu": 1000}),
    ("post", "/volumes", {"mount_path": "/data"}),
    ("post", "/restart", None),
    ("post", "/rebuild", None),
    ("put", "/env", {"vars": [{"key": "API_KEY", "value": "x"}]}),
    ("post", "/env/import", {"dotenv": "A=b"}),
    ("put", "/source", {"source_kind": "git", "repo_url": "https://github.com/a/b.git"}),
]


@pytest_asyncio.fixture
async def member(mongo_db, enc_key, arq_pool) -> AsyncClient:  # noqa: ARG001 — fixtures init state
    """A client whose user is a MEMBER of w1 — read yes, manage no."""
    transport = ASGITransport(app=_build_app("w1", role="member"))
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


async def test_member_can_read(member, w1):
    """``ship.read`` is MEMBER — the console must still render for them."""
    await _ready_box(w1)
    assert (await member.get("/ship/boxes")).status_code == 200
    assert (await member.get("/ship/apps")).status_code == 200


async def test_member_cannot_provision_a_billable_box(member):
    """The costliest unattended write — a real, paid server."""
    resp = await member.post("/ship/boxes", json={"provider": "hcloud"})
    assert resp.status_code == 403, resp.text


async def test_member_cannot_register_an_app(member, w1):
    box_id = await _ready_box(w1)
    resp = await member.post("/ship/apps", json={"name": APP, "box_id": box_id, "image": IMAGE})
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("method,suffix,payload", _APP_WRITES)
async def test_member_is_refused_on_every_app_write(member, w1, method, suffix, payload):
    app_id = await _app_on_box(w1, await _ready_box(w1))

    call = getattr(member, method)
    resp = await (
        call(f"/ship/apps/{app_id}{suffix}", json=payload)
        if payload
        else call(f"/ship/apps/{app_id}{suffix}")
    )

    assert resp.status_code == 403, f"{method.upper()} {suffix} was NOT gated: {resp.status_code}"


async def test_member_cannot_park_a_teardown(member, w1):
    """Even the PARK path is ADMIN — filing a proposal is a real action."""
    app_id = await _app_on_box(w1, await _ready_box(w1))
    assert (await member.delete(f"/ship/apps/{app_id}")).status_code == 403
    box_id = await _ready_box(w1)
    assert (await member.delete(f"/ship/boxes/{box_id}")).status_code == 403


async def test_admin_may_write(w1):
    """The default suites act as ADMIN — the gate must not break them."""
    assert (await w1.post("/ship/boxes", json={"provider": "hcloud"})).status_code == 200


def test_both_ship_actions_are_registered() -> None:
    """An unknown action makes ``require_action_any_workspace`` fail loud, so a
    typo in the route dep would 500 rather than silently allow."""
    from pocketpaw_ee.guards.actions import ACTIONS

    assert "ship.read" in ACTIONS and "ship.manage" in ACTIONS
