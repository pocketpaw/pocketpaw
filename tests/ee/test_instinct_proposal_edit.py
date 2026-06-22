# tests/ee/test_instinct_proposal_edit.py — F4 (edit-in-review PATCH endpoint).
#
# Created: 2026-06-21 (F4 / feat/szd-finish-core). Covers the
# ``PATCH /instinct/actions/{action_id}/proposal`` endpoint that lets a reviewer
# MUTATE a staged discovery proposal (rename an inferred type, drop a spurious link,
# fix an inferred key, tighten a discovered rule's CEL) BEFORE approving — turning the
# Instinct gate from approve/reject-only into review-EDIT-approve. The edit NEVER flips
# status (Approve stays a separate, deliberate second click).
#
# THE LOAD-BEARING ASSERTIONS (the security crux): a discovery proposal blob carries the
# tenant id in TWO places — the top-level ``workspace_id`` AND (for ``_instinct_rule``)
# ``rule_spec.scope.workspace_id`` — and the executor scopes the persisted rule by the
# SECOND one. The edit endpoint must pin BOTH from the original blob and reject any
# client attempt to change either (mismatch → 403). Miss the second copy and a tenant
# edits a proposal into another workspace.
#   * ``test_patch_pins_workspace_id_top_level`` — a foreign top-level ``workspace_id``
#     is pinned back / 403, never persisted.
#   * ``test_patch_pins_rule_scope_workspace_id`` — THE crux — a foreign
#     ``rule_spec.scope.workspace_id`` → 403, not persisted.
#
# Plus the gate invariants:
#   * ``test_patch_rejects_non_pending_409`` — editing an approved/rejected action → 409.
#   * ``test_patch_bad_cel_returns_422_not_persisted`` — a malformed CEL ``when`` → 422,
#     blob unchanged (re-validation happens BEFORE persist).
#   * ``test_patch_emits_human_corrected_edited_without_completed`` — a valid edit emits
#     ``human.corrected(disposition="edited")`` and NO ``decision.completed`` (the chain
#     stays open for the eventual approve).
#   * ``test_patch_kind_mismatch_422`` — payload kind ≠ blob kind → 422.
#   * ``test_patch_edits_rule_when_then_still_pending`` — a valid edit to the rule
#     ``when`` persists and status stays PENDING.
#
# Updated: 2026-06-22 (feat/szd-finish-followups) — happy-path edit coverage for the
# OTHER two blob kinds the PATCH endpoint handles (the rule kind already had it):
#   * ``test_patch_pocket_create_renames_pocket`` — rename a pending ``_pocket_create``
#     proposal → re-validates (CreatePocketRequest), persists, status stays PENDING,
#     top-level tenancy/owner pinned.
#   * ``test_patch_fabric_objects_drops_a_link`` — drop a spurious link on a pending
#     ``_fabric_objects`` proposal → re-validates the lists, persists, status stays
#     PENDING, ``workspace_id`` pinned.
#
# The 403/409/422 tests are sync and drive the router over HTTP via ``TestClient`` (the
# guard fires BEFORE any executor/DB touch, mirroring test_instinct_rule_router). The
# tests that assert chain emits / persistence are async (``AsyncClient`` +
# ``ASGITransport``) and patch the journal writer so no Beanie is needed.
#
# Run with:
#   uv run --group ee pytest tests/ee/test_instinct_proposal_edit.py -q

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from pocketpaw_ee.cloud._core.deps import current_workspace_id  # noqa: E402
from pocketpaw_ee.cloud._core.http import add_error_handler  # noqa: E402
from pocketpaw_ee.cloud.auth import current_active_user  # noqa: E402
from pocketpaw_ee.cloud.fabric_proposals import FABRIC_OBJECTS_PARAM_KEY  # noqa: E402
from pocketpaw_ee.cloud.instinct_rule_proposals import INSTINCT_RULE_PARAM_KEY  # noqa: E402
from pocketpaw_ee.cloud.license import require_license  # noqa: E402
from pocketpaw_ee.cloud.pocket_proposals import POCKET_CREATE_PARAM_KEY  # noqa: E402
from pocketpaw_ee.instinct.router import router  # noqa: E402

