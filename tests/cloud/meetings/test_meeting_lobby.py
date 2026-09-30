# tests/cloud/meetings/test_meeting_lobby.py — guests ask to join; people in the
# call admit or deny them.
#
# Created 2026-10-01 (feat/meetings-lobby, MC-3). A guest with a meeting link
# knocks (public, rate-limited per IP and per code), gets a knock id plus a
# per-knock secret, and polls with that secret. A member of the meeting room who
# is in the call admits or denies; an admitted guest's poll carries a LiveKit
# token for the meeting's room only, under a fixed ``guest-<hex>`` identity, and
# only while a human is in the call (a guest token must never start a room).
# ``access="open"`` admits on its own once the call is running. Waiting knocks
# expire after 10 minutes. ``PATCH /meetings/{id}`` lets the host flip access.
# LiveKit is the stateful mock from conftest.py.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import jwt
import pytest
from pocketpaw_ee.cloud._core import rate_limit
from pocketpaw_ee.cloud.chat import group_service
from pocketpaw_ee.cloud.meetings import lobby_service
from pocketpaw_ee.cloud.meetings import service as meetings_service
from pocketpaw_ee.cloud.meetings.dto import CreateMeetingRequest
from pocketpaw_ee.cloud.models.meeting import Meeting, MeetingKnock
from pocketpaw_ee.cloud.models.user import User
from pocketpaw_ee.cloud.models.workspace import Workspace

KNOCK_KEYS = {"knock_id", "secret", "status"}
STATUS_KEYS = {"status"}
ADMITTED_KEYS = {"status", "token", "room_name", "identity", "livekit_url"}
LIST_KEYS = {"knock_id", "name", "email", "created_at"}
DECISION_KEYS = {"knock_id", "status"}
GUEST_IP = {"x-forwarded-for": "198.51.100.4"}


@pytest.fixture(autouse=True)
def _no_scheduler():
    import pocketpaw_ee.cloud.meetings.providers.livekit  # noqa: F401 — registers provider

    with patch("pocketpaw_ee.cloud.meetings.scheduling.reminders.schedule_meeting_jobs"):
        yield


@pytest.fixture(autouse=True)
def _fresh_limiters():
    limiters = (
        rate_limit._meeting_knock_ip_limiter,
        rate_limit._meeting_knock_code_limiter,
        rate_limit._meeting_knock_poll_limiter,
    )
    for lim in limiters:
        lim._buckets.clear()
    yield
    for lim in limiters:
        lim._buckets.clear()


async def _user(name: str) -> str:
    user = User(email=f"{name.split()[0].lower()}@x.dev", hashed_password="x", full_name=name)
    await user.insert()
    return str(user.id)


async def _workspace(slug: str = "acme") -> str:
    ws = Workspace(name="Acme", slug=slug, owner="owner", plan="enterprise")
    await ws.insert()
    return str(ws.id)


async def _setup(lk, *, live: bool = True, **body):
    """A for-later meeting whose host is (optionally) in the call."""
    ws_id = await _workspace()
    host = await _user("Hana Host")
    out = await meetings_service.create_meeting(
        ws_id, host, CreateMeetingRequest(source="livekit", title="Planning", **body)
    )
    if live:
        _go_live(lk, out.room_group_id, host)
    return SimpleNamespace(ws=ws_id, host=host, m=out, room=out.room_group_id)


def _go_live(lk, room_id: str, *identities: str) -> None:
    name = f"group-call-{room_id}"
    if not any(r.name == name for r in lk.rooms):
        lk.rooms.append(SimpleNamespace(name=name))
    for ident in identities:
        lk.participants.append(SimpleNamespace(identity=ident, name=ident, joined_at=0, kind=0))


async def _knock(client, code: str, name: str = "Gus Guest", headers=None, **extra):
    client.log_out()
    return await client.post(
        f"/api/v1/meetings/by-code/{code}/knock",
        json={"name": name, **extra},
        headers=headers or GUEST_IP,
    )


