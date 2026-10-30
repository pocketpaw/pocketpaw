# skills_loop/graduation.py — XP → SKILL.md graduation for learned procedures.
# Created: 2026-06-16 (feat/self-improving-skills) — grants XP to the skill that
#   tracks a learned procedure each time the procedure is used (via the soul
#   SkillRegistry.grant_xp_for_procedure_use helper). When a grant crosses a
#   level boundary the procedure GRADUATES: it is materialized as a SKILL.md
#   under ~/.pocketpaw/skills, reusing the write precedent established by
#   skills/api_skill_builder.install_api_skill (mkdir parents + write_text +
#   INFO audit log). Below threshold nothing is written.

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _slugify(text: str) -> str:
    """Lowercase slug safe for a directory name.

    Pathological ids (all-punctuation, empty) would otherwise all collapse to
    the literal ``"procedure"`` and collide on the same directory — clobbering
    each other's SKILL.md. When the slug degenerates we disambiguate with a
    short stable suffix derived from the raw text: its alphanumerics if it has
    any, else a short hash so even two distinct all-punctuation ids
    (``"@@@"`` vs ``"###"``) get distinct directories.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if slug:
        return slug[:48]
    raw_alnum = re.sub(r"[^A-Za-z0-9]+", "", text)[:8].lower()
    if raw_alnum:
        return f"procedure-{raw_alnum}"
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
    return f"procedure-{digest}"


def _render_skill_md(procedure_id: str, procedure_text: str) -> str:
    """Render a minimal SKILL.md for a graduated procedure."""
    name = f"learned-{_slugify(procedure_id)}"
    summary = procedure_text.strip().split("\n", 1)[0][:120]
    return (
        f"---\n"
        f"name: {name}\n"
        f"description: >-\n"
        f"  Learned procedure graduated from the workspace agent's soul "
        f"(self-improving skills loop). {summary}\n"
        f"---\n\n"
        f"# {name}\n\n"
        f"This skill was graduated from a procedure the workspace agent learned "
        f"and used repeatedly. It originated in soul procedural memory "
        f"`{procedure_id}` and crossed the XP/use graduation threshold.\n\n"
        f"## Procedure\n\n"
        f"{procedure_text.strip()}\n"
    )


def maybe_graduate_procedure(
    skill_registry: Any,
    *,
    procedure_id: str,
    procedure_text: str,
    amount: int = 10,
    skills_root: Path | None = None,
) -> Path | None:
    """Grant XP for a procedure use and materialize a SKILL.md on graduation.

    Args:
        skill_registry: a soul ``SkillRegistry`` (or compatible) exposing
            ``grant_xp_for_procedure_use(skill_id, amount) -> bool``.
        procedure_id: the soul procedural-memory id backing the procedure. The
            tracking skill id is ``proc:<procedure_id>``.
        procedure_text: the procedure content (rendered into the SKILL.md).
        amount: XP granted for this use (default 10).
        skills_root: override the skills root (for tests). Defaults to
            ``~/.pocketpaw/skills`` — the same root install_api_skill writes to.

    Returns:
        The path to the written SKILL.md when the grant crosses a level
        boundary (graduation), otherwise ``None``.
    """
    skill_id = f"proc:{procedure_id}"
    graduated = skill_registry.grant_xp_for_procedure_use(skill_id, amount)
    if not graduated:
        return None

    root = skills_root if skills_root is not None else (Path.home() / ".pocketpaw" / "skills")
    skill_dir = root / f"learned-{_slugify(procedure_id)}"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(_render_skill_md(procedure_id, procedure_text), encoding="utf-8")

    logger.info(
        "skills_loop: graduated procedure %s → materialized SKILL.md at %s",
        procedure_id,
        skill_md,
    )
    return skill_md


__all__ = ["maybe_graduate_procedure"]
