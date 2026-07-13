# tests/cloud/test_instinct_mcp.py — the agent-facing MCP surface for read-only
# Instinct gate visibility (feat/fabric-instinct-mcp-providers).
#
# Created: 2026-06-11 (feat/fabric-instinct-mcp-providers).
#
# What this pins — the MCP tools, driven through the REAL handlers against a
# tmp-file InstinctStore:
#   * tool-id / server-name contract (SERVER_NAME, *_TOOL_ID, *_TOOL_IDS).
#   * the provider exposes the server + tool ids (extensions wiring).
#   * instinct_pending resolves the workspace from ContextVars and lists the
#     tenant's pending actions only (W4a scope) — JSON-friendly gate-surface
#     fields, and the Action ``parameters`` blob (diffs / call params) is NOT
#     serialized.
#   * instinct_audit returns the workspace's audit entries, clamps the limit,
#     JSON-friendly.
#   * results are size-capped: an oversized list is truncated from the tail and
#     flagged truncated=true.
#   * READ-ONLY pin: the module exposes exactly the two read tools — no
#     propose/approve/reject/execute surface (proposing goes through
#     pocketpaw_external_actions).
#   * error relaying: bad input types refuse cleanly; a store failure returns a
#     plain relayable error; missing identity refuses.
#
# `pocketpaw_ee` is import-skipped on an OSS-only install. The handlers read
# identity through ee.cloud.chat.agent_service ContextVars (set in-test via
# attach_agent_identity) and the store through pocketpaw.stores.get_instinct_store
# (patched to a tmp-file store so nothing touches ~/.pocketpaw/instinct.db).

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pocketpaw_ee")

import pocketpaw_ee.agent.mcp_servers.instinct as instinct_mcp  # noqa: E402
from pocketpaw_ee.cloud.chat.agent_service import (  # noqa: E402
    attach_agent_identity,
    detach_agent_identity,
)
from pocketpaw_ee.extensions import CloudInstinctMcpProvider  # noqa: E402