async def _poll(client, code: str, knock: dict, secret: str | None = None):
    client.log_out()
    headers = dict(GUEST_IP)
    if secret is not False:
        headers["x-knock-secret"] = knock["secret"] if secret is None else secret
    return await client.get(
        f"/api/v1/meetings/by-code/{code}/knocks/{knock['knock_id']}", headers=headers
    )


async def _decide(client, s, knock_id: str, action: str, as_user: str | None = None):
    client.act_as(as_user or s.host, s.ws)
    return await client.post(f"/api/v1/meetings/{s.m.id}/knocks/{knock_id}/{action}")


def _events(bus, etype: str) -> list[dict]:
    return [e.data for e in bus.events if e.type == etype]


# ---------------------------------------------------------------------------
# Happy path: knock → admit → token
# ---------------------------------------------------------------------------


async def test_knock_admit_then_poll_returns_a_guest_token_for_the_meeting_room(
    client, lk, recording_bus
) -> None:
    s = await _setup(lk)

    resp = await _knock(client, s.m.code, email="gus@elsewhere.dev")
    assert resp.status_code == 200
    knock = resp.json()
    assert set(knock) == KNOCK_KEYS
    assert knock["status"] == "waiting"

    # Members of the meeting room hear about it, keyed on the room.
    assert _events(recording_bus, "meeting.knock") == [
        {
            "workspace_id": s.ws,
            "meeting_id": s.m.id,
            "group_id": s.room,
            "knock_id": knock["knock_id"],
            "name": "Gus Guest",
        }
    ]

    waiting = await _poll(client, s.m.code, knock)
    assert waiting.status_code == 200
    assert waiting.json() == {"status": "waiting"}

    client.act_as(s.host, s.ws)
    listed = await client.get(f"/api/v1/meetings/{s.m.id}/knocks")
    assert listed.status_code == 200
    [row] = listed.json()
    assert set(row) == LIST_KEYS
    assert (row["knock_id"], row["name"], row["email"]) == (
        knock["knock_id"],
        "Gus Guest",
        "gus@elsewhere.dev",
    )

    admitted = await _decide(client, s, knock["knock_id"], "admit")
    assert admitted.status_code == 200
    assert admitted.json() == {"knock_id": knock["knock_id"], "status": "admitted"}
    assert _events(recording_bus, "meeting.knock_resolved") == [
        {
            "workspace_id": s.ws,
            "meeting_id": s.m.id,
            "group_id": s.room,
            "knock_id": knock["knock_id"],
            "status": "admitted",
        }
    ]

    first = await _poll(client, s.m.code, knock)
    body = first.json()
    assert set(body) == ADMITTED_KEYS
    assert body["status"] == "admitted"
    assert body["room_name"] == f"group-call-{s.room}"
    assert body["livekit_url"] == "wss://test.livekit.cloud"
    assert body["identity"].startswith("guest-") and len(body["identity"]) == len("guest-") + 16
    claims = jwt.decode(body["token"], options={"verify_signature": False})
    assert claims["sub"] == body["identity"]
    assert claims["video"]["room"] == f"group-call-{s.room}"
    assert claims["video"]["roomJoin"] is True
    assert claims["name"] == "Gus Guest"
    assert 0 < claims["exp"] - claims["nbf"] <= 300  # short: every poll mints a fresh one

    # Re-polling keeps the same identity; the list no longer shows the knock.
    again = (await _poll(client, s.m.code, knock)).json()
    assert again["identity"] == body["identity"]
    client.act_as(s.host, s.ws)
    assert (await client.get(f"/api/v1/meetings/{s.m.id}/knocks")).json() == []


async def test_a_knock_waits_when_nobody_is_in_the_call_yet(client, lk, recording_bus) -> None:
    s = await _setup(lk, live=False)

    resp = await _knock(client, s.m.code)

    assert resp.status_code == 200
    assert resp.json()["status"] == "waiting"
    assert len(_events(recording_bus, "meeting.knock")) == 1
    assert (await _poll(client, s.m.code, resp.json())).json() == {"status": "waiting"}


