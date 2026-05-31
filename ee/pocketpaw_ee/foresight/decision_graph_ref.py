# ee/pocketpaw_ee/foresight/decision_graph_ref.py
# Created: 2026-05-25 (feat/foresight-v14-decision-graph-stub) — RFC 08
# §14.4 wiring stub. The engine layer needs a forward-precedent edge to
# attach to every ``ProjectedDecision`` it emits, but RFC 07's Decision
# Graph implementation is not yet in pocketpaw (the scaffold lives at
# /tmp/team-rfc07; production-izing it is a separate stream). To keep
# v0.5 unblocked we introduce a thin protocol the runner consumes — the
# real RFC 07 wiring will drop in by registering an implementation that
# returns Decision-Graph short-ids instead of synthetic ones.
#
# Public surface:
#   - ``DecisionGraphRef`` (Protocol) — the lookup contract.
#   - ``NoOpDecisionGraphRef`` — the default implementation v0.5 ships.
#     Synthesizes deterministic precedent ids from a scenario seed +
#     anchor + persona triple. When the scenario carries no seed at all
#     it returns ``None``, preserving the v0.1 "field always None"
#     wire-shape contract for un-seeded smoke runs.
#
# Why a protocol and not a concrete class:
#   - The runner imports the protocol type but does not depend on the
#     concrete NoOp. Tests can swap in a fake that returns canned ids,
#     and the future RFC 07 wiring drops in a real implementation by
#     constructing a different ``DecisionGraphRef``-conforming instance
#     and passing it into ``run_scenario(decision_graph_ref=...)``.
#   - The wire shape never changes — ``ProjectedDecision.forward_precedent_decision_id``
#     is already ``str | None`` on the cloud domain. When RFC 07 lands,
#     the only delta is which instance the cloud injects; persisted
#     documents and DTOs are unaffected.

from __future__ import annotations

from hashlib import sha1
from typing import Protocol, runtime_checkable

__all__ = [
    "DecisionGraphRef",
    "NoOpDecisionGraphRef",
    "SYNTHETIC_PRECEDENT_PREFIX",
]

# Short prefix on synthesized ids so consumers can distinguish them
# from real Decision-Graph short-ids (which carry their own scheme —
# typically ``dec_<base32>`` per the RFC 07 scaffold). UI surfaces can
# render synthetic ids with a "stubbed precedent" badge until real
# Decision-Graph wiring lands; the cloud-side backfill pass that
# replaces these with real ids is mechanical (look up the real short-id
# by anchor/persona/scenario and update the row in place).
SYNTHETIC_PRECEDENT_PREFIX = "synthetic-precedent-"


@runtime_checkable
class DecisionGraphRef(Protocol):
    """Forward-precedent lookup contract for the Foresight runner.

    The runner calls ``lookup_precedent`` once per ``ProjectedDecision``
    it emits. The returned id (when not ``None``) populates
    ``ProjectedDecision.forward_precedent_decision_id`` on the engine
    wire shape (``RunResult.projected_decisions[i]``) — the same field
    persisted on ``foresight_projected_decisions`` once the cloud-side
    backfill lands.

    Implementations:

    - :class:`NoOpDecisionGraphRef` (v0.5 default) — deterministic
      synthetic ids derived from the scenario seed. Returns ``None``
      when no seed is configured.
    - Future RFC 07 ``DecisionGraphProjection`` — will lookup a real
      ``Decision`` by anchor + persona + scope in the persisted
      decision graph and return its short-id. Lives in pocketpaw once
      the RFC 07 productionization stream merges (currently parked at
      ``/tmp/team-rfc07``). Drop-in replacement: no engine changes
      required.
    """

    def lookup_precedent(
        self,
        anchor_id: str,
        persona_id: str,
        scenario_id: str,
    ) -> str | None:
        """Return the precedent decision id for this projection bucket,
        or ``None`` when no precedent applies.

        Implementations must be **pure** w.r.t. their inputs — the
        runner invokes this once per (anchor × tick) bucket on every
        scenario run, and the cloud-side replay path expects stable
        outputs given identical inputs so backfill jobs are idempotent.

        Args:
            anchor_id: the projection's anchor id — sub-type-specific
                (``decision:<name>`` / ``segment:<role>`` /
                ``rollout:<event>``).
            persona_id: the modal persona's id (str(UUID)). The runner
                passes an empty string when no persona acted on this
                bucket; implementations may treat the empty string as
                "no persona context" rather than a real id.
            scenario_id: the running scenario's stable identifier (the
                scenario name in v0.5; future versions may switch to a
                scenario-graph short-id once RFC 07 lands).
        """
        ...


class NoOpDecisionGraphRef:
    """Default :class:`DecisionGraphRef` for v0.5.

    Synthesizes deterministic precedent ids by hashing the
    (scenario_id, anchor_id, persona_id, seed) tuple with SHA-1 and
    truncating to 12 hex chars. Same inputs always produce the same
    id — UI dev and replay-style tests can rely on the projection
    carrying a stable, non-None value as long as a seed is configured.

    Algorithm:

      .. code-block:: text

          precedent_id = "synthetic-precedent-" + sha1(
              scenario_id + "|" + anchor_id + "|" + persona_id + "|" + seed
          ).hexdigest()[:12]

    Returns ``None`` when ``seed`` is the empty string. This preserves
    the v0.1 behavior for un-seeded scenarios — the field is "absent" in
    the wire dict (carries ``None``) and the cloud-side persistence
    keeps its current hardcoded ``None`` until the RFC 07 backfill ships.

    The class accepts an optional ``per_anchor_seeds`` mapping that
    overrides the scenario-level ``seed`` for specific anchors. This
    matches the YAML grammar v14 introduces: a scenario can declare a
    global ``precedent_seed`` plus a ``precedent_seeds: {anchor_id:
    seed}`` map for anchor-level overrides.
    """

    def __init__(
        self,
        seed: str = "",
        *,
        per_anchor_seeds: dict[str, str] | None = None,
    ) -> None:
        self._seed = seed
        # Copy to defend against external mutation — the runner builds
        # one NoOp per run and reuses it across (anchor × tick) calls.
        self._per_anchor_seeds: dict[str, str] = dict(per_anchor_seeds or {})

    def lookup_precedent(
        self,
        anchor_id: str,
        persona_id: str,
        scenario_id: str,
    ) -> str | None:
        # Per-anchor override wins; empty-string override falls back to
        # the scenario seed (matches "remove override" semantics).
        seed = self._per_anchor_seeds.get(anchor_id) or self._seed
        if not seed:
            return None
        payload = f"{scenario_id}|{anchor_id}|{persona_id}|{seed}".encode()
        digest = sha1(payload).hexdigest()[:12]  # noqa: S324 — non-crypto id derivation
        return f"{SYNTHETIC_PRECEDENT_PREFIX}{digest}"
