# skills_loop/writer.py — writes a learned procedure into the workspace soul.
# Created: 2026-06-16 (feat/self-improving-skills) — the single write path the
#   reviewer uses. Runs the rubric guard, then writes the procedure as a
#   PROCEDURAL memory tagged AGENT-authored. Provenance is carried two ways so
#   the slice ships independently of the soul-protocol PR:
#     - always on the ``entities`` list (the ``provenance:agent`` tag) — present
#       in every soul-protocol version, so agent-created procedures are always
#       distinguishable from human-authored ones;
#     - additionally via the ``provenance=`` kwarg when the installed
#       soul-protocol exposes ``MemoryProvenance`` (the matching soul PR), so the
#       curator's native provenance field is populated once both land.
#   Writes go through ``Soul.note`` when available (dedup via reconcile_fact),
#   falling back to ``Soul.remember`` on older soul-protocol builds.

from __future__ import annotations

import logging
from typing import Any

from soul_protocol.runtime.types import MemoryType

from pocketpaw.skills_loop.rubric import is_rubric_banned

logger = logging.getLogger(__name__)

# Entities tag marking a procedure as agent-authored. Stable across every
# soul-protocol version (``entities`` is a long-standing field), so the curator
# and any consumer can distinguish loop-learned procedures from human ones even
# before the native ``provenance`` field exists.
AGENT_PROVENANCE_TAG = "provenance:agent"
SKILLS_LOOP_TAG = "skills-loop"

# Resolve the native provenance enum if the installed soul-protocol carries it.
try:  # pragma: no cover - import-time capability probe
    from soul_protocol.runtime.types import MemoryProvenance

    _AGENT_PROVENANCE: Any | None = MemoryProvenance.AGENT
except ImportError:  # pragma: no cover - older soul-protocol, kwarg unsupported
    _AGENT_PROVENANCE = None


class SoulProcedureWriter:
    """Writes rubric-clean, agent-tagged procedures into a workspace soul.

    The soul passed in is the WORKSPACE agent's soul — the writer never resolves
    a per-pocket soul. It only knows how to write to whatever soul it is handed.
    """

    def __init__(self, soul: Any) -> None:
        self._soul = soul

    async def write(self, procedure_text: str, *, importance: int = 6) -> dict:
        """Write one learned procedure. Returns a result dict.

        ``{"written": bool, "id": str | None, "reason": str | None}`` —
        ``written`` is False (with a ``reason``) when the rubric guard rejects
        the candidate, so nothing banned ever reaches the soul.
        """
        banned, reason = is_rubric_banned(procedure_text)
        if banned:
            logger.info("skills_loop: rejected procedure (%s)", reason)
            return {"written": False, "id": None, "reason": reason}

        entities = [AGENT_PROVENANCE_TAG, SKILLS_LOOP_TAG]
        kwargs: dict[str, Any] = {
            "type": MemoryType.PROCEDURAL,
            "importance": importance,
            "entities": entities,
        }
        if _AGENT_PROVENANCE is not None:
            kwargs["provenance"] = _AGENT_PROVENANCE

        result = await self._write_via_soul(procedure_text, kwargs)
        mem_id = result.get("id") if isinstance(result, dict) else result
        logger.info("skills_loop: wrote agent procedure id=%s", mem_id)
        return {"written": True, "id": mem_id, "reason": None}

    async def _write_via_soul(self, content: str, kwargs: dict[str, Any]) -> Any:
        """Prefer ``Soul.note`` (dedup); fall back to ``Soul.remember``."""
        note = getattr(self._soul, "note", None)
        if note is not None:
            return await note(content, **kwargs)
        # Older soul-protocol: no note(), no provenance kwarg.
        kwargs.pop("provenance", None)
        return await self._soul.remember(content, **kwargs)


__all__ = ["SoulProcedureWriter", "AGENT_PROVENANCE_TAG", "SKILLS_LOOP_TAG"]