async def test_the_secret_is_stored_only_as_a_hash(client, lk) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code, name="  Gus Guest  ")).json()

    row = await MeetingKnock.find_one()
    assert row.name == "Gus Guest"
    assert row.meeting == s.m.id and row.workspace == s.ws
    assert knock["secret"] not in row.model_dump_json()
    assert row.secret_hash and row.secret_hash != knock["secret"]


async def test_deny(client, lk, recording_bus) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()

    resp = await _decide(client, s, knock["knock_id"], "deny")

    assert resp.json() == {"knock_id": knock["knock_id"], "status": "denied"}
    assert (await _poll(client, s.m.code, knock)).json() == {"status": "denied"}
    assert [e["status"] for e in _events(recording_bus, "meeting.knock_resolved")] == ["denied"]


async def test_guest_cancels(client, lk, recording_bus) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    client.log_out()

    resp = await client.delete(
        f"/api/v1/meetings/by-code/{s.m.code}/knocks/{knock['knock_id']}",
        headers={**GUEST_IP, "x-knock-secret": knock["secret"]},
    )

    assert resp.status_code == 200
    assert resp.json() == {"status": "cancelled"}
    assert [e["status"] for e in _events(recording_bus, "meeting.knock_resolved")] == ["cancelled"]
    late = await _decide(client, s, knock["knock_id"], "admit")
    assert late.status_code == 409
    assert late.json()["error"]["code"] == "meeting.knock_decided"


async def test_cancel_after_a_decision_is_409(client, lk) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    await _decide(client, s, knock["knock_id"], "deny")
    client.log_out()

    resp = await client.delete(
        f"/api/v1/meetings/by-code/{s.m.code}/knocks/{knock['knock_id']}",
        headers={**GUEST_IP, "x-knock-secret": knock["secret"]},
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "meeting.knock_decided"


async def test_waiting_knock_expires_after_ten_minutes(client, lk, recording_bus) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    later = datetime.now(UTC) + timedelta(minutes=10, seconds=1)

    with patch.object(lobby_service, "_now", return_value=later):
        client.act_as(s.host, s.ws)
        assert (await client.get(f"/api/v1/meetings/{s.m.id}/knocks")).json() == []
        admit = await _decide(client, s, knock["knock_id"], "admit")
        polled = await _poll(client, s.m.code, knock)

    assert admit.status_code == 409
    assert polled.json() == {"status": "expired"}
    assert [e["status"] for e in _events(recording_bus, "meeting.knock_resolved")] == ["expired"]


async def test_knock_still_waits_just_before_ten_minutes(client, lk) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    later = datetime.now(UTC) + timedelta(minutes=9, seconds=50)

    with patch.object(lobby_service, "_now", return_value=later):
        assert (await _poll(client, s.m.code, knock)).json() == {"status": "waiting"}


async def test_an_admission_lasts_an_hour(client, lk) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    await _decide(client, s, knock["knock_id"], "admit")
    later = datetime.now(UTC) + timedelta(hours=1, seconds=1)

    with patch.object(lobby_service, "_now", return_value=later):
        assert (await _poll(client, s.m.code, knock)).json() == {"status": "expired"}
    assert (await MeetingKnock.find_one()).decided_by == s.host  # who let them in is kept


async def test_a_waiting_knock_ends_with_its_meeting(client, lk) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    row = await Meeting.find_one()
    row.status = "ended"
    await row.save()

    admit = await _decide(client, s, knock["knock_id"], "admit")
    polled = await _poll(client, s.m.code, knock)

    assert admit.status_code == 410
    assert admit.json()["error"]["code"] == "meeting.ended"
    assert polled.json() == {"status": "expired"}


async def test_guest_token_never_outlives_the_admission(client, lk) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    await _decide(client, s, knock["knock_id"], "admit")
    decided = lobby_service._aware((await MeetingKnock.find_one()).decided_at)

    with patch.object(lobby_service, "_now", return_value=decided + timedelta(minutes=58)):
        body = (await _poll(client, s.m.code, knock)).json()

    claims = jwt.decode(body["token"], options={"verify_signature": False})
    assert 0 < claims["exp"] - claims["nbf"] <= 120


# ---------------------------------------------------------------------------
# The guest secret
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "how", ["missing", "wrong", "other_code", "bad_id", "unknown_code", "junk_code"]
)
async def test_status_needs_the_right_secret(client, lk, how) -> None:
    """Every miss is the same 404, so poll/cancel can't probe which codes exist."""
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    code = s.m.code
    secret = None
    if how == "missing":
        secret = False
    elif how == "wrong":
        secret = knock["secret"][:-2] + "xx"
    elif how == "other_code":
        other = await meetings_service.create_meeting(
            s.ws, s.host, CreateMeetingRequest(source="livekit", title="Other")
        )
        code = other.code
    elif how == "bad_id":
        knock = {**knock, "knock_id": "not-an-id"}
    elif how == "unknown_code":
        code = "abc-defg-hjk"
    elif how == "junk_code":
        code = "not-a-code"

    resp = await _poll(client, code, knock, secret=secret)
    sent = {} if secret is False else {"x-knock-secret": secret or knock["secret"]}
    cancel = await client.delete(
        f"/api/v1/meetings/by-code/{code}/knocks/{knock['knock_id']}",
        headers={**GUEST_IP, **sent},
    )

    for r in (resp, cancel):
        assert r.status_code == 404
        assert r.json()["error"] == {
            "code": "meeting_knock.not_found",
            "message": "meeting_knock not found",
        }
    assert (await MeetingKnock.find_one()).status == "waiting"


