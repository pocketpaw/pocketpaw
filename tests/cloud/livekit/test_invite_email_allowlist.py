# tests/cloud/livekit/test_invite_email_allowlist.py — server-side guest allow-list.
#
# Created 2026-09-30 (fix/livekit-call-security, MC-0 hole 2). Guest invites
# kept the allowed emails inside the ``display_name`` JSON. The public validate
# endpoint returned that string verbatim, so anyone holding the link saw the
# list, and the email check ran only in the browser, so accept let anyone in.
# Now the list lives on ``MeetingInvite.allowed_emails`` (legacy JSON is still
# honoured), validate returns ``requires_email`` instead of the list, and
# accept compares the guest's email server-side.

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud.livekit import invites as invite_service
from pocketpaw_ee.cloud.models.invite import MeetingInvite as _MeetingInviteDoc
from pocketpaw_ee.cloud.shared.errors import Forbidden

pytestmark = pytest.mark.usefixtures("mongo_db")

EMAILS_DN = json.dumps({"emails": ["Alice@Example.com", "bob@example.com"], "description": "Sync"})


async def test_validate_does_not_leak_the_email_allow_list(livekit_stub, create_invite) -> None:
    token = await create_invite(EMAILS_DN)

    result = await invite_service.validate_meeting_invite(token)

    assert "example.com" not in json.dumps(result).lower()
    assert result["requires_email"] is True
    assert json.loads(result["display_name"]) == {"description": "Sync"}


async def test_accept_enforces_the_email_allow_list_on_the_server(
    livekit_stub, create_invite
) -> None:
    """The finding: accept let anyone with the link in, whatever the list said."""
    token = await create_invite(EMAILS_DN)

    with pytest.raises(Forbidden):
        await invite_service.accept_meeting_invite(token, "Mallory")
    with pytest.raises(Forbidden):
        await invite_service.accept_meeting_invite(token, "Mallory", email="mallory@example.com")

    ok = await invite_service.accept_meeting_invite(token, "Alice", email="  alice@EXAMPLE.com ")
    assert ok["token"] == "guest-lk-token"


async def test_legacy_invite_with_emails_in_display_name_is_enforced(livekit_stub) -> None:
    """Invites created before the allow-list field still gate on their JSON list."""
    doc = _MeetingInviteDoc(
        workspace="ws-mine",
        group_id="g1",
        room_name="group-call-g1",
        token_hash=hashlib.sha256(b"legacy").hexdigest(),
        created_by="u1",
        display_name=EMAILS_DN,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    await doc.insert()

    validated = await invite_service.validate_meeting_invite("legacy")
    assert validated["requires_email"] is True
    assert "example.com" not in validated["display_name"]
    with pytest.raises(Forbidden):
        await invite_service.accept_meeting_invite("legacy", "Mallory")
    ok = await invite_service.accept_meeting_invite("legacy", "Bob", email="bob@example.com")
    assert ok["token"] == "guest-lk-token"


async def test_open_invite_needs_no_email(livekit_stub, create_invite) -> None:
    token = await create_invite(json.dumps({"title": "Standup", "scheduled_at": "x"}))

    validated = await invite_service.validate_meeting_invite(token)
    assert validated["requires_email"] is False
    assert json.loads(validated["display_name"])["title"] == "Standup"
    ok = await invite_service.accept_meeting_invite(token, "Guest")
    assert ok["token"] == "guest-lk-token"
