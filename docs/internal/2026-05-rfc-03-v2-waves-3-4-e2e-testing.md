# End-to-End Testing — RFC 03 v2 Waves 3 + 4 (Runtime + CLI + Fabric)

| Field         | Value                                                          |
|---------------|----------------------------------------------------------------|
| Status        | runnable                                                       |
| Date          | 2026-05-28                                                     |
| Branch        | `feat/rfc-03-v2-waves-3-4-integration` (pocketpaw)             |
| Tracking PR   | pocketpaw#1283 (integration → dev, DRAFT)                      |
| Companion     | `feat/rfc-03-v2-cel-integration` (paw-enterprise) · #305 DRAFT |
| Sub-PRs       | pocketpaw: #1275-1282 · paw-enterprise: #301, #302             |

## What this is

End-to-end runnable smoke for the **full chain** that Waves 3 + 4 land. The library layer's smoke (CEL eval, Instinct decisions, bulk planner, temporal sweeper, Fabric validator) is covered by the earlier wave-2 doc. This doc exercises the **integration** layer: actual approval rows persisting in Mongo, real action_executor calls firing real outcome events, the scheduler sweeping pockets, templates published as bundles + installed, the CLI lint warming up against a real registry. Paired with the paw-enterprise side: the CEL parser + Author UI live validation.

Pairs with two prior docs:
- `docs/internal/2026-05-rfc-03-v2-manual-testing.md` (wave-1, chokepoint + CLI + compile)
- `docs/internal/2026-05-rfc-03-v2-runtime-smoke-testing.md` (wave-2, pure library layer)

## Setup

```bash
# pocketpaw side
cd /Users/prakash-1/Documents/paw-workspace/pocketpaw/.claude/worktrees/rfc03-w34-integration
git log --oneline -5
# Expected: ea0c045a feat(fabric) ... f33c8e3b feat(cli) ... 2ac6802e feat(cli) ...
#           84825fa5 feat(ee) ... 98df8682 feat(ee)

# Full install (EE group needed for tests/cloud + tests/ee)
uv sync --dev --group ee

# paw-enterprise side (parallel)
cd /Users/prakash-1/Documents/paw-workspace/paw-enterprise/.claude/worktrees/wave4e-author-ui
git log --oneline -2
# Expected: 8a180ac feat(ui) ... 625d03b feat(cel) ...
bun install
bunx svelte-kit sync
```

## What landed in waves 3 + 4

| Wave | PR     | Module                                         | Function                                                              |
|------|--------|------------------------------------------------|-----------------------------------------------------------------------|
| 3a   | #1275  | `ee/cloud/instinct_approvals/`                 | Approval queue + Beanie doc + 4-file shape                            |
| 3a   | #1275  | `ee/cloud/pockets/instinct_dispatch.py`        | `gate_action()` — the single Instinct entry from runtime              |
| 3b   | #1276  | `ee/cloud/pockets/bulk_dispatch.py`            | Fan-out planner consumer; ONE approval per batch                       |
| 3c   | #1277  | `ee/cloud/pockets/outcomes_emitter.py`         | `emit_outcomes` on action SUCCESS only                                |
| 3d   | #1278  | `ee/cloud/_core/temporal_scheduler.py`         | Cron-driven sweeper (default 1h, env-configurable, disabled by default)|
| 3d   | #1278  | `ee/cloud/temporal_sweeps/` + `pockets/temporal_dispatcher.py` | Per-pocket sweep + state persistence              |
| 3e   | #1279  | `ee/cloud/pockets/service.py` `resolve_pocket_template` | `template_slug` on Pocket + compile-on-install merge        |
| 4a   | #1280  | `src/pocketpaw/bundled_templates/bundler.py`   | `pack_template` / `unpack_template` / `compute_template_diff`         |
| 4a   | #1280  | `src/pocketpaw/cli/template.py`                | `publish` / `install` / `upgrade` subcommands                         |
| 4b   | #1281  | `src/pocketpaw/bundled_templates/json_registry.py` | `JSONFileFabricRegistry` for lint-time mock                       |
| 4b   | #1281  | `src/pocketpaw/cli/template.py`                | `lint --registry <path>` extension                                    |
| 4c   | #1282  | `ee/pocketpaw_ee/fabric/storage.py` + `registry.py` | `WorkspaceFabricStore` + `WorkspaceFabricRegistry` (SQLite)        |
| 4d   | #301   | `paw-enterprise/src/lib/cel/parser.ts`         | `parseCelExpression` (`@marcbachmann/cel-js`)                         |
| 4e   | #302   | `paw-enterprise/src/lib/components/CelExpressionInput.svelte` | Live CEL validation component + `/cel-playground` route   |

