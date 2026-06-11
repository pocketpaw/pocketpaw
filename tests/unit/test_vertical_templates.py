# tests/unit/test_vertical_templates.py
# Created: 2026-06-11 (feat/triage-member-templates) — covers the two
# generic bundled vertical templates, applications-triage and member-360.
# These are richer than the seed templates: they declare needs: Senses,
# data_sources:, and (for triage) gated actions: + outcomes:. This file
# pins their RFC 03 v2 shape, their load_template + compile path, their
# action-row gated-proposal wiring (triage), their read-only posture
# (member-360), and a full service create() round-trip through the mongo
# fixture so the install-time compile-on-create seam is exercised.
# Modified 2026-06-11 (feat/triage-redesign): DEMO-GRADE redesign of both
# vertical ripple_spec.json files. Updated the burndown assertion (the
# triage burndown now renders color-banded status-dot count cards + a
# queue-total / oldest-waiting stat, not literal `stat` widgets) and the
# member-360 state-key assertion (`stats` -> `kpis`, focal widget is now
# `entity-detail`). Added test_<slug>_bindings_reference_seeded_state — a
# mechanical guard for the class of bug this redesign fixed: every
# {state.X...} binding in a vertical template must reference a state key
# that EXISTS in the seed state, so a detail panel can never bind to a
# state shape that was never seeded (the old "empty Answers / no values"
# failure). It also flags the unsupported method-chain-then-property
# pattern ({...first().field}) that resolved to undefined at render time.
"""Tests for the bundled vertical templates (applications-triage, member-360).

The seed-template field-set assertions in test_bundled_templates.py
intentionally exclude these two slugs — they carry the full v2 surface
(needs / actions / outcomes / data_sources) the seed templates omit.
This file owns their shape and behaviour:

* RFC 03 v2 Pydantic validation of each template.pocket.yaml.
* The OSS ``compile_template`` translation (data_sources -> sources,
  passthrough of actions / outcomes / needs).
* applications-triage's gated action row: every action declares
  ``instinct_policy: require_approval`` and the ripple_spec buttons set
  ``state.pending_proposal`` instead of executing — the binding seam a
  deployment wires to the Instinct gate.
* member-360's read-only posture: zero actions, no ``on_click`` anywhere.
* End-to-end create() through the EE service with the mongo fixture, so
  the compile-on-install merge into the pocket rippleSpec is proven.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from pocketpaw.bundled_templates import PocketTemplate, compile_template
from pocketpaw.bundled_templates.installer import install_bundled_templates
from pocketpaw.bundled_templates.loader import load_template

_VERTICAL_SLUGS = ("applications-triage", "member-360")

_BUNDLED_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "pocketpaw" / "bundled_templates" / "_bundled"
)


def _read_meta(slug: str) -> dict:
    return yaml.safe_load(
        (_BUNDLED_DIR / slug / "template.pocket.yaml").read_text(encoding="utf-8")
    )


def _read_spec(slug: str) -> dict:
    return json.loads((_BUNDLED_DIR / slug / "ripple_spec.json").read_text(encoding="utf-8"))


def _iter_widgets(node: object):
    """Yield every dict node in a ripple_spec tree (depth-first)."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _iter_widgets(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_widgets(item)


# Matches a binding expression's root scope + first key, e.g. `state.selected`
# in `{state.selected.applicant}` or `app` in `{app.score}`. Group 1 is the
# scope (state / data / a loop variable), group 2 is the first key under it.
_BINDING_RE = re.compile(r"\{\s*([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)")

# The Ripple expression engine resolves a method chain ONLY when the whole
# chain ENDS in `)` (e.g. `{x.where('id', y).first()}`). A method call FOLLOWED
# BY a PROPERTY access (`{...first().field}`) resolves to `undefined` — the
# exact bug that left the old triage detail panel empty. This pattern flags a
# `)` followed by `.<name>` where `<name>` is NOT itself a method call (i.e.
# not followed by `(`): chaining another method (`.first()`) is fine; reaching
# into a property (`.applicant`) after a method is the broken form.
_CHAIN_THEN_PROP_RE = re.compile(r"\)\s*\.[a-zA-Z_]\w*(?![\w(])")


