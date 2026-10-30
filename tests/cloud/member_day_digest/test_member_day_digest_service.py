# tests/cloud/member_day_digest/test_member_day_digest_service.py
# Created: 2026-06-08 — VIP Onboarding Phase B chunk 5 (the "your day" digest).
#
# Pins the per-member day-digest contract. The isolation tests come FIRST
# (TDD) because the whole point is that member B's digest can NEVER contain
# member A's mail/calendar — and it is keyed on the opaque member id, never a
# caller-supplied id:
#
#   1. the digest reads THIS member's mail/calendar (per-user clients) and
#      echoes the member id it was asked about.
#   2. member B's digest is built from B's readers only — A's data can't
#      appear (the readers are per-member; the service constructs them from
#      member_id, so two members get two different token buckets).
#   3. the per-user clients are constructed with user_id == member_id (the
#      structural isolation: a second member always gets a different bucket).
#   4. a member with NO connected accounts / empty pulls → an empty digest,
#      no error, no crash (graceful — the agent then emits no block).
#   5. a read failure on one source is isolated → the other source still
#      contributes; the error is recorded, the digest is not lost.
#   6. the digest is bounded (event/mail caps) so one huge inbox can't blow
#      the shape chunk 6 / the briefing consume.
#
# All Gmail/Calendar reads are injected as fakes so the suite runs with no
# network and no OAuth.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.member_day_digest import service as digest_service  # noqa: E402

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Fakes — capture what the digest would read without any I/O.
# --------------------------------------------------------------------------


class FakeGmailReader:
    """Stands in for ``GmailClient``. Records the queries it was asked for."""

    def __init__(self, messages: list[dict] | None = None) -> None:
        self._messages = messages or []
        self.queries: list[str] = []

    async def search(self, query: str, max_results: int = 10) -> list[dict]:
        self.queries.append(query)
        return list(self._messages[:max_results])


class FakeCalendarReader:
    def __init__(self, events: list[dict] | None = None) -> None:
        self._events = events or []
        self.calls: list[tuple] = []

    async def list_events(self, time_min=None, time_max=None, max_results=10, **_kw):
        self.calls.append((time_min, time_max, max_results))
        return list(self._events[:max_results])


def _sample_messages(n: int = 2) -> list[dict]:
    return [
        {
            "id": f"m{i}",
            "subject": f"Subject {i}",
            "from": f"sender{i}@example.com",
            "date": "Wed, 04 Jun 2026 10:00:00 +0000",
            "snippet": f"body snippet {i}",
        }
        for i in range(n)
    ]


def _sample_events(n: int = 2) -> list[dict]:
    return [
        {
            "id": f"e{i}",
            "summary": f"Event {i}",
            "start": "2026-06-10T09:00:00Z",
            "end": "2026-06-10T10:00:00Z",
            "location": "Room A",
            "description": f"agenda {i}",
            "attendees": ["a@example.com"],
        }
        for i in range(n)
    ]


# --------------------------------------------------------------------------
# 1 — the digest reads this member's data and echoes the member id.
# --------------------------------------------------------------------------


async def test_digest_returns_structured_shape_for_member():
    member = "member-alice-objid"
    digest = await digest_service.member_day_digest(
        workspace_id="w1",
        member_id=member,
        gmail_reader=FakeGmailReader(_sample_messages(3)),
        calendar_reader=FakeCalendarReader(_sample_events(2)),
    )

    # The digest is keyed on the member it was asked about.
    assert digest.workspace_id == "w1"
    assert digest.member_id == member
    # Calendar events came through, soonest first, structured.
    assert len(digest.events) == 2
    assert digest.events[0].summary == "Event 0"
    assert digest.events[0].start == "2026-06-10T09:00:00Z"
    # Mail counts + top items came through.
    assert digest.unread_mail_count == 3
    assert len(digest.top_mail) == 3
    assert digest.top_mail[0].subject == "Subject 0"
    assert digest.top_mail[0].sender == "sender0@example.com"
    assert not digest.empty


# --------------------------------------------------------------------------
# 2 + 3 — THE ISOLATION INVARIANT. The member id keys the read; member B's
# digest is built from B's readers, never A's; and the real per-user clients
# are constructed with user_id == member_id (different bucket per member).
# --------------------------------------------------------------------------


async def test_member_b_digest_never_contains_member_a_data():
    alice = "alice-objid"
    bob = "bob-objid"

    alice_digest = await digest_service.member_day_digest(
        workspace_id="w1",
        member_id=alice,
        gmail_reader=FakeGmailReader(
            [{"id": "ma", "subject": "ALICE SECRET", "from": "x@a.com", "date": "", "snippet": ""}]
        ),
        calendar_reader=FakeCalendarReader([]),
    )
    bob_digest = await digest_service.member_day_digest(
        workspace_id="w1",
        member_id=bob,
        gmail_reader=FakeGmailReader(
            [{"id": "mb", "subject": "BOB ONLY", "from": "y@b.com", "date": "", "snippet": ""}]
        ),
        calendar_reader=FakeCalendarReader([]),
    )

    assert alice_digest.member_id == alice
    assert bob_digest.member_id == bob
    # Bob's digest contains only Bob's mail subject — never Alice's.
    bob_subjects = [m.subject for m in bob_digest.top_mail]
    assert "BOB ONLY" in bob_subjects
    assert "ALICE SECRET" not in bob_subjects


