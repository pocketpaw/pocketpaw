# skills_loop — the self-improving skills loop (PocketPaw side).
# Created: 2026-06-16 (feat/self-improving-skills) — a forked WRITE-ONLY reviewer
#   runs at a workspace agent's session-finalize, reads the transcript, and
#   writes/refines learned procedures into the WORKSPACE agent's soul procedural
#   memory under safety machinery:
#     - whitelist.py  : soul-write-ONLY tool restriction (the reviewer physically
#                       cannot call any other tool)
#     - rubric.py     : anti-pattern guard (no env failures, "tool broken"
#                       claims, or one-off narratives)
#     - writer.py     : tags procedures AGENT-authored + writes via Soul.note
#     - reviewer.py   : extract → rubric-filter → write
#     - trigger.py    : background spawn at session-finalize
#     - graduation.py : XP → SKILL.md materialization for high-value procedures
#   An idle curator (soul-protocol's Soul.curate_agent_procedures) consolidates
#   and archives agent-created procedures separately.

from __future__ import annotations

from pocketpaw.skills_loop.graduation import maybe_graduate_procedure
from pocketpaw.skills_loop.reviewer import SkillsLoopReviewer
from pocketpaw.skills_loop.rubric import REVIEWER_SYSTEM_PROMPT, is_rubric_banned
from pocketpaw.skills_loop.trigger import SkillsLoopTrigger
from pocketpaw.skills_loop.whitelist import (
    SOUL_WRITE_TOOL_IDS,
    assert_write_only,
    build_reviewer_whitelist,
)
from pocketpaw.skills_loop.writer import SoulProcedureWriter

__all__ = [
    "SOUL_WRITE_TOOL_IDS",
    "build_reviewer_whitelist",
    "assert_write_only",
    "REVIEWER_SYSTEM_PROMPT",
    "is_rubric_banned",
    "SoulProcedureWriter",
    "SkillsLoopReviewer",
    "SkillsLoopTrigger",
    "maybe_graduate_procedure",
]