def _iter_binding_strings(node: object):
    """Yield every string that contains a `{...}` binding expression, anywhere
    in the spec tree (prop values, `items`, `bind`, action `value`s, ...)."""
    if isinstance(node, dict):
        for value in node.values():
            yield from _iter_binding_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_binding_strings(item)
    elif isinstance(node, str) and "{" in node:
        yield node


def _loop_variables(spec: dict) -> set[str]:
    """Collect every `each` loop variable name (``item_as``) declared in the
    spec — these are loop-local scopes, not seed-state keys, so a `{app.x}`
    reference is satisfied by the loop, not by `state`."""
    names = {"item", "index"}  # the renderer's implicit loop variables
    for node in _iter_widgets(spec.get("ui", {})):
        if node.get("type") == "each":
            alias = node.get("item_as")
            if isinstance(alias, str):
                names.add(alias)
            idx = node.get("index_as")
            if isinstance(idx, str):
                names.add(idx)
    return names


# ---------------------------------------------------------------------------
# Binding integrity — every {state.X} reference must hit a seeded state key.
# This is the mechanical guard for the class of bug the DEMO-GRADE redesign
# fixed: a detail panel that binds to a state shape which was never seeded
# renders empty. Checkable for EVERY vertical template, not just the two here.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", _VERTICAL_SLUGS)
def test_vertical_template_bindings_reference_seeded_state(slug: str) -> None:
    """Every `{state.<key>...}` binding in the ripple_spec references a key
    that EXISTS in the seed `state`, and no binding uses the unsupported
    method-chain-then-property pattern (`{...first().field}`) that resolves to
    undefined at render time. Together these make the "empty detail panel /
    no values" failure mechanically uncatchable-by-eye but catchable here."""
    spec = _read_spec(slug)
    seeded_state_keys = set(spec["state"].keys())
    loop_vars = _loop_variables(spec)

    bad_state_refs: list[tuple[str, str]] = []
    chain_then_prop: list[str] = []

    for raw in _iter_binding_strings(spec["ui"]):
        # Flag the unsupported method-chain-then-property pattern outright.
        if _CHAIN_THEN_PROP_RE.search(raw):
            chain_then_prop.append(raw)
        for scope, first_key in _BINDING_RE.findall(raw):
            if scope == "state":
                if first_key not in seeded_state_keys:
                    bad_state_refs.append((raw, first_key))
            elif scope in {"data"} or scope in loop_vars:
                # data sources hydrate at runtime; loop vars are local scopes.
                continue
            else:
                # An unknown root scope is itself a binding bug — it can only
                # be state-relative or a declared loop variable.
                bad_state_refs.append((raw, scope))

    assert not bad_state_refs, (
        f"{slug}: bindings reference state keys not in the seed state "
        f"(or an undeclared loop scope): {bad_state_refs}"
    )
    assert not chain_then_prop, (
        f"{slug}: bindings use the unsupported method-chain-then-property "
        f"pattern (resolves to undefined at render): {chain_then_prop}"
    )


# ---------------------------------------------------------------------------
# RFC 03 v2 schema — both templates validate through the Pydantic chokepoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", _VERTICAL_SLUGS)
def test_vertical_template_passes_pydantic_validation(slug: str) -> None:
    """Each vertical template.pocket.yaml validates against PocketTemplate
    — the same RFC 03 v2 chokepoint every bundled template clears."""
    template = PocketTemplate.model_validate(_read_meta(slug))
    assert template.name == slug
    assert template.schema_version == "2"
    assert template.shape == "custom"


@pytest.mark.parametrize("slug", _VERTICAL_SLUGS)
def test_vertical_template_declares_generic_data_sense(slug: str) -> None:
    """Both templates declare the generic database Sense as a need so the
    create path can prompt-to-connect a data source. ``paw.db.v1`` is a
    curated core sense, so it validates rather than fragmenting the core."""
    template = PocketTemplate.model_validate(_read_meta(slug))
    assert template.needs == ["paw.db.v1"]
    # A placeholder live-data source is declared for compile-on-install.
    assert len(template.data_sources) == 1
    assert template.data_sources[0].method == "GET"
    assert template.data_sources[0].bind.startswith("state.")


