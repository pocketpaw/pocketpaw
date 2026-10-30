# service.py — The member-day digest: a structured "your day" pull.
# Created: 2026-06-08 — VIP Onboarding Phase B chunk 5.
#
# What this does
# --------------
# Builds a concise, STRUCTURED "your day" digest for one workspace member:
# their upcoming calendar (next ~7 days) + recent/unread mail (count + top
# items), read LIVE with THAT member's OWN per-user Gmail/Calendar token
# (chunk 1's per-user OAuth). It is a reusable SERVICE — the agent briefing
# (chunk 5's gated system-prompt block) renders it down to a capped string,
# and chunk 6's intent board will read the same structured shape.
#
# Live pull, NOT the KB ingest
# ----------------------------
# Chunk 4's ingest writes the member's mail/calendar into their private
# ``user:{id}`` KB for the agent's DEEP retrieval. This digest is the
# COMPLEMENTARY structured "what's on today" — a live, current pull straight
# from the per-user clients (``calendar_list`` for events, ``gmail_search``
# for recent/unread), so the briefing reflects the member's day as it is RIGHT
# NOW, not as of the last ingest tick.
#
# Per-member isolation (the load-bearing invariant)
# --------------------------------------------------
# The digest is keyed on the opaque cloud ``member_id``: the per-user clients
# are constructed as ``GmailClient(user_id=member_id)`` /
# ``CalendarClient(user_id=member_id)``, so each member resolves THEIR OWN
# token bucket and a second member structurally gets a different bucket. The
# returned ``MemberDayDigest.member_id`` is the SAME argument echoed back —
# there is no caller-supplied member id surface (the agent gate derives it
# from the authenticated principal, ``ctx.user_id``). Member B's digest can
# therefore NEVER contain member A's data.
#
# Graceful by design
# ------------------
# A member with no connected accounts → both per-user reads raise "not
# authenticated"; each source is isolated, the error is recorded, and the
# digest comes back EMPTY (``digest.empty is True``) — never an exception. The
# briefing then emits no block and the agent behaves exactly as today. A
# single flaky source is isolated the same way: the healthy source still
# contributes.

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pocketpaw_ee.cloud.member_day_digest.dto import (
    DigestEvent,
    DigestMail,
    MemberDayDigest,
    MemberDayDigestRequest,
)

logger = logging.getLogger(__name__)

# --- Tunables (bounded by design — the digest is "your day", not "everything") ---

# Calendar look-ahead: the next ~7 days of events ("upcoming").
DIGEST_WINDOW_DAYS = 7
# Recent-mail window for the Gmail query.
MAIL_RECENT_DAYS = 7
# Caps so one huge inbox / calendar can't blow the structured shape the
# briefing and chunk 6's intent board consume.
MAX_EVENTS = 10
MAX_TOP_MAIL = 5
# Gmail query: recent + unread, the "needs-attention" set for a day briefing.
_MAIL_QUERY = f"is:unread newer_than:{MAIL_RECENT_DAYS}d"


# --------------------------------------------------------------------------
# Reader protocols — the service depends on these narrow shapes, not the
# concrete clients, so tests inject fakes with no network / OAuth. Mirrors
# the member_ingest precedent.
# --------------------------------------------------------------------------


class GmailReader(Protocol):
    async def search(self, query: str, max_results: int = 10) -> list[dict[str, Any]]: ...


class CalendarReader(Protocol):
    async def list_events(
        self,
        time_min: datetime | None = ...,
        time_max: datetime | None = ...,
        max_results: int = ...,
        **kwargs: Any,
    ) -> list[dict[str, Any]]: ...


# member_day_digest(workspace_id, member_id) -> MemberDayDigest. The async
# contract the agent briefing (and chunk 6) calls; aliased here for callers
# that want to inject a fake digest.
DigestFn = Callable[[str, str], Awaitable[MemberDayDigest]]


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