from pocketpaw.instinct.store import InstinctStore  # noqa: E402

TRIGGER = {"type": "agent", "source": "claude", "reason": "proposal edit test"}


# ---------------------------------------------------------------------------
# Fixtures — auth doubles + a CloudError-aware app (cloned from the rule router test)
# ---------------------------------------------------------------------------


class _FakeMembership:
    def __init__(self, workspace: str, role: str = "admin") -> None:
        self.workspace = workspace
        self.role = role


class _FakeUser:
    def __init__(self, user_id: str = "user-A", workspace_id: str = "ws-A") -> None:
        self.id = user_id
        self.active_workspace = workspace_id
        self.workspaces = [_FakeMembership(workspace=workspace_id, role="admin")]


def _rule_spec(workspace_id: str, name: str = "Require approval on high-value invoices") -> dict:
    """A valid editable RuleDraft-shaped rule_spec. Tenancy is duplicated here in
    ``scope.workspace_id`` AND on the blob's top-level ``workspace_id`` — the edit
    endpoint must pin BOTH."""
    return {
        "name": name,
        "description": "Flag invoices over 10k for human review.",
        "when": "object.amount > 10000",
        "action": "require_approval",
        "scope": {"workspace_id": workspace_id, "object_type": "Invoice"},
        "confidence": 0.82,
        "provenance": ["audit:row-1", "correction:c-9"],
    }


def _rule_blob(workspace_id: str, *, name: str = "rule") -> dict:
    """The ``_instinct_rule`` blob the propose helper stores. Tenancy / owner are SEPARATE
    top-level fields; ``rule_spec.scope.workspace_id`` is the SECOND tenancy copy."""
    return {
        "kind": "instinct_rule",
        "schema": 1,
        "workspace_id": workspace_id,
        "user_id": "user-A",
        "rule_spec": _rule_spec(workspace_id, name=name),
        "summary": f"Create the governed rule {name!r}.",
        "correlation_id": None,
        "proposed_event_id": None,
    }


def _pocket_blob(workspace_id: str, *, name: str = "Discovered data") -> dict:
    """The ``_pocket_create`` blob the propose helper stores. Tenancy / owner are SEPARATE
    top-level fields; the editable ``pocket_spec`` is a CreatePocketRequest body (the
    rippleSpec rides under the ``rippleSpec`` camelCase alias)."""
    return {
        "kind": "pocket_create",
        "schema": 1,
        "workspace_id": workspace_id,
        "user_id": "user-A",
        "pocket_spec": {
            "name": name,
            "rippleSpec": {
                "version": "1.0",
                "root": {"id": "root", "type": "container", "props": {}, "children": []},
                "state": {},
            },
        },
        "summary": f"Create the starter Pocket {name!r}.",
        "correlation_id": None,
        "proposed_event_id": None,
    }


def _fabric_blob(workspace_id: str) -> dict:
    """The ``_fabric_objects`` blob the propose helper stores. Tenancy is a SEPARATE
    top-level field; the editable shape is the three lists object_types / objects / links.
    Seeded with TWO links so a happy-path edit can drop the spurious one."""
    return {
        "kind": "fabric_objects",
        "schema": 1,
        "workspace_id": workspace_id,
        "object_types": [
            {"type_name": "Customer", "description": "", "properties": []},
            {"type_name": "Order", "description": "", "properties": []},
        ],
        "objects": [
            {
                "type_name": "Customer",
                "properties": {"name": "Ada"},
                "source_connector": "discovery:Customer",
                "source_id": "cust-1",
            },
            {
                "type_name": "Order",
                "properties": {"total": 42},
                "source_connector": "discovery:Order",
                "source_id": "ord-1",
            },
        ],
        "links": [
            {
                "from": {"source_connector": "discovery:Customer", "source_id": "cust-1"},
                "to": {"source_connector": "discovery:Order", "source_id": "ord-1"},
                "link_type": "placed",
            },
            {
                # A SPURIOUS link the reviewer drops in the happy-path edit.
                "from": {"source_connector": "discovery:Customer", "source_id": "cust-1"},
                "to": {"source_connector": "discovery:Order", "source_id": "ord-1"},
                "link_type": "bogus",
            },
        ],
        "summary": "Create 2 Fabric object(s) across 2 type(s) and 2 link(s).",
        "correlation_id": None,
        "proposed_event_id": None,
    }