## §1 — Sanity: full unit + targeted cloud suite

```bash
cd <pocketpaw worktree>
uv run pytest tests/unit/ -q
# Expected: ~310+ passed, no failures

uv run pytest tests/cloud/test_instinct_dispatch.py \
              tests/cloud/test_instinct_approvals_service.py \
              tests/cloud/test_bulk_dispatch.py \
              tests/cloud/test_outcomes_emitter.py \
              tests/cloud/test_temporal_dispatcher.py \
              tests/cloud/test_temporal_scheduler.py \
              tests/cloud/test_template_resolution.py \
              tests/ee/test_fabric_storage.py \
              tests/ee/test_fabric_registry.py -q
# Expected: ~145+ passed
```

Pass: every section green. Fail: any FAILED line is a real regression — investigate.

## §2 — Template authoring: lint + Fabric registry

### 2.1 Synthetic-tier template lints clean against NullFabricRegistry

```bash
uv run pocketpaw template lint \
  src/pocketpaw/bundled_templates/_bundled/todo-task-tracker/template.pocket.yaml
# Expected: [OK]   ... is valid (schema_version=v2, name=todo-task-tracker)
# exit 0
```

### 2.2 Registered-tier template (lease-renewal-v2) FAILS against NullFabricRegistry

```bash
uv run pocketpaw template lint tests/fixtures/templates/lease-renewal-v2.yaml
echo exit=$?
# Expected: [FAIL] with errors naming Lease / Tenant / Property / lease_tenant / lease_property
# exit 1 — this is the CORRECT signal that Fabric isn't wired
```

### 2.3 Same template passes with JSON-mock Fabric registry

```bash
cat > /tmp/lease-fabric.json <<'JSON'
{
  "entity_types": ["Lease", "Tenant", "Property"],
  "links": [
    {"from": "Lease", "to": "Tenant", "name": "lease_tenant"},
    {"from": "Lease", "to": "Property", "name": "lease_property"}
  ]
}
JSON
uv run pocketpaw template lint tests/fixtures/templates/lease-renewal-v2.yaml --registry /tmp/lease-fabric.json
echo exit=$?
# Expected: [OK] ... is valid
# exit 0
```

### 2.4 JSON output

```bash
uv run pocketpaw template lint tests/fixtures/templates/lease-renewal-v2.yaml \
  --registry /tmp/lease-fabric.json --json | python3 -m json.tool
# Expected: top-level "fabric_validations": [] (empty) when clean.
```

## §3 — Template lifecycle: publish → install → upgrade

### 3.1 Publish a bundled template (unsigned)

```bash
mkdir -p /tmp/rfc03-e2e
uv run pocketpaw template publish \
  src/pocketpaw/bundled_templates/_bundled/kanban-board/ \
  --output /tmp/rfc03-e2e/ --unsigned
ls -la /tmp/rfc03-e2e/kanban-board-1.0.0.template.tar.gz
# Expected: warn "unsigned" + [OK] writes /tmp/rfc03-e2e/.../kanban-board-1.0.0.template.tar.gz
```

### 3.2 Install the bundle

```bash
uv run pocketpaw template install /tmp/rfc03-e2e/kanban-board-1.0.0.template.tar.gz \
  --dest /tmp/rfc03-e2e/installed
ls /tmp/rfc03-e2e/installed/kanban-board/
# Expected: template.pocket.yaml, ripple_spec.json (if present), manifest.json, README.md
```

### 3.3 Tamper detection

