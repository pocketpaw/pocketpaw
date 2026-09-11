"""``POST /api/v1/sessions`` must not reach another tenant's rows.

Two defects in one function, and they chain.

T-3 — the upsert branch looked its row up by ``sessionId`` alone and compared
neither workspace nor owner before patching it, saving it, emitting
``SessionUpdated`` addressed to the VICTIM's owner id, and returning the row.
The response carries the victim's ``workspace``, ``owner``, ``pocket``,
``group`` and ``agent``.

T-4 — ``create`` stamped ``group=body.group_id`` and ``pocket=body.pocket_id``
straight from the request. The row's own ``workspace`` is the caller's, so it
looks correctly tenanted; but ``get_history`` builds its Message filter from
those fields with no workspace term, so a session naming a foreign group
returned that group's entire transcript to its legitimate owner.

They chain because T-3's response is the id-disclosure primitive T-4 needs.

WHY THE FIX IS AT THE WRITE

``Message.workspace_id`` exists but is ``None`` on every group message written
through REST or WebSocket (``_create_group_message_doc`` never sets it), so a
workspace filter on the READ would return nothing and look correct. The write
is also one place; the read is reached by several.

The check T-3 needs was already in this file — ``link_pocket`` does
``if doc is None or doc.workspace != workspace_id: return`` 375 lines below.

Mutations that must fail these tests: dropping either check, comparing the
row's workspace to itself, and raising Forbidden instead of NotFound.
"""

from __future__ import annotations

import uuid

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.models.group import Group as _GroupDoc
from pocketpaw_ee.cloud.models.session import Session as _SessionDoc
from pocketpaw_ee.cloud.sessions import service as sessions_service
from pocketpaw_ee.cloud.sessions.dto import CreateSessionRequest
from pocketpaw_ee.cloud.shared.errors import NotFound


def _ctx(user_id: str = "u-attacker") -> RequestContext:
    from datetime import UTC, datetime

    return RequestContext(
        user_id=user_id,
        workspace_id="ws-attacker",
        request_id=uuid.uuid4().hex,
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


async def _victim_session(session_id: str) -> _SessionDoc:
    doc = _SessionDoc(
        sessionId=session_id,
        context_type="session",
        workspace="ws-victim",
        owner="u-victim",
        title="the victim's chat",
    )
    await doc.insert()
    return doc


async def _victim_group() -> _GroupDoc:
    doc = _GroupDoc(
        workspace="ws-victim",
        name="General",
        slug="general-victim",
        description="",
        icon="",
        color="",
        type="public",
        members=["u-victim"],
        member_roles={"u-victim": "admin"},
        agents=[],
        pinned_messages=[],
        owner="u-victim",
        archived=False,
    )
    await doc.insert()
    return doc


class TestTheUpsertBranch:
    async def test_a_foreign_session_is_not_patched_or_returned(self, mongo_db):  # noqa: ARG002
        """T-3. The write, the event to the victim, and the id disclosure."""
        victim = await _victim_session(f"cloud:session:{uuid.uuid4().hex[:8]}:a1")

        with pytest.raises(NotFound):
            await sessions_service.create(
                _ctx(),
                "ws-attacker",
                CreateSessionRequest(session_id=victim.sessionId, title="hijacked"),
            )

        after = await _SessionDoc.get(victim.id)
        assert after.title == "the victim's chat", "another tenant's session was patched"
        assert after.workspace == "ws-victim"

    async def test_a_residual_existence_oracle_remains_and_is_bounded(self, mongo_db):  # noqa: ARG002
        """Pinning a known, accepted residual rather than claiming it is closed.

        A foreign ``session_id`` raises NotFound; an unused one creates a row
        and returns it. So the two outcomes DIFFER, and a caller can still
        learn whether a given session id exists somewhere on the deployment.

        It is not closable by creating instead: ``Session.sessionId`` carries a
        unique index (``models/session.py:38``), so the insert would fail
        anyway — the oracle is a property of the uniqueness constraint, not of
        this branch.

        What the fix removes is everything the oracle used to come with: the
        patch, the save, the SessionUpdated event addressed to the victim, and
        a response body carrying the victim's workspace, owner, pocket, group
        and agent. One bit of existence is what is left.

        If that bit ever needs closing, the move is a per-workspace namespace
        on ``sessionId`` rather than a different error here.
        """
        victim = await _victim_session(f"cloud:session:{uuid.uuid4().hex[:8]}:a1")

        with pytest.raises(NotFound):
            await sessions_service.create(
                _ctx(), "ws-attacker", CreateSessionRequest(session_id=victim.sessionId)
            )

        unused = await sessions_service.create(
            _ctx(), "ws-attacker", CreateSessionRequest(session_id="not-taken-anywhere")
        )
        assert unused is not None

        # The victim's row is untouched by either call — that is the part that
        # matters, and the part that was broken.
        after = await _SessionDoc.get(victim.id)
        assert after.title == "the victim's chat"
        assert after.workspace == "ws-victim"

    async def test_the_owners_own_session_still_upserts(self, mongo_db):  # noqa: ARG002
        """The scoping must not break the feature it guards."""
        key = f"cloud:session:{uuid.uuid4().hex[:8]}:a1"
        mine = _SessionDoc(
            sessionId=key,
            context_type="session",
            workspace="ws-attacker",
            owner="u-attacker",
            title="mine",
        )
        await mine.insert()

        result = await sessions_service.create(
            _ctx(), "ws-attacker", CreateSessionRequest(session_id=key, title="renamed")
        )

        assert result is not None
        after = await _SessionDoc.get(mine.id)
        assert after.title == "renamed"


class TestForeignScopeIds:
    async def test_a_session_cannot_name_another_workspaces_group(self, mongo_db):  # noqa: ARG002
        """T-4. This is what made get_history return a foreign transcript."""
        group = await _victim_group()

        with pytest.raises(NotFound):
            await sessions_service.create(
                _ctx(), "ws-attacker", CreateSessionRequest(group_id=str(group.id))
            )

        rows = await _SessionDoc.find({"group": str(group.id)}).to_list()
        assert rows == [], "a session was stamped with a foreign group id"

    async def test_a_group_in_the_callers_workspace_is_accepted(self, mongo_db):  # noqa: ARG002
        mine = _GroupDoc(
            workspace="ws-attacker",
            name="General",
            slug="general-mine",
            description="",
            icon="",
            color="",
            type="public",
            members=["u-attacker"],
            member_roles={"u-attacker": "admin"},
            agents=[],
            pinned_messages=[],
            owner="u-attacker",
            archived=False,
        )
        await mine.insert()

        result = await sessions_service.create(
            _ctx(), "ws-attacker", CreateSessionRequest(group_id=str(mine.id))
        )
        assert result is not None

    async def test_a_session_with_no_scope_ids_is_unaffected(self, mongo_db):  # noqa: ARG002
        """The common case — a plain chat session names neither."""
        result = await sessions_service.create(_ctx(), "ws-attacker", CreateSessionRequest())
        assert result is not None