async def test_the_secret_is_only_read_from_the_header(client, lk) -> None:
    """A ``?secret=`` query would land in access logs; it isn't accepted."""
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    client.log_out()
    url = f"/api/v1/meetings/by-code/{s.m.code}/knocks/{knock['knock_id']}"

    polled = await client.get(url, params={"secret": knock["secret"]}, headers=GUEST_IP)
    cancelled = await client.delete(url, params={"secret": knock["secret"]}, headers=GUEST_IP)

    assert polled.status_code == 404
    assert cancelled.status_code == 404
    assert (await MeetingKnock.find_one()).status == "waiting"


# ---------------------------------------------------------------------------
# Guest-side refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["ended", "cancelled", "expired"])
async def test_knock_on_a_closed_meeting_is_410(client, lk, how) -> None:
    s = await _setup(lk)
    row = await Meeting.find_one()
    if how == "expired":
        row.link_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    else:
        row.status = how
    await row.save()

    resp = await _knock(client, s.m.code)

    assert resp.status_code == 410
    assert resp.json()["error"]["code"] == "meeting.ended"
    assert await MeetingKnock.find_one() is None


async def test_knock_on_an_unknown_code_is_404(client, lk) -> None:
    assert (await _knock(client, "abc-defg-hjk")).status_code == 404


@pytest.mark.parametrize("name", ["", "   ", "x" * 81])
async def test_knock_name_is_1_to_80_chars(client, lk, name) -> None:
    s = await _setup(lk)
    assert (await _knock(client, s.m.code, name=name)).status_code == 422


@pytest.mark.parametrize(
    ("email", "ok"),
    [(None, False), ("eve@x.dev", False), ("  ANA@Guest.dev ", True)],
)
async def test_guest_emails_are_checked_on_the_server(client, lk, email, ok) -> None:
    s = await _setup(lk)
    row = await Meeting.find_one()
    row.guest_emails = ["ana@guest.dev"]
    await row.save()

    resp = await _knock(client, s.m.code, **({"email": email} if email else {}))

    if ok:
        assert resp.status_code == 200
    else:
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "meeting.email_not_allowed"


