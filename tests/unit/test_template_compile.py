# tests/unit/test_template_compile.py
# Created: 2026-05-25 (feat/rfc-03-v2-compile) — RED-first tests for the
# new ``pocketpaw.bundled_templates.compile`` module. Covers:
#   1. ``compile_template`` translates ``data_sources[]`` into a
#      runtime-shaped ``{"sources": {name: {method, path, bind,
#      refresh, refresh_interval_seconds?}}}`` dict.
#   2. The ``name`` field becomes the dict key (matching runtime
#      ``rippleSpec.sources`` convention) and never appears inside the
#      per-source value.
#   3. The compile-time ``refresh`` list translates from the RFC's
#      colon-suffixed form (``interval:1h``, ``signal:gmail.inbox.update``)
#      to the runtime's bare-keyword form
#      (``interval`` + numeric ``refresh_interval_seconds``;
#      ``signal:*`` is dropped because the runtime has no equivalent
#      yet — deferred to a future PR).
#   4. A template with no ``data_sources`` produces ``{"sources": {}}``.
#   5. Cross-boundary shape compatibility — every compiled source dict
#      must validate as a runtime ``SourceBinding``. Gated by
#      ``pytest.importorskip("pocketpaw_ee")`` so the OSS-only install
#      still runs the rest of the suite.
#   6. Deferred fields (actions, agents, triggers, permissions,
#      instinct_rules, connectors, outcomes, data_sources[].transform)
#      passthrough verbatim into the compile output so downstream PRs
#      can rely on the seam progressively.

"""Tests for ``pocketpaw.bundled_templates.compile.compile_template``.

These tests pin the OSS→EE seam:

* The OSS-side ``compile_template`` is a pure function from a validated
  ``PocketTemplate`` (or a v2-shaped dict) to a runtime-shaped
  ``rippleSpec`` dict.
* The runtime-side ``SourceBinding`` (which lives in EE under
  ``pocketpaw_ee.cloud.pockets.source_executor``) MUST be able to
  validate each compiled per-source dict.

The OSS production code never imports ``pocketpaw_ee``. These tests
do — but ONLY inside the test function bodies, guarded by
``pytest.importorskip("pocketpaw_ee")``. This is the same pattern the
existing ``tests/unit/test_bundled_templates.py`` uses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

# RED on import until Phase 2 lands compile.py.
from pocketpaw.bundled_templates import PocketTemplate
from pocketpaw.bundled_templates.compile import compile_template

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "templates"
_LEASE_V2_FIXTURE = _FIXTURES_DIR / "lease-renewal-v2.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture(path: Path) -> PocketTemplate:
    """Parse the lease-renewal fixture into a validated ``PocketTemplate``."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return PocketTemplate.model_validate(data)


def _minimal_v2_dict(**overrides: Any) -> dict[str, Any]:
    """Smallest valid v2 template dict, used as a baseline for tests."""
    base: dict[str, Any] = {
        "schema_version": "2",
        "name": "compile-test",
        "version": "1.0.0",
        "pattern": "app",
        "vertical": "productivity",
        "description": "Minimal template for compile tests.",
        "shape": "data-grid",
        "state": {
            "entity_type": "Thing",
            "columns": [{"field": "title", "widget": "text"}],
        },
    }
    base.update(overrides)
    return base


def _minimal_v2_template(**overrides: Any) -> PocketTemplate:
    return PocketTemplate.model_validate(_minimal_v2_dict(**overrides))


# ---------------------------------------------------------------------------
# data_sources[] translation
# ---------------------------------------------------------------------------


def test_no_data_sources_yields_empty_sources_block() -> None:
    """A template with no ``data_sources`` produces ``{"sources": {}}``."""
    template = _minimal_v2_template()
    out = compile_template(template)
    assert isinstance(out, dict)
    assert out.get("sources") == {}


def test_single_data_source_compiles_to_runtime_shape() -> None:
    """A single source compiles to a dict keyed by name with the four
    runtime-shape fields (``method, path, bind, refresh``)."""
    template = _minimal_v2_template(
        data_sources=[
            {
                "name": "my_source",
                "method": "GET",
                "path": "/items",
                "bind": "state.items",
                # default refresh — exercises the "no transform applied" path
            }
        ]
    )
    out = compile_template(template)
    sources = out["sources"]
    assert "my_source" in sources
    entry = sources["my_source"]
    assert entry["method"] == "GET"
    assert entry["path"] == "/items"
    assert entry["bind"] == "state.items"
    # default refresh comes from the schema's default factory
    assert entry["refresh"] == ["pocket_open", "manual"]
    # name MUST become the dict key — never appear inside the value
    assert "name" not in entry


