# service.py — Per-user Gmail/Calendar → private-KB ingest worker (Phase B).
# Created: 2026-06-08 — VIP Onboarding Phase B (per-user ingest worker).
#
# What this does
# --------------
# For a consented workspace member who connected Gmail and/or Google Calendar
# as a PER-USER connector (chunk 1's per-user OAuth tokens), this worker reads
# their recent mail + upcoming calendar with THEIR OWN token and writes the
# text into their strictly-private ``user:{member_id}`` KB scope (chunk 2's
# gated scope). An initial bounded backfill runs on first connect; an
# incremental pass runs on the periodic schedule thereafter.
#
# Why the keyless ``accept`` path (NOT ``ingest``/``build``)
# ----------------------------------------------------------
# kb-go's ``ingest``/``build`` LLM-compile each article by calling Anthropic
# directly (kb.go:cmdIngest reads ANTHROPIC_API_KEY and fatals without it).
# This cloud backend has no API key. ``kb accept`` stores the raw article +
# rebuilds the BM25 search index with NO LLM call (kb.go:cmdAccept), so the
# mail/calendar text is searchable without a key. We therefore write every
# item through ``_kb_accept`` (the missing keyless wrapper — agents.knowledge
# .KnowledgeService only exposes the ``ingest`` path).
#
# Per-user isolation (the load-bearing invariant)
# ------------------------------------------------
# Every write targets ``user:{member_id}`` where ``member_id`` is the clean
# opaque cloud user id — the SAME value chat.agent_service uses verbatim as a
# kb-go scope (opaque ids can't alias the way two emails could under kb-go's
# ``:``→``_`` sanitize). The scope is DERIVED from ``member_id`` inside
# ``ingest_member``; there is no caller-supplied scope surface to abuse. A
# second member's ingest can never write into the first's scope because the
# scope string is a pure function of that member's id.
#
# Robustness scope (v1)
# ---------------------
# Token refresh on expiry is handled upstream by ``OAuthManager.get_valid_
# token`` (chunk 1 threaded ``user_id`` through the refresh path), so the
# per-user client refreshes transparently; an auth failure surfaces as a read
# exception which this module catches → status=error, nothing written.
# Reads are wrapped in a small bounded exponential backoff for 429/5xx. The
# sweep bounds concurrency with a semaphore so N member-syncs don't swamp the
# box. Deeper robustness (true Gmail historyId cursors, per-message dedup,
# attachment/body extraction) is noted as a follow-up — see the module
# docstring tail in __init__.py.

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import MemberIngestCompleted
from pocketpaw_ee.cloud.member_ingest.dto import (
    IngestMemberRequest,
    SweepRequest,
)

logger = logging.getLogger(__name__)

# --- Tunables (bounded by design — Phase B is "recent", not "everything") ---

# Backfill window: how far back the first run reaches. "Recent mail" per the
# PRD — last 30 days of mail, next 30 days of calendar.
_BACKFILL_DAYS = 30
# Incremental window: a small overlap re-read so a missed item on the boundary
# still lands. Bounded so a long outage re-reads days, not years.
_INCREMENTAL_DAYS = 2
# Per-source item caps so one member's huge inbox can't wedge a sweep tick.
_MAX_MAIL = 100
_MAX_EVENTS = 100
# Backoff for transient read errors (429 / 5xx). Small + bounded — a sweep
# tick must not block for minutes on one flaky member.
_BACKOFF_BASE_SECONDS = 0.5
_BACKOFF_MAX_RETRIES = 3
# Default sweep fan-out cap so N member-syncs don't swamp the box.
_DEFAULT_SWEEP_CONCURRENCY = 4

# Connector registry names that carry per-user mail/calendar data.
_GMAIL_NAME = "gmail"
_GCAL_NAME = "gcalendar"


# --------------------------------------------------------------------------
# Reader protocols — the worker depends on these narrow shapes, not on the
# concrete clients, so tests inject fakes with no network / OAuth.
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


# kb_accept(scope, articles) -> result dict. Async by contract.
KbAcceptFn = Callable[[str, list[dict[str, Any]]], Awaitable[dict[str, Any]]]
# ingest_fn(workspace_id, member_id, **kw) -> result dict. The per-member unit
# the sweep dispatches; defaults to ``ingest_member`` but injectable for tests.
IngestFn = Callable[..., Awaitable[dict[str, Any]]]


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