async def test_guest_emails_apply_to_open_meetings_too(client, lk) -> None:
    s = await _setup(lk)
    row = await Meeting.find_one()
    row.guest_emails = ["ana@guest.dev"]
    row.access = "open"
    await row.save()

    resp = await _knock(client, s.m.code, email="eve@x.dev")

    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Rate limits (public endpoints)
# ---------------------------------------------------------------------------


async def test_knock_is_rate_limited_per_ip(client, lk) -> None:
    s = await _setup(lk)
    cap = rate_limit._meeting_knock_ip_limiter.capacity

    for _ in range(cap):
        assert (await _knock(client, s.m.code)).status_code == 200
    blocked = await _knock(client, s.m.code)
    other_ip = await _knock(client, s.m.code, headers={"x-forwarded-for": "198.51.100.5"})

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "meetings.knock_rate_limited"
    assert other_ip.status_code == 200


async def test_knock_is_rate_limited_per_code(client, lk) -> None:
    s = await _setup(lk)
    cap = rate_limit._meeting_knock_code_limiter.capacity
    forms = [s.m.code, s.m.code.replace("-", ""), s.m.code.upper()]

    for i in range(cap):
        ip = {"x-forwarded-for": f"203.0.113.{i % 250 + 1}"}
        # Every spelling of the code shares one bucket.
        assert (await _knock(client, forms[i % 3], headers=ip)).status_code == 200
    blocked = await _knock(client, s.m.code, headers={"x-forwarded-for": "192.0.2.99"})

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "meetings.knock_rate_limited"


async def test_status_poll_is_rate_limited_per_ip(client, lk) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    cap = rate_limit._meeting_knock_poll_limiter.capacity
    assert cap >= 60  # a 2s poll from several guests behind one NAT must fit

    for _ in range(cap):
        assert (await _poll(client, s.m.code, knock)).status_code == 200
    blocked = await _poll(client, s.m.code, knock)

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "meetings.knock_rate_limited"


# ---------------------------------------------------------------------------
# access = "open"
# ---------------------------------------------------------------------------


async def test_open_access_admits_straight_away_while_the_call_runs(
    client, lk, recording_bus
) -> None:
    s = await _setup(lk)
    row = await Meeting.find_one()
    row.access = "open"
    await row.save()

    resp = await _knock(client, s.m.code)

    assert resp.json()["status"] == "admitted"
    assert _events(recording_bus, "meeting.knock") == []  # nothing to decide
    body = (await _poll(client, s.m.code, resp.json())).json()
    assert set(body) == ADMITTED_KEYS
    assert body["room_name"] == f"group-call-{s.room}"


async def test_open_access_waits_for_someone_to_be_in_the_call(client, lk) -> None:
    s = await _setup(lk, live=False)
    row = await Meeting.find_one()
    row.access = "open"
    await row.save()

    knock = (await _knock(client, s.m.code)).json()
    assert knock["status"] == "waiting"
    assert (await _poll(client, s.m.code, knock)).json() == {"status": "waiting"}

    # Only the call-bot in the room is not a running call either.
    _go_live(lk, s.room, "call-bot")
    assert (await _poll(client, s.m.code, knock)).json() == {"status": "waiting"}

    _go_live(lk, s.room, s.host)
    body = (await _poll(client, s.m.code, knock)).json()
    assert body["status"] == "admitted" and set(body) == ADMITTED_KEYS


async def test_admitted_guest_gets_no_token_once_the_call_is_empty(client, lk) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    await _decide(client, s, knock["knock_id"], "admit")

    lk.participants.clear()  # everyone left; a token now would start a new room
    body = (await _poll(client, s.m.code, knock)).json()

    assert body == {"status": "admitted"}


