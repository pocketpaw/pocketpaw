# tests/cloud/agents/test_agent_senses_router.py
# Created 2026-08-02 (Sense Phase 2, SP2-5 — the config surface) — the HTTP half
# of the sense fields. test_agent_senses.py / test_agent_sense_prefs.py pin the
# schema and the doc<->spec mappers; this module pins the wire, where three
# separate things had to be true before an owner could actually use the feature:
#   * SETTABLE at create (the create DTO has no nested ``config`` dict at all, so
#     before SP2-5 a new agent could not be given senses in one call) and on
#     update, both via the explicit fields and via a nested ``config``.
#   * READABLE — ``GET /agents/{id}`` emits both. Until SP2-5 they were
#     write-only, which makes an EXCLUSIVE mount list unauditable by the person
#     who owns it.
#   * SURVIVING — an unrelated PATCH must not erase them (the config-dict branch
#     rebuilds the whole spec from an explicit field list).
#   * REFUSED LOUDLY — a bogus sense id is a 422 carrying the vocabulary's own
#     message, not a 500. The Beanie validator alone raises a pydantic
#     ValidationError from inside the service, which the CloudError handler does
#     not catch; ``docs/api-reference.md`` has promised 422 since SP2-3.
"""The sense fields are settable, readable, and durable over HTTP."""

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

import pytest_asyncio  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

_USER = "u1"
_WS = "w1"


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    """Duck-typed stand-in for the cloud User (same shape test_rbac_routes uses).

    Overriding ``current_active_user`` rather than ``current_user_id`` /
    ``current_workspace_id`` keeps the REAL RBAC guards
    (``require_action_any_workspace("agent.create")``,
    ``require_agent_owner_or_admin``) in the path — they are wiring this module
    depends on, so stubbing them out would let a guard regression pass here.
    """

    def __init__(self) -> None:
        self.id = _USER
        self.active_workspace = _WS
        self.workspaces = [_FakeMembership(_WS)]


@pytest_asyncio.fixture
async def agents_client(mongo_db):
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.agents.router import router as agents_router
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license

    app = FastAPI()
    add_error_handler(app)
    app.include_router(agents_router, prefix="/api/v1")
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: _FakeUser()

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as client:
        yield client


def _create_body(**overrides):
    body = {
        "name": "Mailer",
        "slug": "mailer",
        # Soul materialization touches the filesystem; every sibling test opts out.
        "soul_enabled": False,
    }
    body.update(overrides)
    return body


async def _create(client, **overrides):
    resp = await client.post("/api/v1/agents", json=_create_body(**overrides))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Create → read
# ---------------------------------------------------------------------------


