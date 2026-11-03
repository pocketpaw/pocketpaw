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
# Only the entity's named skills are copied in, so only they surface.
#
# Lifetime: a plugin dir is CONTENT-ADDRESSED and shared. Its path is
# ``<tmp>/paw-run-skills-p<pid>/paw-run-skills-<fingerprint>``, where the
# fingerprint covers the matched skills' names and files, so every caller in
# this process asking for the same skills gets the same stable path. It is
# built in a sibling staging dir and renamed into place, and reference-counted:
# ``materialize_run_skills`` takes a reference, ``cleanup_run_skills`` drops
# one, and the dir is removed only when the last reference goes. A warm Claude
# CLI is bound to the path it connected with, so a holder (the SDK backend's
# per-digest cache) keeps its reference for as long as such a client may live.
#
# Name resolution: an installed skill (``SkillLoader.SKILL_PATHS``) wins; the
# packaged ``_bundled/skills/`` dir is a last-resort fallback so a surface can
# name a bundled skill even when the boot-time ~/.claude/skills mirror is off.
#
# Invariants: every path component written here is filesystem-safe on every
# OS (skill slugs are validated, unsafe ones skipped; dir names are hex). The
# store root is per process, so the in-process reference counts are the whole
# truth about who uses a dir. ``fs_safe_component`` stays exported for callers
# that fold a session key into a path.

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path

from pocketpaw.skills.loader import get_skill_loader

logger = logging.getLogger(__name__)

# Same guard the GitHub installer uses (``installer.py``): a skill slug becomes
# a directory name under the materialized plugin, so reject path traversal and
# any character outside the safe set before it ever touches the filesystem.
_SLUG_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

_TMP_PREFIX = "paw-run-skills-"

# Default cap for ``fs_safe_component``. Keeps deep temp roots well clear of
# Windows MAX_PATH once skill paths are appended underneath.
_RUN_ID_MAX_LEN = 48

# Reference counts for the materialized plugin dirs this process handed out.
# One lock guards counts AND filesystem build/removal, so a release that drops
# a dir to zero can never race an acquire that is about to reuse it.
_REFS: dict[Path, int] = {}
_REFS_LOCK = threading.Lock()

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
    """Materialize the NAMED installed skills into a shared local-plugin dir.

    Filters ``get_skill_loader().get_all()`` to the requested ``skill_names``,
    copies each matched skill's WHOLE directory (``Skill.path.parent`` — the dir
    holding ``SKILL.md`` plus any assets) into ``<root>/skills/<slug>/``, and
    writes a minimal ``<root>/.claude-plugin/plugin.json`` so the directory is
    a valid Claude Code local plugin.

    The dir is content-addressed (see the module header): the same skills give
    the same path, built once and reused while anyone holds a reference. Each
    call takes ONE reference.

    Args:
        skill_names: the entity's requested skill names (from the resolved
            ``SurfaceProfile.skill_names``). Unknown names are skipped with a
            log line — a typo in one name never fails the whole run.
        run_id: optional run identifier (usually the session key), used only
            in log lines. It never reaches the path, which is shared.

    Returns:
        The plugin ROOT directory (pass to the SDK as
        ``{"type": "local", "path": str(root)}``), or ``None`` when no requested
        name matched an installed skill (caller then skips the ``plugins=``
        wiring entirely). Every non-None return MUST be released exactly once
        via :func:`cleanup_run_skills`, and not before every client connected
        with the path is gone.
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

    store = _store_root()
    root = store / f"{_TMP_PREFIX}{_fingerprint(matched)}"
    with _REFS_LOCK:
        if not _is_plugin_dir(root):
            if root.exists():
                # Half-built or partly reaped by a tmp cleaner: rebuild it. Any
                # client still pointing here gets the same content back.
                shutil.rmtree(root, ignore_errors=True)
            staged = _build_plugin(store, matched)
            if staged is None:
                return None
            try:
                os.replace(staged, root)
            except OSError as exc:
                shutil.rmtree(staged, ignore_errors=True)
                if not _is_plugin_dir(root):
                    logger.warning(
                        "materialize_run_skills: could not publish plugin dir %s: %s", root, exc
                    )
                    return None
            logger.info(
                "materialize_run_skills: materialized %s into %s (run %s)",
                [slug for slug, _ in matched],
                root,
                run_id or "-",
            )
        _REFS[root] = _REFS.get(root, 0) + 1
    return root


def _store_root() -> Path:
    """Per-process parent of every materialized plugin dir."""
    return Path(tempfile.gettempdir()) / f"{_TMP_PREFIX}p{os.getpid()}"


def _fingerprint(matched: list[tuple[str, Path]]) -> str:
    """Hash of the matched skills: slug, source dir, and each file's relative
    path, size and mtime. Changed skill files give a new dir; unchanged ones
    the same dir."""
    h = hashlib.sha256()
    for slug, src_dir in sorted(matched):
        h.update(f"\0{slug}\0{src_dir}\0".encode("utf-8", "replace"))
        try:
            files = sorted(p for p in src_dir.rglob("*") if not p.is_symlink() and p.is_file())
        except OSError:
            files = []
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            rel = f.relative_to(src_dir).as_posix()
            h.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\0".encode("utf-8", "replace"))
    return h.hexdigest()[:16]


def _is_plugin_dir(root: Path) -> bool:
    return (root / ".claude-plugin" / "plugin.json").is_file()


def _build_plugin(store: Path, matched: list[tuple[str, Path]]) -> Path | None:
    """Build the plugin in a staging dir under ``store``; None if nothing copied."""
    store.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=".staging-", dir=store))
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
        shutil.rmtree(root, ignore_errors=True)
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
    return root


def cleanup_run_skills(root: Path | None) -> None:
    """Release one reference to a materialized plugin dir. Best-effort, never raises.

    The dir is removed only when its last reference is released, so a holder
    that still has a client connected with the path keeps it on disk. Safe to
    call with ``None`` (the no-skills path); a path with no references left
    (or one this process never handed out) is simply removed, so a repeat call
    is harmless.
    """
    if root is None:
        return
    root = Path(root)
    try:
        with _REFS_LOCK:
            count = _REFS.get(root, 0)
            if count > 1:
                _REFS[root] = count - 1
                return
            _REFS.pop(root, None)
            shutil.rmtree(root, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 — cleanup must never break a run
        logger.debug("cleanup_run_skills: failed to remove %s: %s", root, exc)


__all__ = ["cleanup_run_skills", "fs_safe_component", "materialize_run_skills"]
