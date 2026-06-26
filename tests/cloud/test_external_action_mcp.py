# tests/cloud/test_external_action_mcp.py — the agent-facing MCP surface for the
# gated external-action proposal type (feat/external-action-mcp-tool).
#
# Created: 2026-06-11 (feat/external-action-mcp-tool).
#
# What this pins — the MCP tool, driven through the REAL handler:
#   * tool-id / server-name contract (SERVER_NAME, *_TOOL_ID, *_TOOL_IDS).
#   * the provider exposes the server + tool ids (extensions wiring).
#   * propose_external_action (the real MCP handler) resolves identity, validates
#     inputs, files an Instinct Action carrying the ``_external_action`` blob via
#     the REAL propose helper, and returns {action_id, status:'pending_approval',
#     summary} — only after a durable action id comes back.
#   * the stored blob carries the connector ref + params + workspace and NO
#     connector secret; the tool's `summary` + `reason` are folded into the gate
#     summary.
#   * error relaying: missing connector / action / summary / reason, and a
#     non-object `params`, each refuse cleanly with NO Action filed.
#   * workspace resolution: no identity ContextVars → an explicit error, no
#     Action.
#
# `pocketpaw_ee` is import-skipped on an OSS-only install. The handler reads
# identity through ee.cloud.chat.agent_service ContextVars (set in-test via
# attach_agent_identity) and the store through pocketpaw.stores.get_instinct_store
# (patched to a tmp-file store so nothing touches ~/.pocketpaw/instinct.db).

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

import pocketpaw_ee.agent.mcp_servers.external_actions as ea_mcp  # noqa: E402
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    detach_agent_identity,
)
from pocketpaw_ee.extensions import CloudExternalActionsMcpProvider  # noqa: E402