async def test_create_accepts_and_get_returns_both_fields(agents_client) -> None:
    created = await _create(
        agents_client,
        senses=["paw.email.v1", "paw.calendar.v1"],
        sense_prefs={"paw.email.v1": "gmail"},
    )
    assert created["config"]["senses"] == ["paw.email.v1", "paw.calendar.v1"]
    assert created["config"]["sense_prefs"] == {"paw.email.v1": "gmail"}

    fetched = await agents_client.get(f"/api/v1/agents/{created['_id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["config"]["senses"] == ["paw.email.v1", "paw.calendar.v1"]
    assert fetched.json()["config"]["sense_prefs"] == {"paw.email.v1": "gmail"}


async def test_create_without_sense_fields_is_unchanged(agents_client) -> None:
    """Empty is "inherit the workspace surface" — an old client that never sends
    the fields must still get exactly that."""
    created = await _create(agents_client)
    assert created["config"]["senses"] == []
    assert created["config"]["sense_prefs"] == {}


async def test_list_also_emits_the_fields(agents_client) -> None:
    await _create(agents_client, senses=["paw.code.v1"])
    resp = await agents_client.get("/api/v1/agents")
    assert resp.status_code == 200, resp.text
    rows = [a for a in resp.json() if a["uname"] == "mailer"]
    assert rows and rows[0]["config"]["senses"] == ["paw.code.v1"]


# ---------------------------------------------------------------------------
# Update — both branches
# ---------------------------------------------------------------------------


async def test_patch_explicit_fields_sets_both(agents_client) -> None:
    created = await _create(agents_client)
    resp = await agents_client.patch(
        f"/api/v1/agents/{created['_id']}",
        json={"senses": ["paw.email.v1"], "sense_prefs": {"paw.email.v1": "gmail"}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["senses"] == ["paw.email.v1"]
    assert resp.json()["config"]["sense_prefs"] == {"paw.email.v1": "gmail"}


async def test_patch_nested_config_sets_both(agents_client) -> None:
    """The documented payload shape in docs/api-reference.md."""
    created = await _create(agents_client)
    resp = await agents_client.patch(
        f"/api/v1/agents/{created['_id']}",
        json={"config": {"senses": ["paw.code.v1"], "sense_prefs": {"paw.code.v1": "gitlab"}}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["senses"] == ["paw.code.v1"]
    assert resp.json()["config"]["sense_prefs"] == {"paw.code.v1": "gitlab"}


async def test_unrelated_patch_does_not_erase_the_mount_list(agents_client) -> None:
    """The load-bearing round-trip: the config-dict branch of ``_apply_update``
    rebuilds the whole spec from an explicit field list, so a field it forgets to
    name is ERASED — and erasing ``senses`` silently WIDENS the agent back to the
    workspace's entire sense surface."""
    created = await _create(
        agents_client,
        senses=["paw.email.v1"],
        sense_prefs={"paw.email.v1": "gmail"},
    )
    agent_id = created["_id"]

    # Touch something else entirely, once per update branch.
    via_config = await agents_client.patch(
        f"/api/v1/agents/{agent_id}", json={"config": {"temperature": 0.4}}
    )
    assert via_config.status_code == 200, via_config.text
    assert via_config.json()["config"]["senses"] == ["paw.email.v1"]
    assert via_config.json()["config"]["sense_prefs"] == {"paw.email.v1": "gmail"}

    via_explicit = await agents_client.patch(
        f"/api/v1/agents/{agent_id}", json={"model": "claude-x"}
    )
    assert via_explicit.status_code == 200, via_explicit.text
    assert via_explicit.json()["config"]["senses"] == ["paw.email.v1"]
    assert via_explicit.json()["config"]["sense_prefs"] == {"paw.email.v1": "gmail"}

    # …and it is really persisted, not just echoed back.
    fetched = await agents_client.get(f"/api/v1/agents/{agent_id}")
    assert fetched.json()["config"]["senses"] == ["paw.email.v1"]


async def test_patch_can_clear_the_mount_list(agents_client) -> None:
    """Carry-forward must not make the field write-once: an explicit empty list is
    a real value ("go back to inheriting"), not "leave unchanged"."""
    created = await _create(agents_client, senses=["paw.email.v1"])
    resp = await agents_client.patch(f"/api/v1/agents/{created['_id']}", json={"senses": []})
    assert resp.status_code == 200, resp.text
    assert resp.json()["config"]["senses"] == []


# ---------------------------------------------------------------------------
# Validation — a bogus id is a 4xx, on every path in
# ---------------------------------------------------------------------------


def _assert_422_mentions(resp, fragment: str) -> None:
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"
    assert fragment in resp.text, resp.text


async def test_create_rejects_unknown_core_sense_id(agents_client) -> None:
    resp = await agents_client.post(
        "/api/v1/agents", json=_create_body(senses=["paw.telepathy.v1"])
    )
    _assert_422_mentions(resp, "unknown core sense id")


async def test_create_rejects_malformed_sense_pref_key(agents_client) -> None:
    resp = await agents_client.post(
        "/api/v1/agents", json=_create_body(sense_prefs={"not an id": "gmail"})
    )
    _assert_422_mentions(resp, "malformed")


async def test_create_accepts_vendor_extension_ids(agents_client) -> None:
    """Only the closed ``paw.*`` set is policed; the extension space is open."""
    created = await _create(
        agents_client, senses=["acme.crm.v1"], sense_prefs={"acme.crm.v1": "salesforce"}
    )
    assert created["config"]["senses"] == ["acme.crm.v1"]


async def test_patch_explicit_field_rejects_bogus_id(agents_client) -> None:
    created = await _create(agents_client)
    resp = await agents_client.patch(
        f"/api/v1/agents/{created['_id']}", json={"senses": ["paw.telepathy.v1"]}
    )
    _assert_422_mentions(resp, "unknown core sense id")


async def test_patch_nested_config_rejects_bogus_id(agents_client) -> None:
    """The path that used to reach the Beanie validator from inside the service —
    a pydantic ValidationError the CloudError handler never sees, i.e. a 500."""
    created = await _create(agents_client)
    resp = await agents_client.patch(
        f"/api/v1/agents/{created['_id']}", json={"config": {"senses": ["paw.telepathy.v1"]}}
    )
    _assert_422_mentions(resp, "unknown core sense id")


async def test_patch_nested_config_rejects_bogus_pref_key(agents_client) -> None:
    created = await _create(agents_client)
    resp = await agents_client.patch(
        f"/api/v1/agents/{created['_id']}",
        json={"config": {"sense_prefs": {"paw.telepathy.v1": "gmail"}}},
    )
    _assert_422_mentions(resp, "unknown core sense id")


async def test_a_rejected_update_leaves_the_stored_config_alone(agents_client) -> None:
    """A 422 must be a no-op, not a half-applied write."""
    created = await _create(agents_client, senses=["paw.email.v1"])
    agent_id = created["_id"]

    resp = await agents_client.patch(
        f"/api/v1/agents/{agent_id}",
        json={"model": "claude-x", "senses": ["paw.telepathy.v1"]},
    )
    assert resp.status_code == 422, resp.text

    fetched = await agents_client.get(f"/api/v1/agents/{agent_id}")
    assert fetched.json()["config"]["senses"] == ["paw.email.v1"]
    assert fetched.json()["config"]["model"] == ""
