# service.py — Cloud calendar entity service.
#
# Updated: 2026-08-06 (feat/coupling-calendar-sot, T-13) — ``list_upcoming``
# is now a PROJECTION over the canonical calendar store
# (``pocketpaw_ee.calendar``), not a direct Composio read. One source of
# truth: the agent's "your upcoming events" preamble and the /calendar
# page read the same ``_EventDoc`` rows, so bridge-minted meetings,
# natively synced gcalendar events, and Composio-fetched events all show
# up in both places, deduped by the Google event id.
#
# Data path per call:
#   1. Validate tenancy + bounds (unchanged — refuse, don't degrade).
#   2. FRESHNESS — sync-on-read with a TTL. If Composio is enabled and
#      this (workspace, user) hasn't refreshed within
#      ``_REFRESH_TTL_SECONDS``, fetch ``GOOGLECALENDAR_LIST_EVENTS``
#      once and reconcile the rows into the store via
#      ``calendar.sync.ingest_composio_events`` (source_connector
#      "composio_google", cross-connector deduped against "gcalendar").
#      Any failure here is swallowed: the store still serves. Tradeoff
#      chosen deliberately: ee/calendar/sync provides on-demand pulls and
#      no scheduler, and the audit forbids new background services, so
#      sync-on-read is the reuse path. Worst-case staleness = the TTL
#      (5 min) for upstream edits; local writes (bridge, /calendar UI)
#      are visible immediately since reads hit the store directly. The
#      attempt timestamp is recorded on failure too, so a Composio outage
#      costs at most one failed round-trip per TTL window, not one per
#      preamble build.
#   3. PROJECT — query ``calendar.service.list_events`` (workspace-
#      filtered, per-calendar policy applied) for the window [now,
#      now + 30d], map to the ``CalendarEvent`` wire dataclass, emit wire
#      dicts. The dataclass stays the preamble's wire shape; only its
#      construction source changed.
#
# Privacy (review round 2): the Composio connection is PER-USER, so the
# ingest lands one member's personal feed — on a per-user PRIVATE
# calendar (see calendar.sync._ensure_composio_calendar), which the
# projection's policy filtering (list_events → can_read_calendar) keeps
# out of every other member's preamble and /calendar.
#
# Field semantics that changed with T-13:
#   * ``id`` is now the CANONICAL LOCAL event id (the /calendar row id),
#     not the upstream Google id. The Google id lives on the store row as
#     ``source_external_id``. Nothing consumed the old id (the preamble
#     renders unaddressed lines), and the local id is the one /calendar
#     and future tools can address.
#   * ``source`` maps from the row's ``source_connector``:
#     google-family connectors → "google"; other connectors pass through;
#     no connector (native /calendar or bridge-minted rows) → "local".
#   * ``start`` / ``end`` render EVENT-LOCAL (store UTC converted through
#     the row's IANA timezone) so the preamble shows wall-clock time —
#     matching the old Composio pass-through. All-day events render
#     date-only, matching Google's ``start.date`` shape.
#
# Failure modes — all degrade to serving what the store has (or ``[]``):
#   * Composio disabled                       → store contents (no refresh)
#   * SDK missing / upstream error / timeout  → store contents (stale ok)
#   * Store unavailable (no Beanie init)      → ``[]``
#   * Cross-workspace query (workspace empty) → ``ValidationError``

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any

from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.calendar.domain import CalendarEvent
from pocketpaw_ee.cloud.calendar.dto import CalendarEventResponse

logger = logging.getLogger(__name__)


# Composio action slug for Google Calendar's events.list. Pinned here so
# the action name has a single canonical home — if the upstream slug
# ever renames, this is the one line to update.
_GOOGLECALENDAR_LIST_EVENTS = "GOOGLECALENDAR_LIST_EVENTS"

# Source tag stamped onto events that came from a Google-family
# connector (native gcalendar OAuth or Composio). Independent of the
# connector slug so future providers (ical, outlook) can be added
# without renaming the field.
_SOURCE_GOOGLE = "google"

# Source tag for events with no external connector: created on /calendar
# directly, or minted by the meetings bridge.
_SOURCE_LOCAL = "local"

