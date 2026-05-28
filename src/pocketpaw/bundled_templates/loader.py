# src/pocketpaw/bundled_templates/loader.py
# Created: 2026-05-22 (feat/bundled-templates, Increment 2a) — reads a
# single installed pocket template back from ``~/.pocketpaw/templates/``
# so the create specialist can instantiate-and-customize it instead of
# generating a pocket from scratch.
# Modified 2026-05-25 (feat/rfc-03-v2-schema-chokepoint): added
# ``_promote_v1_to_v2`` private translation function (4 RFC rewrite
# rules), added optional ``strict`` kwarg on ``load_template`` that
# routes through the new ``PocketTemplate`` Pydantic model and raises
# ``TemplateValidationError`` on failure; ``strict=False`` (the default)
# preserves the original log-warning + return-None behaviour for
# back-compat with current EE consumers.
"""Load an installed pocket template from the user's PocketPaw dir.

The installer (``bundled_templates.installer``) mirrors the bundled
templates into ``~/.pocketpaw/templates/<slug>/`` on boot. This module
reads ONE template back by slug — the create specialist calls it when
the chat agent's STEP 0 keyword match set a ``template_id`` hint.

A template directory holds exactly two files:

- ``template.pocket.yaml`` — RFC 03 v2 Pocket Template Schema metadata.
  v1 templates on disk are auto-promoted by ``_promote_v1_to_v2`` at
  read time; the runtime always sees a v2-shaped dict.
- ``ripple_spec.json``     — the hand-authored rippleSpec skeleton.

``load_template`` returns ``{"meta": <yaml dict>, "ripple_spec": <json
dict>}`` or ``None`` on any failure (unknown slug, missing file, parse
error, permission error, schema validation error in ``strict=True``
mode). The caller treats ``None`` as "no template — fall back to cold
generation"; a template-load failure must never block pocket creation
in the default (non-strict) path.

When ``strict=True`` (CLI ``template lint``, unit tests), a schema
validation failure raises ``TemplateValidationError`` instead of
returning ``None`` so the exact problem can be surfaced to the author.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pocketpaw.bundled_templates.errors import TemplateValidationError

logger = logging.getLogger(__name__)

# Default install location — kept in sync with
# ``installer.install_bundled_templates``'s destination default.
_DEFAULT_TEMPLATES_DIR = Path.home() / ".pocketpaw" / "templates"


def _title_case_slug(slug: str) -> str:
    """Synthesize a display_name from a kebab-case slug by title-casing
    each hyphen-delimited word. ``"todo-task-tracker"`` -> ``"Todo Task
    Tracker"``."""
    return " ".join(word.capitalize() for word in slug.split("-"))


def _lookup_pattern_in_index(name: str, templates_dir: Path | None) -> str | None:
    """Look up ``pattern`` for a template by ``name`` in the sibling
    ``index.json``. Returns ``None`` if the index is missing, unreadable,
    malformed, or the row is absent."""
    if templates_dir is None:
        return None
    index_path = templates_dir / "index.json"
    if not index_path.is_file():
        return None
    try:
        index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — best-effort lookup
        logger.debug("bundled_templates.loader: could not read sibling index.json: %s", exc)
        return None
    rows = index.get("templates", []) if isinstance(index, dict) else []
    for row in rows:
        if isinstance(row, dict) and row.get("slug") == name:
            pat = row.get("pattern")
            if isinstance(pat, str):
                return pat
    return None