@pytest.mark.parametrize("slug", _VERTICAL_SLUGS)
def test_vertical_template_ripple_spec_is_well_formed(slug: str) -> None:
    """Each ripple_spec.json carries ui + state + the mandated
    _placeholder_note, and a placeholder sources block (removed at
    instantiation when there is no backend)."""
    spec = _read_spec(slug)
    assert "ui" in spec and "state" in spec
    assert "_placeholder_note" in spec
    assert isinstance(spec.get("sources"), dict) and spec["sources"]


# ---------------------------------------------------------------------------
# compile_template — data_sources translate, needs/actions pass through
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", _VERTICAL_SLUGS)
def test_vertical_template_compiles_to_runtime_dict(slug: str) -> None:
    """``compile_template`` turns each template's data_source into a
    runtime ``sources`` entry keyed by name, and preserves state."""
    template = PocketTemplate.model_validate(_read_meta(slug))
    out = compile_template(template)

    src_name = template.data_sources[0].name
    assert src_name in out["sources"]
    entry = out["sources"][src_name]
    assert entry["method"] == "GET"
    assert "name" not in entry  # name became the dict key
    # State carries the primary entity binding through to the runtime.
    assert out["state"]["entity_type"] == template.state.entity_type


# ---------------------------------------------------------------------------
# applications-triage — gated action row
# ---------------------------------------------------------------------------


def test_triage_actions_are_all_gated_require_approval() -> None:
    """Every triage action declares ``instinct_policy: require_approval``
    so it surfaces as a PENDING proposal in The Tray, never auto-runs."""
    template = PocketTemplate.model_validate(_read_meta("applications-triage"))
    assert len(template.actions) == 3
    names = {a.name for a in template.actions}
    assert names == {"approve_application", "reject_application", "flag_for_review"}
    for action in template.actions:
        assert action.instinct_policy == "require_approval", action.name


def test_triage_action_outcomes_are_declared_in_catalog() -> None:
    """Each action's outcomes_emitted is declared in the top-level
    outcomes[] catalog — the RFC 03 v2 subset rule (enforced by the model
    validator, asserted here for documentation)."""
    template = PocketTemplate.model_validate(_read_meta("applications-triage"))
    catalog = set(template.outcomes)
    for action in template.actions:
        for outcome in action.outcomes_emitted:
            assert outcome in catalog, f"{action.name} emits undeclared {outcome}"


def test_triage_action_row_proposes_not_executes() -> None:
    """The ripple_spec action buttons set ``state.pending_proposal`` (a
    generic proposal shape) instead of executing — this is the binding
    seam a deployment wires to external_actions.propose. No button mutates
    application status directly."""
    spec = _read_spec("applications-triage")
    buttons = [w for w in _iter_widgets(spec["ui"]) if w.get("type") == "button"]
    assert len(buttons) == 3, "approve / reject / needs-review"
    for button in buttons:
        actions = button.get("on_click")
        assert actions, f"button {button['props']['label']} has no on_click"
        # Every click sets pending_proposal — nothing else.
        targets = {a.get("target") for a in actions}
        assert targets == {"pending_proposal"}, button["props"]["label"]
        # The proposal value carries the generic {action, application_id, summary} shape.
        value = actions[0]["value"]
        assert set(value.keys()) == {"action", "application_id", "summary"}
    # The state seeds the binding seam with an empty proposal slot.
    assert spec["state"].get("pending_proposal") is None


