# pocketpaw_ee/discovery/rule_digester.py — RuleDigester (SZD slice-2 S2-R2).
#
# Created: 2026-06-20 (S2-R2 / feat/szd-slice2-discovery) — the deterministic
# inference engine that reverse-engineers candidate GOVERNED rules from a
# tenant's Instinct exhaust (correction history + audit trail). It generalizes
# the existing 3x-correction → soul-procedural promotion
# (``instinct/correction_soul_bridge.py``: ``_PROMOTION_THRESHOLD = 3`` +
# ``_synthesize_rule``) from a natural-language soul STRING into a structured,
# gate-ready ``RuleDraft`` (CEL ``when`` + action literal + scope + provenance +
# confidence).
#
# SOVEREIGNTY (RK-2): pure / deterministic — NO LLM, NO network. Rule inference
# runs on-box. An LLM refine pass is explicitly out of scope this slice. The only
# inputs are exhaust the box already holds.
#
# Heuristic (per qualifying correction PATH):
#   1. Group corrections by ``patch.path`` (the same axis the seed counts).
#   2. A path qualifies when it recurs >= RULE_RECUR_THRESHOLD (mirrors the
#      seed's promotion threshold of 3).
#   3. ``when`` — a CEL equality on the corrected field's most-common ``after``
#      value (``action.<field> == "<value>"``); when no constant target exists
#      (humans set a different value each time) the signal is weak and the draft
#      is dropped below the confidence floor (see step 5).
#   4. ``action`` — inferred from the corrected field's nature:
#        * escalation fields (category / priority) consistently raised
#          → ``require_approval``;
#        * suppression shapes (recommendation blanked, or a suppress/mute/notify
#          parameter key) → ``notify``;
#        * everything else defaults to ``require_approval`` (the safe gate).
#   5. ``confidence`` — scales with recurrence count AND value consistency:
#        floor-anchored, + slope per extra recurrence, + a consistency bonus.
#      Clamped to [0, 1] by the RuleDraft model. Drafts BELOW
#      RULE_CONFIDENCE_FLOOR are SKIPPED (a weakly-inferred governed rule is
#      worse than a skipped one — never emit noise into the gate).
#   6. ``provenance`` — the contributing correction ids (traceable to source).
#   7. ``scope`` — workspace_id (required tenancy; falls back to the corrections'
#      pocket_id when not passed), and object_type from a single-type OntologyDraft
#      hint when one is supplied.
#
# Degrades cleanly: empty / insufficient / patch-less exhaust → [] (never raises).
# Pure data + logic — depends only on the OSS Correction model, the RuleDraft /
# RuleScope contract (S2-R1), and the optional OntologyDraft hint.

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence

from pocketpaw.instinct.correction import Correction
from pocketpaw_ee.discovery.models import OntologyDraft
from pocketpaw_ee.discovery.rule_models import RuleDraft, RuleScope

# Same axis as the seed promotion (correction_soul_bridge._PROMOTION_THRESHOLD):
# a path must be corrected at least this many times before it can become a rule.
RULE_RECUR_THRESHOLD = 3

# The rule digester's OWN confidence floor (RK-7) — deliberately NOT the
# ontology digester's KEY_CONFIDENCE_FLOOR (that is idempotency-key specific).
# A governed rule below this floor is dropped rather than proposed: a weak rule
# that silently gates a tenant's actions is worse than no rule at all.
RULE_CONFIDENCE_FLOOR = 0.45

# Confidence shaping constants (deterministic, no tuning model).
_BASE_CONFIDENCE = 0.5  # a clean threshold-count consistent signal starts here
_RECUR_SLOPE = 0.05  # added per recurrence beyond the threshold
_CONSISTENCY_WEIGHT = 0.25  # bonus for a single dominant ``after`` value

# Correction paths whose nature implies a particular gate disposition.
_ESCALATION_FIELDS = ("category", "priority")
_SUPPRESSION_FIELDS = ("recommendation",)
_SUPPRESSION_PARAM_HINTS = ("suppress", "mute", "notify", "silence")