async def ingest_member(
    workspace_id: str,
    member_id: str,
    *,
    gmail_reader: GmailReader | None = None,
    calendar_reader: CalendarReader | None = None,
    kb_accept: KbAcceptFn | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read one member's recent mail + upcoming calendar and write it into
    their private ``user:{member_id}`` KB scope.

    The scope is derived internally as ``user:{member_id}`` and is the only
    scope this function ever writes — the per-member isolation guarantee.

    ``gmail_reader`` / ``calendar_reader`` default to the real per-user
    clients (``GmailClient(member_id)`` / ``CalendarClient(member_id)``),
    which resolve THIS member's OAuth token and refresh it on expiry. Tests
    inject fakes. ``kb_accept`` defaults to the keyless ``kb accept``
    subprocess wrapper.

    Backfill-vs-incremental is decided by the persisted
    ``MemberIngestState.backfill_done`` flag. Returns a result dict with
    ``status`` (``ok``/``error``), ``mode`` (``backfill``/``incremental``),
    ``documents``, and any per-source ``errors``.
    """
    # Validate at entry (cloud rule §6) — internal callers (the sweep, the
    # scheduler) get the same guard as an HTTP body would.
    body = IngestMemberRequest.model_validate(
        {"workspace_id": workspace_id, "member_id": member_id}
    )
    workspace_id, member_id = body.workspace_id, body.member_id

    # The scope is a pure function of the opaque member id. Nothing else.
    scope = f"user:{member_id}"
    now = now or datetime.now(UTC)
    accept = kb_accept or _default_kb_accept

    # Lazy-construct the real per-user clients only when no fake was injected.
    # Importing here keeps the module import-light and avoids pulling the
    # OSS client stack into pure-unit tests.
    if gmail_reader is None or calendar_reader is None:
        from pocketpaw.clients.gcalendar import CalendarClient
        from pocketpaw.clients.gmail import GmailClient

        gmail_reader = gmail_reader or GmailClient(user_id=member_id)
        calendar_reader = calendar_reader or CalendarClient(user_id=member_id)

    state = await _load_or_create_state(workspace_id, member_id)
    mode = "backfill" if not state.backfill_done else "incremental"
    state.status = "running"
    await state.save()  # no-event: transient in-progress marker, not a domain change

    errors: list[str] = []
    documents = 0

    # --- Gmail ---
    try:
        articles = await _read_gmail(
            gmail_reader,
            mode=mode,
            cursor=state.gmail_cursor,
            now=now,
        )
        if articles:
            await accept(scope, articles)
            documents += len(articles)
            # Advance the high-water cursor to this run's wall clock. We use
            # the run timestamp (not per-message dates, which Gmail returns in
            # non-uniform header formats) as a coarse but monotonic watermark.
            state.gmail_cursor = now.isoformat()
    except Exception as exc:  # noqa: BLE001 — isolate per-source so one bad
        # source never sinks the other or crashes the sweep.
        logger.warning(
            "member_ingest: gmail read failed for member=%s ws=%s: %s",
            member_id,
            workspace_id,
            exc,
        )
        errors.append(f"gmail: {exc}")

    # --- Calendar ---
    try:
        articles = await _read_calendar(
            calendar_reader,
            mode=mode,
            now=now,
        )
        if articles:
            await accept(scope, articles)
            documents += len(articles)
            state.calendar_cursor = now.isoformat()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "member_ingest: calendar read failed for member=%s ws=%s: %s",
            member_id,
            workspace_id,
            exc,
        )
        errors.append(f"calendar: {exc}")

    # --- Persist outcome ---
    both_failed = len(errors) == 2
    status = "error" if both_failed else "ok"
    state.status = status
    state.last_error = "; ".join(errors)
    state.documents_ingested += documents
    if status == "ok":
        # Only flip backfill_done on a run that actually completed without a
        # total failure — a failed backfill retries (wide window) next time.
        state.backfill_done = True
        state.last_sync_at = now
    await state.save()  # no-event: domain event emitted explicitly below.

    await emit(
        MemberIngestCompleted(
            data={
                "workspace_id": workspace_id,
                "member_id": member_id,
                "scope": scope,
                "mode": mode,
                "status": status,
                "documents": documents,
            }
        )
    )

    return {
        "workspace_id": workspace_id,
        "member_id": member_id,
        "scope": scope,
        "mode": mode,
        "status": status,
        "documents": documents,
        "errors": errors,
    }


async def list_connected_members(workspace_id: str | None = None) -> list[dict[str, str]]:
    """Enumerate (workspace, member) pairs that have a per-user Gmail/Calendar
    connector enabled.

    Returns one entry per (workspace, member) even when a member connected
    BOTH Gmail and Calendar — the worker ingests both sources in a single
    ``ingest_member`` call, so we don't want to dispatch them twice.

    Tenant filter (cloud rule §7): when ``workspace_id`` is given, the Beanie
    query pins it; the global form (``None``) is the scheduler's cross-tenant
    sweep and is explicitly intended.
    """
    # service.py is the only legal reader of its own model class (cloud
    # rule §2). WorkspaceConnector is owned by connectors/service.py; we read
    # it cross-entity here, which the connector service already exposes via a
    # listing helper — but that helper returns Response DTOs that drop the
    # ``scope``/``user_id`` we need. Reading the doc directly with the tenant
    # filter is the minimal correct path; flagged for a future
    # ``connectors.service.list_user_connectors`` extraction.
    from pocketpaw_ee.cloud.models.connector import WorkspaceConnector

    query: dict[str, Any] = {
        "scope": "user",
        "enabled": True,
        "name": {"$in": [_GMAIL_NAME, _GCAL_NAME]},
    }
    if workspace_id:
        query["workspace"] = workspace_id  # tenant filter
    # else: global-read — the scheduler sweeps every tenant's connected members

    docs = await WorkspaceConnector.find(query).to_list()

    seen: set[tuple[str, str]] = set()
    members: list[dict[str, str]] = []
    for d in docs:
        if not d.user_id:
            continue
        key = (d.workspace, d.user_id)
        if key in seen:
            continue
        seen.add(key)
        members.append({"workspace_id": d.workspace, "member_id": d.user_id})
    return members


async def run_ingest_sweep(
    *,
    workspace_id: str | None = None,
    concurrency: int = _DEFAULT_SWEEP_CONCURRENCY,
    ingest_fn: IngestFn | None = None,
) -> dict[str, Any]:
    """Ingest every connected member, bounded by a concurrency cap.

    One member failing never aborts the others — each unit is isolated.
    Returns a summary dict ``{members, ok, errors}``. ``ingest_fn`` defaults
    to ``ingest_member`` (real readers + real accept); tests inject a fake.
    """
    body = SweepRequest.model_validate({"concurrency": concurrency})
    concurrency = body.concurrency
    fn = ingest_fn or ingest_member

    members = await list_connected_members(workspace_id=workspace_id)
    if not members:
        return {"members": 0, "ok": 0, "errors": 0}

    sem = asyncio.Semaphore(concurrency)
    ok = 0
    errors = 0

    async def _one(entry: dict[str, str]) -> None:
        nonlocal ok, errors
        async with sem:
            try:
                result = await fn(entry["workspace_id"], entry["member_id"])
            except Exception as exc:  # noqa: BLE001 — isolate per member so a
                # single blown token doesn't abort the whole sweep.
                logger.warning(
                    "member_ingest: sweep unit failed ws=%s member=%s: %s",
                    entry["workspace_id"],
                    entry["member_id"],
                    exc,
                )
                errors += 1
                return
            # The unit completed; count its self-reported status.
            if isinstance(result, dict) and result.get("status") == "error":
                errors += 1
            else:
                ok += 1

    await asyncio.gather(*(_one(m) for m in members))
    logger.info(
        "member_ingest: sweep complete members=%d ok=%d errors=%d (ws=%s)",
        len(members),
        ok,
        errors,
        workspace_id or "ALL",
    )
    return {"members": len(members), "ok": ok, "errors": errors}


# --------------------------------------------------------------------------
# Reads → accept articles
# --------------------------------------------------------------------------


async def _read_gmail(
    reader: GmailReader,
    *,
    mode: str,
    cursor: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """Search recent mail and shape each message into an accept article.

    Backfill uses ``newer_than:30d`` (the wide window); incremental uses the
    narrow ``newer_than:2d`` overlap. Gmail's ``newer_than`` is the simplest
    bounded-recency filter that works without a historyId cursor — true
    incremental sync via historyId is a documented follow-up.
    """
    days = _BACKFILL_DAYS if mode == "backfill" else _INCREMENTAL_DAYS
    query = f"newer_than:{days}d"
    messages = await _with_backoff(lambda: reader.search(query, max_results=_MAX_MAIL))

    articles: list[dict[str, Any]] = []
    for m in messages:
        subject = (m.get("subject") or "(no subject)").strip()
        sender = (m.get("from") or "").strip()
        date = (m.get("date") or "").strip()
        snippet = (m.get("snippet") or "").strip()
        body = m.get("body") or snippet
        content_lines = [
            f"From: {sender}",
            f"Date: {date}",
            "",
            body,
        ]
        articles.append(
            {
                "title": f"Email: {subject}",
                "content": "\n".join(content_lines).strip() or subject,
                "summary": snippet[:200],
                "source": f"gmail:{m.get('id', '')}",
                "categories": ["email", "gmail"],
            }
        )
    return articles


async def _read_calendar(
    reader: CalendarReader,
    *,
    mode: str,
    now: datetime,
) -> list[dict[str, Any]]:
    """List upcoming events and shape each into an accept article.

    Calendar is forward-looking ("upcoming") — the window is ``now`` →
    ``now + 30d`` for both backfill and incremental (the set of upcoming
    events is small and re-reading it each tick keeps the KB fresh as events
    are added/changed; accept upserts by title slug so re-ingest is cheap).
    """
    time_min = now
    time_max = now + timedelta(days=_BACKFILL_DAYS)
    events = await _with_backoff(
        lambda: reader.list_events(
            time_min=time_min,
            time_max=time_max,
            max_results=_MAX_EVENTS,
        )
    )

    articles: list[dict[str, Any]] = []
    for e in events:
        summary = (e.get("summary") or "(no title)").strip()
        start = (e.get("start") or "").strip()
        end = (e.get("end") or "").strip()
        location = (e.get("location") or "").strip()
        description = (e.get("description") or "").strip()
        attendees = e.get("attendees") or []
        content_lines = [
            f"When: {start} — {end}",
            f"Where: {location}",
            f"Attendees: {', '.join(attendees)}",
            "",
            description,
        ]
        articles.append(
            {
                "title": f"Event: {summary}",
                "content": "\n".join(content_lines).strip() or summary,
                "summary": description[:200] or summary,
                "source": f"gcalendar:{e.get('id', '')}",
                "categories": ["calendar", "event"],
            }
        )
    return articles


# --------------------------------------------------------------------------
# Backoff + state helpers
# --------------------------------------------------------------------------


async def _with_backoff(coro_factory: Callable[[], Awaitable[Any]]) -> Any:
    """Run an async read with bounded exponential backoff on transient errors.

    Retries on 429 / 5xx-shaped failures (detected from the exception text,
    since the per-user clients raise ``httpx.HTTPStatusError`` whose str
    carries the status). Auth failures (RuntimeError "not authenticated")
    are NOT retried — they need re-consent, not a retry, so they propagate
    immediately to be recorded as an error.
    """
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except Exception as exc:  # noqa: BLE001
            text = str(exc).lower()
            transient = (
                "429" in text
                or "rate" in text
                or "500" in text
                or "502" in text
                or "503" in text
                or "504" in text
                or "timeout" in text
                or "timed out" in text
            )
            if not transient or attempt >= _BACKOFF_MAX_RETRIES:
                raise
            delay = _BACKOFF_BASE_SECONDS * (2**attempt)
            logger.info(
                "member_ingest: transient read error (attempt %d/%d), backing off %.1fs: %s",
                attempt + 1,
                _BACKOFF_MAX_RETRIES,
                delay,
                exc,
            )
            await asyncio.sleep(delay)
            attempt += 1


async def _load_or_create_state(workspace_id: str, member_id: str):
    """Fetch the per-member ingest state, creating a fresh row on first run.

    Tenant filter on the read (cloud rule §7): both ``workspace`` and
    ``member_id`` pin the row so two members never share state and no
    cross-tenant row is ever touched.
    """
    from pocketpaw_ee.cloud.models.member_ingest_state import MemberIngestState

    state = await MemberIngestState.find_one(
        MemberIngestState.workspace == workspace_id,
        MemberIngestState.member_id == member_id,
    )
    if state is None:
        state = MemberIngestState(workspace=workspace_id, member_id=member_id)
        await state.insert()
    return state


# --------------------------------------------------------------------------
# The keyless ``accept`` subprocess wrapper (the missing path)
# --------------------------------------------------------------------------


async def _default_kb_accept(scope: str, articles: list[dict[str, Any]]) -> dict[str, Any]:
    """Write articles into a kb-go scope via the KEYLESS ``kb accept`` path.

    ``KnowledgeService.ingest_text_to_scope`` uses ``kb ingest`` which
    LLM-compiles via Anthropic — unusable here (no API key). ``kb accept``
    reads ``{"scope": ..., "articles": [...]}`` on stdin, stores each raw
    article, and rebuilds the BM25 index with no LLM call (kb.go:cmdAccept).
    We pass ``--scope`` explicitly AND in the payload (cmdAccept honors the
    flag first) so the scope is unambiguous.
    """
    import subprocess

    from pocketpaw_ee.cloud.agents.knowledge import KB_BIN

    payload = json.dumps({"scope": scope, "articles": articles})

    def _run() -> dict[str, Any]:
        try:
            result = subprocess.run(
                [KB_BIN, "accept", "--scope", scope, "--json"],
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"kb binary not found at {KB_BIN!r}. Install kb-go or set POCKETPAW_KB_BIN."
            ) from exc
        if result.returncode != 0:
            raise RuntimeError(f"kb accept failed: {result.stderr[:200]}")
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"raw": result.stdout.strip()}

    return await asyncio.to_thread(_run)


__all__ = [
    "ingest_member",
    "list_connected_members",
    "run_ingest_sweep",
]
