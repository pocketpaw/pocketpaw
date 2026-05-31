# Manual Testing — RFC 03 v2 Pocket Template Schema (Integration)

| Field      | Value                                                   |
|------------|---------------------------------------------------------|
| Status     | in progress                                             |
| Date       | 2026-05-25                                              |
| Branch     | `feat/rfc-03-v2-integration`                            |
| Tracking PR | [#1228 (integration → dev, DRAFT)](https://github.com/pocketpaw/pocketpaw/pull/1228) |
| Sub-PRs    | #1225 (schema chokepoint), #1229 (CLI), #1239 (compile) |

## What this document is

Manual test checklist for the RFC 03 v2 work before `feat/rfc-03-v2-integration` merges into `dev`. CI already covers unit tests (pytest, ruff, OSS-EE boundary, wheels build, import-linter contracts). This document covers things CI can't easily express: end-to-end CLI behaviour, on-disk side effects, the v1 promotion path on real authored inputs, cross-boundary compatibility, and observable failure modes.

## Setup

```bash
# Workspace root
cd /Users/prakash-1/Documents/paw-workspace/pocketpaw

# Check out the integration branch (uses the local worktree)
cd .claude/worktrees/rfc03-integration

# Verify HEAD
git log --oneline -6
# Expected: ae735b4c feat(compile) + 60641bd8 feat(cli) + faeef077 style +
#           8d668ecf chore(templates) + 8ccb2e51 feat(schema) +
#           8faa7e4b chore(ci)

# Install dev dependencies (OSS core + the EE group is needed for the
# cross-boundary compile round-trip test below; pure OSS skips that one)
uv sync --dev --group ee
```

For the "OSS-only" smoke test category, swap to a separate clone or use `uv sync --dev` (without `--group ee`). The boundary check confirms pocketpaw_ee never enters the import graph in OSS-only mode.

## What landed

Three sub-PRs merged into integration:

1. **#1225 — Pydantic chokepoint + bundled v1→v2 migration.** `PocketTemplate` Pydantic v2 model, `_promote_v1_to_v2` translation in the loader, `CelExpression` parse-only validation via celpy, `TemplateValidationError`, `strict: bool = False` kwarg on `load_template`, and six bundled YAMLs migrated to v2 in the same PR.
2. **#1229 — CLI lint / migrate / diff.** `pocketpaw template lint <file>`, `pocketpaw template migrate <file>`, `pocketpaw template diff <a> <b>`. All three accept `--json`. Migrate accepts `--yes` and `--no-backup`. argparse `parse_known_args` switch in `__main__.py` for interspersed positional handling.
3. **#1239 — compile module + CLI subcommand.** `pocketpaw.bundled_templates.compile.compile_template(template) -> dict`. `data_sources[]` translation only in this PR; other top-level fields pass through verbatim for downstream PRs. `pocketpaw template compile <file>` CLI subcommand with `--yaml` flag.

## Test categories

Run from inside the integration worktree unless noted otherwise.

### 1 · Bundled templates load via `strict=True`

Smoke test for the chokepoint. All six bundled templates should validate against the v2 Pydantic model after the auto-applied v1→v2 promotion.

```bash
uv run python - <<'PY'
import tempfile
from pathlib import Path
from pocketpaw.bundled_templates.installer import install_bundled_templates
from pocketpaw.bundled_templates.loader import load_template

with tempfile.TemporaryDirectory() as td:
    install_bundled_templates(destination_root=Path(td))
    for slug in [
        "todo-task-tracker", "kanban-board", "metrics-dashboard",
        "crm-record-list", "calendar-planner", "activity-feed",
    ]:
        meta = load_template(slug, templates_dir=Path(td), strict=True)
        assert meta is not None
        assert meta["schema_version"] == "2", slug
        assert "skill_refs" in meta, slug
        print(f"  {slug:25s} ✓ v{meta['schema_version']}  pattern={meta['pattern']}  name={meta['display_name']!r}")
PY
```

Pass criteria: six lines, all `✓`. `display_name` for `crm-record-list` must be `'CRM Record List'` (the acronym preserved fix). `display_name` for `todo-task-tracker` must be `'Task Tracker'` (matches gallery title).

Fail criteria: any `TemplateValidationError` raised, any line missing, or "Crm Record List" / "Todo Task Tracker" appearing in the output.

### 2 · CLI · `template lint`

#### 2.1 · Lint a valid v2 fixture

```bash
uv run pocketpaw template lint tests/fixtures/templates/lease-renewal-v2.yaml
```

Expected: `[OK]   tests/fixtures/templates/lease-renewal-v2.yaml is valid (schema_version=v2, name=lease-renewal-v1)`. Exit code 0.

#### 2.2 · Lint a bundled (v2-migrated) template

```bash
uv run pocketpaw template lint src/pocketpaw/bundled_templates/_bundled/todo-task-tracker/template.pocket.yaml
```

Expected: `[OK]   .../todo-task-tracker/template.pocket.yaml is valid (schema_version=v2, name=todo-task-tracker)`. Exit 0.

#### 2.3 · Lint a v1 YAML (auto-promote)

```bash
cat > /tmp/v1_template.yaml <<'YAML'
name: v1-test-pocket
version: 1.0.0
vertical: productivity
shape: data-grid
description: A v1-shaped template for testing the auto-promote path.
state:
  entity_type: Task
  columns:
    - { field: title, widget: text }
actions: []
connectors: []
skills: []
YAML

uv run pocketpaw template lint /tmp/v1_template.yaml
```

Expected: lint succeeds (exit 0). The output notes the input is v1 and a v1→v2 promotion would apply on read. The rewrite is NOT applied to the file on disk (lint is non-destructive). Verify with `grep schema_version /tmp/v1_template.yaml` — should return nothing.

#### 2.4 · Lint a bad template (missing required field)

```bash
cat > /tmp/bad_template.yaml <<'YAML'
schema_version: "2"
name: bad-test
version: 1.0.0
pattern: app
vertical: productivity
shape: data-grid
description: Missing the required `state` block.
YAML

uv run pocketpaw template lint /tmp/bad_template.yaml
echo "exit=$?"
```

Expected: `[FAIL]` output naming the missing `state` field. Exit code 1.

#### 2.5 · Lint `--json` output

```bash
uv run pocketpaw template lint tests/fixtures/templates/lease-renewal-v2.yaml --json | python3 -m json.tool
```

Expected: single JSON object with `{file, valid, errors, warnings, schema_version, promoted_from_v1}`. `valid` is `true`. `errors` is `[]`. Exit 0.

#### 2.6 · Lint a bad CEL expression

```bash
cat > /tmp/bad_cel.yaml <<'YAML'
schema_version: "2"
name: bad-cel-test
version: 1.0.0
pattern: app
vertical: productivity
shape: data-grid
description: An invalid CEL expression on a saved_view filter.
state:
  entity_type: Task
  columns:
    - { field: title, widget: text }
  saved_views:
    - { name: "Bad filter", filter: "this is not valid CEL ((" }
YAML

uv run pocketpaw template lint /tmp/bad_cel.yaml
echo "exit=$?"
```

Expected: `[FAIL]` with a CEL parse error message on the `saved_views[0].filter` field. Exit code 1.

### 3 · CLI · `template migrate`

#### 3.1 · Migrate a v1 file to v2 in place with `--yes`

```bash
# Reset the v1 fixture from 2.3 (so the test is repeatable)
cat > /tmp/v1_template.yaml <<'YAML'
name: v1-test-pocket
version: 1.0.0
vertical: productivity
shape: data-grid
description: A v1-shaped template for testing the auto-promote path.
state:
  entity_type: Task
  columns:
    - { field: title, widget: text }
actions: []
connectors: []
skills: []
YAML

uv run pocketpaw template migrate --yes /tmp/v1_template.yaml

# Verify the file is now v2 on disk
head -5 /tmp/v1_template.yaml
ls -la /tmp/v1_template.yaml.v1.bak
```

Expected:
- CLI prints `[OK]   migrated /tmp/v1_template.yaml from v1 to v2` and the backup path.
- `head -5` shows `schema_version: "2"` and `display_name` (auto-derived title-case from name) and `pattern: app` (loader fallback default).
- The backup file exists at `/tmp/v1_template.yaml.v1.bak` and still contains the v1 shape (no `schema_version`).

#### 3.2 · Migrate is idempotent on v2 input

```bash
uv run pocketpaw template migrate --yes /tmp/v1_template.yaml
```

Expected: `already v2, no changes`. Exit 0. No new backup created (the old `.v1.bak` from 3.1 is left as-is).

#### 3.3 · Confirmation prompt aborts on empty / no input

```bash
# Restore v1 from backup
cp /tmp/v1_template.yaml.v1.bak /tmp/v1_template_prompt.yaml

# Decline the prompt
echo "" | uv run pocketpaw template migrate /tmp/v1_template_prompt.yaml

# Verify file is unchanged
grep schema_version /tmp/v1_template_prompt.yaml && echo "FAIL: file was migrated" || echo "PASS: file untouched"
```

Expected: prompt is shown, empty answer aborts, file is unchanged on disk, exit 0.

#### 3.4 · `--no-backup` skips backup creation

```bash
cp /tmp/v1_template.yaml.v1.bak /tmp/v1_no_backup.yaml
uv run pocketpaw template migrate --yes --no-backup /tmp/v1_no_backup.yaml
ls /tmp/v1_no_backup.yaml.v1.bak 2>&1
```

Expected: file migrated to v2. `ls` on the backup returns `No such file or directory` (no backup created).

### 4 · CLI · `template diff`

#### 4.1 · Diff between two bundled v2 templates

```bash
uv run pocketpaw template diff \
  src/pocketpaw/bundled_templates/_bundled/todo-task-tracker/template.pocket.yaml \
  src/pocketpaw/bundled_templates/_bundled/kanban-board/template.pocket.yaml
```

Expected: output groups changes by top-level field. At minimum, `~ name` (todo-task-tracker → kanban-board), `~ shape` (data-grid → kanban), `~ display_name` ("Task Tracker" → "Kanban Board"), `~ state.entity_type` (Task → Card), and at least one column-level diff. Exit 0.

#### 4.2 · Diff against identical file is empty

```bash
uv run pocketpaw template diff \
  tests/fixtures/templates/lease-renewal-v2.yaml \
  tests/fixtures/templates/lease-renewal-v2.yaml
```

Expected: no diff entries (or an explicit "no differences" line). Exit 0.

#### 4.3 · Diff `--json` output

```bash
uv run pocketpaw template diff \
  src/pocketpaw/bundled_templates/_bundled/todo-task-tracker/template.pocket.yaml \
  src/pocketpaw/bundled_templates/_bundled/kanban-board/template.pocket.yaml \
  --json | python3 -m json.tool | head -30
```

Expected: valid JSON. Each entry has `kind` (`add` / `remove` / `change`), `path`, and `value` / `old` + `new` as appropriate.

### 5 · CLI · `template compile`

#### 5.1 · Compile the lease-renewal v2 fixture

```bash
uv run pocketpaw template compile tests/fixtures/templates/lease-renewal-v2.yaml
```

Expected JSON output. Key invariants on the `sources` block:

- `sources.expiring_leases.method` is `"GET"`.
- `sources.expiring_leases.refresh` is `["pocket_open", "manual", "interval"]`. The `interval:1h` form has been split.
- `sources.expiring_leases.refresh_interval_seconds` is `3600` (the parsed 1h).
- `sources.tenant_responses.refresh` is `["pocket_open"]`. The `signal:gmail.inbox.update` from the YAML is DROPPED (silently — runtime has no signal trigger yet).
- `sources.tenant_responses.refresh_interval_seconds` is absent (no interval declared).
- `name` is the dict key for each source; it does NOT appear inside the source's value.

The output also passes through every other top-level field unchanged (`actions`, `agents`, `triggers`, `permissions`, `instinct_rules`, `connectors`, `outcomes`, `state`).

#### 5.2 · Compile to YAML

```bash
uv run pocketpaw template compile tests/fixtures/templates/lease-renewal-v2.yaml --yaml | head -40
```

Expected: same data as 5.1, in YAML form. Both renderings must be round-trippable.

#### 5.3 · Compile a bundled template with no data_sources

```bash
uv run pocketpaw template compile src/pocketpaw/bundled_templates/_bundled/todo-task-tracker/template.pocket.yaml | python3 -c "import json,sys; d=json.load(sys.stdin); print('sources:', d['sources']); print('actions:', d['actions'])"
```

Expected: `sources: {}` (empty dict, the bundled template declares no data_sources). `actions: []` is the passthrough from the bundled template's literal `actions: []`. Exit 0.

#### 5.4 · Compile a v1 input (auto-promote then compile)

```bash
uv run pocketpaw template compile /tmp/v1_template.yaml.v1.bak | python3 -c "import json,sys; d=json.load(sys.stdin); print('schema_version:', d.get('schema_version')); print('skill_refs:', d.get('skill_refs')); print('display_name:', d.get('display_name'))"
```

Expected: the file is v1 on disk, but the compile output reflects v2 shape (`schema_version: "2"`, `skill_refs: []`, `display_name: 'V1 Test Pocket'`). The promotion happens inside compile.

### 6 · Cross-boundary compatibility (EE → SourceBinding round-trip)

Requires `uv sync --dev --group ee`. This test confirms the compile output is shape-compatible with the EE runtime's `SourceBinding` Pydantic model, without violating the OSS-EE import boundary (the production code in OSS never imports from EE; this script does, scoped to the test only).

```bash
uv run python - <<'PY'
import yaml
from pathlib import Path
from pocketpaw.bundled_templates.schema import PocketTemplate
from pocketpaw.bundled_templates.compile import compile_template

# The cross-boundary import — only legal in test / verification code.
from pocketpaw_ee.cloud.pockets.source_executor import SourceBinding

fixture = Path("tests/fixtures/templates/lease-renewal-v2.yaml")
template = PocketTemplate.model_validate(yaml.safe_load(fixture.read_text()))
compiled = compile_template(template)

for name, source in compiled["sources"].items():
    SourceBinding.model_validate(source)
    print(f"  {name:25s} ✓ SourceBinding-compatible")
PY
```

Expected: every entry in `compiled["sources"]` validates cleanly against `SourceBinding`. Two `✓` lines (one per source in the lease-renewal fixture). Any `ValidationError` is a regression.

### 7 · Back-compat — `strict=False` returns None on bad input

The default `load_template` path swallows errors and returns `None` + logs a warning. Existing EE callers depend on this.

```bash
uv run python - <<'PY'
import tempfile
from pathlib import Path
from pocketpaw.bundled_templates.loader import load_template

with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    bad_dir = root / "bad-template"
    bad_dir.mkdir()
    (bad_dir / "template.pocket.yaml").write_text("schema_version: '2'\nname: bad\n")  # missing required fields
    (bad_dir / "ripple_spec.json").write_text("{}")

    # Default strict=False: returns None, logs WARNING, does NOT raise
    result = load_template("bad-template", templates_dir=root)
    assert result is None, f"expected None, got {result!r}"
    print("  ✓ strict=False returned None on bad template (back-compat preserved)")

    # strict=True: raises TemplateValidationError
    try:
        load_template("bad-template", templates_dir=root, strict=True)
    except Exception as e:
        print(f"  ✓ strict=True raised: {type(e).__name__}")
PY
```

Expected: two `✓` lines. If the first line raises, EE callers will break in production. If the second line returns `None` without raising, the CLI lint path is silently swallowing errors that should be loud.

### 8 · `_EARLY_COMMANDS` boot-cost check

`template` is in `_EARLY_COMMANDS` (see `src/pocketpaw/__main__.py`). The CLI command must not pay the dashboard / agent / settings boot cost. Verify by timing:

```bash
time uv run pocketpaw template lint tests/fixtures/templates/lease-renewal-v2.yaml >/dev/null
```

Expected: total wall-clock under ~2 seconds (Python startup + uv overhead). If it exceeds ~10 seconds, the lazy-import discipline has been broken — investigate which module is being pulled into the early-command path.

### 9 · OSS-only boundary

Open a fresh shell. Do `uv sync --dev` (without `--group ee`). The OSS install should not pull pocketpaw_ee into the import graph.

```bash
# In a separate shell with OSS-only deps:
uv run python -c "
import pocketpaw.bundled_templates.compile  # OSS module
import sys
assert not any(n.startswith('pocketpaw_ee') for n in sys.modules), 'OSS-EE boundary violation'
print('OSS-only boundary clean')
"
```

Expected: `OSS-only boundary clean`. Any `pocketpaw_ee` module loaded into `sys.modules` is a regression. The import-linter contract in CI already covers this statically, but running it dynamically catches anything the static analysis misses.

In the same shell, the cross-boundary test from §6 should `pytest.importorskip` away cleanly (try `uv run pytest tests/unit/test_template_compile.py::test_compiled_source_validates_against_runtime_source_binding -xvs` — expected: skipped, not failed).

### 10 · The 6 bundled templates render in the create specialist

This one needs the dashboard running. Boots the dashboard, opens it in the browser, and instantiates each of the 6 bundled templates through the chat agent's STEP 0 template-library matcher.

```bash
uv run pocketpaw --dev
```

Then in a separate terminal or in the chat UI, ask the create specialist for each of the six pattern matches:

- "make me a todo list" → matches `todo-task-tracker`
- "kanban board for my sprint" → matches `kanban-board`
- "metrics dashboard" → matches `metrics-dashboard`
- "crm record list" → matches `crm-record-list`
- "calendar planner" → matches `calendar-planner`
- "activity feed" → matches `activity-feed`

Each should instantiate cleanly without a Pydantic validation error in the logs (`uv run pocketpaw logs --follow` from a third terminal to tail).

## Failure modes to watch for during testing

| Symptom | Likely cause |
|---|---|
| `TemplateValidationError` raised at boot | A bundled template regressed; check `_bundled/<slug>/template.pocket.yaml` for fields outside the v2 schema or a typo'd value. |
| `[FAIL]` on `template lint` of an unmodified bundled template | Same — the field-set in `tests/unit/test_bundled_templates.py` (`_RFC03_ALLOWED_FIELDS` / `_RFC03_SHAPE_ENUM`) drifted from the Pydantic schema. |
| `signal:*` refresh entry surviving compile | The `_UNSUPPORTED_REFRESH_PREFIXES` frozenset in `compile.py` was bypassed — check the `_compile_refresh` logic. |
| `pocketpaw_ee.*` shows in `sys.modules` after OSS-only import | An OSS module statically imports an EE module — find via `grep -r "pocketpaw_ee" src/pocketpaw/`. |
| `load_template(slug)` raises (default kwargs) | The `strict=False` back-compat was broken — every EE caller that pattern-matches on `None` will break. |
| `pocketpaw template status --yes` parses without error | This is expected (the `--yes` / `--no-backup` flags are top-level due to the existing positional+subargs dispatch pattern). Cosmetically ugly but harmless. |

## Out of scope for this round of testing

Things this PR does not ship. Do not test for these in this round:

- `pocketpaw template publish / install / upgrade` (Bucket C, Registry work)
- paw-enterprise `cel-js` evaluator (different repo)
- Fabric `tier:registered` / `via_link` registry enforcement (PR 2g, needs Fabric integration)
- Runtime composition of `actions[]`, `triggers[]`, `permissions`, `instinct_rules` (compile passes these through; runtime translation lands in PR 2c–2g)
- The `signal:*` refresh trigger (RFC declares it, runtime does not implement it yet — compile drops it silently)
- Wiki pocket and C4 widget pocket migrations to templates (RFC §"Migration path", separate work)

## When this checklist passes

Once every section from 1 through 9 is green (section 10 is optional smoke), the integration branch is ready for the final review and merge to dev via PR #1228.
