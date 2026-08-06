# domain.py — Cloud calendar entity value objects.
#
# Updated: 2026-08-06 (feat/coupling-calendar-sot, T-13) — events are now
# built from the canonical calendar store (``pocketpaw_ee.calendar``)
# instead of raw Composio payloads. ``id`` is the canonical local event
# id (the /calendar row id); the upstream Google id lives on the store
# row as ``source_external_id``. ``source`` gains the "local" slug for
# rows with no external connector (native /calendar or bridge-minted).
#
# Created: 2026-05-24 (feat/calendar-entity-surface, #1214) — frozen
# dataclass with workspace_id required at construction. Mirrors the
# ee/cloud rule: domain enforces multi-tenancy at construction time so
# the workspace tag is impossible to forget. Field order keeps
# workspace_id first after id, so any positional construction lands
# tenancy in front; missing workspace_id is a TypeError, not silent.
#
# ISO-string start/end (not datetime) — the preamble renders strings and
# does no date math, so parsing would be pure overhead.

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CalendarEvent:
    """A single upcoming calendar event scoped to a workspace.

    Tenancy is enforced at construction — ``workspace_id`` is required
    positionally with no default. Constructing a ``CalendarEvent``
    without one is a TypeError. Same rule as the rest of ``ee/cloud``.

    Fields:
      * ``id`` — the canonical local event id (the /calendar row id).
        The upstream Google event id, when there is one, lives on the
        store row as ``source_external_id``.
      * ``workspace_id`` — owning workspace. Tagged at construction so
        downstream code can fan out events across workspaces without
        accidentally bleeding one tenant into another.
      * ``title`` — event title. Defaults only to a placeholder when
        the upstream payload omitted it at ingest time.
      * ``start`` / ``end`` — ISO 8601 strings rendered from the store's
        UTC datetimes. Empty string when missing.
      * ``attendees`` — list of email strings. Optional; defaults to
        an empty list. Each entry is the raw email — no normalization
        is applied at this layer (downstream services may dedupe).
      * ``source`` — upstream system slug: ``"google"`` for google-family
        connectors (native gcalendar OAuth or Composio), ``"local"`` for
        rows with no external connector (native /calendar creates and
        bridge-minted meetings), other connector slugs pass through.
    """

    id: str
    workspace_id: str
    title: str
    start: str
    end: str
    source: str
    attendees: list[str] = field(default_factory=list)


__all__ = ["CalendarEvent"]