def test_lease_renewal_fixture_data_sources_compile() -> None:
    """The canonical RFC v2 fixture has two ``data_sources`` entries.
    Compile must produce a sources block with both entries under their
    declared names. ``interval:1h`` translates to ``"interval"`` plus a
    3600 ``refresh_interval_seconds`` value. ``signal:gmail.inbox.update``
    drops because the runtime has no equivalent — deferred to a future PR."""
    template = _load_fixture(_LEASE_V2_FIXTURE)
    out = compile_template(template)

    sources = out["sources"]
    assert set(sources.keys()) == {"expiring_leases", "tenant_responses"}

    # expiring_leases: pocket_open, manual, interval:1h
    expiring = sources["expiring_leases"]
    assert expiring["method"] == "GET"
    assert expiring["path"] == "/leases?expiry_within=90d"
    assert expiring["bind"] == "state.leases"
    assert "pocket_open" in expiring["refresh"]
    assert "manual" in expiring["refresh"]
    assert "interval" in expiring["refresh"]
    assert expiring.get("refresh_interval_seconds") == 3600
    # The colon-suffixed RFC form must NOT leak into the runtime dict
    assert "interval:1h" not in expiring["refresh"]

    # tenant_responses: pocket_open + signal:gmail.inbox.update -> signal
    # drops because the runtime has no equivalent yet.
    tenant = sources["tenant_responses"]
    assert "pocket_open" in tenant["refresh"]
    # signal:* dropped per documented deferred-feature behaviour
    assert not any("signal" in r for r in tenant["refresh"])


def test_interval_duration_parses_seconds_minutes_hours() -> None:
    """Verify the duration parser across the three RFC-supported units."""
    template = _minimal_v2_template(
        data_sources=[
            {
                "name": "fast",
                "method": "GET",
                "path": "/a",
                "bind": "state.a",
                "refresh": ["interval:30s"],
            },
            {
                "name": "med",
                "method": "GET",
                "path": "/b",
                "bind": "state.b",
                "refresh": ["interval:5m"],
            },
            {
                "name": "slow",
                "method": "GET",
                "path": "/c",
                "bind": "state.c",
                "refresh": ["interval:2h"],
            },
        ]
    )
    out = compile_template(template)
    assert out["sources"]["fast"]["refresh_interval_seconds"] == 30
    assert out["sources"]["med"]["refresh_interval_seconds"] == 300
    assert out["sources"]["slow"]["refresh_interval_seconds"] == 7200


def test_explicit_refresh_pocket_open_manual_only() -> None:
    """An explicit ``refresh: [pocket_open]`` is passed through as-is —
    no ``refresh_interval_seconds`` should be set."""
    template = _minimal_v2_template(
        data_sources=[
            {
                "name": "only_open",
                "method": "GET",
                "path": "/x",
                "bind": "state.x",
                "refresh": ["pocket_open"],
            }
        ]
    )
    out = compile_template(template)
    entry = out["sources"]["only_open"]
    assert entry["refresh"] == ["pocket_open"]
    assert "refresh_interval_seconds" not in entry


def test_data_source_transform_passes_through() -> None:
    """``data_sources[].transform`` is declared in the RFC but is RFC 04
    M2 runtime — it must pass through the compile output verbatim so
    downstream consumers see it, NOT silently dropped."""
    template = _minimal_v2_template(
        data_sources=[
            {
                "name": "with_xform",
                "method": "GET",
                "path": "/y",
                "bind": "state.y",
                "transform": "flatten_leases",
            }
        ]
    )
    out = compile_template(template)
    entry = out["sources"]["with_xform"]
    assert entry.get("transform") == "flatten_leases"


# ---------------------------------------------------------------------------
# Top-level passthrough for deferred fields
# ---------------------------------------------------------------------------


def test_deferred_top_level_fields_passthrough() -> None:
    """Actions / agents / triggers / permissions / instinct_rules /
    connectors / outcomes are documented as out-of-scope for PR 2b but
    they must still flow through the compile output verbatim so PRs
    2c-2g have something to consume. They're at the top level alongside
    ``sources``."""
    template = _load_fixture(_LEASE_V2_FIXTURE)
    out = compile_template(template)
    # actions: lease-renewal has 5
    assert isinstance(out.get("actions"), list)
    assert len(out["actions"]) == 5
    # agents: 1 (renewal-drafter)
    assert isinstance(out.get("agents"), list)
    assert len(out["agents"]) == 1
    # triggers: 4 (cron, source_change, temporal, manual)
    assert isinstance(out.get("triggers"), list)
    assert len(out["triggers"]) == 4
    # outcomes: 3
    assert out.get("outcomes") == ["lease_drafted", "renewal_sent", "renewal_completed"]
    # connectors: 3
    assert out.get("connectors") == ["yardi", "gmail", "gcalendar"]
    # permissions: workspace
    assert isinstance(out.get("permissions"), dict)
    assert out["permissions"]["scope"] == "workspace"
    # instinct_rules: 4 rules
    assert isinstance(out.get("instinct_rules"), dict)
    assert len(out["instinct_rules"]["rules"]) == 4