# Sync-on-read cadence. Within this window repeated preamble builds read
# the store without touching Composio. See the freshness note in the
# module header for the tradeoff.
_REFRESH_TTL_SECONDS = 300.0

# How many rows to ask Composio for per refresh. Decoupled from the
# caller's ``limit`` (a preamble asking for 10 shouldn't starve the
# store of the rest of the week).
_REFRESH_FETCH_LIMIT = 50

# How far ahead the "upcoming" projection looks. Matches the native
# gcalendar pull's 30-day horizon in ``calendar.sync``.
_UPCOMING_WINDOW_DAYS = 30

# (workspace_id, user_id) → monotonic timestamp of the last refresh
# ATTEMPT. In-process only — a restart simply refreshes on first read.
_last_refresh: dict[tuple[str, str], float] = {}

# Local-midnight sentinel for the all-day rendering heuristic.
_MIDNIGHT = dt_time(0, 0, 0)


def _reset_refresh_cache() -> None:
    """Test hook — clear the sync-on-read TTL memory."""
    _last_refresh.clear()


async def list_upcoming(workspace_id: str, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return wire dicts of the workspace's next ``limit`` calendar events.

    Projection over the canonical calendar store — the same rows
    /calendar lists — refreshed from Composio at most once per TTL
    window. Workspace-scoped; per-calendar read policy applied by the
    underlying ``calendar.service.list_events``. Empty list is the
    graceful-degradation signal — the caller decides what to render
    when nothing comes back.

    Raises ``ValidationError`` only when ``workspace_id`` / ``user_id``
    is empty or ``limit`` is non-positive. All other failure modes
    (Composio disabled, SDK missing, upstream error, store unavailable)
    degrade to whatever the store can serve, down to ``[]``.
    """
    # Tenancy + bounds guard — refuse to issue the call when the basic
    # invariants aren't met. Mirrors the pattern other cloud services
    # use (validate-at-entry per Rule 6 of the entity rules).
    if not workspace_id:
        raise ValidationError(
            "calendar.workspace_required",
            "workspace_id is required for calendar reads",
        )
    if not user_id:
        raise ValidationError(
            "calendar.user_required",
            "user_id is required for calendar reads",
        )
    if limit <= 0:
        raise ValidationError(
            "calendar.invalid_limit",
            "limit must be a positive integer",
        )

    # Freshness — sync-on-read, TTL-gated, never raises.
    await _maybe_refresh_from_composio(workspace_id, user_id)

    # Projection — the store is the source of truth.
    try:
        events = await _upcoming_from_store(workspace_id, user_id, limit)
    except Exception:
        # Store unavailable (Beanie not initialized in this deploy shape,
        # or an internal query error) — degrade to empty; the handler
        # renders its hint text.
        logger.debug("calendar.list_upcoming: store projection failed", exc_info=True)
        return []

    # Pydantic round-trip: domain → response → dict. Same shape as the
    # canonical pockets/cycles wire path (Rule 8: mapping via Pydantic,
    # not hand-rolled helpers).
    return [
        CalendarEventResponse.model_validate(ev, from_attributes=True).model_dump() for ev in events
    ]


# ---------------------------------------------------------------------------
# Store projection — private to this module.
# ---------------------------------------------------------------------------


async def _upcoming_from_store(workspace_id: str, user_id: str, limit: int) -> list[CalendarEvent]:
    """Query the canonical calendar store for the upcoming window and map
    rows to the wire dataclass.

    Lazy import — ``pocketpaw_ee.calendar`` pulls its router/service
    graph; keep that off this module's import path the same way the
    Composio import is deferred.
    """
    from pocketpaw_ee.calendar._context import RequestContext as CalendarContext
    from pocketpaw_ee.calendar.dto import ListEventsRequest
    from pocketpaw_ee.calendar.service import list_events

    ctx = CalendarContext(workspace_id=workspace_id, user_id=user_id)
    # Naive UTC boundaries — matches the router's FastAPI default;
    # list_events stamps tzinfo=UTC before comparing with the
    # timezone-aware datetimes its mapper produces.
    now = datetime.now(UTC).replace(tzinfo=None)
    body = ListEventsRequest(
        starts_after=now,
        starts_before=now + timedelta(days=_UPCOMING_WINDOW_DAYS),
        limit=min(max(limit, 1), 500),
    )
    listed = await list_events(ctx, body)
    return [_event_from_response(ev, workspace_id=workspace_id) for ev in listed.events]


def _event_from_response(ev: Any, *, workspace_id: str) -> CalendarEvent:
    """Map one ``calendar.dto.EventResponse`` onto the preamble's wire
    dataclass. Tenancy is re-tagged from the requesting workspace at
    construction (the domain object refuses to build without it).

    ``start`` / ``end`` render EVENT-LOCAL: the store keeps UTC datetimes
    plus the event's IANA ``timezone``, and the preamble formats
    time-of-day straight off the string — emitting UTC here would show a
    10:30 IST meeting as "5:00 AM". This matches the old Composio
    pass-through behaviour (Google sent local-offset dateTimes).
    All-day events (midnight-to-midnight, whole-day multiples in the
    event's zone) render date-only so the handler's date branch fires,
    matching Google's ``start.date`` wire shape.
    """
    tz = _zone_for(getattr(ev, "timezone", None))
    if _is_all_day(ev.starts_at, ev.ends_at, tz):
        start = _as_utc(ev.starts_at).astimezone(tz).date().isoformat() if ev.starts_at else ""
        end = _as_utc(ev.ends_at).astimezone(tz).date().isoformat() if ev.ends_at else ""
    else:
        start = _render_local_iso(ev.starts_at, tz)
        end = _render_local_iso(ev.ends_at, tz)
    return CalendarEvent(
        id=str(ev.id),
        workspace_id=workspace_id,
        title=ev.title,
        start=start,
        end=end,
        source=_source_for_connector(ev.source_connector),
        attendees=[str(a.email) for a in (ev.attendees or [])],
    )


def _zone_for(tz_name: Any) -> Any:
    """Resolve the row's IANA timezone, falling back to UTC on anything
    unresolvable — a bad zone must degrade the rendering, not the read."""
    from zoneinfo import ZoneInfo

    if isinstance(tz_name, str) and tz_name:
        try:
            return ZoneInfo(tz_name)
        except Exception:  # noqa: BLE001 — zoneinfo raises several types
            logger.debug("calendar.list_upcoming: unknown timezone %r", tz_name)
    return UTC


def _as_utc(dt: datetime) -> datetime:
    """Stamp naive datetimes as UTC (the store's canonical zone)."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


def _render_local_iso(dt: datetime | None, tz: Any) -> str:
    """ISO string in the event's own timezone (UTC store → local wall time)."""
    if dt is None:
        return ""
    return _as_utc(dt).astimezone(tz).isoformat()


def _is_all_day(starts_at: datetime | None, ends_at: datetime | None, tz: Any) -> bool:
    """Heuristic all-day detection: both bounds at local midnight and the
    span a whole multiple of 24h. Google all-day events arrive as
    ``{"date": ...}`` blocks and are stored as midnights, so this
    round-trips them back to date-only strings. A genuinely timed
    midnight-to-midnight event renders date-only too — acceptable, it IS
    a whole-day block."""
    if starts_at is None or ends_at is None:
        return False
    local_start = _as_utc(starts_at).astimezone(tz)
    local_end = _as_utc(ends_at).astimezone(tz)
    if local_start.time() != _MIDNIGHT or local_end.time() != _MIDNIGHT:
        return False
    span = local_end - local_start
    day = timedelta(days=1)
    return span >= day and span % day == timedelta(0)


def _source_for_connector(connector: str | None) -> str:
    """Collapse a store row's ``source_connector`` to the wire ``source``
    slug: google-family connectors → "google"; other connectors pass
    through as-is; no connector → "local" (native + bridge-minted)."""
    if connector is None or connector == "":
        return _SOURCE_LOCAL
    from pocketpaw_ee.calendar.sync import _GOOGLE_CONNECTOR_FAMILY

    if connector in _GOOGLE_CONNECTOR_FAMILY:
        return _SOURCE_GOOGLE
    return connector


# ---------------------------------------------------------------------------
# Composio refresh — private to this module.
# ---------------------------------------------------------------------------


async def _maybe_refresh_from_composio(workspace_id: str, user_id: str) -> None:
    """Sync-on-read: pull the user's Google calendar through Composio and
    reconcile into the store, at most once per TTL window. Never raises.

    The attempt timestamp is stamped up front so a hard upstream outage
    costs one failed round-trip per window, not one per preamble build.
    Composio-disabled deploys skip without stamping (the check is cheap
    and a mid-session enable should take effect immediately).
    """
    key = (workspace_id, user_id)
    last = _last_refresh.get(key)
    if last is not None and (time.monotonic() - last) < _REFRESH_TTL_SECONDS:
        return

    # Lazy import composio service — it pulls the upstream SDK behind
    # ``_get_client`` and we want a fast-path "disabled" return without
    # paying the import cost on cold paths.
    try:
        from pocketpaw_ee.cloud.composio import service as composio_service
    except Exception:
        logger.debug("calendar.refresh: composio service import failed", exc_info=True)
        return

    if not composio_service.is_enabled():
        # Composio not configured — nothing to refresh from. The store
        # still serves native + bridge-minted events.
        return

    _last_refresh[key] = time.monotonic()

    # Build a RequestContext so ``composio_user_id`` can namespace the
    # call the same way every other Composio-using surface does. The
    # context never leaves this function — it's a transport-layer shim.
    ctx = RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="calendar-refresh",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )

    try:
        namespaced = composio_service.composio_user_id(ctx)
        client = await composio_service._get_client()
    except Exception:
        # Most likely paths: ValidationError (composio.disabled raced
        # is_enabled), Internal (sdk_missing). Either way: stale-ok.
        logger.debug("calendar.refresh: composio init failed", exc_info=True)
        return

    args = {"maxResults": _REFRESH_FETCH_LIMIT}
    try:
        result = await asyncio.to_thread(
            _execute_list_events_sync,
            client,
            _GOOGLECALENDAR_LIST_EVENTS,
            str(namespaced),
            args,
        )
    except Exception:
        # Network errors, "no connected account" errors from Composio,
        # 5xx from Google — all stale-ok. The store serves what it has.
        logger.debug("calendar.refresh: GOOGLECALENDAR_LIST_EVENTS failed", exc_info=True)
        return

    items = _extract_items(result)
    if not items:
        return

    try:
        from pocketpaw_ee.calendar._context import RequestContext as CalendarContext
        from pocketpaw_ee.calendar.sync import ingest_composio_events

        touched = await ingest_composio_events(
            CalendarContext(workspace_id=workspace_id, user_id=user_id),
            items,
        )
        logger.debug("calendar.refresh: reconciled %d composio events", touched)
    except Exception:
        # Store write failure — the read path below still serves
        # whatever the store already had.
        logger.debug("calendar.refresh: ingest failed", exc_info=True)


def _execute_list_events_sync(
    client: Any, action: str, user_id: str, arguments: dict[str, Any]
) -> Any:
    """Synchronous wrapper for ``client.tools.execute`` — for ``to_thread``.

    Composio's ``tools.execute`` is a blocking call in every SDK
    version we ship against; running it on the event loop would freeze
    the chat path for the duration of the upstream round-trip. We
    delegate to the default executor exactly like the identity probe
    does (see ``composio/identity.py::probe_identity_sync``).
    """
    return client.tools.execute(action, user_id=user_id, arguments=arguments)


def _extract_items(result: Any) -> list[dict[str, Any]]:
    """Pull the ``items`` array out of Composio's response envelope.

    Composio wraps results as either ``{"data": {...}, "successful":
    True}`` or a pydantic model with those same attrs depending on
    minor SDK version. We tolerate both and fall back to an empty
    list on any unexpected shape — the caller treats that the same
    as "no events".
    """
    data: Any = None
    if isinstance(result, dict):
        data = result.get("data")
    else:
        data = getattr(result, "data", None)
        if data is None and hasattr(result, "model_dump"):
            try:
                dumped = result.model_dump()
            except Exception:  # noqa: BLE001
                return []
            if isinstance(dumped, dict):
                data = dumped.get("data") or dumped

    if isinstance(data, dict):
        items = data.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


__all__ = ["list_upcoming"]
