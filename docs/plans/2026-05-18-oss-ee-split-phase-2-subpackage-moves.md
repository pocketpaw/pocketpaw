# OSS/EE Split — Phase 2: Move Subpackages from `pocketpaw_ee/` to `pocketpaw/` Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Move 8 subpackages that are not multi-tenant features out of `pocketpaw_ee/` and into `pocketpaw/` (the OSS core). After this phase, `pocketpaw_ee/` contains only `cloud/`, `agent/pocket_specialist/`, `audit/`, and `fleet/`.

**Architecture:** One subpackage at a time. For each, audit its imports for hard cloud/composio dependencies. If clean, `git mv` and codemod. If not, decide: lift the cloud dependency out, leave the package in `pocketpaw_ee/`, or split.

**Tech Stack:** Same as Phase 1 — Python 3.11+, `uv`, `hatch`, `pytest`, `ruff`, `mypy`, `import-linter`.

**Reference:** Design doc at `docs/plans/2026-05-16-oss-ee-split-design.md` Section 1. Depends on Phase 1 (`chore/oss-ee-phase-1-rename`) being merged.

---

## Subpackages to move

| Subpackage | Destination | Audit risk |
|---|---|---|
| `pocketpaw_ee.automations` | `pocketpaw.automations` | **Existing** `pocketpaw.ee.automations` at `src/pocketpaw/ee/automations/` — reconcile during move |
| `pocketpaw_ee.fabric` | `pocketpaw.fabric` | Imports `pocketpaw_ee.api` (`get_fabric_store`) — likely cloud-bound store factory |
| `pocketpaw_ee.instinct` | `pocketpaw.instinct` | Imports `pocketpaw_ee.api` (`get_instinct_store`) — same risk |
| `pocketpaw_ee.retrieval` | `pocketpaw.retrieval` | Verify no `pocketpaw_ee.cloud.*` deps |
| `pocketpaw_ee.paw_print` | `pocketpaw.paw_print` | Likely depends on Soul protocol only — should be clean |
| `pocketpaw_ee.ripple` | `pocketpaw.ripple` | Imported by core today (`api/v1/pockets.py`, `agents/sdk_mcp_pocket.py`) — easy move |
| `pocketpaw_ee.widget` | `pocketpaw.widget` | Verify no cloud deps |
| `pocketpaw_ee.guards` (if it lives under `pocketpaw_ee`) | `pocketpaw.guards` | **Note:** `src/pocketpaw/ee/guards/` already exists — confirm origin before moving anything from `pocketpaw_ee/` |

**Pre-existing conflicts in core to resolve first:**
- `src/pocketpaw/ee/automations/` — already in core under a confusing path. Decide whether to merge with the incoming `pocketpaw_ee.automations` or replace.
- `src/pocketpaw/ee/guards/` — same pattern. Path implies it was started as an OSS-side facade for the enterprise `guards` module.

---

## Pre-flight

```bash
git checkout main
git pull
git checkout -b chore/oss-ee-phase-2-subpackage-moves
uv sync --dev
uv run pytest --ignore=tests/e2e -q
```
Record baseline test count.

---

## Task 1: Resolve `pocketpaw.ee.*` shim subpackages already in core

The two existing folders `src/pocketpaw/ee/automations/` and `src/pocketpaw/ee/guards/` are confusing. Decide their fate before moving anything new.

**Step 1: Read each shim**
```bash
ls src/pocketpaw/ee/automations src/pocketpaw/ee/guards
cat src/pocketpaw/ee/automations/__init__.py src/pocketpaw/ee/guards/__init__.py 2>/dev/null | head
```

**Step 2: For each shim, decide:**
- **Pure shim (re-exports from `pocketpaw_ee.*`):** delete; the imports will land naturally in Step Task 3 below.
- **Has its own logic:** treat the shim as the canonical OSS implementation; merge `pocketpaw_ee.*` into it during the move.

**Step 3: Commit the decision**
```bash
git add src/pocketpaw/ee
git commit -m "chore(ee): reconcile pocketpaw.ee shim subpackages before phase 2 moves"
```

(If the shims are just empty placeholders, this commit may be a deletion — `git rm -r src/pocketpaw/ee/automations src/pocketpaw/ee/guards`.)

