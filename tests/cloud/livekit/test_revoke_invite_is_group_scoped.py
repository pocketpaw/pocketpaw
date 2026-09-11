"""Revoking a meeting invite must be confined to the group the caller proved.

``DELETE /api/v1/livekit/rooms/{group_id}/invites/{invite_id}`` checks that the
caller is a member of the group named in the PATH, then passed only
``invite_id`` to ``revoke_meeting_invite``, which fetched by primary key with no
group or workspace comparison. The path ``group_id`` was decorative: satisfy it
with a group you created yourself, then pass any invite id on the deployment.

SWEEPABLE, NOT MERELY TARGETED

Three distinguishable responses — 404 for an unknown id, 200 carrying the
victim's ``group_id`` for a valid foreign one, and an uncaught ``InvalidId`` for
a malformed one. An attacker mints an invite in their own workspace and reads
the id back, which discloses the 5-byte per-process random and the counter
stride shared by every ObjectId that backend mints, collapsing the search to a
timestamp window by counter. Every hit sets ``revoked=True`` permanently, so the
sweep destroys what it finds.

THIS IS A REPEAT

``router.py``'s own module docstring records ``/rooms/{group_id}/leave`` having
the identical defect, fixed 2026-08-11 with a regression test. The pattern was
fixed on one route and left standing on its sibling. ``create_meeting_invite``
cross-checks its room against the group at ``invites.py:55``; this is that check
on the other side.

Mutations that must fail these tests: dropping the group comparison, comparing
the doc's group to itself, and raising a DIFFERENT error for a foreign invite
than for an unknown one (which restores the oracle even while blocking the
write).
"""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.livekit import invites as invite_service
from pocketpaw_ee.cloud.models.invite import MeetingInvite as _MeetingInviteDoc
from pocketpaw_ee.cloud.shared.errors import NotFound


async def _invite(*, group_id: str, workspace: str = "ws-victim") -> _MeetingInviteDoc:
    import hashlib
    from datetime import UTC, datetime, timedelta

    doc = _MeetingInviteDoc(
        group_id=group_id,
        workspace=workspace,
        room_name=f"room-{group_id}",
        token_hash=hashlib.sha256(group_id.encode()).hexdigest(),
        display_name="Standup",
        created_by="u-victim",
        max_uses=10,
        use_count=0,
        guest_identities=[],
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        revoked=False,
    )
    await doc.insert()
    return doc


async def test_an_invite_in_another_group_cannot_be_revoked(mongo_db):  # noqa: ARG001
    """The finding. The path group_id was decorative."""
    victim = await _invite(group_id="g-victim")

    with pytest.raises(NotFound):
        await invite_service.revoke_meeting_invite(str(victim.id), "u-attacker", "g-attackers-own")

    after = await _MeetingInviteDoc.get(victim.id)
    assert after.revoked is False, "another group's invite was revoked"


async def test_a_foreign_invite_is_indistinguishable_from_a_missing_one(mongo_db):  # noqa: ARG001
    """Blocking the write is not enough — the response was the oracle.

    A different error class for "exists but is not yours" would still let a
    sweep enumerate every invite id on the deployment, just without destroying
    them. Both paths must raise the same thing.
    """
    victim = await _invite(group_id="g-victim")

    with pytest.raises(NotFound) as foreign:
        await invite_service.revoke_meeting_invite(str(victim.id), "u-attacker", "g-mine")

    missing = "0" * 24
    with pytest.raises(NotFound) as absent:
        await invite_service.revoke_meeting_invite(missing, "u-attacker", "g-mine")

    assert type(foreign.value) is type(absent.value)


async def test_the_owning_group_can_still_revoke(mongo_db):  # noqa: ARG001
    """The scoping must not break the feature it guards."""
    mine = await _invite(group_id="g-mine", workspace="ws-mine")

    result = await invite_service.revoke_meeting_invite(str(mine.id), "u-owner", "g-mine")

    assert result["revoked"] is True
    after = await _MeetingInviteDoc.get(mine.id)
    assert after.revoked is True


def test_the_route_passes_the_group_it_checked_membership_of():
    """The guard and the sink must be given the same id.

    Asserted against the parsed route: the defect was never that the check was
    missing from the route, it was that the checked id never reached the sink.
    """
    import ast
    import inspect
    import sys

    import pocketpaw_ee.cloud.livekit.router  # noqa: F401

    module = sys.modules["pocketpaw_ee.cloud.livekit.router"]
    tree = ast.parse(inspect.getsource(module))
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "revoke_invite"
    )

    guarded = [
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_require_domain_group_member"
    ]
    assert guarded, "the route no longer checks group membership at all"

    call = next(
        node
        for node in ast.walk(handler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "revoke_meeting_invite"
    )
    passed = [a.id for a in call.args if isinstance(a, ast.Name)]
    assert "group_id" in passed, (
        "the route checks membership of group_id and does not pass it to the "
        "sink, so the path parameter is decorative again"
    )
