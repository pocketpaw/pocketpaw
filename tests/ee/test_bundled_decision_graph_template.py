# tests/ee/test_bundled_decision_graph_template.py — RFC 07 Slice 3a
# Created: 2026-05-25 — pins the bundled `decision-graph` pocket template
#   shipped in
#   `src/pocketpaw/bundled_templates/_bundled/decision-graph/`. Asserts:
#
#     - template.pocket.yaml parses against the RFC 03 Pocket Template
#       Schema (the same field set + shape enum the existing six
#       bundled templates pass against).
#     - ripple_spec.json parses as valid JSON, carries ui + state, and
#       includes the explain-pipeline wiring (the narrator action that
#       invokes POST /api/v1/decisions/explain).
#     - the slug appears in `_bundled/index.json` with keywords the
#       chat agent's STEP 0 keyword match looks for.
#     - the loader returns the template by slug from a tmp install
#       (no traversal into the user's real ~/.pocketpaw/templates/).
#
# Changes:
#   - 2026-05-25 (RFC 07 follow-up): the explain-wiring assertion now
#     looks for the canonical `api` action verb instead of the
#     pre-merge `invoke_endpoint` shape. `invoke_endpoint` is not in
#     the Ripple event dispatcher's switch (see `_KNOWN_ACTION_VERBS`
#     in `src/pocketpaw/ripple/manifest.py` for the 18-verb truth);
#     the renderer would silently no-op on the old spec. Fields
#     mirrored from the dispatcher's `handleApi` (event-dispatcher.ts):
#     `url` (required), `method`, `body`, `response_key` (where the
#     response gets written into state).
"""Tests for the bundled decision-graph pocket template (RFC 07 Slice 3a)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from pocketpaw.bundled_templates.installer import install_bundled_templates
from pocketpaw.bundled_templates.loader import load_template

# Mirrors the RFC 03 schema constants in tests/unit/test_bundled_templates.py
# so the decision-graph template is held to the same field set.
_RFC03_ALLOWED_FIELDS = {
    "name",
    "version",
    "vertical",
    "shape",
    "state",
    "actions",
    "connectors",
    "skills",
    "description",
}
_RFC03_REQUIRED_FIELDS = {"name", "version", "vertical", "shape", "state", "description"}
_RFC03_SHAPE_ENUM = {
    "data-grid",
    "kanban",
    "calendar",
    "map",
    "timeline",
    "tree",
    "chart",
    "custom",
}

_BUNDLED_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "pocketpaw"
    / "bundled_templates"
    / "_bundled"
)
_SLUG = "decision-graph"


# ---------------------------------------------------------------------------
# template.pocket.yaml — RFC 03 schema shape
# ---------------------------------------------------------------------------


def test_template_yaml_matches_rfc03_field_set() -> None:
    """The decision-graph template uses ONLY RFC 03 fields, carries
    every required field, ships actions: [], and uses a valid shape
    enum value (`custom`)."""
    meta_path = _BUNDLED_DIR / _SLUG / "template.pocket.yaml"
    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))

    assert isinstance(meta, dict)
    fields = set(meta.keys())
    extra = fields - _RFC03_ALLOWED_FIELDS
    assert not extra, f"decision-graph: non-RFC03 fields {extra}"
    missing = _RFC03_REQUIRED_FIELDS - fields
    assert not missing, f"decision-graph: missing required fields {missing}"

    # Read-only — no actions; no connectors; no skills.
    assert meta["actions"] == []
    assert meta["connectors"] == []
    assert meta["skills"] == []
    # No `agents` / `triggers` / `outcomes` / `instinct_rules` until the
    # Pocket Template Schema admits them. The narrator backend + read-
    # only enforcement live as runtime defaults, not template metadata.
    for omitted in ("outcomes", "instinct_rules", "triggers", "agents", "kb_scope"):
        assert omitted not in meta, f"decision-graph: should omit {omitted}"

    assert meta["shape"] in _RFC03_SHAPE_ENUM
    assert meta["shape"] == "custom"
    assert meta["vertical"] == "core"
    assert meta["name"] == _SLUG
    assert isinstance(meta["state"], dict) and "entity_type" in meta["state"]
    # RFC 07 § "The UX surface" — entity type is Decision.
    assert meta["state"]["entity_type"] == "Decision"


# ---------------------------------------------------------------------------
# ripple_spec.json — wired to the explain endpoint
# ---------------------------------------------------------------------------


def test_template_ripple_spec_is_valid_json_with_explain_wiring() -> None:
    """The ripple_spec parses, carries ui + state, includes the
    `_placeholder_note`, and binds an Explain button to the
    POST /api/v1/decisions/explain endpoint via the canonical `api`
    action verb (the renderer's event dispatcher has no
    `invoke_endpoint` case — `api` is one of the 18 verbs in
    `_KNOWN_ACTION_VERBS`)."""
    spec_path = _BUNDLED_DIR / _SLUG / "ripple_spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    assert isinstance(spec, dict)
    assert "ui" in spec
    assert "state" in spec
    assert "_placeholder_note" in spec

    # The state seeds the explain inputs the UI binds to.
    state = spec["state"]
    assert "selected_decision_id" in state
    assert "explain_question" in state
    assert "explain_response" in state
    assert state["explain_response"]["decisions_walked"] == []

    # Walk the ui tree looking for the `api` action pointed at
    # /api/v1/decisions/explain. The Slice 3b frontend agent reads
    # this binding to wire the chat panel.
    explain_actions = _find_api_calls(spec["ui"], "/api/v1/decisions/explain")
    assert explain_actions, (
        "decision-graph template must bind an Explain action to "
        "POST /api/v1/decisions/explain so the narrator panel works"
    )
    # The first invocation must POST, pass a `question` from state, and
    # write the response into `state.explain_response` via the
    # canonical `response_key` field that the dispatcher's `handleApi`
    # reads.
    first = explain_actions[0]
    assert first.get("method") == "POST"
    assert first["body"]["question"] == "{state.explain_question}"
    assert first["response_key"] == "explain_response"


def _find_api_calls(ui: dict, url: str) -> list[dict]:
    """Walk the ui tree and collect every `api` action whose `url`
    matches `url`. Recursive over `children` and `on_click` lists."""
    out: list[dict] = []
    if not isinstance(ui, dict):
        return out
    for click_action in ui.get("on_click", []) or []:
        if (
            isinstance(click_action, dict)
            and click_action.get("action") == "api"
            and click_action.get("url") == url
        ):
            out.append(click_action)
    for child in ui.get("children", []) or []:
        out.extend(_find_api_calls(child, url))
    return out


# ---------------------------------------------------------------------------
# index.json registration — the chat agent's keyword match finds it
# ---------------------------------------------------------------------------


def test_index_json_registers_decision_graph_with_keywords() -> None:
    """The bundled index must register `decision-graph` with keywords
    the chat agent's STEP 0 keyword match looks for."""
    index = json.loads((_BUNDLED_DIR / "index.json").read_text())
    rows = index["templates"]
    by_slug = {r["slug"]: r for r in rows}
    assert _SLUG in by_slug
    row = by_slug[_SLUG]
    assert row["shape"] == "custom"
    assert isinstance(row["keywords"], list) and row["keywords"]
    # The RFC 07 demo question keywords must be in the list.
    keywords_joined = " ".join(row["keywords"]).lower()
    assert "decision" in keywords_joined
    assert "audit" in keywords_joined or "audit trail" in keywords_joined


# ---------------------------------------------------------------------------
# Installer + loader — mirrors the existing bundled-templates flow
# ---------------------------------------------------------------------------


def test_installer_includes_decision_graph(tmp_path: Path) -> None:
    """A fresh install mirrors the decision-graph template into the
    user's templates directory alongside the existing six."""
    results = install_bundled_templates(destination_root=tmp_path)
    slugs = {r.name for r in results}
    assert _SLUG in slugs

    decision_graph_dir = tmp_path / _SLUG
    assert (decision_graph_dir / "template.pocket.yaml").is_file()
    assert (decision_graph_dir / "ripple_spec.json").is_file()


def test_loader_returns_decision_graph(tmp_path: Path) -> None:
    """The loader resolves the decision-graph slug to its meta +
    ripple_spec dicts."""
    install_bundled_templates(destination_root=tmp_path)
    loaded = load_template(_SLUG, templates_dir=tmp_path)
    assert loaded is not None
    assert loaded["meta"]["name"] == _SLUG
    assert loaded["meta"]["shape"] == "custom"
    assert loaded["meta"]["vertical"] == "core"
    assert "explain_question" in loaded["ripple_spec"]["state"]
