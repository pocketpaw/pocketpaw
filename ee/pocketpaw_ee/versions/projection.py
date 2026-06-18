# ee/pocketpaw_ee/versions/projection.py
# Created: 2026-06-18 (feat/branch-primitive-revert-history, BP-4) — the READ
# projection for the universal Branch primitive: it replays the append-only
# Journal's ``artifact.version.*`` events into a per-artifact history timeline.
#
# Why a projection (vs. just reading the ArtifactVersion rows): the rows are the
# durable source of truth for the *current* version log (list_versions serves
# the ordered timeline an endpoint shows), but they only carry STATE, not the
# sequence of lifecycle ACTIONS. The journal records every event —
# created / branched / merged / discarded / reverted / published — so the
# projection yields an EVENT history (what happened, in what order, by whom),
# which is what an audit / "history" view needs (BP-6's review UI, BP-7's audit
# agent). The rows answer "what versions exist now"; this answers "what happened
# to this artifact over time".
#
# Contract MIRRORS the Decision/Fabric projections (cloud/decisions/projection.py,
# src/pocketpaw/fabric/projection.py) so the read pattern is uniform:
#   * rebuild(journal_iter, since_seq=0) — full / incremental replay; resets the
#     in-memory state on a full rebuild.
#   * apply(entry) — fold one EventEntry (used inline + by rebuild).
#   * cursor — the latest seq seen, for restart catch-up.
#   * history(scope_type, scope_id, workspace_id=None) — the per-artifact,
#     scope-filtered ordered timeline (oldest → newest by seq, then ts).
#
# It is artifact-GENERIC over (scope_type, scope_id), tenant-aware (every
# ``artifact.version.*`` event stamps ``workspace:<id>`` in its scope, so a read
# can filter on workspace), and process-local (in-memory — one instance per
# org/process, same as the other projections). No SQLite store: the version log
# itself lives in Mongo (the rows), so the projection only needs the lightweight
# event history; rebuilding from the journal on demand is cheap for the bounded
# per-artifact event count.
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from soul_protocol.spec.journal import Actor, EventEntry

logger = logging.getLogger(__name__)

# The lifecycle actions the version projection folds. Anything else on the
# journal is dropped (the projection is concerned only with the Branch
# primitive's version events). Mirrors the Decision projection's tracked-action
# allowlist.
_VERSION_ACTIONS = frozenset(
    {
        "artifact.version.created",
        "artifact.version.branched",
        "artifact.version.merged",
        "artifact.version.discarded",
        "artifact.version.reverted",
        "artifact.version.published",
    }
)


@dataclass
class VersionEvent:
    """One folded version-lifecycle event — a row in the history timeline.

    ``action`` is the short verb ("created" / "branched" / "merged" /
    "discarded" / "reverted" / "published"), stripped of the
    ``artifact.version.`` prefix so a UI/audit reader gets a clean label.
    ``version_id`` / ``branch`` / ``version_no`` identify the version the event
    acted on; ``actor_id`` is who did it (the journal actor); ``payload`` keeps
    the full event payload (e.g. ``reverted_from`` on a revert) for a detail
    view. ``seq`` / ``ts`` are the ordering keys.
    """

    action: str
    scope_type: str
    scope_id: str
    workspace_id: str
    version_id: str | None
    branch: str | None
    version_no: int | None
    actor_id: str | None
    ts: datetime
    seq: int
    payload: dict[str, Any] = field(default_factory=dict)


def _scope_value(scope: list[str], prefix: str) -> str | None:
    """Pull the value of the first ``<prefix>:<value>`` tag in an event scope.

    The versions service stamps every event with
    ``[f"{scope_type}:{scope_id}", f"workspace:{workspace_id}"]`` (see
    service._scope_tags), so the artifact tag carries the scope_type as its
    prefix and the ``workspace:`` tag carries the tenant. Returns None when no
    matching tag is present.
    """
    needle = f"{prefix}:"
    for tag in scope:
        if tag.startswith(needle):
            return tag[len(needle) :]
    return None


