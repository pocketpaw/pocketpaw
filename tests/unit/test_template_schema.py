# tests/unit/test_template_schema.py
# Created: 2026-05-25 (feat/rfc-03-v2-schema-chokepoint) — covers the
# new Pydantic v2 ``PocketTemplate`` model, the CEL expression
# validator, the v1 -> v2 promotion path, and the loader's new strict
# kwarg. RED-first: imports the not-yet-existing schema module so the
# whole file fails on collection until Phase 2 lands the implementation.
"""RED tests for RFC 03 v2 — Pocket Template Schema chokepoint.

The schema module under ``pocketpaw.bundled_templates.schema`` does not
exist yet. These tests intentionally fail (ImportError on collection)
until the Phase 2 implementer lands ``PocketTemplate``,
``TemplateValidationError``, and the loader rewiring.

Test taxonomy
-------------

1. v1 -> v2 translation: each of the 4 RFC rules round-trips.
2. v2 worked example (lease-renewal): loads + validates clean.
3. Pydantic enforcement: missing required field, bad enum, etc.
4. CEL parse-as-validation: a malformed expression raises.
5. Cross-field validator rules:
   * shape x default_view compatibility matrix
   * outcomes_emitted subset of top-level outcomes
   * state.id_field resolves to a column field or implicit ``id``
6. Loader strict kwarg: default returns None on failure; strict raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# These imports drive the RED gate — they only succeed once Phase 2
# ships the new modules. Tests stay RED via ImportError until then.
from pocketpaw.bundled_templates.errors import TemplateValidationError
from pocketpaw.bundled_templates.loader import _promote_v1_to_v2, load_template
from pocketpaw.bundled_templates.schema import PocketTemplate

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "templates"
_BUNDLED_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "pocketpaw" / "bundled_templates" / "_bundled"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_v2_dict() -> dict:
    """A minimal v2-shaped dict that passes Pydantic validation. Tests
    mutate copies of this dict to surface per-rule violations cleanly."""
    return {
        "schema_version": "2",
        "name": "minimal-template",
        "version": "1.0.0",
        "pattern": "app",
        "vertical": "productivity",
        "description": "A minimal template used only by tests.",
        "shape": "data-grid",
        "state": {
            "entity_type": "Thing",
            "columns": [
                {"field": "title", "widget": "text"},
                {"field": "status", "widget": "badge"},
            ],
        },
    }


def _minimal_v1_dict() -> dict:
    """A v1-shaped dict that mirrors the shipped bundled templates."""
    return {
        "name": "todo-task-tracker",
        "version": "1.0.0",
        "vertical": "productivity",
        "shape": "data-grid",
        "description": "A personal task tracker.",
        "state": {
            "entity_type": "Task",
            "columns": [{"field": "title", "widget": "text"}],
        },
        "actions": [],
        "connectors": [],
        "skills": [],
    }


# ---------------------------------------------------------------------------
# v1 -> v2 promotion (each of the 4 RFC translation rules)
# ---------------------------------------------------------------------------


def test_promote_injects_schema_version_when_absent() -> None:
    """Rule 1: absent ``schema_version`` -> ``"2"``."""
    v1 = _minimal_v1_dict()
    assert "schema_version" not in v1
    out = _promote_v1_to_v2(v1)
    assert out["schema_version"] == "2"


def test_promote_synthesizes_display_name_from_slug_title_case() -> None:
    """Rule 2: absent ``display_name`` -> title-case the ``name`` slug."""
    v1 = _minimal_v1_dict()
    assert "display_name" not in v1
    out = _promote_v1_to_v2(v1)
    assert out["display_name"] == "Todo Task Tracker"


def test_promote_renames_skills_to_skill_refs() -> None:
    """Rule 3: ``skills`` -> ``skill_refs`` (array contents identical)."""
    v1 = _minimal_v1_dict()
    v1["skills"] = ["skills/a", "skills/b"]
    out = _promote_v1_to_v2(v1)
    assert "skills" not in out
    assert out["skill_refs"] == ["skills/a", "skills/b"]


def test_promote_injects_pattern_from_sibling_index_json() -> None:
    """Rule 4: absent ``pattern`` looked up from the sibling
    ``index.json`` by ``name``. The bundled ``todo-task-tracker`` row is
    ``pattern: app`` per the shipped index.json — promotion must surface
    that value."""
    v1 = _minimal_v1_dict()
    out = _promote_v1_to_v2(v1, templates_dir=_BUNDLED_DIR)
    assert out["pattern"] == "app"


def test_promote_pattern_falls_back_to_app_when_index_missing() -> None:
    """Rule 4 fallback: no index.json -> ``pattern: 'app'``."""
    v1 = _minimal_v1_dict()
    out = _promote_v1_to_v2(v1, templates_dir=Path("/nonexistent/templates/dir"))
    assert out["pattern"] == "app"


def test_promote_is_idempotent_on_v2_input() -> None:
    """A v2-shaped dict passes through unchanged. Critical for
    round-trip safety of the load -> validate path."""
    v2 = _minimal_v2_dict()
    out = _promote_v1_to_v2(v2)
    assert out["schema_version"] == "2"
    # display_name was not in the v2 input either — promotion still
    # synthesizes it because optional defaults are computed regardless
    # of source version.
    assert out["display_name"] == "Minimal Template"


def test_promote_preserves_skill_refs_when_already_named() -> None:
    """If v1 input already uses ``skill_refs`` (mixed shape), don't
    clobber it."""
    v1 = _minimal_v1_dict()
    del v1["skills"]
    v1["skill_refs"] = ["skills/x"]
    out = _promote_v1_to_v2(v1)
    assert out["skill_refs"] == ["skills/x"]


# ---------------------------------------------------------------------------
# v2 worked example (lease-renewal) loads + validates clean
# ---------------------------------------------------------------------------


def test_lease_renewal_v2_fixture_validates() -> None:
    """The canonical v2 worked example loads and Pydantic-validates
    without any error. Drift here means the schema model and the RFC
    example are out of sync."""
    fixture = _FIXTURES_DIR / "lease-renewal-v2.yaml"
    meta = yaml.safe_load(fixture.read_text(encoding="utf-8"))
    template = PocketTemplate.model_validate(meta)
    assert template.schema_version == "2"
    assert template.name == "lease-renewal-v1"
    assert template.pattern == "app"
    assert template.vertical == "property-management"
    assert template.shape == "data-grid"
    # joined_entities populated
    assert len(template.state.joined_entities) == 2
    assert {je.name for je in template.state.joined_entities} == {"tenant", "property"}
    # CEL saved-view filter parsed
    expiring = next(sv for sv in template.state.saved_views if sv.name == "Expiring 30 days")
    assert "days_remaining" in expiring.filter
    # Temporal trigger present
    temporal = next(t for t in template.triggers if t.type == "temporal")
    assert "within(" in temporal.when
    # confirm widened to object form
    send = next(a for a in template.actions if a.name == "send_to_tenant")
    assert send.confirm is not None


# ---------------------------------------------------------------------------
# Pydantic enforcement — required fields, enums, extra-forbid
# ---------------------------------------------------------------------------


def test_missing_required_top_level_field_raises() -> None:
    """Drop a required field; expect a Pydantic validation error wrapped
    in ``TemplateValidationError`` when invoked through the loader."""
    bad = _minimal_v2_dict()
    del bad["shape"]
    with pytest.raises(Exception):  # noqa: B017 — direct Pydantic ValidationError
        PocketTemplate.model_validate(bad)


def test_extra_top_level_field_rejected() -> None:
    """``extra='forbid'`` on the top-level model means unknown fields
    are rejected. v1 'skills' is the canonical example."""
    bad = _minimal_v2_dict()
    bad["skills"] = ["skills/x"]  # v1 field at v2 schema -> reject
    with pytest.raises(Exception):  # noqa: B017
        PocketTemplate.model_validate(bad)


def test_bad_shape_enum_rejected() -> None:
    """``shape`` must be one of the 11 RFC-listed values."""
    bad = _minimal_v2_dict()
    bad["shape"] = "not-a-real-shape"
    with pytest.raises(Exception):  # noqa: B017
        PocketTemplate.model_validate(bad)


def test_bad_pattern_enum_rejected() -> None:
    """``pattern`` must be one of the 7 RFC-listed values."""
    bad = _minimal_v2_dict()
    bad["pattern"] = "not-a-real-pattern"
    with pytest.raises(Exception):  # noqa: B017
        PocketTemplate.model_validate(bad)


# ---------------------------------------------------------------------------
# CEL parse-as-validation
# ---------------------------------------------------------------------------


def test_bad_cel_in_saved_view_filter_raises() -> None:
    """A malformed CEL expression in ``saved_views[].filter`` raises."""
    bad = _minimal_v2_dict()
    bad["state"]["saved_views"] = [
        {"name": "broken", "filter": "this is not valid CEL ((("},
    ]
    with pytest.raises(Exception):  # noqa: B017
        PocketTemplate.model_validate(bad)


def test_valid_cel_in_saved_view_filter_accepts() -> None:
    """A well-formed CEL expression passes."""
    good = _minimal_v2_dict()
    good["state"]["saved_views"] = [
        {"name": "expiring", "filter": "days_remaining <= 30"},
    ]
    template = PocketTemplate.model_validate(good)
    assert template.state.saved_views[0].filter == "days_remaining <= 30"


def test_bad_cel_in_instinct_rule_when_raises() -> None:
    """A malformed CEL expression in ``instinct_rules.rules[].when``
    raises at validation time."""
    bad = _minimal_v2_dict()
    bad["instinct_rules"] = {
        "rules": [
            {"when": "((( bad syntax", "action": "block"},
        ],
    }
    with pytest.raises(Exception):  # noqa: B017
        PocketTemplate.model_validate(bad)


def test_bad_cel_in_temporal_trigger_when_raises() -> None:
    """A malformed CEL expression in ``triggers[].when`` (temporal)
    raises."""
    bad = _minimal_v2_dict()
    bad["triggers"] = [
        {"type": "temporal", "when": "((("},
    ]
    with pytest.raises(Exception):  # noqa: B017
        PocketTemplate.model_validate(bad)


# ---------------------------------------------------------------------------
# Cross-field validator rules
# ---------------------------------------------------------------------------


def test_shape_kanban_with_default_view_calendar_rejected() -> None:
    """``shape: kanban`` only allows ``default_view: kanban`` per the
    RFC compatibility matrix."""
    bad = _minimal_v2_dict()
    bad["shape"] = "kanban"
    bad["state"]["default_view"] = "calendar"
    with pytest.raises(Exception):  # noqa: B017
        PocketTemplate.model_validate(bad)


def test_shape_data_grid_with_default_view_kanban_accepted() -> None:
    """``shape: data-grid`` accepts ``default_view: kanban`` per the
    matrix (data-grid -> list, grid, kanban)."""
    good = _minimal_v2_dict()
    good["shape"] = "data-grid"
    good["state"]["default_view"] = "kanban"
    template = PocketTemplate.model_validate(good)
    assert template.state.default_view == "kanban"


def test_shape_chart_with_default_view_rejected() -> None:
    """``shape: chart`` declares NO default_view per the matrix."""
    bad = _minimal_v2_dict()
    bad["shape"] = "chart"
    bad["state"]["default_view"] = "list"
    with pytest.raises(Exception):  # noqa: B017
        PocketTemplate.model_validate(bad)


def test_shape_custom_allows_empty_columns() -> None:
    """``shape: custom`` renders via a bespoke widget that doesn't
    project rows into columns. Empty ``state.columns`` is allowed."""
    good = _minimal_v2_dict()
    good["shape"] = "custom"
    good["state"]["columns"] = []
    # Must not raise.
    template = PocketTemplate.model_validate(good)
    assert template.shape == "custom"
    assert template.state.columns == []


def test_shape_non_custom_requires_at_least_one_column() -> None:
    """Every non-custom shape projects rows into columns; empty
    ``state.columns`` is rejected with a meaningful error."""
    bad = _minimal_v2_dict()
    bad["shape"] = "data-grid"
    bad["state"]["columns"] = []
    with pytest.raises(Exception, match="state.columns must declare at least one column"):
        PocketTemplate.model_validate(bad)


def test_outcomes_emitted_subset_of_top_level_outcomes() -> None:
    """An action's ``outcomes_emitted`` must be a subset of the
    template-level ``outcomes[]``."""
    bad = _minimal_v2_dict()
    bad["outcomes"] = ["thing_done"]
    bad["actions"] = [
        {
            "name": "do_thing",
            "label": "Do thing",
            "kind": "single-row",
            "instinct_policy": "auto",
            "outcomes_emitted": ["unrelated_outcome"],
        }
    ]
    with pytest.raises(Exception):  # noqa: B017
        PocketTemplate.model_validate(bad)


def test_outcomes_emitted_subset_accepted_when_aligned() -> None:
    """The subset check passes when outcomes_emitted is listed."""
    good = _minimal_v2_dict()
    good["outcomes"] = ["thing_done"]
    good["actions"] = [
        {
            "name": "do_thing",
            "label": "Do thing",
            "kind": "single-row",
            "instinct_policy": "auto",
            "outcomes_emitted": ["thing_done"],
        }
    ]
    template = PocketTemplate.model_validate(good)
    assert template.actions[0].outcomes_emitted == ["thing_done"]


def test_state_id_field_missing_column_rejected() -> None:
    """``state.id_field`` must resolve to a column field OR be the
    implicit ``id``. A name that matches neither is rejected."""
    bad = _minimal_v2_dict()
    bad["state"]["id_field"] = "no_such_column"
    with pytest.raises(Exception):  # noqa: B017
        PocketTemplate.model_validate(bad)


def test_state_id_field_implicit_id_accepted() -> None:
    """The implicit ``id`` value passes even when no column declares
    ``id`` — every entity carries an implicit row identifier."""
    good = _minimal_v2_dict()
    good["state"]["id_field"] = "id"
    template = PocketTemplate.model_validate(good)
    assert template.state.id_field == "id"


def test_state_id_field_matching_column_accepted() -> None:
    """``id_field`` resolves to a declared column field."""
    good = _minimal_v2_dict()
    good["state"]["id_field"] = "title"  # column declared in _minimal_v2_dict
    template = PocketTemplate.model_validate(good)
    assert template.state.id_field == "title"


# ---------------------------------------------------------------------------
# Loader strict kwarg + TemplateValidationError type
# ---------------------------------------------------------------------------


def _write_template(tmp_path: Path, slug: str, meta: dict, spec: dict) -> Path:
    """Write a slug-shaped template directory under tmp_path; return
    the slug directory."""
    import json

    slug_dir = tmp_path / slug
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / "template.pocket.yaml").write_text(yaml.safe_dump(meta), encoding="utf-8")
    (slug_dir / "ripple_spec.json").write_text(json.dumps(spec), encoding="utf-8")
    return slug_dir


def test_loader_strict_false_returns_none_on_bad_template(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``strict=False`` (default) preserves back-compat: a bad template
    yields ``None`` + a logged warning; pocket creation can fall back to
    cold generation."""
    bad_meta = _minimal_v2_dict()
    bad_meta["shape"] = "not-a-real-shape"  # invalid enum
    _write_template(tmp_path, "bad-template", bad_meta, {"ui": {}, "state": {}})

    with caplog.at_level("WARNING"):
        result = load_template("bad-template", templates_dir=tmp_path, strict=False)

    assert result is None
    # The warning chain mentions the slug and the failure.
    assert any("bad-template" in msg for msg in caplog.messages)


