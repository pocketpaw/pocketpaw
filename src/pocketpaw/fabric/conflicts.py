# fabric/conflicts.py — the open-conflict surface for the lifecycle slice (FST-6).
# Created: 2026-07-10 (feat/fst-6-stewardship) — recompute, don't persist.
#
# ``detect_open_conflicts(store)`` scans the tracked properties (only objects
# WITH statements, via ``FabricStore.list_statement_keys``) and returns a
# :class:`ConflictRecord` for every property whose CURRENT resolution is
# ``unresolvable=True`` — the un-rankable残り the trust ladder cannot order
# (same tier, same rank, both open validity, materially different values,
# observed within the recency epsilon). Policy auto-resolves everything it CAN
# rank; only these records ever reach a human.
#
# Design choices:
#   * NO conflicts table. A conflict is recomputed from statements via the
#     FST-2 resolver on every scan — the statements ARE the state, so a
#     conflict "closes" the moment a steward verb (PIN/IGNORE), a curation
#     verb (CHANGE/CORRECT), or a new higher-tier observation changes the
#     resolution. Nothing to invalidate, nothing to drift.
#   * ``rivals`` carries exactly the statements that TRIGGERED the
#     un-rankable flag (the resolver's own condition, re-applied here via the
#     resolver's private helpers — intra-package reuse, the same deliberate
#     pattern store.py uses for ``_materially_different``). Lower-tier /
#     closed / same-value losers are policy-ranked history, not choices a
#     human needs to arbitrate.
#   * The dedupe key is ``(workspace_id, object_id, property)`` — the EE
#     stewardship glue uses it to guarantee ONE open Instinct proposal per
#     conflicted property across re-sweeps. ``workspace_id`` on the record is
#     the CALLER'S scope (the tenancy the proposal will be filed under), not
#     any individual statement's stamp — legacy NULL-stamped statements are
#     visible inside a workspace scope and must dedupe under it.
#
# Pure read path: this module never writes. The steward verbs that ANSWER a
# conflict live on the store (pin/unpin/ignore, FST-6) next to their FST-5
# siblings (change/correct).

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from pocketpaw.fabric.models import FabricObject, Statement
from pocketpaw.fabric.resolver import _materially_different, _tier, resolve
from pocketpaw.fabric.trust import TrustRules, default_trust_rules

if TYPE_CHECKING:
    from pocketpaw.fabric.store import FabricStore


class ConflictRecord(BaseModel):
    """One un-rankable conflict: a property the trust ladder cannot order.

    ``winner`` is the resolver's PROVISIONAL winner (reads never block —
    FST-2 always returns one); ``rivals`` are the open-validity, same-tier,
    same-rank, materially-different, within-epsilon contenders that made the
    resolution un-rankable. Together they are the CHOICES a steward
    arbitrates between. ``workspace_id`` is the detection scope (see the
    module header), part of the dedupe key.
    """

    object_id: str
    object_type: str = ""  # the object's type_name; "" when unknown
    property: str
    workspace_id: str | None = None
    winner: Statement
    rivals: list[Statement] = Field(default_factory=list)

    @property
    def dedupe_key(self) -> tuple[str | None, str, str]:
        """``(workspace_id, object_id, property)`` — one open proposal per key.

        The EE stewardship sweep compares these tuples against the open
        Instinct proposals' blobs so re-sweeps never file a duplicate while
        one is open.
        """
        return (self.workspace_id, self.object_id, self.property)

    @property
    def signature(self) -> list[str]:
        """Sorted ids of the competing statements (winner + rivals).

        Two scans of the SAME unresolved conflict produce the same
        signature; a new rival observation (or a steward/curation verb)
        changes it. The EE sweep uses it as reject-memory: a human's
        explicit "keep the policy winner" (reject) stands until the conflict
        materially changes shape.
        """
        return sorted([self.winner.id] + [r.id for r in self.rivals])


def _unrankable_rivals(
    winner: Statement,
    losers: list[Statement],
    rules: TrustRules,
    object_type: str | None,
) -> list[Statement]:
    """The losers that triggered ``unresolvable`` — the resolver's own
    condition (same tier, same rank, both open validity, materially
    different, observed within the recency epsilon), re-applied to name the
    contenders. Preserves the losers' best-to-worst order."""
    ladder = rules.ladder_for(object_type, winner.property)
    top_tier = _tier(winner.writer_class, ladder)
    epsilon = rules.recency_epsilon_seconds
    return [
        s
        for s in losers
        if _tier(s.writer_class, ladder) == top_tier
        and s.rank == winner.rank
        and s.valid_to is None
        and _materially_different(s.value, winner.value)
        and abs((winner.observed_at - s.observed_at).total_seconds()) <= epsilon
    ]


async def detect_open_conflicts(
    store: FabricStore,
    *,
    workspace_id: str | None = None,
    object_id: str | None = None,
    rules: TrustRules | None = None,
) -> list[ConflictRecord]:
    """Scan tracked properties and return the currently un-rankable conflicts.

    Recomputes from statements via :func:`resolve` — no persisted conflict
    state (see the module header). Only (object, property) pairs that HAVE
    statements are visited, so the scan is cheap relative to the fabric.
    Disputes the ladder CAN rank (``is_disputed`` without ``unresolvable``)
    are excluded by design: policy already answered them. Pinned properties
    never appear (a pin is authoritative — the pinned path is never
    un-rankable).

    ``workspace_id`` applies the standard W4a read scope to every underlying
    read and stamps the returned records (the dedupe-key tenancy);
    ``object_id`` narrows the scan to one object; ``rules`` overrides the
    trust rules (default: :func:`default_trust_rules`, the same set the
    store's shadow/enforce paths resolve with). Deterministic order:
    ``(object_id, property)``.
    """
    effective_rules = rules if rules is not None else default_trust_rules()
    keys = await store.list_statement_keys(workspace_id=workspace_id, object_id=object_id)

    records: list[ConflictRecord] = []
    object_cache: dict[str, FabricObject | None] = {}
    for obj_id, prop in keys:
        if obj_id not in object_cache:
            object_cache[obj_id] = await store.get_object(obj_id, workspace_id=workspace_id)
        obj = object_cache[obj_id]
        if obj is None:
            # Statements for an object outside the caller's scope (or a
            # deleted object) are not this scope's conflict to arbitrate.
            continue

        object_type = obj.type_name or ""
        statements = await store.get_statements(obj_id, prop, workspace_id=workspace_id)
        resolution = resolve(statements, effective_rules, object_type=object_type or None)
        if not resolution.unresolvable or resolution.winner_statement is None:
            continue
        winner = resolution.winner_statement
        rivals = _unrankable_rivals(winner, resolution.losers, effective_rules, object_type or None)
        records.append(
            ConflictRecord(
                object_id=obj_id,
                object_type=object_type,
                property=prop,
                workspace_id=workspace_id,
                winner=winner,
                rivals=rivals,
            )
        )
    return records


__all__ = ["ConflictRecord", "detect_open_conflicts"]
