"""Composio identity-verification tests (Task 4a).

Two layers:
    * ``probe_identity_sync`` unit tests — registry lookup, envelope
      extraction, field-path walking. Pure functions; no DB needed.
    * ``record_connection`` / ``confirm_identity_change`` integration
      tests — exercise the tripwire against an in-memory mongomock DB
      via the shared ``mongo_db`` fixture. Asserts on the recording
      bus to verify event emission.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.realtime.events import (
    ComposioConnectionMismatch,
    ComposioConnectionVerified,
)
from pocketpaw_ee.cloud.composio import identity
from pocketpaw_ee.cloud.composio import service as composio_service
from pocketpaw_ee.cloud.composio.service import ConnectionRecord


def _ctx(*, user: str = "alice", workspace: str = "ws_acme") -> RequestContext:
    return RequestContext(
        user_id=user,
        workspace_id=workspace,
        request_id="r1",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# probe_identity_sync — pure-function layer
# ---------------------------------------------------------------------------


def test_probe_returns_none_for_unknown_toolkit() -> None:
    """A toolkit with no entry in IDENTITY_PROBES short-circuits to None
    without calling the client. Caller treats this as 'verification
    unavailable' rather than blocking."""
    client = MagicMock()
    result = identity.probe_identity_sync(client, user_id="u", toolkit="madeupkit")
    assert result is None
    client.tools.execute.assert_not_called()


def test_probe_returns_value_from_flat_dict_envelope() -> None:
    """``tools.execute`` returns ``{"data": {"login": "x"}, "successful": True}``."""
    client = MagicMock()
    client.tools.execute.return_value = {
        "data": {"login": "octocat"},
        "successful": True,
    }
    out = identity.probe_identity_sync(client, user_id="u", toolkit="github")
    assert out == "octocat"


def test_probe_walks_nested_field_path() -> None:
    """``googledrive``'s probe uses dot-path ``user.emailAddress``."""
    client = MagicMock()
    client.tools.execute.return_value = {
        "data": {"user": {"emailAddress": "me@example.com", "displayName": "Me"}},
    }
    out = identity.probe_identity_sync(client, user_id="u", toolkit="googledrive")
    assert out == "me@example.com"


def test_probe_returns_none_on_client_exception() -> None:
    """A 5xx from Composio must degrade to None, not raise. The
    chat-side wrapper then surfaces 'verification unavailable'."""
    client = MagicMock()
    client.tools.execute.side_effect = RuntimeError("upstream 503")
    out = identity.probe_identity_sync(client, user_id="u", toolkit="github")
    assert out is None


def test_probe_returns_none_when_field_path_missing() -> None:
    """Response shape doesn't match the registered field_path → None."""
    client = MagicMock()
    client.tools.execute.return_value = {"data": {"unexpected": "shape"}}
    out = identity.probe_identity_sync(client, user_id="u", toolkit="github")
    assert out is None


def test_probe_extracts_from_pydantic_like_envelope() -> None:
    """SDK occasionally returns pydantic models — extract via attrs/model_dump."""

    class FakeResponse:
        data = {"login": "octocat"}

    client = MagicMock()
    client.tools.execute.return_value = FakeResponse()
    out = identity.probe_identity_sync(client, user_id="u", toolkit="github")
    assert out == "octocat"


def test_probe_normalizes_toolkit_case() -> None:
    """Toolkit slug lookup is case-insensitive."""
    client = MagicMock()
    client.tools.execute.return_value = {"data": {"login": "x"}}
    assert identity.probe_identity_sync(client, user_id="u", toolkit="GitHub") == "x"
    assert identity.probe_identity_sync(client, user_id="u", toolkit="GITHUB") == "x"


# ---------------------------------------------------------------------------
# record_connection — DB + tripwire integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_connection_first_time_inserts_and_emits_verified(
    mongo_db: Any, recording_bus: Any
) -> None:
    """First call inserts the doc with the external_identity and
    emits ``ComposioConnectionVerified`` (with first_time=True)."""
    rec = await composio_service.record_connection(
        _ctx(),
        toolkit="github",
        external_identity="octocat",
    )
    assert isinstance(rec, ConnectionRecord)
    assert rec.status == "verified"
    assert rec.external_identity == "octocat"
    assert rec.previous_identity is None

    from pocketpaw_ee.cloud.models.composio_connection import ComposioConnection

    docs = await ComposioConnection.find().to_list()
    assert len(docs) == 1
    assert docs[0].external_identity == "octocat"
    assert docs[0].mismatch_count == 0

    events = [e for e in recording_bus.events if isinstance(e, ComposioConnectionVerified)]
    assert len(events) == 1
    assert events[0].data["first_time"] is True
    assert events[0].data["toolkit"] == "github"


@pytest.mark.asyncio
async def test_record_connection_match_reverifies_quietly(
    mongo_db: Any, recording_bus: Any
) -> None:
    """Re-probing with the same identity bumps last_verified_at and
    emits verified-not-first-time, no mismatch."""
    ctx = _ctx()
    await composio_service.record_connection(ctx, toolkit="github", external_identity="octocat")
    recording_bus.events.clear()

    rec = await composio_service.record_connection(
        ctx, toolkit="github", external_identity="octocat"
    )
    assert rec.status == "verified"
    assert rec.previous_identity is None  # matched, no diff to surface

    mismatch_events = [e for e in recording_bus.events if isinstance(e, ComposioConnectionMismatch)]
    assert mismatch_events == []
    verified = [e for e in recording_bus.events if isinstance(e, ComposioConnectionVerified)]
    assert len(verified) == 1
    assert verified[0].data["first_time"] is False