---

## Task 2: Per-subpackage audit (do all 8 first, before any moves)

For each subpackage in the table above, run:

```bash
SUB=ripple   # repeat per subpackage
echo "--- $SUB imports OUT (anything from pocketpaw_ee.cloud, pocketpaw_ee.audit, pocketpaw_ee.fleet, pocketpaw_ee.agent) ---"
grep -rn "from pocketpaw_ee\.\(cloud\|audit\|fleet\|agent\|api\)\|import pocketpaw_ee\.\(cloud\|audit\|fleet\|agent\|api\)" ee/pocketpaw_ee/$SUB --include="*.py"
echo "--- callers (who imports $SUB) ---"
grep -rn "from pocketpaw_ee\.$SUB\|import pocketpaw_ee\.$SUB" --include="*.py" .
```

**Record findings** in a temporary file `phase-2-audit.md` (delete before commit). For each subpackage:
- Clean (no cloud/audit/fleet/agent deps): move freely.
- Soft dep (uses a facade like `pocketpaw_ee.api`): lift the facade into core or define a Protocol in core that EE provides.
- Hard dep (calls into `pocketpaw_ee.cloud.models` etc.): **defer** to Phase 3 and leave in `pocketpaw_ee/` for now.

**Known hard cases from prior inspection:**
- `instinct` and `fabric` both import `get_instinct_store` / `get_fabric_store` from `pocketpaw_ee.api`. Inspect `ee/pocketpaw_ee/api.py` to see if those factories are cloud-only or pluggable. Most likely: lift the factory functions into `pocketpaw.api` with an in-memory default, and let `pocketpaw_ee.api` override for cloud.
- `automations.evaluator` imports both `pocketpaw_ee.api.get_instinct_store` and `pocketpaw_ee.instinct.models.ActionTrigger`. After moving `instinct` to core, the second one resolves; the first becomes a Phase 3 extension point.

**Output of Task 2:** a per-subpackage verdict — *move now*, *move with lift*, or *defer*. The remaining tasks act only on packages marked *move now* or *move with lift*.

---

## Task 3: Move each clean subpackage (`git mv` + import rewrite)

For each subpackage marked *move now*, in alphabetical order:

**Step 1: Move**
```bash
SUB=paw_print
git mv ee/pocketpaw_ee/$SUB src/pocketpaw/$SUB
```

**Step 2: Rewrite imports across repo (Python files only)**

Write a one-shot codemod parameterized by subpackage:

```python
# scripts/_phase2_move_subpkg.py
import re
import sys
from pathlib import Path

SUB = sys.argv[1]
EXCLUDE = {".venv", ".git", "__pycache__", "dist", "build", "node_modules"}
PATTERNS = [
    (re.compile(rf"^(\s*)from pocketpaw_ee\.{SUB}(\b)", re.MULTILINE), rf"\1from pocketpaw.{SUB}\2"),
    (re.compile(rf"^(\s*)import pocketpaw_ee\.{SUB}(\b)", re.MULTILINE), rf"\1import pocketpaw.{SUB}\2"),
]
changed = 0
for py in Path(".").rglob("*.py"):
    if any(p in EXCLUDE for p in py.parts):
        continue
    text = py.read_text(encoding="utf-8")
    new = text
    for pat, repl in PATTERNS:
        new = pat.sub(repl, new)
    if new != text:
        py.write_text(new, encoding="utf-8")
        changed += 1
print(f"{SUB}: rewrote {changed} files")
```

Run per subpackage:
```bash
uv run python scripts/_phase2_move_subpkg.py $SUB
```

**Step 3: Update `import-linter` contracts in `pyproject.toml`**

Any contract that references `pocketpaw_ee.$SUB` must update its module path to `pocketpaw.$SUB`. Some contracts may need to be split or moved between OSS-side and EE-side contract groups.

**Step 4: Update hatch wheel includes**

After moves, `src/pocketpaw/` contains the moved subpackages; `pyproject.toml` already includes `src/pocketpaw` so no change is needed. But verify no stale entry was added in Phase 1:

```bash
grep -nE "only-include|sources" pyproject.toml | head
```

**Step 5: Run tests for this subpackage**

