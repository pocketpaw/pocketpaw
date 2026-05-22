# src/pocketpaw/bundled_templates/__init__.py
# Created: 2026-05-22 (feat/bundled-templates, Increment 2a) — package
# for the curated set of built-in pocket templates the create
# specialist instantiates instead of generating a pocket from scratch.
"""Built-in pocket templates bundled and auto-installed by PocketPaw.

Third sibling to ``pocketpaw.bundled_skills`` and ``pocketpaw.bundled_kb``.
Where ``bundled_skills`` ships on-demand workflow markdown and ``bundled_kb``
ships pre-compiled kb-go retrieval scopes, ``bundled_templates`` ships
**ready-to-instantiate pocket templates** — hand-authored, production-quality
rippleSpec skeletons paired with RFC 03 schema metadata.

Why this exists
---------------

The pocket-authoring agent generates every pocket from scratch — slow, 2-3
iterations, half-baked. A "todo dashboard" is a solved pattern; it should be
a template, not a fresh generation. The create specialist *instantiates and
customizes* a matching built-in template instead of generating one cold.

Each template is a directory under ``_bundled/<slug>/`` carrying two files:

- ``template.pocket.yaml`` — RFC 03 Pocket Template Schema metadata
  (``name, version, vertical, shape, state, actions, connectors, skills,
  description``). Seed templates ship ``actions: []`` — Instinct / Outcomes
  are not wired yet and dead action declarations are worse than none.
- ``ripple_spec.json`` — a full, hand-authored rippleSpec skeleton: the
  quality lever. The specialist starts from a correct skeleton, not a
  pressured cold generation.

On dashboard boot the installer mirrors ``_bundled/`` into
``~/.pocketpaw/templates/`` (SHA-256 idempotent, same pattern as the two
sibling installers). The loader reads a single template back at
pocket-creation time.

Adding a template: drop a new ``_bundled/<slug>/`` directory with the two
files and register it in ``_bundled/index.json``. The installer discovers
directories via iteration — no installer code changes needed.
"""

from pocketpaw.bundled_templates.installer import (
    TemplateInstallResult,
    install_bundled_templates,
)
from pocketpaw.bundled_templates.loader import load_template

__all__ = [
    "TemplateInstallResult",
    "install_bundled_templates",
    "load_template",
]
