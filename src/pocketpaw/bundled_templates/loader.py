# src/pocketpaw/bundled_templates/loader.py
# Created: 2026-05-22 (feat/bundled-templates, Increment 2a) — reads a
# single installed pocket template back from ``~/.pocketpaw/templates/``
# so the create specialist can instantiate-and-customize it instead of
# generating a pocket from scratch.
"""Load an installed pocket template from the user's PocketPaw dir.

The installer (``bundled_templates.installer``) mirrors the bundled
templates into ``~/.pocketpaw/templates/<slug>/`` on boot. This module
reads ONE template back by slug — the create specialist calls it when
the chat agent's STEP 0 keyword match set a ``template_id`` hint.

A template directory holds exactly two files:

- ``template.pocket.yaml`` — RFC 03 Pocket Template Schema metadata.
- ``ripple_spec.json``     — the hand-authored rippleSpec skeleton.

``load_template`` returns ``{"meta": <yaml dict>, "ripple_spec": <json
dict>}`` or ``None`` on any failure (unknown slug, missing file, parse
error, permission error). The caller treats ``None`` as "no template —
fall back to cold generation"; a template-load failure must never block
pocket creation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Default install location — kept in sync with
# ``installer.install_bundled_templates``'s destination default.
_DEFAULT_TEMPLATES_DIR = Path.home() / ".pocketpaw" / "templates"


def load_template(slug: str, *, templates_dir: Path | None = None) -> dict[str, Any] | None:
    """Read one installed template by slug.

    Args:
        slug: The template directory name (e.g. ``"todo-task-tracker"``).
        templates_dir: Override the templates root — useful for tests.
            Defaults to ``~/.pocketpaw/templates/``.

    Returns:
        ``{"meta": <parsed template.pocket.yaml>, "ripple_spec":
        <parsed ripple_spec.json>}`` on success, or ``None`` on ANY
        failure — unknown slug, a missing sibling file, a YAML/JSON
        parse error, or an I/O error. The caller falls back to cold
        generation when this returns ``None``.

    Never raises. A template-load failure is logged at WARNING and
    surfaced as ``None`` — pocket creation must not break because a
    template file is missing or corrupt.
    """

    if templates_dir is None:
        templates_dir = _DEFAULT_TEMPLATES_DIR

    # Guard against a slug that tries to escape the templates root via
    # path traversal — the slug is an untrusted hint from the chat agent.
    if not slug or "/" in slug or "\\" in slug or slug in (".", ".."):
        logger.warning("bundled_templates.loader: rejecting unsafe slug %r", slug)
        return None

    slug_dir = templates_dir / slug
    meta_path = slug_dir / "template.pocket.yaml"
    spec_path = slug_dir / "ripple_spec.json"

    if not meta_path.is_file() or not spec_path.is_file():
        logger.warning(
            "bundled_templates.loader: template %r incomplete or missing (meta=%s spec=%s)",
            slug,
            meta_path.is_file(),
            spec_path.is_file(),
        )
        return None

    try:
        import yaml

        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        ripple_spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — any parse / I/O error → None
        logger.warning(
            "bundled_templates.loader: failed to load template %r: %s",
            slug,
            exc,
        )
        return None

    if not isinstance(meta, dict) or not isinstance(ripple_spec, dict):
        logger.warning(
            "bundled_templates.loader: template %r has a non-dict file (meta=%s spec=%s)",
            slug,
            type(meta).__name__,
            type(ripple_spec).__name__,
        )
        return None

    return {"meta": meta, "ripple_spec": ripple_spec}


__all__ = ["load_template"]