async def test_guests_alone_do_not_count_as_a_running_call(client, lk) -> None:
    """Only a member keeps the lobby open: once the last member leaves, an admitted
    guest gets no fresh token and an open meeting stops admitting new knocks, even
    though guests (and the call-bot) are still in the LiveKit room."""
    s = await _setup(lk)
    a = (await _knock(client, s.m.code, name="Ann Guest")).json()
    await _decide(client, s, a["knock_id"], "admit")
    first = (await _poll(client, s.m.code, a)).json()
    assert "token" in first
    _go_live(lk, s.room, first["identity"], "call-bot")  # A connects; bot is there too

    lk.participants[:] = [p for p in lk.participants if p.identity != s.host]  # host leaves

    assert (await _poll(client, s.m.code, a)).json() == {"status": "admitted"}
    row = await Meeting.find_one()
    row.access = "open"
    await row.save()
    b = (await _knock(client, s.m.code, name="Bob Guest")).json()
    assert b["status"] == "waiting"
    assert (await _poll(client, s.m.code, b)).json() == {"status": "waiting"}


async def test_admitted_guest_gets_no_token_once_the_meeting_ended(client, lk) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    await _decide(client, s, knock["knock_id"], "admit")
    row = await Meeting.find_one()
    row.status = "ended"
    await row.save()

    assert (await _poll(client, s.m.code, knock)).json() == {"status": "admitted"}


# ---------------------------------------------------------------------------
# Member side: who may list and decide
# ---------------------------------------------------------------------------


async def test_a_workspace_member_outside_the_room_cannot_list_or_decide(client, lk) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    outsider = await _user("Olga Outsider")
    _go_live(lk, s.room, outsider)  # even "in" the LiveKit room, not a member

    client.act_as(outsider, s.ws)
    listed = await client.get(f"/api/v1/meetings/{s.m.id}/knocks")
    admit = await _decide(client, s, knock["knock_id"], "admit", as_user=outsider)
    deny = await _decide(client, s, knock["knock_id"], "deny", as_user=outsider)

    for resp in (listed, admit, deny):
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "livekit.room_forbidden"
    assert (await MeetingKnock.find_one()).status == "waiting"


async def test_another_workspace_gets_404(client, lk) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    other_ws = await _workspace("other")
    stranger = await _user("Sam Stranger")

    client.act_as(stranger, other_ws)
    listed = await client.get(f"/api/v1/meetings/{s.m.id}/knocks")
    admit = await client.post(f"/api/v1/meetings/{s.m.id}/knocks/{knock['knock_id']}/admit")

    assert listed.status_code == 404
    assert admit.status_code == 404


async def test_a_room_member_who_is_not_in_the_call_cannot_list_or_decide(client, lk) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    mate = await _user("Mia Mate")
    await group_service.add_meeting_room_member(s.room, mate)

    client.act_as(mate, s.ws)
    listed = await client.get(f"/api/v1/meetings/{s.m.id}/knocks")
    admit = await _decide(client, s, knock["knock_id"], "admit", as_user=mate)

    # Waiting guests' names and emails are only shown to people in the call.
    for resp in (listed, admit):
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "meeting.not_in_call"

    _go_live(lk, s.room, mate)
    client.act_as(mate, s.ws)
    assert (await client.get(f"/api/v1/meetings/{s.m.id}/knocks")).status_code == 200
    assert (await _decide(client, s, knock["knock_id"], "admit", as_user=mate)).status_code == 200


async def test_deciding_twice_is_409(client, lk) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()

    assert (await _decide(client, s, knock["knock_id"], "admit")).status_code == 200
    again = await _decide(client, s, knock["knock_id"], "deny")

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "meeting.knock_decided"
    assert (await MeetingKnock.find_one()).status == "admitted"


async def test_two_members_racing_only_one_decision_lands(client, lk, recording_bus) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()
    meeting = await Meeting.find_one()
    row = await MeetingKnock.find_one()
    stale = await MeetingKnock.find_one()
    ok_first = await lobby_service._transition(row, meeting, "admitted", decided_by=s.host)
    ok_second = await lobby_service._transition(stale, meeting, "denied", decided_by=s.host)

    assert (ok_first, ok_second) == (True, False)
    assert (await MeetingKnock.find_one()).status == "admitted"
    assert [e["status"] for e in _events(recording_bus, "meeting.knock_resolved")] == ["admitted"]
    assert knock["knock_id"] == str(row.id)