def test_loader_strict_true_raises_template_validation_error(tmp_path: Path) -> None:
    """``strict=True`` propagates a ``TemplateValidationError`` so the
    CLI and tests can surface the exact problem."""
    bad_meta = _minimal_v2_dict()
    bad_meta["shape"] = "not-a-real-shape"
    _write_template(tmp_path, "bad-template", bad_meta, {"ui": {}, "state": {}})

    with pytest.raises(TemplateValidationError) as exc_info:
        load_template("bad-template", templates_dir=tmp_path, strict=True)
    assert "bad-template" in str(exc_info.value)


def test_loader_strict_true_loads_v2_template_successfully(tmp_path: Path) -> None:
    """A valid v2 template loads through the strict path and returns a
    dict shaped exactly like the existing API: ``{meta, ripple_spec}``."""
    good_meta = _minimal_v2_dict()
    _write_template(tmp_path, "good-template", good_meta, {"ui": {}, "state": {}})

    result = load_template("good-template", templates_dir=tmp_path, strict=True)
    assert result is not None
    assert set(result.keys()) == {"meta", "ripple_spec"}
    assert isinstance(result["meta"], dict)
    assert result["meta"]["schema_version"] == "2"


def test_loader_strict_true_translates_v1_then_validates(tmp_path: Path) -> None:
    """A v1 template on disk is promoted before validation under
    ``strict=True`` — the translation path stays exercised even after
    the bundled migration lands."""
    v1_meta = _minimal_v1_dict()
    _write_template(tmp_path, "todo-task-tracker", v1_meta, {"ui": {}, "state": {}})

    result = load_template("todo-task-tracker", templates_dir=tmp_path, strict=True)
    assert result is not None
    meta = result["meta"]
    assert meta["schema_version"] == "2"
    assert "skills" not in meta and meta.get("skill_refs") == []
    assert meta["display_name"] == "Todo Task Tracker"