def _normalize(value: object) -> str:
    """Stable string form of a corrected value, for grouping + CEL emission."""
    if value is None:
        return ""
    if hasattr(value, "value"):  # enum → its string value (matches store shape)
        return str(value.value)
    return str(value)


def _cel_field(path: str) -> str:
    """Map a correction ``path`` to its CEL accessor under ``action``.

    ``category`` → ``action.category``; ``parameters.foo`` → ``action.parameters.foo``.
    Only dotted identifier segments are produced, so the result always parses as
    a CEL field selector.
    """
    return "action." + path


def _cel_literal(value: str) -> str:
    """A CEL double-quoted string literal with the inner quotes escaped."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


class RuleDigester:
    """Reverse-engineer candidate governed rules from Instinct exhaust.

    Deterministic and side-effect-free. ``infer`` consumes the correction
    history (the primary signal) plus an optional audit trail and an optional
    discovered ontology, and returns a list of ``RuleDraft`` — one per
    correction path that recurs often and consistently enough to clear the
    confidence floor. It never raises on thin or malformed input.
    """

    def infer(
        self,
        *,
        corrections: Sequence[Correction] | None,
        audit: Sequence[object] | None = None,
        ontology: OntologyDraft | None = None,
        workspace_id: str | None = None,
    ) -> list[RuleDraft]:
        """Emit ``RuleDraft``s reverse-engineered from the exhaust.

        ``corrections`` is the exhaust shape returned by
        ``InstinctStore.get_corrections_for_pocket`` (``list[Correction]``).
        ``audit`` (``list[AuditEntry]`` from ``query_audit``) is accepted for
        corroboration / future weighting but is not required to produce a draft.
        ``ontology`` is the optional ``KbCompileDigester`` / structured digester
        output used to scope a rule to a discovered ``object_type``.
        ``workspace_id`` sets the rule's tenancy; when omitted it falls back to
        the corrections' ``pocket_id`` (discovery anchors non-pocket blobs with
        ``pocket_id == workspace_id``).
        """
        if not corrections:
            return []

        # Group every single-path patch by its path. Each entry collects the
        # contributing correction ids and the corrected ``after`` values.
        by_path: dict[str, _PathSignal] = {}
        for correction in corrections:
            for patch in correction.patches:
                signal = by_path.setdefault(patch.path, _PathSignal(path=patch.path))
                signal.add(correction_id=correction.id, after=_normalize(patch.after))

        resolved_ws = workspace_id or self._infer_workspace(corrections)
        object_type = self._infer_object_type(ontology)

        drafts: list[RuleDraft] = []
        for path in sorted(by_path):  # deterministic ordering
            signal = by_path[path]
            if signal.count < RULE_RECUR_THRESHOLD:
                continue
            draft = self._build_draft(
                signal=signal,
                workspace_id=resolved_ws,
                object_type=object_type,
            )
            if draft is None:
                continue
            # The own confidence floor (RK-7): drop weak rules rather than
            # propose noise into the gate.
            if draft.confidence < RULE_CONFIDENCE_FLOOR:
                continue
            drafts.append(draft)
        return drafts

    # ------------------------------------------------------------------ #
    # Draft construction
    # ------------------------------------------------------------------ #
    def _build_draft(
        self,
        *,
        signal: _PathSignal,
        workspace_id: str,
        object_type: str | None,
    ) -> RuleDraft | None:
        """Synthesize a single RuleDraft from one path's signal, or None."""
        dominant, dominance, dominant_count = signal.dominant_after()
        field = _cel_field(signal.path)
        is_suppression = self._is_suppression(signal.path, dominant)

        # A value is a real "constant target" only when the SAME value was
        # chosen more than once. A path where every ``after`` differs (humans
        # rewrite freely) has dominant_count == 1 — no consensus, no target —
        # and degrades to the weak presence branch below the floor.
        has_target = bool(dominant) and dominant_count >= 2

        if has_target:
            when = f"{field} == {_cel_literal(dominant)}"
        elif is_suppression:
            # consistently cleared → match the cleared state
            when = f'{field} == ""'
        else:
            # No constant target and not a suppression — a presence rule only.
            # This is the weak branch; confidence will fall below the floor.
            when = f"has({field})"

        confidence = self._score(signal=signal, dominance=dominance, has_target=has_target)
        action = "notify" if is_suppression else self._infer_action(signal.path)

        return RuleDraft(
            name=f"corrected:{signal.path}",
            description=(
                f"Inferred from {signal.count} repeated correction(s) on "
                f"'{signal.path}' in this workspace."
            ),
            when=when,
            action=action,
            scope=RuleScope(
                workspace_id=workspace_id,
                object_type=object_type,
            ),
            confidence=confidence,
            provenance=list(signal.correction_ids),
        )

    def _score(self, *, signal: _PathSignal, dominance: float, has_target: bool) -> float:
        """Deterministic confidence in [0, 1] (clamped by RuleDraft).

        Anchored at a base for a clean threshold-count signal, raised per extra
        recurrence and by how dominant the most-common corrected value is.
        Inconsistent signals (no single dominant value, no constant target)
        score below the floor and get dropped upstream.
        """
        recur_bonus = _RECUR_SLOPE * max(0, signal.count - RULE_RECUR_THRESHOLD)
        consistency_bonus = _CONSISTENCY_WEIGHT * dominance
        score = _BASE_CONFIDENCE + recur_bonus + consistency_bonus
        if not has_target:
            # A presence-only rule (no constant value) is inherently weak — pull
            # it below the base so only a suppression-with-target survives.
            score -= 0.30
        return score

    @staticmethod
    def _infer_action(path: str) -> str:
        """Map a corrected path to a gate disposition (default require_approval)."""
        field = path.split(".", 1)[0]
        if field in _ESCALATION_FIELDS:
            return "require_approval"
        # Default to the safe gate: ask a human before acting.
        return "require_approval"

    @staticmethod
    def _is_suppression(path: str, dominant: str) -> bool:
        """True when the correction shape reads as suppressing the auto-output."""
        if path in _SUPPRESSION_FIELDS and dominant == "":
            return True
        if path.startswith("parameters."):
            key = path.split(".", 1)[1].lower()
            return any(hint in key for hint in _SUPPRESSION_PARAM_HINTS)
        return False

    @staticmethod
    def _infer_workspace(corrections: Sequence[Correction]) -> str:
        """Fallback tenancy: the (single) pocket_id the corrections share."""
        for correction in corrections:
            if correction.pocket_id:
                return correction.pocket_id
        return ""

    @staticmethod
    def _infer_object_type(ontology: OntologyDraft | None) -> str | None:
        """Scope to a discovered object_type when the ontology names exactly one."""
        if ontology is None:
            return None
        if len(ontology.object_types) == 1:
            return ontology.object_types[0].name
        return None


class _PathSignal:
    """Mutable accumulator for one correction path's recurrence + value spread."""

    __slots__ = ("path", "correction_ids", "_afters")

    def __init__(self, path: str) -> None:
        self.path = path
        self.correction_ids: list[str] = []
        self._afters: Counter[str] = Counter()

    def add(self, *, correction_id: str, after: str) -> None:
        self.correction_ids.append(correction_id)
        self._afters[after] += 1

    @property
    def count(self) -> int:
        return len(self.correction_ids)

    def dominant_after(self) -> tuple[str, float, int]:
        """Most-common corrected value, its share in [0, 1], and its raw count.

        Returns ``("", 0.0, 0)`` when there were no values. When the most common
        value is the empty string (the field was consistently blanked), the
        returned value is ``""`` but the dominance share is still meaningful.
        """
        if not self._afters:
            return "", 0.0, 0
        value, freq = self._afters.most_common(1)[0]
        return value, freq / self.count, freq


__all__ = ["RuleDigester", "RULE_CONFIDENCE_FLOOR", "RULE_RECUR_THRESHOLD"]
