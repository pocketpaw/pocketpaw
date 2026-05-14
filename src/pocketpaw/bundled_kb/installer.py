# src/pocketpaw/bundled_kb/installer.py
# Created: 2026-05-14 (feat/ripple-recipes-poc) — auto-installs the
# bundled kb-go scopes from
# ``src/pocketpaw/bundled_kb/_bundled/<scope>/`` into the user's
# ``~/.knowledge-base/<scope>/`` so PocketPaw's existing
# ``_get_kb_context`` injection (bootstrap.context_builder) can
# retrieve the recipes via BM25 search at pocket-creation time.
"""Auto-install bundled kb-go scopes into the user's kb-go root.

Why this exists
---------------

PocketPaw's chat agent already runs kb-go queries via the existing
``_get_kb_context`` injection in ``bootstrap/context_builder.py``
(PR #913). Articles from configured scopes get retrieved by intent
and spliced into the agent's system prompt up to the ``kb_context``
token budget (~3000 chars).

We use this to ship ``ripple-recipes`` — 3 hand-authored pattern
recipes pre-compiled via kb-go's agent-mode flow (``kb prepare`` →
agent compilation → ``kb accept``). The compiled scope is a ~64KB
directory tree (``cache/`` + ``raw/`` + ``wiki/`` + ``index.json``)
that lives inside the Python package and is mirrored to
``~/.knowledge-base/ripple-recipes/`` on boot.

Behavior
--------

- **First boot**: scope copied to ``~/.knowledge-base/<scope>/``.
- **Subsequent boots, same content**: no-op (SHA-256 match per file).
- **PocketPaw upgrade with new scope content**: overwrites the user's
  copy. We don't merge customisations — operators who hand-edit the
  scope should set ``POCKETPAW_AUTO_INSTALL_BUNDLED_KB_SCOPES=false``.
- **Permissions / I/O failures**: logged at WARNING, never raised.
  KB retrieval is a non-critical enhancement — pocket creation still
  works via the existing MCP tool + skill flow even when the bundled
  scope can't install.

Opt-out
-------

Set ``POCKETPAW_AUTO_INSTALL_BUNDLED_KB_SCOPES=false`` in the
environment. The MCP-tool path still works; users who want recipe
retrieval can manually copy the scope from
``src/pocketpaw/bundled_kb/_bundled/``.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Where the bundled kb scope directories live inside the package.
_BUNDLED_DIR = Path(__file__).parent / "_bundled"


@dataclass(frozen=True)
class KbInstallResult:
    """Per-scope install outcome surfaced by ``install_bundled_kb_scopes``.

    ``status`` is one of:
      - ``"installed"`` — destination didn't exist; freshly copied.
      - ``"updated"``   — destination existed but content hash differed.
      - ``"skipped"``   — destination existed and hash matched; no-op.
      - ``"failed"``    — I/O error during install; details in ``error``.
    """

    name: str
    status: str
    destination: Path
    error: str | None = None


def install_bundled_kb_scopes(
    *, destination_root: Path | None = None
) -> list[KbInstallResult]:
    """Mirror every bundled kb-go scope into the user's kb-go root.

    Args:
        destination_root: Override the install target — useful for
            tests. Defaults to ``~/.knowledge-base/``.

    Returns:
        A list of ``KbInstallResult``s, one per bundled scope. Sorted
        by scope name for deterministic ordering.

    Never raises — per-scope failures are caught and surfaced via
    ``KbInstallResult.status == "failed"`` so a permission error on
    one scope doesn't block others.
    """

    if destination_root is None:
        destination_root = Path.home() / ".knowledge-base"

    if not _BUNDLED_DIR.is_dir():
        logger.warning(
            "bundled_kb.installer: bundled dir %s missing — nothing to install",
            _BUNDLED_DIR,
        )
        return []

    results: list[KbInstallResult] = []
    for scope_dir in sorted(p for p in _BUNDLED_DIR.iterdir() if p.is_dir()):
        result = _install_one(scope_dir, destination_root)
        results.append(result)
        logger.info(
            "bundled_kb.installer: %s -> %s",
            scope_dir.name,
            result.status,
        )
    return results


def _install_one(scope_src: Path, destination_root: Path) -> KbInstallResult:
    """Mirror one bundled scope directory to the user's kb-go root.

    Same SHA-256-hash-compare-per-file pattern as
    ``bundled_skills.installer._install_one`` — keep the two
    installers symmetric so the file-mirror logic is one mental model.
    """

    name = scope_src.name
    dest_dir = destination_root / name

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return KbInstallResult(
            name=name,
            status="failed",
            destination=dest_dir,
            error=f"mkdir failed: {exc}",
        )

    dest_existed = any(dest_dir.iterdir())
    any_change = False
    try:
        for src_file in scope_src.rglob("*"):
            if not src_file.is_file():
                continue
            relative = src_file.relative_to(scope_src)
            dest_file = dest_dir / relative
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            if dest_file.exists() and _sha256(dest_file) == _sha256(src_file):
                continue
            shutil.copy2(src_file, dest_file)
            any_change = True
    except OSError as exc:
        return KbInstallResult(
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
    return KbInstallResult(name=name, status=status, destination=dest_dir)


def _sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's contents.

    Reads in 64 KB chunks so the function works for kb-go's
    ``index.json`` (which can grow large for big scopes) without
    holding the whole file in memory.
    """

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = ["KbInstallResult", "install_bundled_kb_scopes"]