@pytest.fixture
def edit_store(tmp_path: Path) -> InstinctStore:
    return InstinctStore(tmp_path / "instinct_proposal_edit.db")


def _make_app(user: _FakeUser, monkeypatch) -> FastAPI:
    """Build a FastAPI app over the instinct router with a CloudError handler so a
    ``Forbidden`` / ``Conflict`` map to 403 / 409 (not 500), license / plan deps stubbed."""
    import pocketpaw_ee.cloud.workspace.service as ws_svc

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))

    app = FastAPI()
    add_error_handler(app)
    app.include_router(router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: user.active_workspace
    return app


def _make_client(edit_store: InstinctStore, user: _FakeUser, monkeypatch) -> TestClient:
    return TestClient(_make_app(user, monkeypatch))


def _propose_rule_action(client: TestClient, *, workspace_id: str, name: str = "rule") -> str:
    """Seed a PENDING Action carrying an ``_instinct_rule`` blob over HTTP, return its id."""
    resp = client.post(
        "/instinct/actions",
        json={
            "pocket_id": workspace_id,
            "title": f"governed rule {name}",
            "trigger": TRIGGER,
            "parameters": {INSTINCT_RULE_PARAM_KEY: _rule_blob(workspace_id, name=name)},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _propose_pocket_action(
    client: TestClient, *, workspace_id: str, name: str = "Discovered data"
) -> str:
    """Seed a PENDING Action carrying a ``_pocket_create`` blob over HTTP, return its id."""
    resp = client.post(
        "/instinct/actions",
        json={
            "pocket_id": workspace_id,
            "title": f"starter pocket {name}",
            "trigger": TRIGGER,
            "parameters": {POCKET_CREATE_PARAM_KEY: _pocket_blob(workspace_id, name=name)},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _propose_fabric_action(client: TestClient, *, workspace_id: str) -> str:
    """Seed a PENDING Action carrying a ``_fabric_objects`` blob over HTTP, return its id."""
    resp = client.post(
        "/instinct/actions",
        json={
            "pocket_id": workspace_id,
            "title": "fabric ontology",
            "trigger": TRIGGER,
            "parameters": {FABRIC_OBJECTS_PARAM_KEY: _fabric_blob(workspace_id)},
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _action_by_id(client: TestClient, action_id: str) -> dict[str, Any]:
    resp = client.get("/instinct/actions", params={"limit": 500})
    assert resp.status_code == 200, resp.text
    for action in resp.json()["actions"]:
        if action["id"] == action_id:
            return action
    raise AssertionError(f"action {action_id} not found")


def _stored_rule_blob(client: TestClient, action_id: str) -> dict[str, Any]:
    return _action_by_id(client, action_id)["parameters"][INSTINCT_RULE_PARAM_KEY]


def _stored_pocket_blob(client: TestClient, action_id: str) -> dict[str, Any]:
    return _action_by_id(client, action_id)["parameters"][POCKET_CREATE_PARAM_KEY]


def _stored_fabric_blob(client: TestClient, action_id: str) -> dict[str, Any]:
    return _action_by_id(client, action_id)["parameters"][FABRIC_OBJECTS_PARAM_KEY]


# ===========================================================================
# THE SECURITY CRUX — BOTH tenancy copies are pinned. A foreign top-level
# ``workspace_id`` OR a foreign ``rule_spec.scope.workspace_id`` → 403, never persisted.
# ===========================================================================


def test_patch_pins_workspace_id_top_level(edit_store: InstinctStore, monkeypatch) -> None:
    """A client sending a foreign TOP-LEVEL ``workspace_id`` in the edit is refused with
    403 and the blob is never persisted with the foreign workspace."""
    client = _make_client(edit_store, _FakeUser("user-A", "ws-A"), monkeypatch)
    with patch("pocketpaw_ee.instinct.router._store", return_value=edit_store):
        action_id = _propose_rule_action(client, workspace_id="ws-A")
        # The editable sub-field is fine, but the client also smuggles a foreign
        # top-level workspace_id alongside it.
        edited_spec = _rule_spec("ws-A")
        edited_spec["when"] = "object.amount > 5000"
        resp = client.patch(
            f"/instinct/actions/{action_id}/proposal",
            json={"workspace_id": "ws-EVIL", "rule_spec": edited_spec},
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"

        # The blob's top-level workspace_id is STILL ws-A — the foreign value never landed.
        blob = _stored_rule_blob(client, action_id)
        assert blob["workspace_id"] == "ws-A"
        # And the editable when was NOT persisted (the whole edit was refused).
        assert blob["rule_spec"]["when"] == "object.amount > 10000"
        assert _action_by_id(client, action_id)["status"] == "pending"


def test_patch_pins_rule_scope_workspace_id(edit_store: InstinctStore, monkeypatch) -> None:
    """THE security crux — a client sending a foreign ``rule_spec.scope.workspace_id``
    (the SECOND tenancy copy, which the executor scopes the rule by) → 403, not persisted.
    Miss this copy and a tenant edits the rule into another workspace."""
    client = _make_client(edit_store, _FakeUser("user-A", "ws-A"), monkeypatch)
    with patch("pocketpaw_ee.instinct.router._store", return_value=edit_store):
        action_id = _propose_rule_action(client, workspace_id="ws-A")
        # Foreign workspace smuggled INSIDE the editable rule_spec's scope.
        edited_spec = _rule_spec("ws-A")
        edited_spec["scope"]["workspace_id"] = "ws-EVIL"
        resp = client.patch(
            f"/instinct/actions/{action_id}/proposal",
            json={"rule_spec": edited_spec},
        )
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["code"] == "instinct.cross_workspace_approval"

        # The SECOND tenancy copy is STILL ws-A — the foreign scope never landed.
        blob = _stored_rule_blob(client, action_id)
        assert blob["rule_spec"]["scope"]["workspace_id"] == "ws-A"
        assert _action_by_id(client, action_id)["status"] == "pending"


# ===========================================================================
# GATE INVARIANTS — status / kind / re-validation.
# ===========================================================================


def test_patch_rejects_non_pending_409(edit_store: InstinctStore, monkeypatch) -> None:
    """Editing an already-approved (non-PENDING) action → 409. Its chain is closed; an
    edit-in-review only makes sense before the approve click."""
    client = _make_client(edit_store, _FakeUser("user-A", "ws-A"), monkeypatch)
    with patch("pocketpaw_ee.instinct.router._store", return_value=edit_store):
        action_id = _propose_rule_action(client, workspace_id="ws-A")
        # Flip the action out of PENDING directly via the store (no executor side-effects).
        import asyncio

        asyncio.run(edit_store.approve(action_id, approver="user-A"))

        edited_spec = _rule_spec("ws-A")
        edited_spec["when"] = "object.amount > 1"
        resp = client.patch(
            f"/instinct/actions/{action_id}/proposal",
            json={"rule_spec": edited_spec},
        )
        assert resp.status_code == 409, resp.text


def test_patch_bad_cel_returns_422_not_persisted(edit_store: InstinctStore, monkeypatch) -> None:
    """A malformed CEL ``when`` fails re-validation with 422 and the blob is unchanged —
    re-validation runs BEFORE persist, so a broken rule never lands."""
    client = _make_client(edit_store, _FakeUser("user-A", "ws-A"), monkeypatch)
    with patch("pocketpaw_ee.instinct.router._store", return_value=edit_store):
        action_id = _propose_rule_action(client, workspace_id="ws-A")
        edited_spec = _rule_spec("ws-A")
        edited_spec["when"] = "object.amount >>> (((bad cel"
        resp = client.patch(
            f"/instinct/actions/{action_id}/proposal",
            json={"rule_spec": edited_spec},
        )
        assert resp.status_code == 422, resp.text

        # The stored when is unchanged — the broken edit never persisted.
        blob = _stored_rule_blob(client, action_id)
        assert blob["rule_spec"]["when"] == "object.amount > 10000"
        assert _action_by_id(client, action_id)["status"] == "pending"


def test_patch_kind_mismatch_422(edit_store: InstinctStore, monkeypatch) -> None:
    """A payload whose kind (``pocket_spec``) does not match the action's blob kind
    (``_instinct_rule``) → 422. The endpoint edits ONLY the matching sub-field."""
    client = _make_client(edit_store, _FakeUser("user-A", "ws-A"), monkeypatch)
    with patch("pocketpaw_ee.instinct.router._store", return_value=edit_store):
        action_id = _propose_rule_action(client, workspace_id="ws-A")
        resp = client.patch(
            f"/instinct/actions/{action_id}/proposal",
            json={"pocket_spec": {"name": "wrong kind"}},
        )
        assert resp.status_code == 422, resp.text

        # The rule blob is untouched.
        blob = _stored_rule_blob(client, action_id)
        assert blob["rule_spec"]["when"] == "object.amount > 10000"


def test_patch_edits_rule_when_then_still_pending(edit_store: InstinctStore, monkeypatch) -> None:
    """A valid edit to the rule ``when`` persists the tightened CEL AND keeps the action
    PENDING — the edit does NOT approve; Approve stays a separate click."""
    client = _make_client(edit_store, _FakeUser("user-A", "ws-A"), monkeypatch)
    with patch("pocketpaw_ee.instinct.router._store", return_value=edit_store):
        action_id = _propose_rule_action(client, workspace_id="ws-A")
        edited_spec = _rule_spec("ws-A")
        edited_spec["when"] = "object.amount > 25000"
        resp = client.patch(
            f"/instinct/actions/{action_id}/proposal",
            json={"rule_spec": edited_spec},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["action"]["status"] == "pending"
        assert resp.json()["correction"] is not None

        # The tightened when PERSISTED; tenancy copies are intact; status still PENDING.
        blob = _stored_rule_blob(client, action_id)
        assert blob["rule_spec"]["when"] == "object.amount > 25000"
        assert blob["workspace_id"] == "ws-A"
        assert blob["rule_spec"]["scope"]["workspace_id"] == "ws-A"
        assert _action_by_id(client, action_id)["status"] == "pending"


def test_patch_pocket_create_renames_pocket(edit_store: InstinctStore, monkeypatch) -> None:
    """A valid edit to a ``_pocket_create`` proposal renames the staged Pocket: it
    re-validates (CreatePocketRequest), persists, status stays PENDING, and the top-level
    tenancy / owner stay pinned (they live OUTSIDE the editable pocket_spec)."""
    client = _make_client(edit_store, _FakeUser("user-A", "ws-A"), monkeypatch)
    with patch("pocketpaw_ee.instinct.router._store", return_value=edit_store):
        action_id = _propose_pocket_action(client, workspace_id="ws-A", name="Discovered data")
        # Rename the pocket; carry the rippleSpec along so CreatePocketRequest re-validates.
        edited_spec = {
            "name": "Renamed dashboard",
            "rippleSpec": {
                "version": "1.0",
                "root": {"id": "root", "type": "container", "props": {}, "children": []},
                "state": {},
            },
        }
        resp = client.patch(
            f"/instinct/actions/{action_id}/proposal",
            json={"pocket_spec": edited_spec},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["action"]["status"] == "pending"
        assert resp.json()["correction"] is not None

        # The new name PERSISTED; tenancy/owner pinned; status still PENDING.
        blob = _stored_pocket_blob(client, action_id)
        assert blob["pocket_spec"]["name"] == "Renamed dashboard"
        assert blob["workspace_id"] == "ws-A"
        assert blob["user_id"] == "user-A"
        assert _action_by_id(client, action_id)["status"] == "pending"


def test_patch_fabric_objects_drops_a_link(edit_store: InstinctStore, monkeypatch) -> None:
    """A valid edit to a ``_fabric_objects`` proposal drops a spurious link: it re-validates
    the editable lists, persists, status stays PENDING, and the top-level ``workspace_id``
    stays pinned (it lives OUTSIDE the editable lists)."""
    client = _make_client(edit_store, _FakeUser("user-A", "ws-A"), monkeypatch)
    with patch("pocketpaw_ee.instinct.router._store", return_value=edit_store):
        action_id = _propose_fabric_action(client, workspace_id="ws-A")
        before = _stored_fabric_blob(client, action_id)
        assert len(before["links"]) == 2  # seeded with the real link + the spurious one

        # Keep ONLY the legitimate link (drop the "bogus" one).
        kept_links = [link for link in before["links"] if link["link_type"] == "placed"]
        resp = client.patch(
            f"/instinct/actions/{action_id}/proposal",
            json={"fabric": {"links": kept_links}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["action"]["status"] == "pending"
        assert resp.json()["correction"] is not None

        # The spurious link is GONE; objects/types untouched; tenancy pinned; still PENDING.
        blob = _stored_fabric_blob(client, action_id)
        assert len(blob["links"]) == 1
        assert blob["links"][0]["link_type"] == "placed"
        assert {ot["type_name"] for ot in blob["object_types"]} == {"Customer", "Order"}
        assert len(blob["objects"]) == 2
        assert blob["workspace_id"] == "ws-A"
        assert _action_by_id(client, action_id)["status"] == "pending"


# ===========================================================================
# CHAIN — a valid edit emits human.corrected(edited) and NO decision.completed
# (the chain stays OPEN for the eventual approve).
# ===========================================================================


async def _async_client(user: _FakeUser, monkeypatch) -> AsyncClient:
    app = _make_app(user, monkeypatch)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def test_patch_emits_human_corrected_edited_without_completed(
    edit_store: InstinctStore, monkeypatch
) -> None:
    """A valid edit lands ``agent.proposed → human.corrected(edited)`` WITHOUT a
    ``decision.completed`` — the chain stays open for the eventual approve click."""
    import pocketpaw_ee.cloud.decisions.journal_writer as jw

    corrected: list[dict[str, Any]] = []
    completed: list[dict[str, Any]] = []

    def _spy_corrected(**kwargs: Any) -> Any:
        corrected.append(kwargs)

        class _E:
            id = "evt-corrected"

        return _E()

    def _spy_completed(**kwargs: Any) -> Any:
        completed.append(kwargs)
        return None

    monkeypatch.setattr(jw, "record_human_corrected", _spy_corrected)
    monkeypatch.setattr(jw, "record_decision_completed", _spy_completed)

    user = _FakeUser("user-A", "ws-A")
    with patch("pocketpaw_ee.instinct.router._store", return_value=edit_store):
        async with await _async_client(user, monkeypatch) as client:
            # Seed a rule action carrying a non-null correlation_id so the emit fires.
            blob = _rule_blob("ws-A")
            blob["correlation_id"] = "11111111-1111-1111-1111-111111111111"
            seed = await client.post(
                "/instinct/actions",
                json={
                    "pocket_id": "ws-A",
                    "title": "governed rule emit",
                    "trigger": TRIGGER,
                    "parameters": {INSTINCT_RULE_PARAM_KEY: blob},
                },
            )
            assert seed.status_code == 201, seed.text
            action_id = seed.json()["id"]

            edited_spec = _rule_spec("ws-A")
            edited_spec["when"] = "object.amount > 50000"
            resp = await client.patch(
                f"/instinct/actions/{action_id}/proposal",
                json={"rule_spec": edited_spec},
            )
            assert resp.status_code == 200, resp.text

    # human.corrected fired exactly once with disposition="edited".
    assert len(corrected) == 1
    assert corrected[0]["payload"]["disposition"] == "edited"
    # NO decision.completed — the chain stays open for the eventual approve.
    assert completed == []
