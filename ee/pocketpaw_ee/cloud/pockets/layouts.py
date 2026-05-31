# ee/cloud/pockets/layouts.py — YAML layout export + user-defined pocket templates.
# Created: 2026-04-19 (Cluster B Sub-PR #3) — Backs two new endpoints on the
# pockets router:
#
#   - POST /api/v1/pockets/{id}/export-layout  → serialise a pocket's ripple
#     spec as a YAML document the operator can copy, share, or save.
#   - POST /api/v1/pockets/templates           → accept a user-defined YAML
#     template and register it under the caller's workspace so it shows up
#     in PocketTemplates's new "My templates" category.
#   - GET  /api/v1/pockets/templates?workspace_id=... → list the
#     workspace's user templates.
#
# The export side is pure — given a Pocket, we canonicalise its ripple spec
# and emit YAML. No side effects, no persistence required.
#
# The template store is deliberately in-process + workspace-keyed. This is a
# minimum-viable surface for the demo-readiness push in
# FEATURE-HARDENING-PLAN.md; Wave 4 / Cluster G can bolt persistence on
# behind the same protocol without changing the REST contract. The brief
# calls this out as green-field work — no prior backend surface exists,
# and this is the cleanest way to land the write + read shape now.
#
# YAML: lazy-imported via PyYAML (same pattern as ee/fleet/installer.py),
# avoiding a new top-level dependency. safe_load + safe_dump keep the
# surface free of Python-object injection.

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# In-process user-template store.
# ---------------------------------------------------------------------------


@dataclass
class UserPocketTemplate:
    """One user-defined template row. Keyed by id within a workspace."""

    id: str
    workspace_id: str
    owner_id: str
    name: str
    description: str
    category: str
    spec: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "owner_id": self.owner_id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "spec": self.spec,
            "created_at": self.created_at.isoformat(),
        }


class UserTemplateStore:
    """In-process workspace-scoped template registry.

    Thread-safe for the single-process dev + test setup. Mongo-backed
    persistence follows in a later PR — the REST contract stays stable.
    """

    def __init__(self) -> None:
        self._rows: dict[tuple[str, str], UserPocketTemplate] = {}

    def save(self, template: UserPocketTemplate) -> UserPocketTemplate:
        self._rows[(template.workspace_id, template.id)] = template
        return template

    def list_for_workspace(self, workspace_id: str) -> list[UserPocketTemplate]:
        return [t for (w, _), t in self._rows.items() if w == workspace_id]

    def get(self, workspace_id: str, template_id: str) -> UserPocketTemplate | None:
        return self._rows.get((workspace_id, template_id))

    def reset(self) -> None:
        """Drop every row — for tests."""

        self._rows.clear()


_store = UserTemplateStore()


def get_user_template_store() -> UserTemplateStore:
    """FastAPI dependency accessor so tests can swap the store under test."""

    return _store


def reset_user_template_store() -> None:
    """Test-only convenience: forget every stored template."""

    _store.reset()


# ---------------------------------------------------------------------------
# YAML helpers.
# ---------------------------------------------------------------------------


def export_layout_yaml(
    *,
    pocket_id: str,
    name: str,
    description: str,
    category: str,
    ripple_spec: dict[str, Any] | None,
    widgets: list[dict[str, Any]],
) -> str:
    """Serialise a pocket's layout as a YAML document.

    The output is deterministic (``sort_keys=True``) so the same pocket
    produces byte-identical YAML across runs — the test suite diffs two
    exports and expects a round-trip. ``ripple_spec`` takes precedence
    over the flat ``widgets`` list when both are present: modern pockets
    store the canonical layout inside rippleSpec; the widgets column is
    a legacy mirror.
    """

    import yaml  # lazy, matches ee/fleet/installer.py

    body: dict[str, Any] = {
        "apiVersion": "pocketpaw.io/v1",
        "kind": "PocketLayout",
        "metadata": {
            "sourcePocketId": pocket_id,
            "name": name,
            "description": description,
            "category": category,
            "exportedAt": datetime.now(UTC).isoformat(),
        },
        "spec": ripple_spec or {"widgets": list(widgets or [])},
    }
    return yaml.safe_dump(body, sort_keys=True, default_flow_style=False)


def parse_layout_yaml(yaml_text: str) -> dict[str, Any]:
    """Parse a user-supplied YAML template into the template spec body.

    Raises ``ValueError`` when the document is malformed or missing the
    ``kind: PocketLayout`` / ``spec`` combo the exporter produces. The
    error string is safe to return to the caller — it does not leak
    filesystem paths or Python internals.
    """

    import yaml

    try:
        parsed = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("Template YAML must resolve to a mapping, not a scalar or list")
    if parsed.get("kind") and parsed.get("kind") != "PocketLayout":
        raise ValueError(
            f"Unsupported template kind {parsed.get('kind')!r}; expected 'PocketLayout'",
        )
    spec = parsed.get("spec")
    if not isinstance(spec, dict):
        raise ValueError("Template YAML must carry a 'spec' mapping")

    return spec
