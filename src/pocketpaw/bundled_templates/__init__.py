# src/pocketpaw/bundled_templates/__init__.py
# Created: 2026-05-22 (feat/bundled-templates, Increment 2a) — package
# for the curated set of built-in pocket templates the create
# specialist instantiates instead of generating a pocket from scratch.
# Modified 2026-05-25 (feat/rfc-03-v2-schema-chokepoint): re-exports
# ``PocketTemplate``, ``TemplateValidationError``, and the sub-models
# so callers (CLI, tests, future runtime) can import them at the
# package root without reaching into ``schema`` / ``errors`` submodules.
# Modified 2026-05-25 (feat/rfc-03-v2-compile): re-exports
# ``compile_template`` — the OSS-side template-to-runtime translation
# seam. PR 2b implements ``data_sources[]`` only; other top-level
# fields passthrough until PRs 2c-2g land their runtime executors.
# Modified 2026-05-28 (feat/rfc-03-v2-cel-eval): re-exports the CEL
# runtime evaluator (``evaluate_cel``, ``CelEvaluationError``) and the
# identifier-resolver Protocol (``IdentifierResolver``,
# ``TemplateIdentifierResolver``). PR 2c lays the foundation that
# PR 2d (Instinct 5-step composer), PR 2f (temporal trigger sweeper),
# and PR 2g (Fabric ``tier: registered`` linter) all consume.
"""Built-in pocket templates bundled and auto-installed by PocketPaw.

Third sibling to ``pocketpaw.bundled_skills`` and ``pocketpaw.bundled_kb``.
Where ``bundled_skills`` ships on-demand workflow markdown and ``bundled_kb``
ships pre-compiled kb-go retrieval scopes, ``bundled_templates`` ships
**ready-to-instantiate pocket templates** — hand-authored, production-quality
rippleSpec skeletons paired with RFC 03 v2 schema metadata.

Why this exists
---------------

The pocket-authoring agent generates every pocket from scratch — slow, 2-3
iterations, half-baked. A "todo dashboard" is a solved pattern; it should be
a template, not a fresh generation. The create specialist *instantiates and
customizes* a matching built-in template instead of generating one cold.

Each template is a directory under ``_bundled/<slug>/`` carrying two files:

- ``template.pocket.yaml`` — RFC 03 v2 Pocket Template Schema metadata
  (``schema_version, name, version, vertical, pattern, display_name,
  shape, state, ...``). Seed templates ship ``actions: []`` — Instinct /
  Outcomes are not wired yet and dead action declarations are worse than
  none. The seed bundled templates were migrated v1 -> v2 in the same PR
  that introduced the Pydantic chokepoint.
- ``ripple_spec.json`` — a full, hand-authored rippleSpec skeleton: the
  quality lever. The specialist starts from a correct skeleton, not a
  pressured cold generation.

On dashboard boot the installer mirrors ``_bundled/`` into
``~/.pocketpaw/templates/`` (SHA-256 idempotent, same pattern as the two
sibling installers). The loader reads a single template back at
pocket-creation time, validating it against the ``PocketTemplate``
Pydantic v2 model. Validation failure returns ``None`` in the default
loader path (back-compat) or raises ``TemplateValidationError`` under
``strict=True`` (CLI ``template lint``, tests).

Adding a template: drop a new ``_bundled/<slug>/`` directory with the two
files and register it in ``_bundled/index.json``. The installer discovers
directories via iteration — no installer code changes needed.
"""

from pocketpaw.bundled_templates.cel_runtime import (
    CelEvaluationError,
    evaluate_cel,
)
from pocketpaw.bundled_templates.compile import compile_template
from pocketpaw.bundled_templates.errors import TemplateValidationError
from pocketpaw.bundled_templates.identifier_resolver import (
    IdentifierResolver,
    TemplateIdentifierResolver,
)
from pocketpaw.bundled_templates.installer import (
    TemplateInstallResult,
    install_bundled_templates,
)
from pocketpaw.bundled_templates.loader import load_template
from pocketpaw.bundled_templates.schema import (
    ActionDef,
    AgentDef,
    ColumnDef,
    ConfirmDef,
    DataSourceDef,
    InstinctRule,
    InstinctRulesDef,
    JoinedEntity,
    PermissionsDef,
    PocketTemplate,
    SavedView,
    StateBinding,
    TriggerDef,
)

__all__ = [
    "ActionDef",
    "AgentDef",
    "CelEvaluationError",
    "ColumnDef",
    "ConfirmDef",
    "DataSourceDef",
    "IdentifierResolver",
    "InstinctRule",
    "InstinctRulesDef",
    "JoinedEntity",
    "PermissionsDef",
    "PocketTemplate",
    "SavedView",
    "StateBinding",
    "TemplateIdentifierResolver",
    "TemplateInstallResult",
    "TemplateValidationError",
    "TriggerDef",
    "compile_template",
    "evaluate_cel",
    "install_bundled_templates",
    "load_template",
]