def test_template_validation_error_carries_slug_and_pydantic_error(
    tmp_path: Path,
) -> None:
    """``TemplateValidationError`` exposes the slug and the underlying
    Pydantic ValidationError for CLI rendering."""
    from pydantic import ValidationError

    bad_meta = _minimal_v2_dict()
    bad_meta["shape"] = "not-a-real-shape"
    _write_template(tmp_path, "bad-template", bad_meta, {"ui": {}, "state": {}})

    with pytest.raises(TemplateValidationError) as exc_info:
        load_template("bad-template", templates_dir=tmp_path, strict=True)

    err = exc_info.value
    assert err.slug == "bad-template"
    assert isinstance(err.pydantic_error, ValidationError)
    assert isinstance(err, ValueError)  # ValueError subclass per the brief


def test_loader_strict_default_is_false_for_back_compat(tmp_path: Path) -> None:
    """The default behaviour (no kwarg) must continue to return None on
    failure — EE consumers pattern-match on None."""
    bad_meta = _minimal_v2_dict()
    bad_meta["shape"] = "not-a-real-shape"
    _write_template(tmp_path, "bad-template", bad_meta, {"ui": {}, "state": {}})

    # No ``strict`` kwarg at all — should NOT raise.
    result = load_template("bad-template", templates_dir=tmp_path)
    assert result is None