def test_top_level_state_passes_through() -> None:
    """The ``state`` block (Fabric binding) compiles into the top-level
    of the rippleSpec — UI layer needs it for column/widget rendering."""
    template = _load_fixture(_LEASE_V2_FIXTURE)
    out = compile_template(template)
    state = out.get("state")
    assert isinstance(state, dict)
    assert state["entity_type"] == "Lease"
    assert state["id_field"] == "id"
    # joined_entities preserved
    names = [j["name"] for j in state["joined_entities"]]
    assert "tenant" in names and "property" in names


def test_top_level_metadata_passes_through() -> None:
    """``name``, ``version``, ``display_name``, etc. — the create-side
    template metadata flows through so install-time tooling can read it
    off the compile output without re-parsing the template."""
    template = _load_fixture(_LEASE_V2_FIXTURE)
    out = compile_template(template)
    assert out["name"] == "lease-renewal-v1"
    assert out["version"] == "1.0.0"
    assert out["pattern"] == "app"
    assert out["shape"] == "data-grid"
    assert out["display_name"] == "Lease Renewal"
    assert out["schema_version"] == "2"


# ---------------------------------------------------------------------------
# Cross-boundary shape compatibility — the critical test.
#
# Lazy-imports ``SourceBinding`` from EE INSIDE the test body, guarded
# by ``pytest.importorskip("pocketpaw_ee")`` so the test is a no-op on
# the OSS-only install. Production code never imports EE.
# ---------------------------------------------------------------------------


def test_compiled_sources_validate_as_runtime_source_binding() -> None:
    """For every compiled source dict, the runtime
    ``SourceBinding.model_validate(dict)`` must succeed. This is the
    cross-boundary contract test — it proves the compile output is
    consumable by the runtime executor."""
    pytest.importorskip("pocketpaw_ee")
    # Lazy import — never reached on an OSS-only install.
    from pocketpaw_ee.cloud.pockets.source_executor import SourceBinding

    template = _load_fixture(_LEASE_V2_FIXTURE)
    out = compile_template(template)
    sources = out["sources"]
    assert sources, "fixture has data_sources; compile must produce a non-empty sources block"

    for name, entry in sources.items():
        # SourceBinding ignores unknown keys (no extra='forbid'), so
        # ``transform`` and other passthrough keys are tolerated.
        binding = SourceBinding.model_validate(entry)
        # Spot-check the round-tripped shape mirrors compile output for
        # the fields the runtime cares about.
        assert binding.method == entry["method"], f"method mismatch for {name}"
        assert binding.path == entry["path"], f"path mismatch for {name}"
        assert binding.bind == entry["bind"], f"bind mismatch for {name}"
        # refresh list comparison — runtime preserves order
        assert list(binding.refresh) == entry["refresh"], f"refresh mismatch for {name}"


def test_minimal_template_compiled_source_validates_runtime() -> None:
    """A hand-built minimal source dict also validates against the
    runtime ``SourceBinding`` — catches drift between the compile output
    shape and the runtime's Pydantic field set."""
    pytest.importorskip("pocketpaw_ee")
    from pocketpaw_ee.cloud.pockets.source_executor import SourceBinding

    template = _minimal_v2_template(
        data_sources=[
            {
                "name": "trivial",
                "method": "GET",
                "path": "/items",
                "bind": "state.items",
                "refresh": ["pocket_open", "manual", "interval:10m"],
            }
        ]
    )
    out = compile_template(template)
    entry = out["sources"]["trivial"]
    binding = SourceBinding.model_validate(entry)
    assert "interval" in binding.refresh
    assert binding.refresh_interval_seconds == 600


# ---------------------------------------------------------------------------
# Input flexibility — accept either a PocketTemplate or a dict.
# ---------------------------------------------------------------------------


def test_compile_accepts_pocket_template_instance() -> None:
    """Primary contract — ``compile_template`` accepts a validated
    ``PocketTemplate`` model instance."""
    template = _minimal_v2_template()
    out = compile_template(template)
    assert isinstance(out, dict)


# ---------------------------------------------------------------------------
# Determinism + purity
# ---------------------------------------------------------------------------


def test_compile_is_pure_no_template_mutation() -> None:
    """``compile_template`` must not mutate the input PocketTemplate."""
    template = _load_fixture(_LEASE_V2_FIXTURE)
    before_dump = template.model_dump(mode="json")
    compile_template(template)
    after_dump = template.model_dump(mode="json")
    assert before_dump == after_dump, "compile_template mutated the input template"


def test_compile_is_deterministic() -> None:
    """Twice-compiled identical inputs produce identical outputs."""
    template = _load_fixture(_LEASE_V2_FIXTURE)
    a = compile_template(template)
    b = compile_template(template)
    assert a == b
