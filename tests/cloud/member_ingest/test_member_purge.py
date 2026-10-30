# tests/cloud/member_ingest/test_member_purge.py
# Created: 2026-06-08 — VIP Onboarding Phase B, chunk 7 (the purge path).
#
# Pins the per-member data-purge contract. When a member disconnects their
# accounts or is offboarded, ALL of their Phase B per-user data MUST be
# deleted — it's the member's personal Gmail/calendar; leaving must purge it.
# The four stores, all keyed on the opaque member id:
#   1. the ``user:{member_id}`` kb-go scope (their ingested mail/cal),
#   2. their per-user OAuth tokens (token_store: google_gmail / google_calendar),
#   3. their per-user WorkspaceConnector rows (scope="user"),
#   4. their MemberIngestState sync-state doc.
#
# The ISOLATION test comes FIRST (TDD), mirroring the ingest suite: purging
# member A must NEVER touch member B's data. Then all-four-deleted, then
# idempotency (safe to call twice; safe when nothing exists).
#
# The kb-go ``clear`` subprocess and the on-disk token store are injected /
# redirected so the suite runs with no kb binary and no real ~/.pocketpaw.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.member_ingest import purge as purge_mod  # noqa: E402
from pocketpaw_ee.cloud.models.connector import WorkspaceConnector  # noqa: E402
from pocketpaw_ee.cloud.models.member_ingest_state import (  # noqa: E402
    MemberIngestState,
)

from pocketpaw.clients.token_store import OAuthTokens, TokenStore  # noqa: E402

pytestmark = pytest.mark.asyncio

# Token store service names the per-user Gmail/Calendar clients load under.
# GmailClient(user_id) -> service="google_gmail"; CalendarClient(user_id) ->
# service="google_calendar". The purge MUST delete BOTH for the member.
_GMAIL_SERVICE = "google_gmail"
_CAL_SERVICE = "google_calendar"


class CapturingKbClear:
    """Stands in for the keyless ``kb clear --scope <s>`` subprocess. Records
    every scope it was asked to clear so a test can prove the purge clears the
    member's OWN scope and nobody else's."""

    def __init__(self) -> None:
        self.scopes_cleared: list[str] = []

    async def __call__(self, scope: str) -> dict:
        self.scopes_cleared.append(scope)
        return {"cleared": scope}


@pytest.fixture
def token_store(tmp_path, monkeypatch) -> TokenStore:
    """A TokenStore rooted at a tmp dir (never the real ~/.pocketpaw/oauth).

    token_store builds its dir via ``pocketpaw.config.get_config_dir`` resolved
    at call time, so redirecting that function isolates the on-disk tokens.
    """
    monkeypatch.setattr(
        "pocketpaw.clients.token_store.get_config_dir",
        lambda: tmp_path,
    )
    return TokenStore()


def _seed_tokens(store: TokenStore, user_id: str) -> None:
    """Write per-user Gmail + Calendar tokens for ``user_id``."""
    store.save(
        OAuthTokens(service=_GMAIL_SERVICE, access_token="g-tok"),
        user_id=user_id,
    )
    store.save(
        OAuthTokens(service=_CAL_SERVICE, access_token="c-tok"),
        user_id=user_id,
    )


async def _seed_connectors(workspace_id: str, user_id: str) -> None:
    """Insert the member's per-user gmail + gcalendar connector rows."""
    await WorkspaceConnector(
        workspace=workspace_id, name="gmail", scope="user", user_id=user_id
    ).insert()
    await WorkspaceConnector(
        workspace=workspace_id, name="gcalendar", scope="user", user_id=user_id
    ).insert()


async def _seed_state(workspace_id: str, member_id: str) -> None:
    await MemberIngestState(
        workspace=workspace_id, member_id=member_id, backfill_done=True, status="ok"
    ).insert()


# --------------------------------------------------------------------------
# 1 — THE ISOLATION INVARIANT. Purging member A never touches member B.
# --------------------------------------------------------------------------