def _promote_v1_to_v2(meta: dict[str, Any], *, templates_dir: Path | None = None) -> dict[str, Any]:
    """Apply the four RFC 03 v1 -> v2 translation rules in-place on a
    COPY of the input dict, returning the promoted dict.

    The translation is **non-destructive**: callers pass a freshly
    loaded dict; this function never writes back to disk.

    Rules (per RFC 03 v2, "Schema version" -> "Translation: v1 -> v2"):

    1. Absent ``schema_version`` -> ``"2"``.
    2. Absent ``display_name`` -> title-case the ``name`` slug.
    3. ``skills`` -> ``skill_refs`` (array contents identical; if both
       are present, ``skill_refs`` wins and ``skills`` is dropped).
    4. Absent ``pattern`` -> look up by ``name`` in the sibling
       ``index.json``; fall back to ``"app"`` if missing.

    The function is idempotent on v2-shaped input (each rule is a
    "fill if absent" or "rename if present" — no field is overwritten
    when already correctly populated).

    Args:
        meta: Parsed YAML dict from ``template.pocket.yaml``.
        templates_dir: Optional path to the templates root; used only
            for Rule 4's ``index.json`` lookup. Pass ``None`` to skip
            the lookup (rule 4 then falls back to ``"app"`` directly).

    Returns:
        A new dict with the four translations applied. The input dict
        is not mutated.
    """
    out = dict(meta)  # shallow copy — sub-dicts share references

    # Rule 1: schema_version
    if "schema_version" not in out:
        out["schema_version"] = "2"

    # Rule 3: skills -> skill_refs (run before rule 2 just for clarity;
    # ordering does not matter since the rules are independent).
    if "skills" in out:
        if "skill_refs" not in out:
            out["skill_refs"] = out["skills"]
        del out["skills"]

    # Rule 2: display_name
    if "display_name" not in out:
        slug = out.get("name")
        if isinstance(slug, str) and slug:
            out["display_name"] = _title_case_slug(slug)

    # Rule 4: pattern
    if "pattern" not in out:
        name = out.get("name")
        looked_up = _lookup_pattern_in_index(name, templates_dir) if isinstance(name, str) else None
        out["pattern"] = looked_up if looked_up is not None else "app"

    return out


def load_template(
    slug: str,
    *,
    templates_dir: Path | None = None,
    strict: bool = False,
) -> dict[str, Any] | None:
    """Read one installed template by slug.

    Args:
        slug: The template directory name (e.g. ``"todo-task-tracker"``).
        templates_dir: Override the templates root — useful for tests.
            Defaults to ``~/.pocketpaw/templates/``.
        strict: When ``True``, the parsed (and v1->v2-promoted) meta
            dict is validated through the ``PocketTemplate`` Pydantic
            model. Validation failures raise
            ``TemplateValidationError`` instead of returning ``None``.
            Defaults to ``False`` for back-compat with existing EE
            consumers that pattern-match on ``None``.

    Returns:
        ``{"meta": <parsed + promoted template.pocket.yaml>,
        "ripple_spec": <parsed ripple_spec.json>}`` on success, or
        ``None`` on ANY failure when ``strict=False`` — unknown slug, a
        missing sibling file, a YAML/JSON parse error, an I/O error, or
        a Pydantic validation error. The caller falls back to cold
        generation when this returns ``None``.

    Raises:
        TemplateValidationError: Only when ``strict=True`` AND the
            template fails Pydantic validation. All other failure modes
            still return ``None`` (the strict flag only escalates the
            schema-validation failure mode — it does NOT promote I/O or
            parse errors).
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
        import yaml  # noqa: PLC0415 — lazy import preserves OSS install path

        meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))
        ripple_spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — any parse / I/O error -> None
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

    # v1 -> v2 promotion. Idempotent on v2 input.
    merged_meta = _promote_v1_to_v2(meta, templates_dir=templates_dir)

    # Pydantic validation gate. Lazy import keeps the optional code path
    # off the hot loader-import boot path (matters: dashboard boot calls
    # the installer / loader before pydantic is necessarily live).
    try:
        from pydantic import ValidationError  # noqa: PLC0415 — lazy

        from pocketpaw.bundled_templates.schema import (  # noqa: PLC0415 — lazy
            PocketTemplate,
        )

        PocketTemplate.model_validate(merged_meta)
    except ValidationError as exc:
        if strict:
            raise TemplateValidationError(slug, exc) from exc
        logger.warning(
            "bundled_templates.loader: template %r failed schema validation: %s",
            slug,
            exc,
        )
        return None
    except Exception as exc:  # noqa: BLE001 — unexpected import-time failures
        # An import failure of schema.py would be a bug; surface it
        # under strict, swallow under non-strict for safety.
        if strict:
            raise
        logger.warning(
            "bundled_templates.loader: unexpected error validating template %r: %s",
            slug,
            exc,
        )
        return None

    # Preserve the historical dict return contract — return the
    # promoted meta dict, not the Pydantic instance. EE consumers
    # pattern-match on ``meta["name"]``, ``meta["state"]["entity_type"]``
    # etc.; never on a Pydantic model.
    return {"meta": merged_meta, "ripple_spec": ripple_spec}


__all__ = ["_promote_v1_to_v2", "load_template"]
