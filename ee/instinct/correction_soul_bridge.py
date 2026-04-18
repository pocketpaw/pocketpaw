# ee/instinct/correction_soul_bridge.py — Wires Corrections into soul-protocol.
# Created: 2026-04-12 (Move 1 PR-B) — Turns each captured human edit into a soul
# observation, and promotes repeated edits on the same field into a procedural
# memory. No new soul primitive — uses soul.observe() + soul.remember() as-is.

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ee.instinct.correction import Correction, CorrectionPatch
from ee.instinct.models import Action

logger = logging.getLogger(__name__)

_PROMOTION_THRESHOLD = 3  # Same path edited N times → procedural memory
_PROCEDURAL_IMPORTANCE = 7
_EPISODIC_IMPORTANCE = 5

if TYPE_CHECKING:
    from ee.instinct.store import InstinctStore  # noqa: F401


class CorrectionSoulBridge:
    """Connects captured Corrections to soul-protocol's learning hooks.

    Call `record(correction, action)` right after persisting the Correction
    in the store. The bridge:

    - Always observes the edit as an Interaction so it enters the soul's
      episodic tier with a recall-friendly summary.
    - Counts how many times the same `path` has been edited in this pocket.
      On the third match, synthesizes a short rule and stores it as a
      procedural memory (importance 7).

    The bridge degrades silently when the soul is not loaded — corrections
    still persist to SQLite, and the agent tool can still read them back.
    Nothing in the request path fails because soul is down.
    """

    def __init__(self, soul_manager: object, store: object) -> None:
        self._soul_manager = soul_manager
        self._store = store

    async def record(self, correction: Correction, action: Action) -> None:
        soul = self._get_soul()
        if soul is None:
            logger.debug("No active soul — correction recorded to store only")
            return

        await self._observe_correction(soul, correction, action)
        await self._maybe_promote_to_procedural(soul, correction)

    async def _observe_correction(
        self, soul: object, correction: Correction, action: Action
    ) -> None:
        """Record the correction as an Interaction so it enters episodic memory."""
        try:
            from soul_protocol import Interaction

            patches_text = "\n".join(
                f"  - {p.path}: {self._fmt(p.before)} → {self._fmt(p.after)}"
                for p in correction.patches
            )
            user_input = (
                f"Correction on action '{action.title}' "
                f"(pocket={correction.pocket_id}, actor={correction.actor})"
            )
            agent_output = (
                f"Original recommendation: {action.recommendation or '(none)'}\n"
                f"Edits applied by human:\n{patches_text}\n"
                f"Summary: {correction.context_summary}"
            )
            await soul.observe(Interaction(user_input=user_input, agent_output=agent_output))
            logger.info(
                "Correction %s recorded to soul (action=%s, paths=%s)",
                correction.id,
                correction.action_id,
                [p.path for p in correction.patches],
            )
        except Exception:
            logger.exception("Failed to observe correction — continuing without soul record")

    async def _maybe_promote_to_procedural(self, soul: object, correction: Correction) -> None:
        """When a path has been corrected _PROMOTION_THRESHOLD times, synthesize a rule."""
        if not hasattr(soul, "remember"):
            return

        for patch in correction.patches:
            try:
                count = await self._store.count_corrections_by_path(
                    pocket_id=correction.pocket_id,
                    path=patch.path,
                )
            except Exception:
                logger.debug("Failed to count corrections for path %s", patch.path)
                continue

            if count != _PROMOTION_THRESHOLD:
                continue

            rule = self._synthesize_rule(patch, correction)
            try:
                await soul.remember(
                    content=rule,
                    type="procedural",
                    importance=_PROCEDURAL_IMPORTANCE,
                )
                logger.info(
                    "Promoted to procedural after %dx '%s' corrections: %s",
                    _PROMOTION_THRESHOLD,
                    patch.path,
                    rule,
                )
            except Exception:
                logger.exception("Failed to persist procedural rule for path %s", patch.path)

    def _get_soul(self) -> object | None:
        """Resolve the active Soul, tolerating different manager shapes."""
        manager = self._soul_manager
        if manager is None:
            return None
        soul = getattr(manager, "soul", None)
        return soul

    @staticmethod
    def _synthesize_rule(patch: CorrectionPatch, correction: Correction) -> str:
        """Short natural-language rule — no LLM, deterministic."""
        if patch.path.startswith("parameters."):
            key = patch.path.split(".", 1)[1]
            return (
                f"For actions in pocket {correction.pocket_id}, "
                f"{correction.actor} consistently sets {key} to "
                f"{CorrectionSoulBridge._fmt(patch.after)} "
                f"(changed from {CorrectionSoulBridge._fmt(patch.before)})."
            )
        return (
            f"For actions in pocket {correction.pocket_id}, "
            f"{correction.actor} consistently rewrites {patch.path} — "
            f"most recent: {CorrectionSoulBridge._fmt(patch.before)} → "
            f"{CorrectionSoulBridge._fmt(patch.after)}."
        )

    @staticmethod
    def _fmt(value: object) -> str:
        if value is None:
            return "(none)"
        s = str(value)
        return s if len(s) <= 80 else s[:77] + "..."