async def test_purge_member_a_never_touches_member_b(mongo_db, token_store):  # noqa: ARG001
    alice = "alice-objid"
    bob = "bob-objid"
    ws = "w1"

    # Seed BOTH members across all four stores.
    _seed_tokens(token_store, alice)
    _seed_tokens(token_store, bob)
    await _seed_connectors(ws, alice)
    await _seed_connectors(ws, bob)
    await _seed_state(ws, alice)
    await _seed_state(ws, bob)

    kb_clear = CapturingKbClear()

    await purge_mod.purge_member_data(ws, alice, kb_clear=kb_clear, token_store=token_store)

    # KB: only alice's scope was cleared — never bob's.
    assert kb_clear.scopes_cleared == [f"user:{alice}"]
    assert f"user:{bob}" not in kb_clear.scopes_cleared

    # Tokens: alice's are gone, bob's survive untouched.
    assert token_store.load(_GMAIL_SERVICE, alice) is None
    assert token_store.load(_CAL_SERVICE, alice) is None
    assert token_store.load(_GMAIL_SERVICE, bob) is not None
    assert token_store.load(_CAL_SERVICE, bob) is not None

    # Connector rows: alice's user-scoped rows gone, bob's remain.
    alice_rows = await WorkspaceConnector.find(
        WorkspaceConnector.workspace == ws,
        WorkspaceConnector.user_id == alice,
    ).to_list()
    bob_rows = await WorkspaceConnector.find(
        WorkspaceConnector.workspace == ws,
        WorkspaceConnector.user_id == bob,
    ).to_list()
    assert alice_rows == []
    assert len(bob_rows) == 2

    # Ingest state: alice's gone, bob's remains.
    assert (
        await MemberIngestState.find_one(
            MemberIngestState.workspace == ws,
            MemberIngestState.member_id == alice,
        )
        is None
    )
    assert (
        await MemberIngestState.find_one(
            MemberIngestState.workspace == ws,
            MemberIngestState.member_id == bob,
        )
        is not None
    )


# --------------------------------------------------------------------------
# 2 — purge deletes ALL FOUR stores for the member.
# --------------------------------------------------------------------------


async def test_purge_deletes_all_four_stores(mongo_db, token_store):  # noqa: ARG001
    member = "member-to-purge"
    ws = "w1"

    _seed_tokens(token_store, member)
    await _seed_connectors(ws, member)
    await _seed_state(ws, member)

    kb_clear = CapturingKbClear()
    result = await purge_mod.purge_member_data(
        ws, member, kb_clear=kb_clear, token_store=token_store
    )

    # 1. KB scope cleared (the member's own, derived internally).
    assert kb_clear.scopes_cleared == [f"user:{member}"]
    assert result["scope"] == f"user:{member}"
    assert result["kb_cleared"] is True

    # 2. Both per-user tokens deleted.
    assert token_store.load(_GMAIL_SERVICE, member) is None
    assert token_store.load(_CAL_SERVICE, member) is None
    assert result["tokens_deleted"] == 2

    # 3. Both per-user connector rows deleted.
    rows = await WorkspaceConnector.find(
        WorkspaceConnector.workspace == ws,
        WorkspaceConnector.user_id == member,
    ).to_list()
    assert rows == []
    assert result["connectors_deleted"] == 2

    # 4. Ingest state deleted.
    assert (
        await MemberIngestState.find_one(
            MemberIngestState.workspace == ws,
            MemberIngestState.member_id == member,
        )
        is None
    )
    assert result["ingest_state_deleted"] is True

    assert result["status"] == "ok"


# --------------------------------------------------------------------------
# 3 — idempotent: safe to call twice; safe when nothing exists.
# --------------------------------------------------------------------------


async def test_purge_is_idempotent_second_call_is_noop(mongo_db, token_store):  # noqa: ARG001
    member = "twice"
    ws = "w1"

    _seed_tokens(token_store, member)
    await _seed_connectors(ws, member)
    await _seed_state(ws, member)

    kb_clear = CapturingKbClear()

    first = await purge_mod.purge_member_data(
        ws, member, kb_clear=kb_clear, token_store=token_store
    )
    # Second call must not raise and must report nothing left to delete.
    second = await purge_mod.purge_member_data(
        ws, member, kb_clear=kb_clear, token_store=token_store
    )

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    # Second pass deletes zero rows / zero tokens — everything is already gone.
    assert second["tokens_deleted"] == 0
    assert second["connectors_deleted"] == 0
    assert second["ingest_state_deleted"] is False


async def test_purge_when_member_has_no_data(mongo_db, token_store):  # noqa: ARG001
    """A member who never connected anything — purge is a clean no-op."""
    kb_clear = CapturingKbClear()
    result = await purge_mod.purge_member_data(
        "w1", "ghost-member", kb_clear=kb_clear, token_store=token_store
    )
    assert result["status"] == "ok"
    assert result["tokens_deleted"] == 0
    assert result["connectors_deleted"] == 0
    assert result["ingest_state_deleted"] is False


# --------------------------------------------------------------------------
# 4 — only the member's OWN connector rows + state are removed, even when a
#     second member shares the same workspace AND a workspace-scoped row of
#     the same connector name exists (must not be swept by the user purge).
# --------------------------------------------------------------------------