@pytest.mark.asyncio
async def test_record_connection_mismatch_does_not_overwrite(
    mongo_db: Any, recording_bus: Any
) -> None:
    """Tripwire: probe returns a different identity. The stored
    external_identity is NOT overwritten — the user must confirm
    via confirm_identity_change."""
    ctx = _ctx()
    await composio_service.record_connection(ctx, toolkit="github", external_identity="octocat")
    recording_bus.events.clear()

    rec = await composio_service.record_connection(
        ctx, toolkit="github", external_identity="octocat-alt"
    )
    assert rec.status == "mismatch"
    assert rec.external_identity == "octocat-alt"  # what was probed
    assert rec.previous_identity == "octocat"  # what was stored

    from pocketpaw_ee.cloud.models.composio_connection import ComposioConnection

    doc = await ComposioConnection.find_one(
        ComposioConnection.workspace == ctx.workspace_id,
        ComposioConnection.paw_user_id == ctx.user_id,
        ComposioConnection.toolkit == "github",
    )
    assert doc is not None
    assert doc.external_identity == "octocat"  # NOT overwritten
    assert doc.mismatch_count == 1
    assert doc.last_mismatch_identity == "octocat-alt"

    mm = [e for e in recording_bus.events if isinstance(e, ComposioConnectionMismatch)]
    assert len(mm) == 1
    assert mm[0].data["stored_identity"] == "octocat"
    assert mm[0].data["probed_identity"] == "octocat-alt"


@pytest.mark.asyncio
async def test_record_connection_none_identity_is_unverified(
    mongo_db: Any, recording_bus: Any
) -> None:
    """``external_identity=None`` (probe failed / no registry entry)
    inserts an unverified record on first call; subsequent calls
    leave the stored identity intact and bump last_verified_at."""
    ctx = _ctx()
    rec = await composio_service.record_connection(ctx, toolkit="exotic", external_identity=None)
    assert rec.status == "unverified"
    assert rec.external_identity is None

    # Now store a real identity, then probe again with None — the
    # stored identity must survive.
    await composio_service.record_connection(ctx, toolkit="github", external_identity="x")
    rec2 = await composio_service.record_connection(ctx, toolkit="github", external_identity=None)
    assert rec2.status == "unverified"
    assert rec2.external_identity == "x"  # echoes the stored value


@pytest.mark.asyncio
async def test_record_connection_partitions_by_workspace_and_user(
    mongo_db: Any, recording_bus: Any
) -> None:
    """Two users in different workspaces can each have their own
    connected GitHub without collision. The (workspace, user, toolkit)
    tuple is the natural unique key."""
    await composio_service.record_connection(
        _ctx(user="alice", workspace="ws_a"),
        toolkit="github",
        external_identity="alice-gh",
    )
    await composio_service.record_connection(
        _ctx(user="bob", workspace="ws_a"),
        toolkit="github",
        external_identity="bob-gh",
    )
    await composio_service.record_connection(
        _ctx(user="alice", workspace="ws_b"),
        toolkit="github",
        external_identity="alice-gh-altworkspace",
    )

    from pocketpaw_ee.cloud.models.composio_connection import ComposioConnection

    docs = await ComposioConnection.find().to_list()
    assert len(docs) == 3
    by_key = {(d.workspace, d.paw_user_id): d.external_identity for d in docs}
    assert by_key[("ws_a", "alice")] == "alice-gh"
    assert by_key[("ws_a", "bob")] == "bob-gh"
    assert by_key[("ws_b", "alice")] == "alice-gh-altworkspace"


@pytest.mark.asyncio
async def test_confirm_identity_change_overwrites_and_clears_mismatch(
    mongo_db: Any, recording_bus: Any
) -> None:
    """After a mismatch, calling confirm_identity_change with the
    new identity overwrites the stored value and clears the mismatch
    flags so subsequent probes re-verify cleanly."""
    ctx = _ctx()
    await composio_service.record_connection(ctx, toolkit="github", external_identity="octocat")
    await composio_service.record_connection(ctx, toolkit="github", external_identity="octocat-alt")
    recording_bus.events.clear()

    confirmed = await composio_service.confirm_identity_change(
        ctx, toolkit="github", external_identity="octocat-alt"
    )
    assert confirmed.status == "verified"
    assert confirmed.previous_identity == "octocat"

    from pocketpaw_ee.cloud.models.composio_connection import ComposioConnection

    doc = await ComposioConnection.find_one(
        ComposioConnection.workspace == ctx.workspace_id,
        ComposioConnection.paw_user_id == ctx.user_id,
        ComposioConnection.toolkit == "github",
    )
    assert doc is not None
    assert doc.external_identity == "octocat-alt"  # overwritten
    assert doc.last_mismatch_identity is None
    assert doc.last_mismatch_at is None

    # Re-probing now treats octocat-alt as the canonical identity.
    recording_bus.events.clear()
    rec = await composio_service.record_connection(
        ctx, toolkit="github", external_identity="octocat-alt"
    )
    assert rec.status == "verified"
    mm = [e for e in recording_bus.events if isinstance(e, ComposioConnectionMismatch)]
    assert mm == []


@pytest.mark.asyncio
async def test_confirm_identity_change_raises_without_prior_record(mongo_db: Any) -> None:
    """Can't confirm a change for a connection we've never seen."""
    from pocketpaw_ee.cloud._core.errors import ValidationError

    with pytest.raises(ValidationError):
        await composio_service.confirm_identity_change(
            _ctx(), toolkit="github", external_identity="x"
        )
