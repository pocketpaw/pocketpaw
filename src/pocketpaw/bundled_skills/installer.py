# src/pocketpaw/bundled_skills/installer.py
# Created: 2026-05-14 (feat/pocket-creator-skill) — auto-installs the
# bundled Claude Code skill files from
# ``src/pocketpaw/bundled_skills/_bundled/<name>/`` into the user's
# ``~/.claude/skills/<name>/`` so the chat agent can invoke them
# without the operator manually staging the files. Idempotent via
# SHA-256 hash comparison.
"""Auto-install bundled Claude Code skills into the user config dir.

Why this exists
---------------

Claude Code looks for skills in ``~/.claude/skills/<name>/SKILL.md``
(and in cwd-local ``.claude/skills/`` when running from a project
directory). PocketPaw runs from the user's home dir, so cwd-local
skills aren't visible — the chat agent only sees ``~/.claude/skills/``.

We ship the ``pocketpaw-create-pocket`` skill (and any future skills)
inside the Python package at
``src/pocketpaw/bundled_skills/_bundled/<name>/SKILL.md``. On boot,
the installer mirrors those files into ``~/.claude/skills/<name>/``
so the SDK picks them up.

Behavior
--------

- **First boot**: skill file copied to ``~/.claude/skills/<name>/SKILL.md``.
- **Subsequent boots, same content**: no-op (SHA-256 match).
- **PocketPaw upgrade with new skill content**: overwrites the user's
  copy. We don't merge user customizations — if you've edited the
  file by hand, set ``POCKETPAW_AUTO_INSTALL_BUNDLED_SKILLS=false`` to freeze
  your version.
- **Permissions / I/O failures**: logged at WARNING, never raised.
  Skill installation is best-effort — pocket creation still works
  via the MCP tool even when the skill isn't installed.

Opt-out
-------

Set ``POCKETPAW_AUTO_INSTALL_BUNDLED_SKILLS=false`` in the environment to
disable the installer entirely. The MCP-tool flow still works; users
who want the skill behavior can stage the files manually from
``src/pocketpaw/bundled_skills/_bundled/``.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Where the bundled skill files live inside the Python package.
_BUNDLED_DIR = Path(__file__).parent / "_bundled"


@dataclass(frozen=True)
class InstallResult:
    """Per-skill install outcome surfaced by ``install_bundled_skills``.

    ``status`` is one of:
      - ``"installed"`` — destination didn't exist; freshly copied.
      - ``"updated"``   — destination existed but content hash differed;
                          overwritten.
      - ``"skipped"``   — destination existed and hash matched; no-op.
      - ``"failed"``    — I/O error during install; details in ``error``.
    """

    name: str
    status: str
    destination: Path
    error: str | None = None


def install_bundled_skills(*, destination_root: Path | None = None) -> list[InstallResult]:
    """Mirror every bundled skill into the user's Claude config.

    Args:
        destination_root: Override the install target — useful for
            tests. Defaults to ``~/.claude/skills/``. When the override
            is supplied, the standard home-dir resolution is skipped
            entirely.

    Returns:
        A list of ``InstallResult``s, one per bundled skill. The list
        is sorted by skill name for deterministic ordering.

    The function never raises. Per-skill failures are caught and
    surfaced via ``InstallResult.status == "failed"`` so a permission
    error on one skill doesn't block install of the others.
    """

    if destination_root is None:
        destination_root = Path.home() / ".claude" / "skills"

    if not _BUNDLED_DIR.is_dir():
        logger.warning(
            "bundled_skills.installer: bundled dir %s missing — nothing to install",
            _BUNDLED_DIR,
        )
        return []

    results: list[InstallResult] = []
    for skill_dir in sorted(p for p in _BUNDLED_DIR.iterdir() if p.is_dir()):
        result = _install_one(skill_dir, destination_root)
        results.append(result)
        logger.info(
            "bundled_skills.installer: %s -> %s",
            skill_dir.name,
            result.status,
        )
    return results


def _install_one(skill_src: Path, destination_root: Path) -> InstallResult:
    """Copy a single bundled skill directory into the user's Claude
    config. Used by ``install_bundled_skills`` per directory entry.

    The skill directory is mirrored verbatim — every file under
    ``_bundled/<name>/`` is copied to ``~/.claude/skills/<name>/``
    preserving the subdirectory structure. SHA-256 hash comparison
    decides between ``installed`` / ``updated`` / ``skipped``.
    """

    name = skill_src.name
    dest_dir = destination_root / name

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return InstallResult(
            name=name,
            status="failed",
            destination=dest_dir,
            error=f"mkdir failed: {exc}",
        )

    # The dest_dir was just created if missing; if it pre-existed it
    # may already have files. We track that to disambiguate
    # "installed" vs "updated" in the result.
    dest_existed = any(dest_dir.iterdir())

    any_change = False
    try:
        for src_file in skill_src.rglob("*"):
            if not src_file.is_file():
                continue
            relative = src_file.relative_to(skill_src)
            dest_file = dest_dir / relative
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            if dest_file.exists() and _sha256(dest_file) == _sha256(src_file):
                # Content matches — leave it alone.
                continue
            shutil.copy2(src_file, dest_file)
            any_change = True
    except OSError as exc:
        return InstallResult(
            name=name,
            status="failed",
            destination=dest_dir,
            error=f"copy failed: {exc}",
        )

    if not any_change:
        status = "skipped"
    elif dest_existed:
        status = "updated"
    else:
        status = "installed"
    return InstallResult(name=name, status=status, destination=dest_dir)


def _sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's contents.

    Reads in 64 KB chunks so the function works for skill files of any
    size without holding the whole file in memory. The hash is the
    comparison primitive that decides install / update / skip.
    """

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = ["InstallResult", "install_bundled_skills"]
