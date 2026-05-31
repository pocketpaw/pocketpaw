# src/pocketpaw/bundled_templates/installer.py
# Created: 2026-05-22 (feat/bundled-templates, Increment 2a) — auto-installs
# the built-in pocket templates from
# ``src/pocketpaw/bundled_templates/_bundled/<slug>/`` into the user's
# ``~/.pocketpaw/templates/<slug>/`` so the create specialist can
# instantiate-and-customize a matching template instead of generating a
# pocket from scratch. Idempotent via SHA-256 hash comparison — the same
# mirror pattern as ``bundled_kb.installer`` and ``bundled_skills.installer``.
"""Auto-install bundled pocket templates into the user's PocketPaw dir.

Why this exists
---------------

The pocket-authoring agent generates every pocket from scratch — slow,
2-3 iterations, half-baked. PocketPaw ships a curated set of built-in
templates (todo tracker, kanban board, metrics dashboard, CRM list,
calendar planner, activity feed). On boot the installer mirrors them
into ``~/.pocketpaw/templates/`` so the create specialist — and the
chat agent's STEP 0 template-library check — can read a template back
and customize it instead of cold-generating.

Behavior
--------

- **First boot**: every ``_bundled/<slug>/`` directory plus the
  top-level ``index.json`` are copied to ``~/.pocketpaw/templates/``.
- **Subsequent boots, same content**: no-op (SHA-256 match per file).
- **PocketPaw upgrade with new template content**: overwrites the
  user's copy. We don't merge customizations — operators who hand-edit
  a template should set
  ``POCKETPAW_AUTO_INSTALL_BUNDLED_TEMPLATES=false``.
- **Permissions / I/O failures**: logged at WARNING, never raised.
  The template library is a non-critical enhancement — pocket creation
  still works (the specialist cold-generates) even when the install
  can't run.

Opt-out
-------

Set ``POCKETPAW_AUTO_INSTALL_BUNDLED_TEMPLATES=false`` in the
environment. The cold-generation path still works; users who want the
template boost can manually copy ``_bundled/`` into
``~/.pocketpaw/templates/``.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Where the bundled template directories live inside the package.
_BUNDLED_DIR = Path(__file__).parent / "_bundled"


@dataclass(frozen=True)
class TemplateInstallResult:
    """Per-template install outcome surfaced by ``install_bundled_templates``.

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


def install_bundled_templates(
    *, destination_root: Path | None = None
) -> list[TemplateInstallResult]:
    """Mirror every bundled pocket template into the user's PocketPaw dir.

    Both the per-slug template directories (``<slug>/template.pocket.yaml``
    + ``<slug>/ripple_spec.json``) and the top-level ``index.json`` are
    copied. ``index.json`` is mirrored as a synthetic result entry named
    ``"index.json"`` so a hash-drift on the index alone is visible.

    Args:
        destination_root: Override the install target — useful for
            tests. Defaults to ``~/.pocketpaw/templates/``.

    Returns:
        A list of ``TemplateInstallResult``s, one per bundled template
        plus one for ``index.json``. Sorted by name for deterministic
        ordering.

    Never raises — per-entry failures are caught and surfaced via
    ``TemplateInstallResult.status == "failed"`` so a permission error
    on one entry doesn't block the others.
    """

    if destination_root is None:
        destination_root = Path.home() / ".pocketpaw" / "templates"

    if not _BUNDLED_DIR.is_dir():
        logger.warning(
            "bundled_templates.installer: bundled dir %s missing — nothing to install",
            _BUNDLED_DIR,
        )
        return []

    results: list[TemplateInstallResult] = []

    for slug_dir in sorted(p for p in _BUNDLED_DIR.iterdir() if p.is_dir()):
        result = _install_one(slug_dir, destination_root)
        results.append(result)
        logger.info(
            "bundled_templates.installer: %s -> %s",
            slug_dir.name,
            result.status,
        )

    # The registry index sits beside the slug directories — copy it too
    # so the chat agent's STEP 0 keyword match has the index to read.
    index_src = _BUNDLED_DIR / "index.json"
    if index_src.is_file():
        index_result = _install_index(index_src, destination_root)
        results.append(index_result)
        logger.info(
            "bundled_templates.installer: index.json -> %s",
            index_result.status,
        )

    results.sort(key=lambda r: r.name)
    return results


def _install_one(slug_src: Path, destination_root: Path) -> TemplateInstallResult:
    """Mirror one bundled template directory to the user's PocketPaw dir.

    Same SHA-256-hash-compare-per-file pattern as
    ``bundled_kb.installer._install_one`` — keep the three installers
    symmetric so the file-mirror logic is one mental model.
    """

    name = slug_src.name
    dest_dir = destination_root / name

    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return TemplateInstallResult(
            name=name,
            status="failed",
            destination=dest_dir,
            error=f"mkdir failed: {exc}",
        )

    dest_existed = any(dest_dir.iterdir())
    any_change = False
    try:
        for src_file in slug_src.rglob("*"):
            if not src_file.is_file():
                continue
            relative = src_file.relative_to(slug_src)
            dest_file = dest_dir / relative
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            if dest_file.exists() and _sha256(dest_file) == _sha256(src_file):
                continue
            shutil.copy2(src_file, dest_file)
            any_change = True
    except OSError as exc:
        return TemplateInstallResult(
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
    return TemplateInstallResult(name=name, status=status, destination=dest_dir)


def _install_index(index_src: Path, destination_root: Path) -> TemplateInstallResult:
    """Mirror the top-level ``index.json`` registry into the user's dir.

    Single-file twin of ``_install_one`` — the index is not inside a
    slug directory so it gets its own copy step. Same SHA-256 compare
    decides installed / updated / skipped.
    """

    dest_file = destination_root / "index.json"
    try:
        destination_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return TemplateInstallResult(
            name="index.json",
            status="failed",
            destination=dest_file,
            error=f"mkdir failed: {exc}",
        )

    dest_existed = dest_file.exists()
    try:
        if dest_existed and _sha256(dest_file) == _sha256(index_src):
            return TemplateInstallResult(name="index.json", status="skipped", destination=dest_file)
        shutil.copy2(index_src, dest_file)
    except OSError as exc:
        return TemplateInstallResult(
            name="index.json",
            status="failed",
            destination=dest_file,
            error=f"copy failed: {exc}",
        )

    status = "updated" if dest_existed else "installed"
    return TemplateInstallResult(name="index.json", status=status, destination=dest_file)


def _sha256(path: Path) -> str:
    """Return the SHA-256 hex digest of a file's contents.

    Reads in 64 KB chunks so the function works for the rippleSpec JSON
    files (which can grow large for rich templates) without holding the
    whole file in memory.
    """

    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


__all__ = ["TemplateInstallResult", "install_bundled_templates"]
