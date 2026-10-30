# src/pocketpaw/skills/materialize.py
# Per-run materialization of a NAMED subset of skills into a throwaway Claude
# Code *local plugin* directory. This is what makes ``SurfaceProfile.skill_names``
# a real allowlist for the claude_agent_sdk backend.
#
# Why a local plugin (not ``~/.claude/skills`` and not the SDK ``skills=``
# option): the SDK backend launches with ``setting_sources=[]`` for persona
# isolation, which DISABLES filesystem skill discovery AND the ``skills=``
# option. A local plugin dir (``.claude-plugin/plugin.json`` +
# ``skills/<slug>/SKILL.md``) is the ONLY channel that survives that isolation.
# A FRESH plugin is built per run so only the entity's named skills surface.
#
# Name resolution: an installed skill (``SkillLoader.SKILL_PATHS``) wins; the
# packaged ``_bundled/skills/`` dir is a last-resort fallback so a surface can
# name a bundled skill even when the boot-time ~/.claude/skills mirror is off.
#
# Invariants: every path component written here is filesystem-safe on every
# OS. Skill slugs are validated (unsafe ones skipped); the run id, which is a
# session key full of ``:``, is reduced by ``fs_safe_component`` before it
# reaches ``mkdtemp``. Temp dirs keep the ``paw-run-skills-`` prefix.

from __future__ import annotations

import hashlib
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

# Longest run id folded into a temp-dir name. Keeps deep temp roots well clear
# of Windows MAX_PATH once skill paths are appended underneath.
_RUN_ID_MAX_LEN = 48

_UNSAFE_FS_CHARS_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DASH_RUN_RE = re.compile(r"-{2,}")


def fs_safe_component(value: str, *, max_len: int = _RUN_ID_MAX_LEN) -> str:
    """Reduce ``value`` to a single path component legal on every OS.

    Any run of characters outside ``[A-Za-z0-9._-]`` (``:``, ``/``, backslash,
    ``*``, ``?``, quotes, ``<>|``, spaces, non-ASCII) becomes one ``-``; leading
    and trailing ``-``/``.`` are trimmed (Windows rejects a trailing dot, and
    ``.``/``..`` collapse to empty). A result longer than ``max_len`` is cut and
    suffixed with 8 hex chars of the FULL value's sha256, so two long ids that
    share a prefix still map to different names. May return ``""``.
    """
    safe = _DASH_RUN_RE.sub("-", _UNSAFE_FS_CHARS_RE.sub("-", value)).strip("-.")
    if len(safe) <= max_len:
        return safe
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    head = safe[: max(max_len - len(digest) - 1, 0)].rstrip("-.")
    return f"{head}-{digest}" if head else digest


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


def _bundled_plugin_root() -> Path | None:
    """Return the PACKAGED bundled-skills plugin root, or ``None``.

    A seam, not just a wrapper: tests swap this for a throwaway bundle, and the
    import is local so ``pocketpaw.skills`` never hard-depends on the shipping
    side. Any failure degrades to "no bundle" — resolution then behaves exactly
    as it did before the bundled fallback existed.
    """
    try:
        from pocketpaw.bundled_skills import bundled_skills_plugin_dir

        return bundled_skills_plugin_dir()
    except Exception:  # noqa: BLE001 — a missing bundle must never fail a run
        return None


def _bundled_skill_dirs() -> dict[str, Path]:
    """Map bundled skill name → its directory, for name-based resolution.

    ``SkillLoader.SKILL_PATHS`` covers ``~/.agents``, ``~/.claude`` and
    ``~/.pocketpaw`` — NOT the packaged ``_bundled/skills/``. Bundled skills
    reached the loader only through the boot-time ~/.claude/skills mirror, which
    ``POCKETPAW_AUTO_INSTALL_BUNDLED_SKILLS=false`` disables. This reads the
    package directly so a surface can name a bundled skill unconditionally.
    """
    root = _bundled_plugin_root()
    if root is None:
        return {}
    try:
        return {p.name: p for p in (root / "skills").iterdir() if (p / "SKILL.md").is_file()}
    except OSError:  # bundle absent or unreadable — degrade to no bundle
        return {}


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
        run_id: optional run identifier (usually the session key) folded into
            the temp-dir prefix purely for log/debug legibility. It is passed
            through :func:`fs_safe_component` first, since session keys carry
            ``:`` and can be long.

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
    bundled = _bundled_skill_dirs()
    matched: list[tuple[str, Path]] = []
    unknown: list[str] = []
    for name in sorted(requested):
        slug = _safe_slug(name)
        if slug is None:
            logger.warning("materialize_run_skills: skipping unsafe skill slug %r", name)
            continue
        # Resolution order: an INSTALLED skill (SKILL_PATHS) shadows a packaged
        # bundled one of the same name, so a user override still wins. The
        # bundled fallback exists because SKILL_PATHS does not cover the
        # packaged ``_bundled/skills/`` dir — without it, naming a bundled
        # skill only worked when the boot-time ~/.claude/skills mirror was on.
        # ``Skill.path`` points at SKILL.md; its parent is the skill directory.
        skill = available.get(name)
        skill_dir = skill.path.parent if skill is not None else bundled.get(name)
        if skill_dir is None:
            unknown.append(name)
            continue
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

    safe_run_id = fs_safe_component(run_id) if run_id else ""
    prefix = f"{_TMP_PREFIX}{safe_run_id + '-' if safe_run_id else ''}"
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


__all__ = ["cleanup_run_skills", "fs_safe_component", "materialize_run_skills"]