```bash
# Corrupt one byte in the bundle and re-install
cp /tmp/rfc03-e2e/kanban-board-1.0.0.template.tar.gz /tmp/rfc03-e2e/tampered.tar.gz
printf 'X' | dd of=/tmp/rfc03-e2e/tampered.tar.gz bs=1 seek=200 count=1 conv=notrunc 2>/dev/null
uv run pocketpaw template install /tmp/rfc03-e2e/tampered.tar.gz --dest /tmp/rfc03-e2e/tampered_install
echo exit=$?
# Expected: [FAIL] with a rejection message — exit 1.
# Corrupting a byte at offset 200 typically lands inside the gzip frame,
# so unpack fails earlier with a zlib error rather than the inner-hash
# mismatch. Both are correct rejections (refuses to install a corrupt
# bundle); the exact error string depends on which byte you flip.
```

### 3.4 Sign + verify round-trip (Ed25519)

```bash
# Generate an Ed25519 keypair via python stdlib (or use the bundler's helper if one exists)
uv run python - <<'PY'
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
priv = Ed25519PrivateKey.generate()
seed = priv.private_bytes_raw()
pub = priv.public_key().public_bytes_raw()
open('/tmp/rfc03-e2e/ed25519.seed', 'wb').write(seed)
open('/tmp/rfc03-e2e/ed25519.pub', 'wb').write(pub)
print('keypair written to /tmp/rfc03-e2e/')
PY

uv run pocketpaw template publish \
  src/pocketpaw/bundled_templates/_bundled/kanban-board/ \
  --output /tmp/rfc03-e2e/signed/ \
  --key /tmp/rfc03-e2e/ed25519.seed
ls /tmp/rfc03-e2e/signed/
uv run pocketpaw template install /tmp/rfc03-e2e/signed/kanban-board-1.0.0.template.tar.gz \
  --dest /tmp/rfc03-e2e/signed_install \
  --verify-key /tmp/rfc03-e2e/ed25519.pub
echo exit=$?
# Expected: [OK] hash + signature verified, install succeeds, exit 0
```

### 3.5 Upgrade with destructive-diff prompt

The bundled `kanban-board` template ships with no `outcomes` or `actions`, so
dropping a section there only exercises the non-destructive path. To trigger
the destructive prompt, start from the lease-renewal v2 fixture (which has
real outcomes + actions + instinct rules) and drop one.

```bash
# Stage the fixture as an installable directory + a tweaked v1.1.0 copy
mkdir -p /tmp/rfc03-e2e/lease-src /tmp/rfc03-e2e/lease-v2-src
cp tests/fixtures/templates/lease-renewal-v2.yaml /tmp/rfc03-e2e/lease-src/template.pocket.yaml
cp tests/fixtures/templates/lease-renewal-v2.yaml /tmp/rfc03-e2e/lease-v2-src/template.pocket.yaml

# Publish v1.0.0, install it, then prepare a destructive v1.1.0 (drops one outcome).
uv run pocketpaw template publish /tmp/rfc03-e2e/lease-src/ \
  --output /tmp/rfc03-e2e/lease-bundle/ --unsigned
uv run pocketpaw template install \
  /tmp/rfc03-e2e/lease-bundle/lease-renewal-v1-1.0.0.template.tar.gz \
  --dest /tmp/rfc03-e2e/lease-installed/

python3 -c "
import yaml
p = '/tmp/rfc03-e2e/lease-v2-src/template.pocket.yaml'
data = yaml.safe_load(open(p).read())
data['version'] = '1.1.0'
# Destructive: drop one outcome from the catalog.
data['outcomes'] = data.get('outcomes', [])[:-1]
open(p, 'w').write(yaml.safe_dump(data, sort_keys=False))
"
uv run pocketpaw template publish /tmp/rfc03-e2e/lease-v2-src/ \
  --output /tmp/rfc03-e2e/lease-v2-bundle/ --unsigned

# --no-prompt + destructive → exit 2.
uv run pocketpaw template upgrade \
  /tmp/rfc03-e2e/lease-v2-bundle/lease-renewal-v1-1.1.0.template.tar.gz \
  --no-prompt 2>&1 || echo "exit=$?"
# Expected: structured diff lists the removed outcome with destructive=true,
# the upgrade refuses, exit=2.
```

## §4 — Pocket creation with `template_slug`

This requires a running dashboard / API. Boot it first:

```bash
uv run pocketpaw &  # web dashboard mode, default port 8888
DASH_PID=$!
sleep 4  # let it boot

# Create a pocket from the kanban-board template via API (workspace + user IDs are placeholders;
# replace with real ones from your local instance)
WORKSPACE_ID="<your-workspace-id>"
USER_TOKEN="<your-bearer-token>"
curl -s -X POST http://localhost:8888/api/v1/pockets \
  -H "Authorization: Bearer $USER_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"name\": \"E2E Kanban\", \"workspace_id\": \"$WORKSPACE_ID\", \"template_slug\": \"kanban-board\"}" \
  | python3 -m json.tool
# Expected JSON: template_slug=="kanban-board" + rippleSpec populated by compile-on-install

# Cleanup
kill $DASH_PID
```

Pass criteria: response includes `templateSlug: "kanban-board"` AND `rippleSpec.state.entity_type == "Card"` (from the compile pass).

## §5 — Instinct gate paths (Block / Approval / Execute)

Use the lease-renewal-v2 fixture via the test harness (faster + deterministic than API calls).

```bash
uv run python - <<'PY'
import asyncio
import yaml
from datetime import datetime, timezone
from pocketpaw.bundled_templates.schema import PocketTemplate

fixture = yaml.safe_load(open("tests/fixtures/templates/lease-renewal-v2.yaml").read())
template = PocketTemplate.model_validate(fixture)

now = datetime(2026, 5, 28, tzinfo=timezone.utc)

# Block path: 5% rent cut trips the block rule
row_block = {"rent_proposed": 1800.0, "rent_current": 2000.0, "renewal_stage": "sent",
             "days_remaining": 25, "tenant": {"late_payment_count_12mo": 0},
             "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc),
             "rent_proposed_delta_pct": 2.5}
# Approval path: 4 late payments trips the operator rule
row_approval = {**row_block, "rent_proposed": 2050.0,
                "tenant": {"late_payment_count_12mo": 4}}
# Execute path: clean row
row_exec = {**row_block, "rent_proposed": 2050.0,
            "tenant": {"late_payment_count_12mo": 1}}

from pocketpaw.bundled_templates import resolve_instinct
for label, row in [("BLOCK", row_block), ("APPROVAL", row_approval), ("EXECUTE", row_exec)]:
    d = resolve_instinct(template, "send_to_tenant", row, now=now)
    print(f"  {label}: send_to_tenant → {d.verdict} ({d.reason})")
PY
```

Expected:
```
  BLOCK: send_to_tenant → BLOCK (blocked_by_rule)
  APPROVAL: send_to_tenant → ESCALATE_APPROVAL (operator_overlay_escalated)
  EXECUTE: send_to_tenant → ESCALATE_APPROVAL (author_floor)
```

