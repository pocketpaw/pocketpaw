# skills_loop/reviewer.py — the forked write-only session reviewer.
# Created: 2026-06-16 (feat/self-improving-skills) — runs at an agent's
#   session-finalize. It replays the session transcript, asks an extractor (the
#   LLM, faked in tests) for candidate procedures, filters them through the
#   anti-pattern rubric, and writes the survivors into the WORKSPACE agent's
#   soul via SoulProcedureWriter. The reviewer EXPOSES a soul-write-ONLY tool
#   whitelist (whitelist.build_reviewer_whitelist) as ``tool_whitelist``. That
#   set is the CONTRACT the SDK caller MUST forward as ``allow_sdk_tools`` (with
#   a deny-everything base policy) when it spawns the forked reviewer — that is
#   what makes the reviewer physically unable to call any tool but soul-write.
#   This PR does NOT yet spawn the reviewer through the SDK, so the runtime
#   restriction is NOT enforced here; the whitelist + assert_write_only only
#   guarantee the CONTRACT is well-formed. Wiring the spawn (forwarding
#   allow_sdk_tools + the rubric system prompt) is the follow-up runtime-hookup
#   PR tracked alongside the soul-protocol dep bump.

from __future__ import annotations

import logging
from typing import Any, Protocol

from pocketpaw.skills_loop.whitelist import assert_write_only, build_reviewer_whitelist
from pocketpaw.skills_loop.writer import SoulProcedureWriter

logger = logging.getLogger(__name__)


class ProcedureExtractor(Protocol):
    """The LLM-backed step that reads a transcript and proposes procedures.

    Faked in tests; in production this is the forked Claude SDK run launched
    with the write-only whitelist + the rubric system prompt.
    """

    async def extract(self, transcript: str) -> list[str]: ...


class SkillsLoopReviewer:
    """Replays a session and writes learned procedures into the workspace soul.

    Args:
        soul: the WORKSPACE agent's soul (the single default agent carrying the
            workspace soul). The reviewer never resolves a per-pocket soul.
        extractor: proposes candidate procedures from the transcript.
        importance: importance score stamped on written procedures.
    """

    def __init__(
        self,
        *,
        soul: Any,
        extractor: ProcedureExtractor,
        importance: int = 6,
    ) -> None:
        self._writer = SoulProcedureWriter(soul)
        self._extractor = extractor
        self._importance = importance
        # The soul-write-ONLY CONTRACT the SDK caller must forward as
        # ``allow_sdk_tools`` when spawning the forked reviewer. Asserted here
        # so a mis-constructed whitelist fails loudly. NOTE: this object does
        # not itself spawn an SDK run, so the restriction is not enforced at
        # this layer — the caller wiring (the follow-up runtime-hookup PR) is
        # responsible for actually forwarding ``tool_whitelist`` to the spawn.
        # TODO(skills-loop runtime-hookup PR): forward ``tool_whitelist`` to the
        #   Claude SDK backend as ``allow_sdk_tools`` (+ rubric system prompt)
        #   at session-finalize spawn so the enforcement this contract describes
        #   becomes real. Lands with the soul-protocol dep bump.
        self.tool_whitelist = build_reviewer_whitelist()
        assert_write_only(self.tool_whitelist)

    async def review_session(self, transcript: str) -> dict:
        """Extract, rubric-filter, and write procedures. Returns a report.

        ``{"candidates": int, "written": int, "rejected": int, "ids": list[str]}``
        """
        candidates = await self._extractor.extract(transcript)
        written_ids: list[str] = []
        rejected = 0
        for proc in candidates:
            result = await self._writer.write(proc, importance=self._importance)
            if result["written"]:
                written_ids.append(result["id"])
            else:
                rejected += 1

        report = {
            "candidates": len(candidates),
            "written": len(written_ids),
            "rejected": rejected,
            "ids": written_ids,
        }
        logger.info(
            "skills_loop reviewer: %d candidates → %d written, %d rejected",
            report["candidates"],
            report["written"],
            report["rejected"],
        )
        return report


__all__ = ["SkillsLoopReviewer", "ProcedureExtractor"]
