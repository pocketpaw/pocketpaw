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
# Modified 2026-06-11 (feat/demo-template-suite): extended _VERTICAL_SLUGS to
# the four new demo-suite templates — events-board, renewals-radar,
# orders-fulfillment, revenue-pulse — so the generic binding-guard /
# well-formed / data-sense / compile / installer / loader parametrizations
# cover them too. The pydantic shape assertion is now per-slug
# (_VERTICAL_SHAPES) because orders-fulfillment is `kanban` and revenue-pulse
# is `chart`, not `custom`. Added a demo-suite block: a render-populated guard
# (ui.children non-empty + the focal seed list carries rows), a
# compile-round-trip that asserts the same on the loaded spec, the gated
# action-row contract for the three action templates, and per-template story
# checks (events sell-through + run-of-show, renewals risk bands + lapsed
# member, orders stage board, revenue read-only charts).
"""Tests for the bundled vertical templates (triage, member-360, demo suite).

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

_VERTICAL_SLUGS = (
    "applications-triage",
    "member-360",
    "events-board",
    "renewals-radar",
    "orders-fulfillment",
    "revenue-pulse",
)

# The RFC 03 v2 shape each vertical template declares. Most are ``custom``
# composite canvases, but the focal-widget-driven ones pin their organising
# shape: orders-fulfillment is a kanban stage board, revenue-pulse a chart
# dashboard. The shape assertion is per-slug, not a blanket ``custom``.
_VERTICAL_SHAPES = {
    "applications-triage": "custom",
    "member-360": "custom",
    "events-board": "custom",
    "renewals-radar": "custom",
    "orders-fulfillment": "kanban",
    "revenue-pulse": "chart",
}

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
    assert template.shape == _VERTICAL_SHAPES[slug]


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
# Demo-suite templates — events-board / renewals-radar / orders-fulfillment /
# revenue-pulse. The render-surface guard (ui.children non-empty + the focal
# seeded list carries rows) and the gated-action / read-only posture checks
# that mirror the triage + member-360 contracts for the four new templates.
# ---------------------------------------------------------------------------

# The four demo-suite templates and the focal seeded state list whose rows
# the rendered canvas iterates — the list that MUST carry seed rows so a
# pocket created from the template renders populated, never an empty shell.
_DEMO_SUITE = {
    "events-board": "events",
    "renewals-radar": "members",
    "orders-fulfillment": "orders",
    "revenue-pulse": "trend",
}

# The three demo-suite templates that ship gated action rows, mapped to the
# proposal-payload entity-id key each button carries. revenue-pulse is a
# read-only dashboard and is excluded.
_DEMO_GATED = {
    "events-board": "event_id",
    "renewals-radar": "membership_id",
    "orders-fulfillment": "order_id",
}


@pytest.mark.parametrize("slug", sorted(_DEMO_SUITE))
def test_demo_template_renders_populated(slug: str) -> None:
    """Each demo-suite template renders a populated canvas: the ui tree has
    top-level children (so a pocket created from it is never an empty shell —
    the #1431 empty-canvas class of bug) and the focal seeded list carries
    rows. Asserts the user-visible surface, not just the compiled machinery."""
    spec = _read_spec(slug)
    children = spec["ui"].get("children")
    assert children, f"{slug}: ui tree has no top-level children — empty canvas"

    focal_key = _DEMO_SUITE[slug]
    rows = spec["state"].get(focal_key)
    assert isinstance(rows, list) and rows, (
        f"{slug}: focal seed list state.{focal_key} is empty — nothing to render"
    )


@pytest.mark.parametrize("slug", sorted(_DEMO_SUITE))
def test_demo_template_compile_round_trips_with_ui_and_seed(tmp_path: Path, slug: str) -> None:
    """``load_template`` round-trips each demo-suite template back to a spec
    whose ui carries children and whose focal seed list carries rows — the
    install-time compile-on-create seam preserves the rendered surface, not
    just the sources/state-binding machinery."""
    install_bundled_templates(destination_root=tmp_path)
    loaded = load_template(slug, templates_dir=tmp_path)
    assert loaded is not None
    spec = loaded["ripple_spec"]
    assert spec["ui"].get("children"), f"{slug}: round-tripped ui has no children"
    focal_key = _DEMO_SUITE[slug]
    assert spec["state"].get(focal_key), f"{slug}: round-tripped seed list empty"


@pytest.mark.parametrize("slug", sorted(_DEMO_GATED))
def test_demo_template_actions_are_all_gated_require_approval(slug: str) -> None:
    """Every action on a gated demo-suite template declares
    ``instinct_policy: require_approval`` so it surfaces as a PENDING proposal
    in The Tray, never auto-runs — the same contract triage clears."""
    template = PocketTemplate.model_validate(_read_meta(slug))
    assert template.actions, f"{slug}: expected gated actions"
    for action in template.actions:
        assert action.instinct_policy == "require_approval", action.name


@pytest.mark.parametrize("slug", sorted(_DEMO_GATED))
def test_demo_template_action_outcomes_are_declared_in_catalog(slug: str) -> None:
    """Each gated action's outcomes_emitted is declared in the top-level
    outcomes[] catalog — the RFC 03 v2 subset rule."""
    template = PocketTemplate.model_validate(_read_meta(slug))
    catalog = set(template.outcomes)
    for action in template.actions:
        for outcome in action.outcomes_emitted:
            assert outcome in catalog, f"{slug}: {action.name} emits undeclared {outcome}"


@pytest.mark.parametrize("slug", sorted(_DEMO_GATED))
def test_demo_template_action_row_proposes_not_executes(slug: str) -> None:
    """The ripple_spec action buttons set ``state.pending_proposal`` (a
    generic {action, <entity>_id, summary} proposal) instead of executing —
    the binding seam a deployment wires to external_actions.propose. The
    state seeds an empty proposal slot."""
    spec = _read_spec(slug)
    id_key = _DEMO_GATED[slug]
    buttons = [w for w in _iter_widgets(spec["ui"]) if w.get("type") == "button"]
    assert buttons, f"{slug}: no action buttons"
    for button in buttons:
        actions = button.get("on_click")
        assert actions, f"{slug}: button {button['props']['label']} has no on_click"
        targets = {a.get("target") for a in actions}
        assert targets == {"pending_proposal"}, button["props"]["label"]
        value = actions[0]["value"]
        assert set(value.keys()) == {"action", id_key, "summary"}, button["props"]["label"]
    assert spec["state"].get("pending_proposal") is None


def test_events_board_has_sell_through_and_run_of_show() -> None:
    """events-board seeds the sell-through strip (status counts + headline
    stats), the events queue, and per-event ticket tiers + run-of-show, and
    renders a sell-through ring + progress bars."""
    spec = _read_spec("events-board")
    state = spec["state"]
    for key in ("status_counts", "total_sold", "total_revenue", "events", "selected"):
        assert key in state, f"events-board state missing {key}"
    # Every event carries the story fields the canvas reads.
    for ev in state["events"]:
        assert ev.get("sold_pct") is not None and ev.get("pct_color"), ev.get("name")
        assert ev.get("tiers") and ev.get("run_of_show"), ev.get("name")
    # The redesign tells a story: one nearly sold out, one needing a push, one past.
    pcts = {ev["sold_pct"] for ev in state["events"]}
    assert max(pcts) >= 90, "no nearly-sold-out event"
    assert any(p < 30 for p in pcts), "no slow event needing a push"
    rings = [w for w in _iter_widgets(spec["ui"]) if w.get("type") == "progress-ring"]
    assert rings, "events-board should render a sell-through ring"


def test_renewals_radar_bands_by_risk_with_lapsed_recoverable() -> None:
    """renewals-radar seeds members across high/medium/safe risk bands with a
    revenue-at-risk strip, a churn-risk ring, and at least one lapsed member
    (negative days_left) that is recoverable within grace."""
    spec = _read_spec("renewals-radar")
    state = spec["state"]
    for key in ("risk_counts", "revenue_at_risk", "members", "selected"):
        assert key in state, f"renewals-radar state missing {key}"
    bands = {m["risk_band"] for m in state["members"]}
    assert {"High risk", "Medium", "Safe"} <= bands, f"missing a risk band: {bands}"
    assert any(m["days_left"] < 0 for m in state["members"]), "no lapsed member"
    rings = [w for w in _iter_widgets(spec["ui"]) if w.get("type") == "progress-ring"]
    assert rings, "renewals-radar should render a churn-risk ring"


def test_orders_fulfillment_has_stage_board_and_detail() -> None:
    """orders-fulfillment seeds orders across the four stages, drives a kanban
    stage board bound to the order list, and seeds per-order items + shipping
    + timeline for the detail panel."""
    spec = _read_spec("orders-fulfillment")
    state = spec["state"]
    for key in ("stage_counts", "stage_columns", "orders", "selected"):
        assert key in state, f"orders-fulfillment state missing {key}"
    stages = {o["stage_id"] for o in state["orders"]}
    assert {"to_ship", "shipped", "delivered", "refund_requested"} <= stages, stages
    for o in state["orders"]:
        assert o.get("items") and o.get("shipping") and o.get("timeline"), o.get("ref")
    kanbans = [w for w in _iter_widgets(spec["ui"]) if w.get("type") == "kanban"]
    assert len(kanbans) == 1, "orders-fulfillment should drive one kanban stage board"
    assert kanbans[0].get("bind") == "orders", "kanban must bind the order list"
    assert kanbans[0]["props"].get("columnKey") == "stage_id"


def test_revenue_pulse_is_read_only_dashboard_with_charts() -> None:
    """revenue-pulse is a read-only executive dashboard: zero actions, no
    on_click anywhere, a six-month trend series, a by-category split, and the
    approval funnel — rendered through stat tiles + chart widgets."""
    template = PocketTemplate.model_validate(_read_meta("revenue-pulse"))
    assert template.actions == []

    spec = _read_spec("revenue-pulse")
    for widget in _iter_widgets(spec["ui"]):
        assert "on_click" not in widget, f"dashboard has an on_click: {widget.get('type')}"
    state = spec["state"]
    for key in ("kpis", "trend", "category_chart", "categories", "approvals"):
        assert key in state, f"revenue-pulse state missing {key}"
    assert len(state["trend"]) == 6, "expected six months of trend data"
    charts = [w for w in _iter_widgets(spec["ui"]) if w.get("type") == "chart"]
    assert len(charts) >= 2, "revenue-pulse should render trend + category charts"
    chart_types = {c["props"].get("type") for c in charts}
    assert chart_types <= {"area", "bar", "line"}, f"non-catalog chart type: {chart_types}"


# ---------------------------------------------------------------------------
# Installer — every vertical template mirrors like every other template
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
