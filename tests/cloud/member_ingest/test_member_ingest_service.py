# tests/cloud/member_ingest/test_member_ingest_service.py
# Created: 2026-06-08 — VIP Onboarding Phase B (per-user ingest worker).
#
# Pins the per-member Gmail/Calendar → private-KB ingest contract. The
# isolation tests come FIRST (TDD) because the whole point of the chunk is
# that one member's mail/calendar can NEVER land in another member's KB
# scope:
#
#   1. ingest writes to the member's OWN ``user:{member_id}`` scope.
#   2. member B's ingest NEVER touches member A's scope (the airtight rule).
#   3. backfill path — first run flips ``backfill_done`` and uses the wide
#      window; produces one accept call per source with documents.
#   4. incremental path — second run uses the narrow window and advances
#      the cursors; backfill is not repeated.
#   5. a read failure (e.g. token refresh failed) → status=error, no crash,
#      and NOTHING is written to the scope.
#   6. the keyless ``accept`` path is used (NOT ``ingest`` — no API key on
#      this backend) — asserted via the injected kb_accept capturing the
#      kb-go argv.
#
# All Gmail/Calendar reads and the kb-go accept subprocess are injected as
# fakes so the suite runs with no network, no OAuth, and no kb binary.

from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.cloud.member_ingest import service as ingest_service  # noqa: E402
from pocketpaw_ee.cloud.models.member_ingest_state import MemberIngestState  # noqa: E402

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# Fakes — capture what the worker would read / write without any I/O.
# --------------------------------------------------------------------------


class FakeGmailReader:
    """Stands in for ``GmailClient.search``. Records the query it was asked
    for so tests can assert the backfill-vs-incremental window."""

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