class VersionProjection:
    """Replay ``artifact.version.*`` Journal events into a per-artifact history.

    Wire contract matches the Decision/Fabric projections:
      * one instance per process (per org)
      * ``rebuild(journal_iter, since_seq=0)`` for full / incremental replay
      * ``apply(entry)`` for inline per-event update
      * ``cursor`` surfaces the latest seen seq for restart catch-up

    State is an in-memory dict keyed by ``(scope_type, scope_id)`` → ordered list
    of :class:`VersionEvent`. ``history()`` reads it back, scope- and (optionally)
    workspace-filtered.
    """

    def __init__(self) -> None:
        # (scope_type, scope_id) → events in apply order. We keep them sorted by
        # seq on read rather than insert so an out-of-order replay still yields a
        # correctly ordered timeline.
        self._by_artifact: dict[tuple[str, str], list[VersionEvent]] = {}
        self._cursor: int = 0

    @property
    def cursor(self) -> int:
        return self._cursor

    # --- rebuild / apply ----------------------------------------------------

    def rebuild(self, journal_iter: Iterable[EventEntry], *, since_seq: int = 0) -> int:
        """Replay events. Returns the count applied.

        On a full rebuild (``since_seq == 0``) the in-memory state is reset
        first; on an incremental replay (``since_seq > 0``) events at or below
        the cursor are skipped (they were already folded). Idempotent: a folded
        event is keyed on its ``version_id`` + ``action`` so re-applying the same
        entry does not duplicate the timeline row. Mirrors the Decision
        projection's rebuild contract.
        """
        if since_seq == 0:
            self._by_artifact = {}
            self._cursor = 0

        applied = 0
        for entry in journal_iter:
            entry_seq = getattr(entry, "seq", None)
            if since_seq > 0 and entry_seq is not None and entry_seq <= since_seq:
                continue
            self.apply(entry)
            applied += 1
        return applied

    def apply(self, entry: EventEntry) -> VersionEvent | None:
        """Fold one event into the per-artifact history.

        Returns the folded :class:`VersionEvent` (for callers wiring inline
        update side-effects), or ``None`` when the event is not a tracked
        version action or lacks the scope tag needed to key it.
        """
        if entry.action not in _VERSION_ACTIONS:
            return None

        seq = getattr(entry, "seq", None) or 0
        if seq > self._cursor:
            self._cursor = seq

        payload = entry.payload or {}
        # Prefer the payload's explicit scope (the service always sets it); fall
        # back to the scope tags so an externally-produced event still folds.
        scope_type = str(payload.get("scope_type") or "")
        scope_id = str(payload.get("scope_id") or "")
        workspace_id = _scope_value(list(entry.scope), "workspace") or ""
        if not scope_type:
            # Recover scope_type/scope_id from the artifact tag (the FIRST tag,
            # ``<scope_type>:<scope_id>``) when the payload omits them.
            for tag in entry.scope:
                if tag.startswith("workspace:"):
                    continue
                head, _, tail = tag.partition(":")
                if head and tail:
                    scope_type, scope_id = head, tail
                    break
        if not (scope_type and scope_id):
            logger.debug(
                "version projection: %s without a resolvable scope — dropped", entry.action
            )
            return None

        actor: Actor | None = getattr(entry, "actor", None)
        event = VersionEvent(
            action=entry.action.removeprefix("artifact.version."),
            scope_type=scope_type,
            scope_id=scope_id,
            workspace_id=workspace_id,
            version_id=payload.get("version_id"),
            branch=payload.get("branch"),
            version_no=payload.get("version_no"),
            actor_id=getattr(actor, "id", None) if actor is not None else None,
            ts=entry.ts,
            seq=seq,
            payload=dict(payload),
        )

        bucket = self._by_artifact.setdefault((scope_type, scope_id), [])
        # Idempotence — drop a re-applied duplicate (same version_id + action +
        # seq). A rebuild from seq=0 after an incremental replay must converge.
        for existing in bucket:
            if (
                existing.version_id == event.version_id
                and existing.action == event.action
                and existing.seq == event.seq
            ):
                return existing
        bucket.append(event)
        return event

    # --- reads --------------------------------------------------------------

    def history(
        self,
        *,
        scope_type: str,
        scope_id: str,
        workspace_id: str | None = None,
    ) -> list[VersionEvent]:
        """The per-artifact event history, oldest → newest.

        Ordered by ``seq`` then ``ts`` (a stable timeline even when seqs tie or
        are absent on an older journal wheel). When ``workspace_id`` is given the
        result is tenant-filtered — a foreign workspace cannot read another
        tenant's version history through a known scope_id (the same isolation the
        version pointer reads enforce).
        """
        bucket = self._by_artifact.get((scope_type, scope_id), [])
        rows = [
            e
            for e in bucket
            if workspace_id is None or e.workspace_id == workspace_id or e.workspace_id == ""
        ]
        return sorted(rows, key=lambda e: (e.seq, e.ts))


__all__ = ["VersionEvent", "VersionProjection"]
