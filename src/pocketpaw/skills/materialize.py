# src/pocketpaw/skills/materialize.py
# Created: 2026-06-07 (feat/entity-pocket-profile-field, entity-rooms A2) —
# per-run materialization of a NAMED subset of installed skills into a
# throwaway Claude Code *local plugin* directory. This is the keystone that
# makes ``SurfaceProfile.skill_names`` LIVE for the claude_agent_sdk backend.
#
# Why a local plugin (not ``~/.claude/skills`` and not the SDK ``skills=``
# option): the SDK backend launches with ``setting_sources=[]`` for persona
# isolation, which DISABLES filesystem skill discovery AND the ``skills=``
# option. A local plugin dir (a dir carrying ``.claude-plugin/plugin.json`` +
# ``skills/<slug>/SKILL.md``) is the ONLY channel that survives that isolation,
# exactly as the bundled-skills plugin does. We build a FRESH plugin per run so
# only the entity's named skills are surfaced — never the full installed set.

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path

from pocketpaw.skills.loader import get_skill_loader

logger = logging.getLogger(__name__)

# Same guard the GitHub installer uses (``installer.py``): a skill slug becomes
# a directory name under the materialized plugin, so reject path traversal and
# any character outside the safe set before it ever touches the filesystem.
_SLUG_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

_TMP_PREFIX = "paw-run-skills-"


def _ignore_symlinks(src: str, names: list[str]) -> set[str]:
    """Return names that are symlinks so ``shutil.copytree`` skips them.

    Mirrors ``pocketpaw.skills.installer._ignore_symlinks`` — a copied skill
    must never drag a symlink (which could point outside the skill dir) into
    the per-run plugin.
    """
    return {n for n in names if os.path.islink(os.path.join(src, n))}


def _safe_slug(name: str) -> str | None:
    """Return a filesystem-safe slug for ``name`` or ``None`` if it's unsafe.

    A skill's registered ``name`` may carry characters that are fine in a prompt
    but unsafe as a directory component (``/``, ``..``). The SDK matches skills
    by their plugin directory name, so we keep the slug == the registered name
    whenever it passes the guard, and reject (skip) it otherwise.
    """
    if name in (".", "..") or not _SLUG_RE.match(name):
        return None
    return name


def materialize_run_skills(
    skill_names: Iterable[str],
    run_id: str | None = None,
) -> Path | None:
    """Materialize the NAMED installed skills into a throwaway local-plugin dir.

    Filters ``get_skill_loader().get_all()`` to the requested ``skill_names``,
    copies each matched skill's WHOLE directory (``Skill.path.parent`` — the dir
    holding ``SKILL.md`` plus any assets) into ``<tmproot>/skills/<slug>/``, and
    writes a minimal ``<tmproot>/.claude-plugin/plugin.json`` so the directory is
    a valid Claude Code local plugin.

    Args:
        skill_names: the entity's requested skill names (from the resolved
            ``SurfaceProfile.skill_names``). Unknown names are skipped with a
            log line — a typo in one name never fails the whole run.
        run_id: optional run identifier folded into the temp-dir prefix purely
            for log/debug legibility.

    Returns:
        The plugin ROOT directory (pass to the SDK as
        ``{"type": "local", "path": str(root)}``), or ``None`` when no requested
        name matched an installed skill (caller then skips the ``plugins=``
        wiring entirely). The caller OWNS the returned dir and MUST clean it up
        via :func:`cleanup_run_skills` after the run.
    """
    requested = {n for n in skill_names if n}
    if not requested:
        return None

    available = get_skill_loader().get_all()
    matched: list[tuple[str, Path]] = []
    unknown: list[str] = []
    for name in sorted(requested):
        skill = available.get(name)
        if skill is None:
            unknown.append(name)
            continue
        slug = _safe_slug(name)
        if slug is None:
            logger.warning("materialize_run_skills: skipping unsafe skill slug %r", name)
            continue
        # ``Skill.path`` points at SKILL.md; its parent is the skill directory.
        skill_dir = skill.path.parent
        if not (skill_dir / "SKILL.md").is_file():
            logger.warning(
                "materialize_run_skills: skill %r has no SKILL.md at %s — skipping",
                name,
                skill_dir,
            )
            continue
        matched.append((slug, skill_dir))

    if unknown:
        logger.info("materialize_run_skills: unknown skill names skipped: %s", unknown)

    if not matched:
        logger.info("materialize_run_skills: no requested skill matched an installed skill")
        return None

    prefix = f"{_TMP_PREFIX}{run_id + '-' if run_id else ''}"
    root = Path(tempfile.mkdtemp(prefix=prefix))
    skills_dir = root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for slug, src_dir in matched:
        dest = skills_dir / slug
        try:
            shutil.copytree(src_dir, dest, ignore=_ignore_symlinks)
        except OSError as exc:
            logger.warning(
                "materialize_run_skills: failed to copy skill %r from %s: %s",
                slug,
                src_dir,
                exc,
            )
            continue
        copied.append(slug)

    if not copied:
        # Every copy failed — leave nothing half-built behind.
        cleanup_run_skills(root)
        logger.warning("materialize_run_skills: all skill copies failed — no plugin produced")
        return None

    manifest = root / ".claude-plugin" / "plugin.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    # Mirror the bundled-skills manifest's required keys (name / version /
    # description). A per-run plugin name keeps it distinct from the bundled one
    # so both can coexist in the SDK ``plugins=`` list without a name clash.
    manifest.write_text(
        json.dumps(
            {
                "name": "pocketpaw-run-skills",
                "version": "0.1.0",
                "description": (
                    "Per-run skill subset materialized from the entity's "
                    "SurfaceProfile.skill_names so only the requested skills are "
                    "surfaced to the agent. Loaded via the SDK plugins= option, "
                    "which survives setting_sources=[] persona isolation."
                ),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    logger.info(
        "materialize_run_skills: materialized %d skill(s) %s into %s",
        len(copied),
        copied,
        root,
    )
    return root


def cleanup_run_skills(root: Path | None) -> None:
    """Remove a per-run materialized-skills plugin dir. Best-effort, never raises.

    Safe to call with ``None`` (the no-skills path) and idempotent. The caller
    invokes this in a ``finally`` after the SDK run so a throwaway plugin never
    leaks into the temp dir across runs.
    """
    if root is None:
        return
    try:
        shutil.rmtree(root, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 — cleanup must never break a run
        logger.debug("cleanup_run_skills: failed to remove %s: %s", root, exc)


__all__ = ["cleanup_run_skills", "materialize_run_skills"]