async def member_day_digest(
    workspace_id: str,
    member_id: str,
    *,
    gmail_reader: GmailReader | None = None,
    calendar_reader: CalendarReader | None = None,
    now: datetime | None = None,
) -> MemberDayDigest:
    """Build the structured "your day" digest for one member.

    Reads upcoming calendar (next ~7 days) + recent/unread mail with THIS
    member's per-user token. ``gmail_reader`` / ``calendar_reader`` default to
    the real per-user clients (``GmailClient(user_id=member_id)`` /
    ``CalendarClient(user_id=member_id)``) — the structural per-member
    isolation. Tests inject fakes.

    Per-source failures are isolated and recorded in ``digest.errors``; a
    member with no connected accounts comes back EMPTY (never raises). The
    returned ``member_id`` is exactly the argument — the caller cannot swap
    which member the digest is for.
    """
    # Validate at entry (cloud rule §6) — internal callers (the agent briefing,
    # chunk 6's intent board) get the same guard an HTTP body would. A blank
    # member id is rejected so it can never collapse into a cross-member read.
    body = MemberDayDigestRequest.model_validate(
        {"workspace_id": workspace_id, "member_id": member_id}
    )
    workspace_id, member_id = body.workspace_id, body.member_id

    now = now or datetime.now(UTC)

    # Lazy-construct the real per-user clients only when no fake was injected.
    # Importing here keeps the module import-light and avoids pulling the OSS
    # client stack into pure-unit tests. ``user_id=member_id`` is the only
    # identity threaded in — never a caller-supplied id.
    if gmail_reader is None or calendar_reader is None:
        from pocketpaw.clients.gcalendar import CalendarClient
        from pocketpaw.clients.gmail import GmailClient

        gmail_reader = gmail_reader or GmailClient(user_id=member_id)
        calendar_reader = calendar_reader or CalendarClient(user_id=member_id)

    errors: list[str] = []
    events: list[DigestEvent] = []
    unread_count = 0
    top_mail: list[DigestMail] = []

    # --- Calendar: upcoming events, next ~7 days, soonest first ---
    try:
        time_min = now
        time_max = now + timedelta(days=DIGEST_WINDOW_DAYS)
        raw_events = await calendar_reader.list_events(
            time_min=time_min,
            time_max=time_max,
            max_results=MAX_EVENTS,
        )
        for e in raw_events[:MAX_EVENTS]:
            events.append(
                DigestEvent(
                    summary=(e.get("summary") or "(no title)").strip(),
                    start=(e.get("start") or "").strip(),
                    end=(e.get("end") or "").strip(),
                    location=(e.get("location") or "").strip(),
                )
            )
    except Exception as exc:  # noqa: BLE001 — isolate per-source so one bad
        # source never sinks the other or raises into the briefing path.
        logger.warning(
            "member_day_digest: calendar read failed for member=%s ws=%s: %s",
            member_id,
            workspace_id,
            exc,
        )
        errors.append(f"calendar: {exc}")

    # --- Mail: recent/unread, count + top items ---
    try:
        raw_mail = await gmail_reader.search(_MAIL_QUERY, max_results=MAX_TOP_MAIL)
        unread_count = len(raw_mail)
        for m in raw_mail[:MAX_TOP_MAIL]:
            top_mail.append(
                DigestMail(
                    subject=(m.get("subject") or "(no subject)").strip(),
                    sender=(m.get("from") or "").strip(),
                    date=(m.get("date") or "").strip(),
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "member_day_digest: gmail read failed for member=%s ws=%s: %s",
            member_id,
            workspace_id,
            exc,
        )
        errors.append(f"gmail: {exc}")

    return MemberDayDigest(
        workspace_id=workspace_id,
        member_id=member_id,
        events=events,
        unread_mail_count=unread_count,
        top_mail=top_mail,
        errors=errors,
    )


__all__ = ["member_day_digest", "MAX_EVENTS", "MAX_TOP_MAIL"]