@pytest.mark.parametrize("action", ["admit", "deny"])
async def test_decisions_are_audited(client, lk, action) -> None:
    s = await _setup(lk)
    knock = (await _knock(client, s.m.code)).json()

    with patch("pocketpaw_ee.guards.audit.log_privileged_action") as audit:
        assert (await _decide(client, s, knock["knock_id"], action)).status_code == 200
        again = await _decide(client, s, knock["knock_id"], action)

    assert again.status_code == 409
    audit.assert_called_once_with(
        actor=s.host,
        action="meeting.knock_decide",
        resource_id=knock["knock_id"],
        workspace_id=s.ws,
        meeting_id=s.m.id,
        decision="admitted" if action == "admit" else "denied",
    )


async def test_unknown_knock_is_404(client, lk) -> None:
    s = await _setup(lk)
    resp = await _decide(client, s, "0123456789abcdef01234567", "admit")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Realtime audience
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("etype", ["meeting.knock", "meeting.knock_resolved"])
async def test_knock_events_go_to_the_meeting_rooms_members(etype) -> None:
    from pocketpaw_ee.cloud._core.realtime.audience import AudienceResolver
    from pocketpaw_ee.cloud.meetings.events import MeetingKnock as KnockEvent
    from pocketpaw_ee.cloud.meetings.events import MeetingKnockResolved

    asked: list[str] = []

    async def _members(gid: str) -> list[str]:
        asked.append(gid)
        return ["u1", "u2"]

    cls = KnockEvent if etype == "meeting.knock" else MeetingKnockResolved
    assert cls.EVENT_TYPE == etype
    resolver = AudienceResolver(group_members=_members)

    assert sorted(await resolver.audience(cls(data={"group_id": "room1"}))) == ["u1", "u2"]
    assert asked == ["room1"]
    assert await resolver.audience(cls(data={"meeting_id": "m1"})) == []


# ---------------------------------------------------------------------------
# PATCH /meetings/{id}
# ---------------------------------------------------------------------------


async def test_host_can_change_access_title_and_description(client, lk, recording_bus) -> None:
    s = await _setup(lk)
    client.act_as(s.host, s.ws)

    resp = await client.patch(
        f"/api/v1/meetings/{s.m.id}",
        json={"access": "open", "title": "  Retro ", "description": "notes"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert (body["access"], body["title"], body["description"]) == ("open", "Retro", "notes")
    row = await Meeting.find_one()
    assert (row.access, row.title, row.description) == ("open", "Retro", "notes")
    assert _events(recording_bus, "meeting.updated")[-1]["group_id"] == s.room


async def test_only_the_host_can_patch(client, lk) -> None:
    s = await _setup(lk)
    mate = await _user("Mia Mate")
    await group_service.add_meeting_room_member(s.room, mate)
    client.act_as(mate, s.ws)

    resp = await client.patch(f"/api/v1/meetings/{s.m.id}", json={"access": "open"})

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "meeting.host_only"
    assert (await Meeting.find_one()).access == "ask"


@pytest.mark.parametrize(
    "body",
    [
        {"access": "public"},
        {"title": "   "},
        {"scheduled_start": "2026-10-02T10:00:00Z"},  # not supported: loud, not a no-op
    ],
)
async def test_patch_rejects_bad_or_unsupported_fields(client, lk, body) -> None:
    s = await _setup(lk)
    client.act_as(s.host, s.ws)

    resp = await client.patch(f"/api/v1/meetings/{s.m.id}", json=body)

    assert resp.status_code == 422
    assert (await Meeting.find_one()).title == "Planning"


async def test_patch_in_another_workspace_is_404(client, lk) -> None:
    s = await _setup(lk)
    other_ws = await _workspace("other")
    client.act_as(s.host, other_ws)

    resp = await client.patch(f"/api/v1/meetings/{s.m.id}", json={"access": "open"})

    assert resp.status_code == 404