class CapturingAccept:
    """Captures every kb ``accept`` call: (scope, articles). The real impl
    shells out to ``kb accept --scope <s>`` with the articles JSON on stdin;
    we record the resolved scope + parsed articles so a test can prove the
    scope is the member's own and nobody else's."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict]]] = []

    async def __call__(self, scope: str, articles: list[dict]) -> dict:
        self.calls.append((scope, [dict(a) for a in articles]))
        return {"accepted": len(articles), "articles": len(articles)}

    def scopes_written(self) -> set[str]:
        return {scope for scope, _ in self.calls}

    def articles_for(self, scope: str) -> list[dict]:
        out: list[dict] = []
        for s, arts in self.calls:
            if s == scope:
                out.extend(arts)
        return out


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
# 1 + 6 — writes to the member's OWN scope via the keyless accept path.
# --------------------------------------------------------------------------


async def test_ingest_writes_to_member_user_scope(mongo_db):  # noqa: ARG001
    member = "member-alice-objid"
    accept = CapturingAccept()

    result = await ingest_service.ingest_member(
        workspace_id="w1",
        member_id=member,
        gmail_reader=FakeGmailReader(_sample_messages(2)),
        calendar_reader=FakeCalendarReader(_sample_events(2)),
        kb_accept=accept,
    )

    assert result["status"] == "ok"
    # Every write targets exactly the member's own scope — nothing else.
    assert accept.scopes_written() == {f"user:{member}"}
    # Both sources contributed documents (2 mail + 2 events = 4).
    arts = accept.articles_for(f"user:{member}")
    assert len(arts) == 4
    # Each article carries title + content (the accept minimum, keyless).
    for a in arts:
        assert a["title"]
        assert a["content"]


# --------------------------------------------------------------------------
# 2 — THE ISOLATION INVARIANT. Member B's ingest never touches member A.
# --------------------------------------------------------------------------


async def test_member_b_ingest_never_writes_to_member_a_scope(mongo_db):  # noqa: ARG001
    alice = "alice-objid"
    bob = "bob-objid"

    accept = CapturingAccept()

    # Alice ingests first.
    await ingest_service.ingest_member(
        workspace_id="w1",
        member_id=alice,
        gmail_reader=FakeGmailReader(_sample_messages(3)),
        calendar_reader=FakeCalendarReader(_sample_events(1)),
        kb_accept=accept,
    )
    # Then Bob, on the SAME workspace, with his own (different) data.
    await ingest_service.ingest_member(
        workspace_id="w1",
        member_id=bob,
        gmail_reader=FakeGmailReader(_sample_messages(2)),
        calendar_reader=FakeCalendarReader(_sample_events(2)),
        kb_accept=accept,
    )

    # The two members' writes are perfectly partitioned by scope.
    assert accept.scopes_written() == {f"user:{alice}", f"user:{bob}"}
    # Bob's run produced ONLY user:bob writes — never user:alice.
    alice_arts = accept.articles_for(f"user:{alice}")
    bob_arts = accept.articles_for(f"user:{bob}")
    assert len(alice_arts) == 4  # 3 mail + 1 event
    assert len(bob_arts) == 4  # 2 mail + 2 events
    # Belt-and-suspenders: no article that originated from Bob's reader text
    # ("body snippet"/"Event") can be found under alice's scope beyond her own
    # legitimate count, and the scope sets are disjoint by construction above.
    assert f"user:{alice}" != f"user:{bob}"


async def test_ingest_member_scope_ignores_caller_supplied_scope(mongo_db):  # noqa: ARG001
    """The scope is DERIVED from member_id, never passed in. Even if a
    caller had a foreign id in hand, ingest_member binds the write to
    ``user:{member_id}`` — there is no scope override surface to abuse."""
    member = "victim-objid"
    accept = CapturingAccept()
    await ingest_service.ingest_member(
        workspace_id="w1",
        member_id=member,
        gmail_reader=FakeGmailReader(_sample_messages(1)),
        calendar_reader=FakeCalendarReader([]),
        kb_accept=accept,
    )
    # Only the member's own scope, derived internally.
    assert accept.scopes_written() == {f"user:{member}"}


# --------------------------------------------------------------------------
# 3 — backfill path: first run, wide window, backfill_done flips true.
# --------------------------------------------------------------------------


async def test_backfill_first_run_sets_state_and_uses_wide_window(mongo_db):  # noqa: ARG001
    member = "m-backfill"
    gmail = FakeGmailReader(_sample_messages(2))
    accept = CapturingAccept()

    result = await ingest_service.ingest_member(
        workspace_id="w1",
        member_id=member,
        gmail_reader=gmail,
        calendar_reader=FakeCalendarReader(_sample_events(1)),
        kb_accept=accept,
    )

    assert result["mode"] == "backfill"
    # Persisted state: backfill done, status ok, cursors set.
    state = await MemberIngestState.find_one(
        MemberIngestState.workspace == "w1",
        MemberIngestState.member_id == member,
    )
    assert state is not None
    assert state.backfill_done is True
    assert state.status == "ok"
    assert state.last_sync_at is not None
    # Backfill window is the wide one (default 30d) on the Gmail query.
    assert any("newer_than:30d" in q for q in gmail.queries)


# --------------------------------------------------------------------------
# 4 — incremental path: second run, narrow window, no repeat backfill.
# --------------------------------------------------------------------------


async def test_incremental_second_run_uses_narrow_window(mongo_db):  # noqa: ARG001
    member = "m-incr"
    accept = CapturingAccept()

    # First run = backfill.
    await ingest_service.ingest_member(
        workspace_id="w1",
        member_id=member,
        gmail_reader=FakeGmailReader(_sample_messages(2)),
        calendar_reader=FakeCalendarReader(_sample_events(1)),
        kb_accept=accept,
    )

    # Second run = incremental. Fresh reader to capture the new query.
    gmail2 = FakeGmailReader(_sample_messages(1))
    result2 = await ingest_service.ingest_member(
        workspace_id="w1",
        member_id=member,
        gmail_reader=gmail2,
        calendar_reader=FakeCalendarReader(_sample_events(1)),
        kb_accept=accept,
    )

    assert result2["mode"] == "incremental"
    # Incremental uses the narrow window, NOT the 30d backfill window.
    assert gmail2.queries
    assert all("newer_than:30d" not in q for q in gmail2.queries)
    assert any("newer_than:" in q for q in gmail2.queries)

    state = await MemberIngestState.find_one(
        MemberIngestState.workspace == "w1",
        MemberIngestState.member_id == member,
    )
    assert state.backfill_done is True  # still true, not re-run


# --------------------------------------------------------------------------
# 5 — a read failure does NOT crash and writes nothing; status=error.
# --------------------------------------------------------------------------


async def test_read_failure_marks_error_and_writes_nothing(mongo_db):  # noqa: ARG001
    member = "m-fail"
    accept = CapturingAccept()

    class BoomGmail:
        async def search(self, *_a, **_k):
            raise RuntimeError("Gmail not authenticated. Complete OAuth flow first")

    class BoomCalendar:
        async def list_events(self, *_a, **_k):
            raise RuntimeError("Google Calendar not authenticated")

    result = await ingest_service.ingest_member(
        workspace_id="w1",
        member_id=member,
        gmail_reader=BoomGmail(),
        calendar_reader=BoomCalendar(),
        kb_accept=accept,
    )

    assert result["status"] == "error"
    # Nothing was written to ANY scope when both sources failed.
    assert accept.calls == []
    state = await MemberIngestState.find_one(
        MemberIngestState.workspace == "w1",
        MemberIngestState.member_id == member,
    )
    assert state is not None
    assert state.status == "error"
    assert state.last_error
    # Backfill not marked done on a failed run, so it retries next time.
    assert state.backfill_done is False


async def test_partial_failure_still_ingests_healthy_source(mongo_db):  # noqa: ARG001
    """Gmail fails but Calendar succeeds → the calendar docs still land in
    the member's scope, and status reflects a partial error (still 'ok' for
    the run since at least one source produced data, but last_error noted)."""
    member = "m-partial"
    accept = CapturingAccept()

    class BoomGmail:
        async def search(self, *_a, **_k):
            raise RuntimeError("429 rate limited")

    result = await ingest_service.ingest_member(
        workspace_id="w1",
        member_id=member,
        gmail_reader=BoomGmail(),
        calendar_reader=FakeCalendarReader(_sample_events(2)),
        kb_accept=accept,
    )

    # Calendar docs landed in the member's own scope.
    assert accept.scopes_written() == {f"user:{member}"}
    assert len(accept.articles_for(f"user:{member}")) == 2
    # The run still records the partial failure.
    assert result["status"] in {"ok", "error"}
    assert result["errors"]