async def test_purge_leaves_workspace_scoped_connector_rows(mongo_db, token_store):  # noqa: ARG001
    member = "m1"
    ws = "w1"
    # A WORKSPACE-scoped gmail connector (no user_id) must survive a per-user purge.
    await WorkspaceConnector(workspace=ws, name="gmail", scope="workspace", user_id=None).insert()
    await _seed_connectors(ws, member)

    kb_clear = CapturingKbClear()
    await purge_mod.purge_member_data(ws, member, kb_clear=kb_clear, token_store=token_store)

    # The user-scoped rows are gone; the workspace-scoped row remains.
    survivors = await WorkspaceConnector.find(WorkspaceConnector.workspace == ws).to_list()
    assert len(survivors) == 1
    assert survivors[0].scope == "workspace"
    assert survivors[0].user_id is None


# --------------------------------------------------------------------------
# 5 — emit-on-write: a MemberDataPurged event fires on a successful purge.
# --------------------------------------------------------------------------


async def test_purge_emits_member_data_purged_event(mongo_db, token_store, monkeypatch):  # noqa: ARG001
    member = "emit-me"
    ws = "w1"
    await _seed_state(ws, member)

    captured: list = []

    async def _fake_emit(event) -> None:
        captured.append(event)

    monkeypatch.setattr(purge_mod, "emit", _fake_emit)

    await purge_mod.purge_member_data(
        ws, member, kb_clear=CapturingKbClear(), token_store=token_store
    )

    assert len(captured) == 1
    evt = captured[0]
    assert evt.type == "member_ingest.purged"
    assert evt.data["workspace_id"] == ws
    assert evt.data["member_id"] == member
    assert evt.data["scope"] == f"user:{member}"


# --------------------------------------------------------------------------
# 6 — a failing store delete does not abort the others (best-effort cascade).
# --------------------------------------------------------------------------


async def test_purge_continues_when_kb_clear_fails(mongo_db, token_store):  # noqa: ARG001
    member = "partial"
    ws = "w1"
    _seed_tokens(token_store, member)
    await _seed_connectors(ws, member)
    await _seed_state(ws, member)

    async def _boom_kb_clear(scope: str) -> dict:
        raise RuntimeError("kb binary not found")

    result = await purge_mod.purge_member_data(
        ws, member, kb_clear=_boom_kb_clear, token_store=token_store
    )

    # kb clear failed, but tokens / connectors / state were still purged.
    assert result["kb_cleared"] is False
    assert result["errors"]
    assert token_store.load(_GMAIL_SERVICE, member) is None
    rows = await WorkspaceConnector.find(
        WorkspaceConnector.workspace == ws,
        WorkspaceConnector.user_id == member,
    ).to_list()
    assert rows == []
    assert (
        await MemberIngestState.find_one(
            MemberIngestState.workspace == ws,
            MemberIngestState.member_id == member,
        )
        is None
    )
    # Status reflects the partial failure.
    assert result["status"] == "error"


# --------------------------------------------------------------------------
# 7 — the disconnect trigger: connectors.service.disconnect_member purges the
#     member's data (the member-facing "disconnect my accounts" door).
# --------------------------------------------------------------------------


async def test_disconnect_member_purges_caller_data(mongo_db, token_store, monkeypatch):  # noqa: ARG001
    from pocketpaw_ee.cloud.connectors import service as connectors_service

    member = "self-disconnecter"
    ws = "w1"
    _seed_tokens(token_store, member)
    await _seed_connectors(ws, member)
    await _seed_state(ws, member)

    # disconnect_member delegates to purge_member_data with the REAL defaults
    # (no kb_clear/token_store injection point on that surface), so redirect the
    # purge module's token store + kb clear here.
    monkeypatch.setattr(
        "pocketpaw.clients.token_store.TokenStore",
        lambda: token_store,
    )

    async def _fake_kb_clear(scope: str) -> dict:
        return {"cleared": scope}

    monkeypatch.setattr(purge_mod, "_default_kb_clear", _fake_kb_clear)

    result = await connectors_service.disconnect_member(ws, member)

    assert result["status"] == "ok"
    assert result["scope"] == f"user:{member}"
    assert result["tokens_deleted"] == 2
    assert result["connectors_deleted"] == 2
    assert result["ingest_state_deleted"] is True
    # The member's stores are actually gone.
    assert token_store.load(_GMAIL_SERVICE, member) is None
    assert (
        await MemberIngestState.find_one(
            MemberIngestState.workspace == ws,
            MemberIngestState.member_id == member,
        )
        is None
    )
