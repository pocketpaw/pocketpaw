# Smoke Testing — RFC 03 v2 Runtime Layer (cel / instinct / temporal / fabric / bulk)

| Field        | Value                                          |
|--------------|------------------------------------------------|
| Status       | runnable                                       |
| Date         | 2026-05-28                                     |
| Branch       | `feat/rfc-03-v2-integration` (HEAD `749f41d9`) |
| Tracking PR  | [#1228 (integration → dev, DRAFT)](https://github.com/pocketpaw/pocketpaw/pull/1228) |
| Sub-PRs (merged) | #1269 (CEL), #1271 (Instinct), #1272 (Temporal), #1273 (Fabric), #1274 (Bulk) |

## What this document is

Runnable smoke-test checklist for the **runtime decision layer** of RFC 03 v2. CI already covers unit tests, ruff, OSS-EE boundary, wheels. This file covers end-to-end behaviour from real YAML through compile, CEL eval, Instinct decisions, temporal rising edges, Fabric validation, and bulk fan-out. Pairs with the wave-1 doc at `docs/internal/2026-05-rfc-03-v2-manual-testing.md` (chokepoint + CLI + compile).

## Setup

```bash
# Workspace root, then check out the integration worktree
cd /Users/prakash-1/Documents/paw-workspace/pocketpaw/.claude/worktrees/rfc03-integration

# Verify HEAD has all five runtime PRs merged
git log --oneline -7
# Expected (top 5):
#   749f41d9 feat(bulk)
#   2f3a6000 feat(fabric)
#   a0d5df23 feat(temporal)
#   de4da929 feat(instinct)
#   e9e01a30 feat(cel)

# Install with the EE group so cross-boundary tests can verify shape compatibility
uv sync --dev --group ee
```

For the OSS-only boundary check (§8), use a separate clone or `uv sync --dev` without `--group ee`.

## What this layer ships

Five OSS-side pure libraries that together turn an RFC 03 v2 `PocketTemplate` into runtime decisions:

| Module | Public surface | Consumes |
|---|---|---|
| `cel_runtime.py` | `evaluate_cel`, `collect_free_identifiers`, `CelEvaluationError` | celpy |
| `identifier_resolver.py` | `IdentifierResolver` Protocol, `TemplateIdentifierResolver` | schema |
| `instinct_composer.py` | `resolve_instinct`, `InstinctDecision`, `InstinctResolutionError` | cel_runtime + identifier_resolver |
| `temporal_sweeper.py` | `sweep_temporal_triggers`, `SweepResult`, `TemporalRisingEdge` | cel_runtime + identifier_resolver |
| `fabric_registry.py` + `fabric_resolver.py` + `fabric_validator.py` | `FabricRegistry` Protocol, `FabricResolver`, `validate_template_with_registry` | cel_runtime + identifier_resolver |
| `bulk_executor.py` | `plan_bulk_execution`, `BulkPlan`, `BulkApprovalRequest` | instinct_composer |

All pure functions. No I/O. No EE imports. Caller decides what to do with the output.

## §1 — Sanity: full unit suite green

```bash
uv run pytest tests/unit/ -q
```

Pass criteria: all green, no failures. Approximately ~205 passed + ~13 pre-existing skips (the `pytest.importorskip("pocketpaw_ee")` gates).

Fail criteria: any unit test failure. The runtime layer is library-only — any failure here is a real regression.

## §2 — CEL runtime evaluator

```bash
uv run python - <<'PY'
from datetime import datetime, timezone
from pocketpaw.bundled_templates import evaluate_cel, TemplateIdentifierResolver
from pocketpaw.bundled_templates.schema import StateBinding

# Minimal state declaring one joined entity for dotted-path tests
state = StateBinding.model_validate({
    "entity_type": "Lease",
    "joined_entities": [{"name": "tenant", "entity_type": "Tenant", "via_link": "lease_tenant"}],
    "columns": [{"field": "days_remaining", "widget": "text"}, {"field": "tenant.name", "widget": "text"}],
})
resolver = TemplateIdentifierResolver(state)

now = datetime(2026, 5, 28, tzinfo=timezone.utc)
cases = [
    ("days_remaining <= 30", {"days_remaining": 25}, True),
    ("renewal_stage == 'sent'", {"renewal_stage": "sent"}, True),
    ("privilege_flag == null", {"privilege_flag": None}, True),
    ("rent_proposed < rent_current * 0.95", {"rent_proposed": 1800.0, "rent_current": 2000.0}, True),
    ("tenant.late_payment_count_12mo >= 3", {"tenant": {"late_payment_count_12mo": 4}}, True),
]
for expr, ctx, expected in cases:
    result = evaluate_cel(expr, ctx, resolver, now=now)
    print(f"  {expr!r:60s} -> {result}  (expected {expected})")
    assert result == expected, expr
print("CEL smoke OK")
PY
```

Pass criteria: 5 lines, all `True`, final `CEL smoke OK`.

Fail criteria: any `CelEvaluationError`, any wrong result, any `False` where `True` expected.

**Known gotcha:** floats vs ints. `rent_proposed < rent_current * 0.95` only works when both inputs are Python `float`. `IntType * DoubleType` raises in celpy. Production callers must coerce numeric columns to `float` before passing context. Documented in PR 2c's PR body.

## §3 — Instinct composer (the 3 RFC worked examples)

```bash
uv run python - <<'PY'
import yaml
from datetime import datetime, timezone
from pocketpaw.bundled_templates import resolve_instinct
from pocketpaw.bundled_templates.schema import PocketTemplate

template = PocketTemplate.model_validate(yaml.safe_load(open("tests/fixtures/templates/lease-renewal-v2.yaml").read()))
now = datetime(2026, 5, 28, tzinfo=timezone.utc)

# Example A — auto / notify_only action blocked by top-level rule
row_a = {"rent_proposed": 1800.0, "rent_current": 2000.0, "renewal_stage": "sent",
         "days_remaining": 25, "tenant": {"late_payment_count_12mo": 0}, "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc),
         "rent_proposed_delta_pct": 2.5}
d_a = resolve_instinct(template, "mark_renewed", row_a, now=now)
print(f"  A: mark_renewed + 5% cut → {d_a.verdict} ({d_a.reason})")
assert d_a.verdict == "BLOCK"

# Example B — auto action promoted to approval by overlay rule
row_b = {"rent_proposed": 2050.0, "rent_current": 2000.0, "renewal_stage": "sent",
         "days_remaining": 25, "tenant": {"late_payment_count_12mo": 4}, "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc),
         "rent_proposed_delta_pct": 2.5}
d_b = resolve_instinct(template, "send_to_tenant", row_b, now=now)
print(f"  B: send_to_tenant + 4 late payments → {d_b.verdict} ({d_b.reason})")
assert d_b.verdict == "ESCALATE_APPROVAL"

# Example C — require_approval per-action floor with no rule match
row_c = {"rent_proposed": 2050.0, "rent_current": 2000.0, "renewal_stage": None,
         "days_remaining": 60, "tenant": {"late_payment_count_12mo": 1}, "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc),
         "rent_proposed_delta_pct": 2.5}
d_c = resolve_instinct(template, "bulk_draft", row_c, now=now)
print(f"  C: bulk_draft + no rules match → {d_c.verdict} ({d_c.reason})")
assert d_c.verdict == "ESCALATE_APPROVAL"
print("Instinct smoke OK")
PY
```

Pass criteria: `A: BLOCK (blocked_by_rule)`, `B: ESCALATE_APPROVAL (operator_overlay_escalated)`, `C: ESCALATE_APPROVAL (author_floor)`, final `Instinct smoke OK`.

Fail criteria: any verdict mismatch. These are the canonical RFC worked examples; mismatch means the resolution order regressed.

## §4 — Temporal trigger sweeper (rising-edge detection)

```bash
uv run python - <<'PY'
import yaml
from datetime import datetime, timezone
from pocketpaw.bundled_templates import sweep_temporal_triggers
from pocketpaw.bundled_templates.schema import PocketTemplate

template = PocketTemplate.model_validate(yaml.safe_load(open("tests/fixtures/templates/lease-renewal-v2.yaml").read()))
now = datetime(2026, 5, 28, tzinfo=timezone.utc)

# Three leases: one within 60d + null stage (should fire), one too far out, one already sent
rows = [
    {"id": "lease-1", "expires_at": datetime(2026, 7, 15, tzinfo=timezone.utc), "renewal_stage": None},
    {"id": "lease-2", "expires_at": datetime(2026, 12, 15, tzinfo=timezone.utc), "renewal_stage": None},
    {"id": "lease-3", "expires_at": datetime(2026, 7, 15, tzinfo=timezone.utc), "renewal_stage": "sent"},
]

# First sweep — empty prior state. lease-1's predicate is currently true; rising-edge from False default.
r1 = sweep_temporal_triggers(template, rows, last_seen_state={}, now=now)
print(f"  sweep 1: {len(r1.rising_edges)} rising edges, {len(r1.errors)} errors")
for e in r1.rising_edges:
    print(f"    {e.row_id} → action={e.action}")

# Second sweep — same rows, prior state from r1. No new rising edges (lease-1 is continuing-true).
r2 = sweep_temporal_triggers(template, rows, last_seen_state=r1.new_state, now=now)
print(f"  sweep 2 (idempotent): {len(r2.rising_edges)} rising edges (expected 0)")
assert len(r2.rising_edges) == 0
print("Temporal smoke OK")
PY
```

Pass criteria: sweep 1 fires 1 rising edge for `lease-1 → draft_renewal`. Sweep 2 fires 0 (idempotent — continuing-true does not re-fire). Final `Temporal smoke OK`.

**Known gotcha:** pass `expires_at` as a Python `datetime`, not an ISO string. celpy's `json_to_cel` does not auto-convert ISO strings to `TimestampType`. Production callers reading from JSON wire must parse timestamps first.

## §5 — Fabric tier:registered validator

```bash
uv run python - <<'PY'
import yaml
from pocketpaw.bundled_templates import (
    PocketTemplate, validate_template_with_registry, NullFabricRegistry,
)

class MockRegistry:
    def __init__(self, entities, links):
        self.entities = set(entities)
        self.links = set(links)
    def entity_type_exists(self, name): return name in self.entities
    def link_exists(self, from_type, to_type, link_name):
        return (from_type, to_type, link_name) in self.links
    def get_entity_properties(self, name): return set()

template = PocketTemplate.model_validate(yaml.safe_load(open("tests/fixtures/templates/lease-renewal-v2.yaml").read()))

# Clean: registry knows everything
clean = MockRegistry(
    entities={"Lease", "Tenant", "Property"},
    links={("Lease", "Tenant", "lease_tenant"), ("Lease", "Property", "lease_property")},
)
e_clean = validate_template_with_registry(template, clean)
print(f"  lease-renewal + clean registry: {len(e_clean)} errors")
assert len(e_clean) == 0

# Broken: registry missing the joined entities + links
broken = MockRegistry(entities={"Lease"}, links=set())
e_broken = validate_template_with_registry(template, broken)
print(f"  lease-renewal + broken registry: {len(e_broken)} errors")
for e in e_broken[:4]:
    print(f"    {e.severity}: {e.message}")
assert len(e_broken) >= 4  # both joined entities + both via_links

# Synthetic-tier template (todo-task-tracker has no dots, no joins): passthrough
todo = PocketTemplate.model_validate(yaml.safe_load(open("src/pocketpaw/bundled_templates/_bundled/todo-task-tracker/template.pocket.yaml").read()))
e_synth = validate_template_with_registry(todo, NullFabricRegistry())
print(f"  todo-task-tracker + NullFabricRegistry: {len(e_synth)} errors (synthetic passthrough)")
assert len(e_synth) == 0
print("Fabric smoke OK")
PY
```

Pass criteria: clean registry → 0 errors. Broken registry → at least 4 errors (both Tenant + Property unknown, both via_links missing). Synthetic + NullRegistry → 0 errors. Final `Fabric smoke OK`.

## §6 — Bulk fan-out planner

```bash
uv run python - <<'PY'
import yaml
from datetime import datetime, timezone
from pocketpaw.bundled_templates import plan_bulk_execution
from pocketpaw.bundled_templates.schema import PocketTemplate

template = PocketTemplate.model_validate(yaml.safe_load(open("tests/fixtures/templates/lease-renewal-v2.yaml").read()))
now = datetime(2026, 5, 28, tzinfo=timezone.utc)

# bulk_draft has instinct_policy: require_approval. 3 rows:
# - lease-1: clean → approval
# - lease-2: clean → approval
# - lease-3: rent_proposed < rent_current * 0.95 → BLOCK
rows = [
    {"id": "lease-1", "rent_proposed": 2050.0, "rent_current": 2000.0, "renewal_stage": None,
     "days_remaining": 60, "tenant": {"late_payment_count_12mo": 1}, "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc),
     "rent_proposed_delta_pct": 2.5},
    {"id": "lease-2", "rent_proposed": 1900.0, "rent_current": 2000.0, "renewal_stage": None,
     "days_remaining": 45, "tenant": {"late_payment_count_12mo": 0}, "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc),
     "rent_proposed_delta_pct": 5.0},
    {"id": "lease-3", "rent_proposed": 1700.0, "rent_current": 2000.0, "renewal_stage": None,
     "days_remaining": 30, "tenant": {"late_payment_count_12mo": 0}, "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc),
     "rent_proposed_delta_pct": 15.0},
]

plan = plan_bulk_execution(template, "bulk_draft", rows, now=now)
print(f"  total_rows: {plan.total_rows}")
print(f"  executions: {len(plan.executions)}")
print(f"  blocked:    {len(plan.blocked)}  (lease-3 trips block rule)")
print(f"  approval:   {'present' if plan.approval_request else 'None'}  (single batch for lease-1 + lease-2)")
if plan.approval_request:
    print(f"    row_ids:  {plan.approval_request.row_ids}")
    print(f"    reason:   {plan.approval_request.reason}")
assert plan.total_rows == 3
assert len(plan.blocked) == 1
assert plan.approval_request is not None
assert set(plan.approval_request.row_ids) == {"lease-1", "lease-2"}
print("Bulk smoke OK")
PY
```

Pass criteria: total=3, executions=0 (require_approval policy), blocked=1 (lease-3), single approval_request for lease-1 + lease-2 with reason `author_floor`. Final `Bulk smoke OK`.

## §7 — End-to-end chain (template → compile → bulk → instinct decision)

The full library layer composed:

```bash
uv run python - <<'PY'
import yaml
from datetime import datetime, timezone
from pocketpaw.bundled_templates import (
    PocketTemplate, compile_template, plan_bulk_execution, resolve_instinct,
    validate_template_with_registry, NullFabricRegistry,
)

# Load + validate
template = PocketTemplate.model_validate(yaml.safe_load(open("tests/fixtures/templates/lease-renewal-v2.yaml").read()))
print(f"  ✓ Template loaded: {template.name} v{template.version}")

# Validate against a permissive registry (NullFabricRegistry skips strict tier checks
# for templates that don't declare joins; for lease-renewal which DOES declare joins,
# expect errors — that's correct, since Null doesn't know about Lease/Tenant/Property)
errors = validate_template_with_registry(template, NullFabricRegistry())
print(f"  ✓ Fabric validator ran: {len(errors)} errors against NullFabricRegistry")

# Compile data_sources to runtime spec
runtime = compile_template(template)
print(f"  ✓ Compiled: sources={list(runtime['sources'].keys())}, actions={len(runtime['actions'])}")

# Pick a row and ask the composer what should happen for one action
now = datetime(2026, 5, 28, tzinfo=timezone.utc)
row = {"id": "lease-1", "rent_proposed": 2050.0, "rent_current": 2000.0, "renewal_stage": None,
       "days_remaining": 60, "tenant": {"late_payment_count_12mo": 4}, "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc),
       "rent_proposed_delta_pct": 2.5}
decision = resolve_instinct(template, "send_to_tenant", row, now=now)
print(f"  ✓ Instinct decision for send_to_tenant: {decision.verdict} ({decision.reason})")

# Fan it out as a bulk operation on 3 rows
rows = [row, {**row, "id": "lease-2"}, {**row, "id": "lease-3", "rent_proposed": 1700.0}]
plan = plan_bulk_execution(template, "bulk_draft", rows, now=now)
print(f"  ✓ Bulk plan: {plan.total_rows} rows · {len(plan.blocked)} blocked · "
      f"{len(plan.executions)} executions · approval={'yes' if plan.approval_request else 'no'}")

print("End-to-end smoke OK")
PY
```

Pass criteria: all 5 `✓` lines + final `End-to-end smoke OK`. Demonstrates the library layer composes cleanly when consumed end-to-end.

## §8 — OSS-only boundary check

Open a fresh shell. Run `uv sync --dev` (without `--group ee`). Confirm no EE module enters the import graph when the runtime layer is exercised:

```bash
uv run python - <<'PY'
import sys
from pocketpaw.bundled_templates import (
    evaluate_cel, resolve_instinct, sweep_temporal_triggers,
    validate_template_with_registry, plan_bulk_execution,
)
# Exercise the imports above; check sys.modules
ee_modules = [n for n in sys.modules if n.startswith("pocketpaw_ee")]
assert not ee_modules, f"OSS-EE boundary violation: {ee_modules}"
print("✓ OSS-only boundary clean — no pocketpaw_ee imports in production path")
PY
```

Pass criteria: `✓ OSS-only boundary clean`. Any `pocketpaw_ee.*` module in `sys.modules` after the import set above is a regression. The import-linter contract in CI covers this statically; this dynamic check catches anything static analysis misses.

## §9 — Known gotchas (production caller contracts)

The runtime layer is type-strict by design. Production callers must coerce wire data before passing context:

| Gotcha | Where | Fix |
|---|---|---|
| `IntType * DoubleType` raises in celpy | Anywhere `evaluate_cel` runs an expression mixing int columns with float literals (`rent_current * 0.95`) | Coerce numeric columns to `float` before assembling the context dict. |
| ISO timestamps don't parse to `TimestampType` | `within(field, duration(...))` with `field` as an ISO string | Parse ISO timestamps to `datetime` objects before passing them in the context. |
| `tier: registered` is implicit | The schema has no explicit `tier` field. `FabricResolver` is strict; `TemplateIdentifierResolver` is loose. | Choose the resolver explicitly per call site. PR 2c's reference resolver is the right default for runtime; PR 2g's `FabricResolver` is for lint paths. |
| `BulkApprovalRequest.row_ids` are strings | Whatever you pass for `row_id_field` gets `str()`-coerced | Don't rely on int round-trip; the approval queue carries strings. |
| `NullFabricRegistry` is non-passthrough for templates with joins | A template with declared joins + `NullFabricRegistry` produces errors | Use `NullFabricRegistry` only when no joins are declared, or wire a real `FabricRegistry`. |

## What's NOT covered here (separate work)

- EE-side wiring of all five modules into `ee/cloud/pockets/` (the actual approval queue, action invocation, outcome emission, sweep scheduler, etc.). Tracked separately.
- Concrete `FabricRegistry` implementation in `ee/fabric/`. Tracked separately.
- CLI wiring of `validate_template_with_registry` into `template lint`. Follow-up.
- End-to-end browser-level smoke (create pocket from template → fire a bulk action → see approval queue entry). Requires EE wiring to land first.

## When the checklist passes

All §1 through §8 green = the runtime decision layer is feature-complete and ready for EE integration. The integration branch is ready to merge to `dev` via PR #1228 once §10 (manual testing checklist from wave-1 doc) is also re-run against the new HEAD.