async def test_per_user_clients_keyed_on_member_id(monkeypatch):
    """When no reader is injected, the digest constructs the REAL per-user
    clients with ``user_id == member_id`` — the structural isolation: member
    B always gets a different OAuth-token bucket than member A. No
    caller-supplied user id is ever threaded into the client."""
    constructed: list[tuple[str, str]] = []

    class _SpyGmail:
        def __init__(self, user_id=None):
            constructed.append(("gmail", user_id))

        async def search(self, *_a, **_k):
            return []

    class _SpyCalendar:
        def __init__(self, user_id=None):
            constructed.append(("calendar", user_id))

        async def list_events(self, *_a, **_k):
            return []

    monkeypatch.setattr("pocketpaw.clients.gmail.GmailClient", _SpyGmail)
    monkeypatch.setattr("pocketpaw.clients.gcalendar.CalendarClient", _SpyCalendar)

    await digest_service.member_day_digest(workspace_id="w1", member_id="member-xyz")

    # Both clients were constructed for THIS member's id and no other.
    assert ("gmail", "member-xyz") in constructed
    assert ("calendar", "member-xyz") in constructed
    assert all(uid == "member-xyz" for _, uid in constructed)


async def test_digest_member_id_is_not_caller_overridable():
    """The echoed ``member_id`` is exactly the argument — there is no body
    field or reader attribute that can swap which member the digest is for."""
    member = "victim-objid"
    digest = await digest_service.member_day_digest(
        workspace_id="w1",
        member_id=member,
        gmail_reader=FakeGmailReader(_sample_messages(1)),
        calendar_reader=FakeCalendarReader([]),
    )
    assert digest.member_id == member


# --------------------------------------------------------------------------
# 4 — graceful empty: no connected accounts / empty pulls → empty digest.
# --------------------------------------------------------------------------


async def test_no_accounts_yields_empty_digest_no_error():
    """A member who connected nothing: both readers raise the "not
    authenticated" error → the digest is EMPTY but does not raise, and the
    errors are recorded (so the agent emits no block, behaves as today)."""

    class Unauth:
        async def search(self, *_a, **_k):
            raise RuntimeError("Gmail not authenticated. Complete OAuth flow first")

        async def list_events(self, *_a, **_k):
            raise RuntimeError("Google Calendar not authenticated")

    digest = await digest_service.member_day_digest(
        workspace_id="w1",
        member_id="m-empty",
        gmail_reader=Unauth(),
        calendar_reader=Unauth(),
    )
    assert digest.empty
    assert digest.events == []
    assert digest.top_mail == []
    assert digest.unread_mail_count == 0
    # Both failures recorded — non-fatal.
    assert len(digest.errors) == 2


async def test_empty_pulls_yield_empty_digest():
    """Connected accounts but genuinely nothing → empty digest, no errors."""
    digest = await digest_service.member_day_digest(
        workspace_id="w1",
        member_id="m-quiet",
        gmail_reader=FakeGmailReader([]),
        calendar_reader=FakeCalendarReader([]),
    )
    assert digest.empty
    assert digest.errors == []


# --------------------------------------------------------------------------
# 5 — a one-source failure is isolated; the other source still contributes.
# --------------------------------------------------------------------------


async def test_partial_failure_keeps_healthy_source():
    class BoomGmail:
        async def search(self, *_a, **_k):
            raise RuntimeError("429 rate limited")

    digest = await digest_service.member_day_digest(
        workspace_id="w1",
        member_id="m-partial",
        gmail_reader=BoomGmail(),
        calendar_reader=FakeCalendarReader(_sample_events(2)),
    )
    # Calendar still landed; mail errored.
    assert len(digest.events) == 2
    assert digest.unread_mail_count == 0
    assert any("gmail" in e.lower() for e in digest.errors)
    assert not digest.empty  # calendar gives it content


# --------------------------------------------------------------------------
# 6 — bounded: caps cap the event/mail counts.
# --------------------------------------------------------------------------


async def test_digest_caps_events_and_mail():
    digest = await digest_service.member_day_digest(
        workspace_id="w1",
        member_id="m-big",
        gmail_reader=FakeGmailReader(_sample_messages(50)),
        calendar_reader=FakeCalendarReader(_sample_events(50)),
    )
    # The structured digest is bounded so the briefing/intent-board stay small.
    assert len(digest.events) <= digest_service.MAX_EVENTS
    assert len(digest.top_mail) <= digest_service.MAX_TOP_MAIL


async def test_blank_member_id_rejected():
    with pytest.raises(Exception):
        await digest_service.member_day_digest(workspace_id="w1", member_id="   ")
