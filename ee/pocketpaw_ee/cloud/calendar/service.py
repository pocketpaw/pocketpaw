# service.py — Cloud calendar entity service.
#
# Created: 2026-05-24 (feat/calendar-entity-surface, #1214) —
# ``list_upcoming(workspace_id, user_id, limit=10)`` returns wire dicts
# for the next N events on the user's connected calendar. Reads only;
# no Beanie touches; no router.
#
# Today's data path:
#   1. Gate on ``composio_service.is_enabled()``. If Composio isn't
#      configured, return ``[]`` quietly so the surface handler can
#      fall back to its hint text.
#   2. Build the namespaced ``user_id`` via ``composio_user_id`` so
#      cross-tenant calls collide at the Composio layer (defense in
#      depth — the ``workspace_id`` filter at our layer is the primary
#      guard).
#   3. Call ``GOOGLECALENDAR_LIST_EVENTS`` through ``tools.execute``.
#      The action wraps Google's standard ``events.list`` endpoint;
#      we pass ``maxResults`` so the upstream limit matches ours.
#   4. Parse Google's response shape (``items[]`` with ``start.dateTime``
#      / ``start.date`` etc.), tag every event with the requesting
#      ``workspace_id`` at construction time, and emit wire dicts.
#
# Failure modes — all degrade to ``[]`` (never raise to the handler):
#   * Composio disabled                                 → ``[]``
#   * SDK missing                                       → ``[]``
#   * User hasn't connected a calendar                  → ``[]``
#   * Upstream timeout / 5xx / parse error              → ``[]``
#   * Cross-workspace query (workspace_id empty)        → ``ValidationError``
#
# The handler always sees a list — it decides whether to render the
# events block or the Composio hint based on len(events). The only
# raised error is the tenancy guard (the same pattern other cloud
# services use to refuse a missing workspace).

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
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

# Source tag stamped onto every event the Composio path produces.
# Independent of the toolkit slug so future providers (ical, outlook)
# can be added without renaming the field.
_SOURCE_GOOGLE = "google"


async def list_upcoming(workspace_id: str, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Return wire dicts of the user's next ``limit`` calendar events.

    Read-only — no writes, no events emitted. Workspace-scoped: each
    event domain object is tagged with ``workspace_id`` at construction
    so the caller can fan results across workspaces without crossing
    tenancy. Empty list is the graceful-degradation signal — the caller
    decides what to render when nothing comes back.

    Raises ``ValidationError`` only when ``workspace_id`` is empty or
    ``limit`` is non-positive. All other failure modes (Composio
    disabled, SDK missing, upstream error, parse failure) return ``[]``
    so the surface handler can fall back to its hint text without
    needing to catch.
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

    # Lazy import composio service — it pulls the upstream SDK behind
    # ``_get_client`` and we want a fast-path "disabled" return without
    # paying the import cost on cold paths.
    try:
        from pocketpaw_ee.cloud.composio import service as composio_service
    except Exception:
        logger.debug("calendar.list_upcoming: composio service import failed", exc_info=True)
        return []

    if not composio_service.is_enabled():
        # Composio not configured at all — the handler will render the
        # static hint instead. No error: this is the expected state for
        # any deploy that hasn't wired Composio yet.
        return []

    # Build a RequestContext so ``composio_user_id`` can namespace the
    # call the same way every other Composio-using surface does. The
    # context never leaves this function — it's a transport-layer shim.
    ctx = RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="calendar-list-upcoming",
        scope=ScopeKind.WORKSPACE,
        started_at=datetime.now(UTC),
    )

    try:
        namespaced = composio_service.composio_user_id(ctx)
        client = await composio_service._get_client()
    except Exception:
        # Most likely paths: ValidationError (composio.disabled raced
        # is_enabled), Internal (sdk_missing). Either way: graceful empty.
        logger.debug("calendar.list_upcoming: composio init failed", exc_info=True)
        return []

    # Cap the upstream limit at our limit. Composio honors maxResults
    # as a hint; if the upstream returns more we still trim below.
    args = {"maxResults": int(limit)}

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
        # 5xx from Google — all degrade to empty. The handler renders
        # its fallback hint and the chat continues.
        logger.debug("calendar.list_upcoming: GOOGLECALENDAR_LIST_EVENTS failed", exc_info=True)
        return []

    items = _extract_items(result)
    events: list[CalendarEvent] = []
    for raw in items[:limit]:
        event = _event_from_google_item(raw, workspace_id=workspace_id)
        if event is not None:
            events.append(event)

    # Pydantic round-trip: domain → response → dict. Same shape as the
    # canonical pockets/cycles wire path (Rule 8: mapping via Pydantic,
    # not hand-rolled helpers).
    return [
        CalendarEventResponse.model_validate(ev, from_attributes=True).model_dump() for ev in events
    ]


# ---------------------------------------------------------------------------
# Composio helpers — private to this module.
# ---------------------------------------------------------------------------


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


def _event_from_google_item(raw: dict[str, Any], *, workspace_id: str) -> CalendarEvent | None:
    """Build a tenant-tagged ``CalendarEvent`` from one Google API item.

    Returns ``None`` when the upstream payload is missing the bare
    minimum we need (``id``) — better to skip a single bad row than
    surface a partial event with confusing fields. ``start`` /
    ``end`` collapse Google's date vs dateTime variants into a single
    string field (the date or the dateTime, whichever is present).
    """
    event_id = raw.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None

    start_raw = raw.get("start") or {}
    end_raw = raw.get("end") or {}
    start = ""
    end = ""
    if isinstance(start_raw, dict):
        start = str(start_raw.get("dateTime") or start_raw.get("date") or "")
    if isinstance(end_raw, dict):
        end = str(end_raw.get("dateTime") or end_raw.get("date") or "")

    attendees_raw = raw.get("attendees") or []
    attendees: list[str] = []
    if isinstance(attendees_raw, list):
        for a in attendees_raw:
            if isinstance(a, dict):
                email = a.get("email")
                if isinstance(email, str) and email:
                    attendees.append(email)

    return CalendarEvent(
        id=event_id,
        workspace_id=workspace_id,
        title=str(raw.get("summary") or "(no title)"),
        start=start,
        end=end,
        source=_SOURCE_GOOGLE,
        attendees=attendees,
    )


__all__ = ["list_upcoming"]