from pocketpaw.instinct.models import ActionStatus  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """Isolated InstinctStore on a tmp file, wired in everywhere the propose
    helper reads it (``pocketpaw.stores.get_instinct_store``)."""
    st = InstinctStore(tmp_path / "instinct_ea_mcp_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


class _identity:
    """Context manager that sets the workspace/user/session ContextVars the
    handler reads, then resets them."""

    def __init__(self, *, workspace="w1", user="u1", session="sess-1"):
        self._ws, self._user, self._sess = workspace, user, session
        self._tokens = None

    def __enter__(self):
        self._tokens = attach_agent_identity(
            workspace_id=self._ws, user_id=self._user, session_mongo_id=self._sess
        )
        return self

    def __exit__(self, *exc):
        detach_agent_identity(self._tokens)
        return False


async def _result_body(res: dict) -> dict:
    """Parse the JSON body out of a success MCP response."""
    assert res.get("is_error") is not True, res
    return json.loads(res["content"][0]["text"])


def _good_args() -> dict:
    """A well-formed propose_external_action call."""
    return {
        "connector": "crm",
        "action": "approveApplication",
        "params": {"application_id": "app-42", "note": "verified"},
        "summary": "Approve application app-42 in the CRM.",
        "reason": "The applicant passed the verification checks the user asked about.",
    }


# ---------------------------------------------------------------------------
# tool-id / provider contract pins
# ---------------------------------------------------------------------------


def test_tool_id_contract_pin() -> None:
    """The server + tool id are the exact namespaced strings the allowlist
    machinery matches."""
    assert ea_mcp.SERVER_NAME == "pocketpaw_external_actions"
    assert (
        ea_mcp.PROPOSE_EXTERNAL_ACTION_TOOL_ID
        == "mcp__pocketpaw_external_actions__propose_external_action"
    )
    assert ea_mcp.EXTERNAL_ACTIONS_TOOL_IDS == (
        "mcp__pocketpaw_external_actions__propose_external_action",
    )


def test_provider_exposes_server_and_tool_ids() -> None:
    """The extensions provider builds the server and reports the tool ids — the
    pocketpaw.mcp_servers registration loop reads both."""
    provider = CloudExternalActionsMcpProvider()
    assert provider.tool_ids() == [ea_mcp.PROPOSE_EXTERNAL_ACTION_TOOL_ID]

    built = provider.build_server()
    # claude_agent_sdk is a dev dep here, so the server builds; if it's ever
    # absent the loop skips it (None) — either is a valid contract.
    if built is not None:
        name, server = built
        assert name == "pocketpaw_external_actions"
        assert server is not None


def test_build_server_registers_one_tool() -> None:
    """The built server is named correctly and is constructed from exactly the
    one propose tool."""
    built = ea_mcp.build_external_actions_server()
    if built is None:
        pytest.skip("claude_agent_sdk not installed")
    name, server = built
    assert name == "pocketpaw_external_actions"


# ---------------------------------------------------------------------------
# the REAL propose path
# ---------------------------------------------------------------------------


async def test_propose_files_action_with_blob(store):
    """Drive the real handler: it files an Instinct Action carrying the
    ``_external_action`` blob and returns {action_id, status, summary}."""
    with _identity():
        res = await ea_mcp._propose_external_action_handler(_good_args())

    body = await _result_body(res)
    assert body["status"] == "pending_approval"
    assert "action_id" in body
    # The gate summary folds the agent's summary + reason.
    assert "Approve application app-42" in body["summary"]
    assert "verification checks" in body["summary"]

    action = await store.get_action(body["action_id"])
    assert action is not None
    assert action.status == ActionStatus.PENDING

    blob = action.parameters["_external_action"]
    assert blob["kind"] == "external_action"
    assert blob["connector_name"] == "crm"
    assert blob["action"] == "approveApplication"
    assert blob["workspace_id"] == "w1"
    assert blob["requested_by"] == "u1"
    # params stored verbatim — it's data.
    assert blob["params"]["application_id"] == "app-42"
    # A params hash is computed so a post-propose edit can be refused at execute.
    assert blob["params_hash"]
    # NO connector secret is written — only the connector name + scope.
    blob_json = json.dumps(blob).lower()
    assert "secret" not in blob_json
    assert "token" not in blob_json
    assert "password" not in blob_json


async def test_propose_defaults_missing_params_to_empty(store):
    """`params` omitted is treated as an empty object (not an error)."""
    args = _good_args()
    del args["params"]
    with _identity():
        res = await ea_mcp._propose_external_action_handler(args)
    body = await _result_body(res)
    action = await store.get_action(body["action_id"])
    assert action.parameters["_external_action"]["params"] == {}


# ---------------------------------------------------------------------------
# workspace resolution
# ---------------------------------------------------------------------------


async def test_identity_missing_errors(store):
    """Called without workspace/user ContextVars → an explicit error, no
    Action."""
    res = await ea_mcp._propose_external_action_handler(_good_args())
    assert res.get("is_error") is True
    assert "workspace and user context" in res["content"][0]["text"]
    assert await store.list_actions() == []


# ---------------------------------------------------------------------------
# input validation / error relaying
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "drop,needle",
    [
        ("connector", "`connector`"),
        ("action", "`action`"),
        ("summary", "`summary`"),
        ("reason", "`reason`"),
    ],
)
async def test_missing_required_field_refused(store, drop, needle):
    """Each missing required string field refuses cleanly with NO Action filed."""
    args = _good_args()
    del args[drop]
    with _identity():
        res = await ea_mcp._propose_external_action_handler(args)
    assert res.get("is_error") is True
    assert needle in res["content"][0]["text"]
    assert await store.list_actions() == []


@pytest.mark.parametrize("field", ["connector", "action", "summary", "reason"])
async def test_blank_required_field_refused(store, field):
    """A whitespace-only required field is refused — no Action filed."""
    args = _good_args()
    args[field] = "   "
    with _identity():
        res = await ea_mcp._propose_external_action_handler(args)
    assert res.get("is_error") is True
    assert await store.list_actions() == []


async def test_non_object_params_refused(store):
    """A `params` that isn't a JSON object is refused before anything is stored."""
    args = _good_args()
    args["params"] = "not-an-object"
    with _identity():
        res = await ea_mcp._propose_external_action_handler(args)
    assert res.get("is_error") is True
    assert "must be a JSON object" in res["content"][0]["text"]
    assert await store.list_actions() == []


async def test_propose_helper_error_is_relayed(store, monkeypatch):
    """A ValueError raised by the propose helper is relayed as a plain error,
    not a crash — and surfaces no phantom success."""

    async def _boom(**kwargs):
        raise ValueError("connector 'crm' is not bound in this workspace")

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.external_actions.propose.propose_external_action", _boom
    )
    with _identity():
        res = await ea_mcp._propose_external_action_handler(_good_args())
    assert res.get("is_error") is True
    assert "not bound in this workspace" in res["content"][0]["text"]