from pocketpaw.instinct.models import ActionTrigger  # noqa: E402
from pocketpaw.instinct.store import InstinctStore  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> InstinctStore:
    """Isolated InstinctStore on a tmp file, wired in where the handlers read
    it (``pocketpaw.stores.get_instinct_store``)."""
    st = InstinctStore(tmp_path / "instinct_mcp_test.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: st)
    return st


async def _seed_actions(store: InstinctStore) -> list[str]:
    """Propose three actions: two in workspace w1 (one carrying a parameters
    blob), one in workspace w2. Returns the w1 action ids."""
    trigger = ActionTrigger(type="agent", source="test", reason="seed")
    a = await store.propose(
        pocket_id="p1",
        title="Reorder oat milk",
        description="stock is low",
        recommendation="Order 20 units",
        trigger=trigger,
        workspace_id="w1",
    )
    b = await store.propose(
        pocket_id="p2",
        title="Flag invoice",
        description="amount looks off",
        recommendation="Hold for review",
        trigger=trigger,
        parameters={"_external_action": {"params": {"secret_payload": True}}},
        workspace_id="w1",
    )
    await store.propose(
        pocket_id="p9",
        title="Other tenant action",
        description="not ours",
        recommendation="Should never be visible",
        trigger=trigger,
        workspace_id="w2",
    )
    return [a.id, b.id]


class _identity:
    """Context manager that sets the workspace/user/session ContextVars the
    handlers read, then resets them."""

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


# ---------------------------------------------------------------------------
# tool-id / provider contract pins
# ---------------------------------------------------------------------------


def test_tool_id_contract_pin() -> None:
    """The server + tool ids are the exact namespaced strings the allowlist
    machinery matches."""
    assert instinct_mcp.SERVER_NAME == "pocketpaw_instinct"
    assert instinct_mcp.INSTINCT_PENDING_TOOL_ID == "mcp__pocketpaw_instinct__instinct_pending"
    assert instinct_mcp.INSTINCT_AUDIT_TOOL_ID == "mcp__pocketpaw_instinct__instinct_audit"
    assert instinct_mcp.INSTINCT_TOOL_IDS == (
        "mcp__pocketpaw_instinct__instinct_pending",
        "mcp__pocketpaw_instinct__instinct_audit",
    )


def test_read_only_surface_pin() -> None:
    """The server exposes exactly the two READ tools — no propose / approve /
    reject / execute surface. Gated proposing on this backend goes through
    pocketpaw_external_actions."""
    assert len(instinct_mcp.INSTINCT_TOOL_IDS) == 2
    for tool_id in instinct_mcp.INSTINCT_TOOL_IDS:
        assert "propose" not in tool_id
        assert "approve" not in tool_id
        assert "reject" not in tool_id
        assert "execute" not in tool_id


def test_provider_exposes_server_and_tool_ids() -> None:
    """The extensions provider builds the server and reports the tool ids — the
    pocketpaw.mcp_servers registration loop reads both."""
    provider = CloudInstinctMcpProvider()
    assert provider.tool_ids() == list(instinct_mcp.INSTINCT_TOOL_IDS)

    built = provider.build_server()
    if built is not None:
        name, server = built
        assert name == "pocketpaw_instinct"
        assert server is not None


# ---------------------------------------------------------------------------
# instinct_pending — the REAL read path
# ---------------------------------------------------------------------------


async def test_pending_lists_workspace_actions_only(store):
    """Pending actions come back JSON-friendly and scoped to the caller's
    workspace — another tenant's queue never appears."""
    w1_ids = await _seed_actions(store)

    with _identity(workspace="w1"):
        res = await instinct_mcp._instinct_pending_handler({})

    body = await _result_body(res)
    assert body["count"] == 2
    assert body["truncated"] is False
    got_ids = {a["id"] for a in body["actions"]}
    assert got_ids == set(w1_ids)
    titles = {a["title"] for a in body["actions"]}
    assert "Other tenant action" not in titles
    for a in body["actions"]:
        assert a["status"] == "pending"
        assert a["recommendation"]
        assert a["priority"]


async def test_pending_never_serializes_parameters_blob(store):
    """The Action ``parameters`` blob (diffs / call params) must NOT ride on
    the gate-visibility response."""
    await _seed_actions(store)

    with _identity(workspace="w1"):
        res = await instinct_mcp._instinct_pending_handler({})

    raw = res["content"][0]["text"]
    assert "secret_payload" not in raw
    assert "_external_action" not in raw
    body = json.loads(raw)
    for a in body["actions"]:
        assert "parameters" not in a


async def test_pending_pocket_filter(store):
    """The optional pocket_id filter narrows the list."""
    await _seed_actions(store)

    with _identity(workspace="w1"):
        res = await instinct_mcp._instinct_pending_handler({"pocket_id": "p1"})

    body = await _result_body(res)
    assert body["count"] == 1
    assert body["actions"][0]["title"] == "Reorder oat milk"


async def test_pending_truncates_oversized_results(store, monkeypatch):
    """An action list over the byte budget is truncated from the tail and
    flagged."""
    await _seed_actions(store)
    monkeypatch.setattr(instinct_mcp, "MAX_RESULT_BYTES", 80)

    with _identity(workspace="w1"):
        res = await instinct_mcp._instinct_pending_handler({})

    body = await _result_body(res)
    assert body["truncated"] is True
    assert body["returned"] < body["count"]


# ---------------------------------------------------------------------------
# instinct_audit
# ---------------------------------------------------------------------------


async def test_audit_returns_workspace_entries(store):
    """Proposing writes audit entries; the handler returns the workspace's
    entries JSON-friendly."""
    await _seed_actions(store)

    with _identity(workspace="w1"):
        res = await instinct_mcp._instinct_audit_handler({})

    body = await _result_body(res)
    assert body["count"] >= 2
    events = {e["event"] for e in body["entries"]}
    assert any("propose" in ev for ev in events)
    descriptions = " ".join(e["description"] or "" for e in body["entries"])
    assert "Other tenant action" not in descriptions
    for e in body["entries"]:
        assert e["actor"]
        assert e["category"]


async def test_audit_clamps_limit(store):
    """A limit over the cap is clamped, not refused."""
    await _seed_actions(store)

    with _identity(workspace="w1"):
        res = await instinct_mcp._instinct_audit_handler({"limit": 10_000})

    body = await _result_body(res)
    assert body["returned"] <= instinct_mcp.MAX_AUDIT_LIMIT


# ---------------------------------------------------------------------------
# workspace resolution + error relaying
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "handler",
    [instinct_mcp._instinct_pending_handler, instinct_mcp._instinct_audit_handler],
)
async def test_identity_missing_errors(store, handler):
    """Called without workspace ContextVars → an explicit error."""
    res = await handler({})
    assert res.get("is_error") is True
    assert "workspace context" in res["content"][0]["text"]


@pytest.mark.parametrize(
    "handler,args,needle",
    [
        (instinct_mcp._instinct_pending_handler, {"pocket_id": 42}, "`pocket_id`"),
        (instinct_mcp._instinct_audit_handler, {"limit": 0}, "`limit`"),
        (instinct_mcp._instinct_audit_handler, {"limit": "ten"}, "`limit`"),
    ],
)
async def test_bad_input_refused(store, handler, args, needle):
    """Malformed inputs refuse cleanly with the offending field named."""
    with _identity(workspace="w1"):
        res = await handler(args)
    assert res.get("is_error") is True
    assert needle in res["content"][0]["text"]


async def test_store_error_is_relayed(store, monkeypatch):
    """A store failure is relayed as a plain error, not a crash."""

    class _Boom:
        async def pending(self, pocket_id=None, workspace_id=None):
            raise RuntimeError("instinct db is locked")

    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: _Boom())
    with _identity(workspace="w1"):
        res = await instinct_mcp._instinct_pending_handler({})
    assert res.get("is_error") is True
    assert "instinct db is locked" in res["content"][0]["text"]