def test_triage_has_burndown_stat_row() -> None:
    """The triage canvas carries a status-count burndown — an each-loop over
    state.status_counts rendering one color-banded count card per status, plus
    a queue-total and oldest-waiting stat. The DEMO-GRADE redesign renders each
    bucket as a colored status-dot + count (the `stat` widget has no color
    prop), so the burndown is asserted by the each-loop over status_counts and
    the status-dot color carriers, not by a literal `stat` node."""
    spec = _read_spec("applications-triage")
    assert "status_counts" in spec["state"]
    # Each bucket carries a status color and a status-dot variant so the row
    # reads as color-banded counts, not a flat number list.
    for bucket in spec["state"]["status_counts"]:
        assert bucket.get("color"), f"status bucket {bucket.get('label')} has no color"
        assert bucket.get("dot"), f"status bucket {bucket.get('label')} has no status-dot variant"
    # The headline stats the burndown strip adds beyond the per-status counts.
    assert "queue_total" in spec["state"], "burndown is missing the total stat"
    assert "oldest_waiting" in spec["state"], "burndown is missing the oldest-waiting stat"
    # The colored status-dot is the per-status count carrier.
    dots = [w for w in _iter_widgets(spec["ui"]) if w.get("type") == "status-dot"]
    assert dots, "burndown color-banded status-dot row missing"
    # The burndown is driven by an each-loop over status_counts.
    eaches = [
        w
        for w in _iter_widgets(spec["ui"])
        if w.get("type") == "each" and w.get("items") == "{state.status_counts}"
    ]
    assert eaches, "burndown each-loop over state.status_counts missing"


# ---------------------------------------------------------------------------
# member-360 — read-only
# ---------------------------------------------------------------------------


def test_member_360_is_read_only() -> None:
    """member-360 declares zero actions and its ripple_spec has no on_click
    anywhere — a pure read-only view."""
    template = PocketTemplate.model_validate(_read_meta("member-360"))
    assert template.actions == []

    spec = _read_spec("member-360")
    for widget in _iter_widgets(spec["ui"]):
        assert "on_click" not in widget, f"read-only view has an on_click: {widget.get('type')}"


def test_member_360_has_profile_membership_and_lists() -> None:
    """The member-360 state seeds the header member, the entity-detail KPI
    strip + facts rail, the key-value profile + membership blocks, and the
    three record lists. The DEMO-GRADE redesign renders the headline numbers
    through the entity-detail focal widget's ``kpis`` strip (renamed from the
    flat ``stats`` list) and the facts through its right rail."""
    spec = _read_spec("member-360")
    state = spec["state"]
    for key in ("member", "kpis", "facts", "profile", "membership", "tickets", "orders", "notes"):
        assert key in state, f"member-360 state missing {key}"
    assert state["member"]["name"]
    # The focal widget is the catalog's "view one record" layout, so the header
    # KPI strip and facts rail are bound, not hand-assembled stat tiles.
    entity_details = [w for w in _iter_widgets(spec["ui"]) if w.get("type") == "entity-detail"]
    assert entity_details, "member-360 should use the entity-detail focal widget"


# ---------------------------------------------------------------------------
# Installer — both vertical templates mirror like every other template
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", _VERTICAL_SLUGS)
def test_installer_mirrors_vertical_template(tmp_path: Path, slug: str) -> None:
    """The installer copies each vertical template directory + both files."""
    results = install_bundled_templates(destination_root=tmp_path)
    result = next(r for r in results if r.name == slug)
    assert result.status == "installed"
    assert (tmp_path / slug / "template.pocket.yaml").is_file()
    assert (tmp_path / slug / "ripple_spec.json").is_file()


@pytest.mark.parametrize("slug", _VERTICAL_SLUGS)
def test_loader_round_trips_vertical_template(tmp_path: Path, slug: str) -> None:
    """``load_template`` reads each installed vertical template back to
    {meta, ripple_spec}, and the strict path validates it clean."""
    install_bundled_templates(destination_root=tmp_path)
    loaded = load_template(slug, templates_dir=tmp_path)
    assert loaded is not None
    assert loaded["meta"]["name"] == slug
    assert "ui" in loaded["ripple_spec"]
    # strict=True must not raise — the template is schema-clean.
    strict = load_template(slug, templates_dir=tmp_path, strict=True)
    assert strict is not None
