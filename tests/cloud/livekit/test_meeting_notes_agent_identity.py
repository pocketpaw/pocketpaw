"""Meeting notes are authored by a real Agent and land in that agent's soul.

T-18. Before this, ``post_meeting_notes_to_group`` wrote the notes under a
hardcoded pseudo-user (``CALL_BOT_USER_ID = "__livekit_call_bot__"``): the
poster had no avatar, no config, no row in ``/agents``, and the meeting never
reached any agent's memory — so the workspace agent sitting in the same group
could not recall the call it had supposedly attended.

What these tests pin:

* the author is the group's first attached agent, and when the group has none,
  the workspace-default ``pocketpaw`` agent (the same fallback
  ``chat.agent_service`` uses) — asserted through
  ``message_service.create_agent_message``, not the pseudo-user writer;
* a revoked or deleted agent never gets the byline. Neither
  ``agents.service.delete`` nor AW-4's soft ``_set_disabled`` detaches the
  agent from ``group.agents``, so that list can hand back a dangling or
  revoked id; authoring under one would put a revoked agent's name and avatar
  on brand-new messages;
* the notes still post when NO agent can be resolved, or when the group lookup
  itself fails — a meeting summary outranks its byline;
* the meeting is observed into the authoring agent's soul, with the instance
  materialized first (``pool.observe`` is a silent no-op for an agent with no
  live instance), and a soul-write failure never reaches the caller.

Mocks stop at the service boundary (message_service / group_service /
agent_service / the agent pool) — the notes path itself runs for real.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pocketpaw_ee.cloud._core.realtime.events import CallNotesPosted, MessageNew


def _group(*agent_ids: str, workspace_id: str = "ws1") -> SimpleNamespace:
    """Minimal stand-in for the group domain object the bridge reads."""
    return SimpleNamespace(
        id="g1",
        workspace_id=workspace_id,
        agents=[
            SimpleNamespace(agent_id=a, role="assistant", respond_mode="mention_only")
            for a in agent_ids
        ],
    )


@contextmanager
def _notes_env(
    *,
    group: SimpleNamespace | None,
    default_agent_id: str | None = None,
    group_lookup_error: Exception | None = None,
    pool: MagicMock | None = None,
    revoked: frozenset[str] = frozenset(),
):
    """Patch every collaborator ``post_meeting_notes_to_group`` reaches for.

    ``revoked`` names agent ids that are disabled or deleted — for those,
    ``agents.service.get_persona`` answers ``None``, exactly as it does for a
    soft-disabled (AW-4) or missing agent.
    """
    agent_msg = MagicMock()
    agent_msg.id = "msg_agent"
    legacy_msg = MagicMock()
    legacy_msg.id = "msg_legacy"

    get_for_dispatch = AsyncMock(
        side_effect=group_lookup_error if group_lookup_error else None,
        return_value=group,
    )
    pool = pool if pool is not None else _fake_pool()

    async def _persona(agent_id: str) -> str | None:
        return None if agent_id in revoked else f"persona of {agent_id}"

    with (
        patch(
            "pocketpaw_ee.cloud.agents.service.get_persona",
            new=AsyncMock(side_effect=_persona),
        ) as mock_persona,
        patch(
            "pocketpaw_ee.cloud.chat.group_service.get_for_dispatch",
            new=get_for_dispatch,
        ),
        patch(
            "pocketpaw_ee.cloud.chat.agent_service._get_default_workspace_agent_id",
            new=AsyncMock(return_value=default_agent_id),
        ) as mock_default,
        patch(
            "pocketpaw_ee.cloud.chat.message_service.create_agent_message",
            new=AsyncMock(return_value=agent_msg),
        ) as mock_agent_post,
        patch(
            "pocketpaw_ee.cloud.chat.message_service._create_group_message_doc",
            new=AsyncMock(return_value=legacy_msg),
        ) as mock_legacy_post,
        patch(
            "pocketpaw_ee.cloud.shared.events.event_bus.emit",
            new=AsyncMock(),
        ) as mock_bus,
        patch("pocketpaw.agents.pool.get_agent_pool", return_value=pool),
    ):
        yield SimpleNamespace(
            agent_post=mock_agent_post,
            legacy_post=mock_legacy_post,
            default_agent=mock_default,
            persona=mock_persona,
            bus=mock_bus,
            pool=pool,
        )


def _fake_pool(observe_error: Exception | None = None, get_error: Exception | None = None):
    pool = MagicMock()
    pool.get = AsyncMock(side_effect=get_error)
    pool.observe = AsyncMock(side_effect=observe_error)
    return pool


async def _post(**overrides):
    from pocketpaw_ee.cloud.livekit import service

    kwargs = {
        "group_id": "g1",
        "transcript": "Alice: ship it. Bob: agreed.",
        "summary": "The team agreed to ship.",
        "action_items": ["Ship it"],
        "participants": ["Alice", "Bob"],
        "duration_seconds": 120,
        "workspace_id": "",
    }
    kwargs.update(overrides)
    await service.post_meeting_notes_to_group(**kwargs)


# ---------------------------------------------------------------------------
# Authorship
# ---------------------------------------------------------------------------


async def test_notes_are_authored_by_the_groups_first_agent() -> None:
    """Mutation that breaks this: post via ``_create_group_message_doc`` with
    ``sender=CALL_BOT_USER_ID`` again (the pre-T-18 behavior)."""
    with _notes_env(group=_group("agent_a", "agent_b")) as env:
        await _post()

    env.agent_post.assert_awaited_once()
    assert env.agent_post.await_args.kwargs["agent_id"] == "agent_a"
    assert env.agent_post.await_args.kwargs["group_id"] == "g1"
    assert "Meeting Notes" in env.agent_post.await_args.kwargs["content"]
    assert "The team agreed to ship." in env.agent_post.await_args.kwargs["content"]
    # The pseudo-user writer must not run at all on this path.
    env.legacy_post.assert_not_awaited()
    env.default_agent.assert_not_awaited()


async def test_notes_fall_back_to_the_workspace_default_agent() -> None:
    """A group with no attached agents still gets a real byline.

    Mutation that breaks this: drop the ``_get_default_workspace_agent_id``
    fallback and return ``None`` whenever the group has no agents.
    """
    with _notes_env(group=_group(), default_agent_id="ws_default") as env:
        await _post()

    env.default_agent.assert_awaited_once_with("ws1")
    env.agent_post.assert_awaited_once()
    assert env.agent_post.await_args.kwargs["agent_id"] == "ws_default"
    env.legacy_post.assert_not_awaited()


async def test_workspace_id_argument_is_used_when_the_group_has_no_agents() -> None:
    """The caller's ``workspace_id`` wins over the group's — it is the meeting's
    own workspace and is always passed on the real call path."""
    with _notes_env(group=_group(workspace_id="ws_from_group"), default_agent_id="d") as env:
        await _post(workspace_id="ws_from_arg")

    env.default_agent.assert_awaited_once_with("ws_from_arg")


# ---------------------------------------------------------------------------
# Revocation — a dead or revoked agent must not author
# ---------------------------------------------------------------------------


async def test_a_revoked_first_agent_does_not_get_the_byline() -> None:
    """AW-4's soft disable does not detach the agent from ``group.agents``, so
    the notes path has to check liveness itself — otherwise a revoked agent's
    name and avatar appear on brand-new messages.

    Mutation that breaks this: return ``group.agents[0].agent_id`` without the
    ``_agent_can_author`` check.
    """
    with _notes_env(
        group=_group("agent_a"),
        default_agent_id="ws_default",
        revoked=frozenset({"agent_a"}),
    ) as env:
        await _post()

    env.agent_post.assert_awaited_once()
    assert env.agent_post.await_args.kwargs["agent_id"] == "ws_default"


async def test_a_deleted_group_agent_does_not_get_the_byline() -> None:
    """``agents.service.delete`` leaves the id in ``group.agents`` too, so the
    list can hand back an id no Agent row answers to. ``get_persona`` returns
    ``None`` for both cases — this pins the deleted one explicitly."""
    with _notes_env(
        group=_group("dangling_id"),
        default_agent_id="ws_default",
        revoked=frozenset({"dangling_id"}),
    ) as env:
        await _post()

    assert env.agent_post.await_args.kwargs["agent_id"] == "ws_default"
    env.persona.assert_any_await("dangling_id")


async def test_a_revoked_workspace_default_falls_back_to_the_pseudo_user() -> None:
    """The fallback candidate gets the same check as the first one.

    Mutation that breaks this: return ``_get_default_workspace_agent_id``'s
    answer unchecked.
    """
    with _notes_env(
        group=_group(),
        default_agent_id="ws_default",
        revoked=frozenset({"ws_default"}),
    ) as env:
        await _post()

    env.agent_post.assert_not_awaited()
    env.legacy_post.assert_awaited_once()
    env.pool.observe.assert_not_awaited()


async def test_a_failing_liveness_check_does_not_hand_out_the_byline() -> None:
    """The check fails closed: an unreadable agent row is treated as unusable,
    not waved through."""
    with _notes_env(group=_group("agent_a"), default_agent_id=None) as env:
        env.persona.side_effect = RuntimeError("mongo is having a day")
        await _post()

    env.agent_post.assert_not_awaited()
    env.legacy_post.assert_awaited_once()


# ---------------------------------------------------------------------------
# Clean degradation — the summary outranks the byline
# ---------------------------------------------------------------------------


async def test_notes_still_post_when_no_agent_exists_anywhere() -> None:
    """No group agents and no workspace default: the notes post under the legacy
    pseudo-user rather than being lost.

    Mutation that breaks this: raise (or return early) instead of falling back
    when ``_resolve_notes_agent_id`` answers ``None``.
    """
    with _notes_env(group=_group(), default_agent_id=None) as env:
        await _post()

    env.agent_post.assert_not_awaited()
    env.legacy_post.assert_awaited_once()
    from pocketpaw_ee.cloud.livekit.service import CALL_BOT_USER_ID

    assert env.legacy_post.await_args.kwargs["sender"] == CALL_BOT_USER_ID
    assert env.legacy_post.await_args.kwargs["sender_type"] == "user"
    # This path keeps the legacy bus emit: nothing else bumps the group stats.
    env.bus.assert_awaited_once()
    assert env.bus.await_args.args[0] == "message.sent"
    # Nothing to observe into — no agent owns this meeting.
    env.pool.observe.assert_not_awaited()


async def test_notes_still_post_when_the_group_lookup_blows_up() -> None:
    """Mutation that breaks this: drop the try/except around
    ``group_service.get_for_dispatch``."""
    with _notes_env(
        group=None,
        default_agent_id="ws_default",
        group_lookup_error=RuntimeError("mongo is having a day"),
    ) as env:
        await _post(workspace_id="ws1")

    # Fell through to the workspace default rather than dying.
    env.agent_post.assert_awaited_once()
    assert env.agent_post.await_args.kwargs["agent_id"] == "ws_default"


async def test_a_missing_group_does_not_stop_the_notes() -> None:
    with _notes_env(group=None, default_agent_id=None) as env:
        await _post()

    env.legacy_post.assert_awaited_once()


# ---------------------------------------------------------------------------
# The agent path does not double-count group stats
# ---------------------------------------------------------------------------


async def test_agent_path_does_not_emit_the_legacy_message_sent_event() -> None:
    """``create_agent_message`` already bumps ``message_count``; emitting the
    legacy event too would make ``_on_message_sent`` bump it a second time —
    and would hand an agent-authored message back to the agent bridge.

    Mutation that breaks this: re-add the ``event_bus.emit("message.sent", …)``
    call on the agent branch.
    """
    with _notes_env(group=_group("agent_a")) as env:
        await _post()

    env.bus.assert_not_awaited()


# ---------------------------------------------------------------------------
# Realtime payload
# ---------------------------------------------------------------------------


async def test_message_new_payload_carries_the_agent_identity(recording_bus) -> None:
    """Mutation that breaks this: leave ``senderType`` at ``"user"`` /
    ``sender`` at ``CALL_BOT_USER_ID`` in the ``MessageNew`` payload."""
    with _notes_env(group=_group("agent_a")):
        await _post()

    new = [e for e in recording_bus.events if isinstance(e, MessageNew)]
    assert len(new) == 1
    data = new[0].data
    assert data["senderType"] == "agent"
    assert data["agent"] == "agent_a"
    assert data["sender"] is None
    assert data["_id"] == "msg_agent"
    assert data["message_id"] == "msg_agent"


async def test_message_new_payload_keeps_the_pseudo_user_shape_when_unattributed(
    recording_bus,
) -> None:
    with _notes_env(group=_group(), default_agent_id=None):
        await _post()

    from pocketpaw_ee.cloud.livekit.service import CALL_BOT_USER_ID

    data = [e for e in recording_bus.events if isinstance(e, MessageNew)][0].data
    assert data["senderType"] == "user"
    assert data["sender"] == CALL_BOT_USER_ID
    assert data["agent"] is None


async def test_call_notes_posted_carries_a_string_message_id(recording_bus) -> None:
    """``create_agent_message`` returns a Beanie doc whose ``id`` is a
    ``PydanticObjectId``; the event payload must carry ``str(id)``.

    Mutation that breaks this: emit ``msg.id`` raw.
    """
    from bson import ObjectId

    oid = ObjectId()
    msg = MagicMock()
    msg.id = oid

    with _notes_env(group=_group("agent_a")) as env:
        env.agent_post.return_value = msg
        await _post()

    posted = [e for e in recording_bus.events if isinstance(e, CallNotesPosted)]
    assert len(posted) == 1
    assert posted[0].data["message_id"] == str(oid)
    assert isinstance(posted[0].data["message_id"], str)


# ---------------------------------------------------------------------------
# Soul
# ---------------------------------------------------------------------------


async def test_meeting_is_observed_into_the_authoring_agents_soul() -> None:
    """Mutation that breaks this: delete the ``pool.observe`` call."""
    with _notes_env(group=_group("agent_a")) as env:
        await _post()

    env.pool.observe.assert_awaited_once()
    agent_id, digest, summary = env.pool.observe.await_args.args
    assert agent_id == "agent_a"
    assert "Alice: ship it." in digest
    assert summary == "The team agreed to ship."


async def test_observe_materializes_the_agent_instance_first() -> None:
    """``pool.observe`` looks the instance up in the pool cache and silently
    does nothing when it is absent — so the soul write only lands if the
    instance (and its SoulManager) is built first.

    Mutation that breaks this: drop the ``pool.get(agent_id)`` call.
    """
    with _notes_env(group=_group("agent_a")) as env:
        await _post()

    env.pool.get.assert_awaited_once_with("agent_a")


async def test_a_failing_soul_write_never_breaks_the_notes() -> None:
    """Mutation that breaks this: remove the try/except around the
    ``pool.get`` / ``pool.observe`` pair."""
    pool = _fake_pool(observe_error=RuntimeError("soul file is corrupt"))
    with _notes_env(group=_group("agent_a"), pool=pool) as env:
        await _post()  # must not raise

    env.agent_post.assert_awaited_once()


async def test_an_agent_revoked_mid_flight_costs_the_soul_write_not_the_notes() -> None:
    """An agent that passes the liveness check and is revoked before
    ``pool.get`` runs raises ``AgentDisabled`` inside the observe helper. That
    race window costs the soul write only — the notes are already posted."""
    from pocketpaw.agents.errors import AgentDisabled

    pool = _fake_pool(get_error=AgentDisabled("agent_a"))
    with _notes_env(group=_group("agent_a"), pool=pool) as env:
        await _post()

    env.agent_post.assert_awaited_once()
    pool.observe.assert_not_awaited()


async def test_a_post_failure_still_raises() -> None:
    """The notes path's existing contract: a failed write propagates, so the
    agent subprocess reaper logs it. Only the soul write is best-effort."""
    with _notes_env(group=_group("agent_a")) as env:
        env.agent_post.side_effect = RuntimeError("write failed")
        with pytest.raises(RuntimeError):
            await _post()

    env.pool.observe.assert_not_awaited()


# ---------------------------------------------------------------------------
# Digest
# ---------------------------------------------------------------------------


def test_soul_digest_truncates_a_long_transcript() -> None:
    """A raw transcript runs 14K+ chars; a soul memory wants a digest.

    Mutation that breaks this: pass the transcript through untruncated.
    """
    from pocketpaw_ee.cloud.livekit.service import (
        _SOUL_DIGEST_TRANSCRIPT_CHARS,
        _meeting_soul_digest,
    )

    digest = _meeting_soul_digest("x" * 20_000)
    assert len(digest) < 20_000
    assert digest.count("x") == _SOUL_DIGEST_TRANSCRIPT_CHARS
    assert "truncated" in digest


def test_soul_digest_frames_the_transcript() -> None:
    """Without the framing sentence the soul stores a wall of dialogue with no
    clue where it came from."""
    from pocketpaw_ee.cloud.livekit.service import _meeting_soul_digest

    assert _meeting_soul_digest("Alice: hi").startswith("We had a group call.")
    assert "No transcript" in _meeting_soul_digest("   ")