(Note: `send_to_tenant` is `require_approval`, so the execute-row hits the author floor — that's correct.)

For a true EXECUTE row, run against an `auto`-policy action:

```bash
uv run python - <<'PY'
import yaml
from datetime import datetime, timezone
from pocketpaw.bundled_templates.schema import PocketTemplate
from pocketpaw.bundled_templates import resolve_instinct

template = PocketTemplate.model_validate(yaml.safe_load(open("tests/fixtures/templates/lease-renewal-v2.yaml").read()))
now = datetime(2026, 5, 28, tzinfo=timezone.utc)

row = {"rent_proposed": 2050.0, "rent_current": 2000.0, "renewal_stage": None,
       "days_remaining": 60, "tenant": {"late_payment_count_12mo": 1},
       "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc),
       "rent_proposed_delta_pct": 2.5}
d = resolve_instinct(template, "draft_renewal", row, now=now)
print(f"  draft_renewal (auto policy, clean row) → {d.verdict} ({d.reason})")
PY
# Expected: EXECUTE (auto)
```

## §6 — Bulk fan-out: ONE approval blesses the whole batch

```bash
uv run python - <<'PY'
import yaml
from datetime import datetime, timezone
from pocketpaw.bundled_templates.schema import PocketTemplate
from pocketpaw.bundled_templates import plan_bulk_execution

template = PocketTemplate.model_validate(yaml.safe_load(open("tests/fixtures/templates/lease-renewal-v2.yaml").read()))
now = datetime(2026, 5, 28, tzinfo=timezone.utc)

# 3 rows: 2 needing approval (bulk_draft policy floor), 1 blocked (5% rent cut)
rows = [
    {"id": "lease-1", "rent_proposed": 2050.0, "rent_current": 2000.0, "renewal_stage": None,
     "days_remaining": 60, "tenant": {"late_payment_count_12mo": 1},
     "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc), "rent_proposed_delta_pct": 2.5},
    {"id": "lease-2", "rent_proposed": 1900.0, "rent_current": 2000.0, "renewal_stage": None,
     "days_remaining": 45, "tenant": {"late_payment_count_12mo": 0},
     "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc), "rent_proposed_delta_pct": 5.0},
    {"id": "lease-3", "rent_proposed": 1700.0, "rent_current": 2000.0, "renewal_stage": None,
     "days_remaining": 30, "tenant": {"late_payment_count_12mo": 0},
     "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc), "rent_proposed_delta_pct": 15.0},
]

plan = plan_bulk_execution(template, "bulk_draft", rows, now=now)
print(f"  total_rows: {plan.total_rows}")
print(f"  executions: {len(plan.executions)}")
print(f"  blocked: {len(plan.blocked)} (lease-3 trips block rule)")
print(f"  approval: {'ONE batch for ' + str(plan.approval_request.row_ids) if plan.approval_request else 'None'}")
PY
```

Expected: `total_rows=3, executions=0, blocked=1, approval=ONE batch for ['lease-1', 'lease-2']`.

## §7 — Outcome events fire only on SUCCESS

This needs a full EE harness. Run the targeted unit test that exercises the path:

```bash
uv run pytest tests/cloud/test_outcomes_emitter.py -v --tb=short
```

Expected: all 13 tests pass. The interesting ones are:
- `test_action_with_outcomes_emitted_fires_events`
- `test_action_with_empty_outcomes_emitted_fires_none`
- `test_http_failure_path_emits_zero_events`
- `test_approval_pending_path_emits_zero_events`
- `test_bulk_dispatch_emits_per_row`

## §8 — Temporal sweep scheduler

The scheduler is **disabled by default** (intentional for pytest + multi-replica safety). Enable + trigger one tick:

```bash
POCKETPAW_TEMPORAL_SWEEP_ENABLED=true \
POCKETPAW_TEMPORAL_SWEEP_INTERVAL_SECONDS=60 \
uv run pytest tests/cloud/test_temporal_scheduler.py -v --tb=short
```

Expected: 15 tests green. Notable:
- `test_scheduler_task_starts_on_cloud_startup`
- `test_tick_frequency_honors_env_var`
- `test_graceful_cancel_on_shutdown`
- `test_idempotent_on_per_tick_reentry`

The dispatcher (per-pocket sweep) has its own tests:

```bash
uv run pytest tests/cloud/test_temporal_dispatcher.py -v --tb=short
```

Expected: 13 tests green. The rising-edge / continuing-true / falling-edge / multi-tenant cases all here.

## §9 — paw-enterprise side: CEL parser + live UI validation

Switch to the paw-enterprise worktree:

```bash
cd /Users/prakash-1/Documents/paw-workspace/paw-enterprise/.claude/worktrees/wave4e-author-ui
```

### 9.1 Parser tests

```bash
bunx vitest run src/lib/cel/parser.test.ts
# Expected: 15/15 passed
```

### 9.2 Author UI component tests

```bash
bunx vitest run src/lib/components/CelExpressionInput.test.ts
# Expected: 9/9 passed
```

### 9.3 Live UI smoke (manual, requires browser)

```bash
# Boot the SvelteKit dev server on a non-default port (avoid Tauri collision on 1420)
bunx vite dev --port 1423 &
DEV_PID=$!
sleep 3
open http://localhost:1423/cel-playground
# Manual: paste expressions into the input. Verify:
#   - "days_remaining <= 30"           → green border, no error
#   - "1 ++ 2"                         → red border, "Unexpected token: PLUS" error
#   - ""                               → soft warning ("CEL expression must not be empty")
#   - "tenant.late_payment_count_12mo >= 3"  → green
kill $DEV_PID
```

## §10 — End-to-end chain (the full pipeline)

Demonstrates: template lint → compile → install → action firing → Instinct decision → outcome emit, all via library calls.

```bash
uv run python - <<'PY'
import yaml, tempfile, shutil
from datetime import datetime, timezone
from pathlib import Path
from pocketpaw.bundled_templates import (
    PocketTemplate, compile_template, plan_bulk_execution, resolve_instinct,
    validate_template_with_registry, NullFabricRegistry, JSONFileFabricRegistry,
    pack_template, unpack_template,
)
# Note: pack_template / unpack_template were re-exported from the package
# __init__ in the smoke-finding follow-up; they originally lived only at
# pocketpaw.bundled_templates.bundler.

# 1. Load + validate
template = PocketTemplate.model_validate(yaml.safe_load(open("tests/fixtures/templates/lease-renewal-v2.yaml").read()))
print(f"  ✓ Template loaded: {template.name} v{template.version}")

# 2. Lint with registered-tier Fabric (use a JSON mock since no live EE Fabric)
import json
registry_file = Path(tempfile.mkstemp(suffix=".json")[1])
registry_file.write_text(json.dumps({
    "entity_types": ["Lease", "Tenant", "Property"],
    "links": [
        {"from": "Lease", "to": "Tenant", "name": "lease_tenant"},
        {"from": "Lease", "to": "Property", "name": "lease_property"},
    ],
}))
registry = JSONFileFabricRegistry(registry_file)
errors = validate_template_with_registry(template, registry)
print(f"  ✓ Fabric validation: {len(errors)} errors (expected 0)")

# 3. Compile to runtime
runtime = compile_template(template)
print(f"  ✓ Compiled: sources={list(runtime['sources'].keys())}")

# 4. Resolve Instinct for one row
row = {"rent_proposed": 2050.0, "rent_current": 2000.0, "renewal_stage": None,
       "days_remaining": 60, "tenant": {"late_payment_count_12mo": 1},
       "expires_at": datetime(2026, 12, 1, tzinfo=timezone.utc),
       "rent_proposed_delta_pct": 2.5}
d = resolve_instinct(template, "draft_renewal", row,
                     now=datetime(2026, 5, 28, tzinfo=timezone.utc))
print(f"  ✓ Instinct decision: draft_renewal → {d.verdict}")

# 5. Bulk plan for 3 rows
plan = plan_bulk_execution(template, "bulk_draft", [row, {**row}, {**row}],
                            now=datetime(2026, 5, 28, tzinfo=timezone.utc))
print(f"  ✓ Bulk plan: {plan.total_rows} rows, {len(plan.executions)} executions, "
      f"{len(plan.blocked)} blocked, approval={'present' if plan.approval_request else 'None'}")

# 6. Pack the template into a bundle + unpack to verify round-trip
src = Path("src/pocketpaw/bundled_templates/_bundled/kanban-board")
with tempfile.TemporaryDirectory() as td:
    bundle_path = pack_template(src, output_path=Path(td))
    print(f"  ✓ Published bundle: {bundle_path.name}")
    result = unpack_template(bundle_path, Path(td) / "installed")
    print(f"  ✓ Unpacked: slug={result.slug} hash_ok={result.hash_verified}")

registry_file.unlink()
print("End-to-end chain OK")
PY
```

Pass criteria: 6 `✓` lines + final `End-to-end chain OK`. Each `✓` corresponds to one stage of the pipeline.

## What's NOT covered

- **Live HTTP action invocation** against a real third-party (Yardi / Gmail / etc.) — the smoke uses the lease-renewal fixture which has no live backend.
- **Browser-level smoke** of the full pocket-creation → action-fire flow. §4 boots the dashboard but doesn't drive the UI end-to-end. That needs Playwright + a real workspace.
- **Distributed scheduler / HA temporal-sweep coordination** — v0 is single-node.
- **Outcomes meter consumer / billing aggregation** — Wave 3c emits events; the meter is a downstream consumer not in scope here.

## When this checklist passes

§1 through §10 all green = RFC 03 v2 Waves 3 + 4 are integration-ready. The two tracking PRs (pocketpaw#1283 + qbtrix/paw-enterprise#305) are ready for the final captain merge into `dev`.