```bash
uv run pytest tests/ -k $SUB -q 2>&1 | tail
uv run pytest tests/cloud/ -k $SUB -q 2>&1 | tail
uv run pytest tests/ee/ -k $SUB -q 2>&1 | tail
```

**Step 6: Commit per subpackage**
```bash
git add -A
git commit -m "chore(ee): move $SUB to pocketpaw core (phase 2)"
```

Smaller commits = easier review and bisect.

---

## Task 4: Move each "lift" subpackage (with dependency lift)

Same flow as Task 3 plus one extra step *before* the `git mv`:

**Step 1: Lift the shared facade into core**

Example for `pocketpaw_ee.api.get_instinct_store`:
- Create `src/pocketpaw/api.py` (or extend existing) with `get_instinct_store` returning a default in-memory store.
- In `ee/pocketpaw_ee/api.py`, override `get_instinct_store` to return the cloud-backed store. Register the override at import time (this is a Phase 3 hand-off; for Phase 2 use a simple module-level rebinding, with a comment marking the spot to convert to entry-point in Phase 3).
- Rewrite the consuming subpackage's imports from `pocketpaw_ee.api` → `pocketpaw.api`.

**Step 2..6: Same as Task 3.**

---

## Task 5: Tests, lint, types, import-linter (full sweep)

After all moves:
```bash
uv run ruff check . --fix
uv run ruff format .
uv run mypy .
uv run lint-imports
uv run pytest --ignore=tests/e2e -q
uv run pytest tests/cloud -q
uv run pytest tests/ee -q
```

All must be at parity with the Phase 1 baseline. Stop and fix per-subpackage if any subpackage's tests regress.

---

## Task 6: Delete the one-shot codemod script

```bash
git rm -f scripts/_phase2_move_subpkg.py 2>/dev/null || rm -f scripts/_phase2_move_subpkg.py
git rm -f phase-2-audit.md 2>/dev/null || rm -f phase-2-audit.md
```

---

## Task 7: Update CLAUDE.md

`backend/CLAUDE.md` should list the moved subpackages as core, not enterprise. Bump the description of `ee/pocketpaw_ee/` to "cloud + agent/pocket_specialist + audit + fleet only."

---

## Task 8: Open PR

```bash
git push -u origin chore/oss-ee-phase-2-subpackage-moves
gh pr create --title "chore(ee): move non-SaaS subpackages from pocketpaw_ee to pocketpaw (Phase 2)" --body "$(cat <<'EOF'
## Summary
- Phase 2 of the open-core split (see `docs/plans/2026-05-16-oss-ee-split-design.md`).
- Moves 8 subpackages out of `pocketpaw_ee/`: $LIST_OF_MOVED.
- For subpackages with soft deps on `pocketpaw_ee.api`, the facade is lifted into `pocketpaw.api` with an EE override hook (formalized as an entry-point in Phase 3).
- `pocketpaw_ee/` now contains only `cloud/`, `agent/pocket_specialist/`, `audit/`, and `fleet/`.

## Risks accepted / deferred
- $LIST_OF_DEFERRED stay in `pocketpaw_ee/` for now due to hard cloud deps. They become Phase 3 extension points.

## Test plan
- [ ] Same pass count as Phase 1 baseline across `tests/`, `tests/cloud`, `tests/ee`
- [ ] `lint-imports` green against the updated contracts
- [ ] `import pocketpaw.<moved_subpkg>` works in a venv with `pocketpaw_ee` *not* installed (manual smoke test)
EOF
)"
```

---

## Definition of done

- [ ] Every subpackage in scope is in `src/pocketpaw/<name>/` or explicitly deferred with a note in the PR
- [ ] `grep -rn "from pocketpaw_ee\.\(automations\|fabric\|instinct\|retrieval\|paw_print\|ripple\|widget\|guards\)" --include="*.py" .` returns empty (modulo deferred subpackages)
- [ ] `pocketpaw_ee/` contains: `cloud/`, `agent/`, `audit/`, `fleet/`, `__init__.py`, `api.py` (now an EE-only override), `journal_dep.py` (audit before keeping or moving)
- [ ] All test suites at parity
- [ ] `import-linter` contracts updated and passing
- [ ] PR opened, CI green
