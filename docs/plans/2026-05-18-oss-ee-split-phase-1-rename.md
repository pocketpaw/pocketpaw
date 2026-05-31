# OSS/EE Split — Phase 1: Rename `ee.*` → `pocketpaw_ee.*` Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Rename the enterprise namespace from bare `ee.*` to `pocketpaw_ee.*` without changing any behavior. One wheel still ships both packages. This unblocks Phases 2–5 of the open-core split.

**Architecture:** Move `ee/<subpackage>/` contents into `ee/pocketpaw_ee/<subpackage>/`. Update hatch wheel config so `pocketpaw_ee` is installed as a top-level distribution package. Codemod all `from ee.` and `import ee.` references across the repo. Update the 9 existing `import-linter` contracts so their module paths track the new namespace.

**Tech Stack:** Python 3.11+, `uv`, `hatch` (build), `pytest`, `ruff`, `mypy`, `import-linter`.

**Reference:** Design doc at `docs/plans/2026-05-16-oss-ee-split-design.md`. This plan implements Phase 1 (Section 7) only.

---

## Scope and non-goals

**In scope (Phase 1):**
- Directory move: `ee/X` → `ee/pocketpaw_ee/X` for every Python subpackage and module.
- Hatch wheel config update so the installed package name is `pocketpaw_ee`.
- Codemod of all `from ee.` / `import ee.` references in Python source, tests, and config.
- Update `import-linter` contracts' module paths.
- Update `backend/CLAUDE.md` and any active docs (wiki/plans) that reference `ee.*` paths for engineers.

**Out of scope (later phases):**
- Splitting `pyproject.toml` into two (Phase 4).
- Moving subpackages out of `ee/` into core (Phase 2).
- Introducing extension-point Protocols (Phase 3).
- License headers per-file, contributor docs (Phase 4/6 of design doc).
- Coordinating renames in `paw-enterprise` or other workspace projects (these consume backend via HTTP, not Python imports — no cross-repo Python coupling exists today, but a quick grep is a Task below).

---

## Pre-flight (one-time, before any tasks)

Working directory: `D:\paw\backend\`. Branch off `main` per design doc; current `feat/composio-integration` work is unrelated.

### Pre-flight 1: Resolve dirty working tree

```bash
git status --short
```
Expected: `M ee/ripple/_inline.py` (one unrelated modification).

Either commit on `feat/composio-integration`, or stash:
```bash
git stash push -m "wip: ripple _inline before oss-ee phase 1" ee/ripple/_inline.py
```

### Pre-flight 2: Create branch off main

```bash
git fetch origin
git checkout -b chore/oss-ee-phase-1-rename origin/main
```

### Pre-flight 3: Confirm baseline test suite passes

```bash
uv sync --dev
uv run pytest --ignore=tests/e2e -q
```
Expected: green. Record the test count — Phase 1 must end with the same count, all passing.

If the baseline is red on `main`, stop and report — do not start the rename on a broken baseline.

---

## Task 1: Move `ee/__init__.py` aside and create `pocketpaw_ee` package root

**Files:**
- Create: `ee/pocketpaw_ee/__init__.py` (content copied from current `ee/__init__.py`)
- Modify (later, in Task 3): `ee/__init__.py` becomes empty or is deleted entirely

**Step 1: Inspect current top-level `ee/__init__.py`**
```bash
cat ee/__init__.py
```
Note any re-exports or `__all__` declarations.

**Step 2: Create new package root**
```bash
mkdir ee/pocketpaw_ee
cp ee/__init__.py ee/pocketpaw_ee/__init__.py
```

**Step 3: Verify** — `ee/pocketpaw_ee/__init__.py` exists and matches the original byte-for-byte:
```bash
diff ee/__init__.py ee/pocketpaw_ee/__init__.py && echo OK
```

**Step 4: Commit checkpoint**
```bash
git add ee/pocketpaw_ee/__init__.py
git commit -m "chore(ee): scaffold pocketpaw_ee package root (no imports rewritten yet)"
```

---

## Task 2: Move all `ee/<subpackage>/` directories under `pocketpaw_ee/`

**Files moved (all top-level Python content of `ee/`):**

| Source | Destination |
|---|---|
| `ee/agent/` | `ee/pocketpaw_ee/agent/` |
| `ee/api.py` | `ee/pocketpaw_ee/api.py` |
| `ee/audit/` | `ee/pocketpaw_ee/audit/` |
| `ee/automations/` | `ee/pocketpaw_ee/automations/` |
| `ee/cloud/` | `ee/pocketpaw_ee/cloud/` |
| `ee/fabric/` | `ee/pocketpaw_ee/fabric/` |
| `ee/fleet/` | `ee/pocketpaw_ee/fleet/` |
| `ee/instinct/` | `ee/pocketpaw_ee/instinct/` |
| `ee/journal_dep.py` | `ee/pocketpaw_ee/journal_dep.py` |
| `ee/paw_print/` | `ee/pocketpaw_ee/paw_print/` |
| `ee/retrieval/` | `ee/pocketpaw_ee/retrieval/` |
| `ee/ripple/` | `ee/pocketpaw_ee/ripple/` |
| `ee/widget/` | `ee/pocketpaw_ee/widget/` |

**Not moved (stay at `ee/` root):**
- `ee/LICENSE` (license boundary marker)
- `ee/docs/` (kept at `ee/docs/` for now; can move/split in a later phase)

**Step 1: Move each subpackage with `git mv` (preserves history)**

```bash
git mv ee/agent ee/pocketpaw_ee/agent
git mv ee/api.py ee/pocketpaw_ee/api.py
git mv ee/audit ee/pocketpaw_ee/audit
git mv ee/automations ee/pocketpaw_ee/automations
git mv ee/cloud ee/pocketpaw_ee/cloud
git mv ee/fabric ee/pocketpaw_ee/fabric
git mv ee/fleet ee/pocketpaw_ee/fleet
git mv ee/instinct ee/pocketpaw_ee/instinct
git mv ee/journal_dep.py ee/pocketpaw_ee/journal_dep.py
git mv ee/paw_print ee/pocketpaw_ee/paw_print
git mv ee/retrieval ee/pocketpaw_ee/retrieval
git mv ee/ripple ee/pocketpaw_ee/ripple
git mv ee/widget ee/pocketpaw_ee/widget
```

**Step 2: Delete the old top-level `ee/__init__.py`**

It now lives at `ee/pocketpaw_ee/__init__.py`; the directory `ee/` should not be a Python package itself.

```bash
git rm ee/__init__.py
```

**Step 3: Verify layout**

```bash
ls ee/
# Expected: LICENSE  docs/  pocketpaw_ee/
ls ee/pocketpaw_ee/
# Expected: __init__.py agent/ api.py audit/ automations/ cloud/ fabric/ fleet/ instinct/ journal_dep.py paw_print/ retrieval/ ripple/ widget/
```

**Step 4: Do NOT commit yet** — repo is in a broken state (imports still say `from ee.`). Continue to Task 3.

---

## Task 3: Update hatch wheel config to install `pocketpaw_ee` as a top-level package

**Files:**
- Modify: `pyproject.toml:395-400`

**Step 1: Read current config**

Around line 395:
```toml
[tool.hatch.build.targets.wheel]
only-include = ["src/pocketpaw", "ee"]

[tool.hatch.build.targets.wheel.sources]
"src" = ""
```

**Step 2: Rewrite to point at `ee/pocketpaw_ee`**

```toml
[tool.hatch.build.targets.wheel]
only-include = ["src/pocketpaw", "ee/pocketpaw_ee"]

[tool.hatch.build.targets.wheel.sources]
"src" = ""
"ee" = ""
```

The `"ee" = ""` source mapping strips the `ee/` prefix so `ee/pocketpaw_ee/` installs as the top-level distribution package `pocketpaw_ee`.

**Step 3: Test the build locally**

```bash
uv build --wheel
unzip -l dist/pocketpaw-*.whl | head -30
```
Expected: see entries like `pocketpaw/...` AND `pocketpaw_ee/cloud/...` — NOT `ee/pocketpaw_ee/...`.

**Step 4: Do not commit yet** — codemod follows.

---

## Task 4: Codemod `from ee.` / `import ee.` → `pocketpaw_ee.`

**Files:** all `.py` files under `src/`, `ee/`, `tests/`, `scripts/`, `connectors/` (everywhere except `.venv`, `docs/`, `dist/`, `build/`, `__pycache__`).

**Patterns to rewrite (in this order, on `.py` files only):**

| Pattern | Replacement |
|---|---|
| `^from ee\.` (line-start) | `from pocketpaw_ee.` |
| `^from ee import ` (line-start) | `from pocketpaw_ee import ` |
| `^import ee\.` (line-start) | `import pocketpaw_ee.` |
| `^import ee$` (bare top-level — must verify there are zero of these) | hand-fix if any |
| Indented variants (`    from ee.`, `\tfrom ee.`) | same prefix swap |

**Step 1: Check for bare `import ee` (no submodule)**

```bash
grep -rn "^[[:space:]]*import ee$" --include="*.py" .
```
Expected: zero matches. If any, list them and hand-fix in Task 5.

**Step 2: Write the codemod script**

Create `scripts/_phase1_rename.py`:

```python
"""One-shot Phase 1 codemod. Run from backend/ repo root.

Rewrites Python imports:
  from ee.X    -> from pocketpaw_ee.X
  from ee      -> from pocketpaw_ee
  import ee.X  -> import pocketpaw_ee.X
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

EXCLUDE_DIRS = {".venv", ".git", "__pycache__", "dist", "build", "node_modules"}

PATTERNS = [
    (re.compile(r"^(\s*)from ee\."), r"\1from pocketpaw_ee."),
    (re.compile(r"^(\s*)from ee import "), r"\1from pocketpaw_ee import "),
    (re.compile(r"^(\s*)import ee\."), r"\1import pocketpaw_ee."),
]

def rewrite(path: Path) -> int:
    text = path.read_text(encoding="utf-8")
    new = text
    for pat, repl in PATTERNS:
        new = pat.sub(repl, new, flags=0) if not pat.flags & re.MULTILINE else new
        # Need MULTILINE for ^ to match each line:
        new = pat.sub(repl, new) if False else new
    # Apply with MULTILINE explicitly:
    new = text
    for pat, repl in PATTERNS:
        new = re.sub(pat.pattern, repl, new, flags=re.MULTILINE)
    if new != text:
        path.write_text(new, encoding="utf-8")
        return 1
    return 0

def main() -> int:
    root = Path(".").resolve()
    changed = 0
    for py in root.rglob("*.py"):
        if any(part in EXCLUDE_DIRS for part in py.parts):
            continue
        changed += rewrite(py)
    print(f"Modified {changed} files")
    return 0

if __name__ == "__main__":
    sys.exit(main())
```

**Step 3: Dry-run by counting matches first**

```bash
grep -rn --include="*.py" -E "^(\s*)(from ee\.|from ee import |import ee\.)" . | wc -l
```
Record this number (call it N). Expect roughly 694 from earlier inspection but exact number depends on exclusions.

**Step 4: Run the codemod**

```bash
uv run python scripts/_phase1_rename.py
```
Expected output: `Modified <some number> files` — should be close to 250 (the file count from earlier).

**Step 5: Verify no `from ee.` or `import ee.` remains in `.py` files**

```bash
grep -rn --include="*.py" -E "^(\s*)(from ee\.|from ee import |import ee\.|import ee$)" .
```
Expected: empty output.

**Step 6: Delete the codemod script (it's one-shot)**

```bash
git rm -f scripts/_phase1_rename.py 2>/dev/null || rm -f scripts/_phase1_rename.py
```

**Step 7: Do not commit yet** — import-linter config still needs updating.

---

## Task 5: Update `import-linter` contracts

**Files:**
- Modify: `pyproject.toml` around lines 481–590 (the 9 `[[tool.importlinter.contracts]]` blocks).

**Step 1: Read the contracts**

```bash
sed -n '481,600p' pyproject.toml
```

**Step 2: Replace module path prefixes**

Every contract has `modules`, `forbidden_modules`, `source_modules`, or `layers` fields that reference module paths. Apply these substitutions inside the `[tool.importlinter]` section ONLY (do not touch the rest of the file — the codemod in Task 4 was Python-only, this is TOML):

| Substring | Replacement |
|---|---|
| `"ee.cloud` | `"pocketpaw_ee.cloud` |
| `"ee.agent` | `"pocketpaw_ee.agent` |
| `"ee.audit` | `"pocketpaw_ee.audit` |
| `"ee.automations` | `"pocketpaw_ee.automations` |
| `"ee.fabric` | `"pocketpaw_ee.fabric` |
| `"ee.fleet` | `"pocketpaw_ee.fleet` |
| `"ee.instinct` | `"pocketpaw_ee.instinct` |
| `"ee.paw_print` | `"pocketpaw_ee.paw_print` |
| `"ee.retrieval` | `"pocketpaw_ee.retrieval` |
| `"ee.ripple` | `"pocketpaw_ee.ripple` |
| `"ee.widget` | `"pocketpaw_ee.widget` |
| `"ee.api` | `"pocketpaw_ee.api` |
| `"ee.journal_dep` | `"pocketpaw_ee.journal_dep` |
| `"ee"` (exact, as a root module) | `"pocketpaw_ee"` |

Use a careful text editor pass — these are TOML strings, not Python imports, so a sloppy regex risks corrupting comments or non-importlinter fields. Do this section by section in the editor.

**Step 3: Sanity grep**

```bash
grep -nE '"ee\.|"ee"' pyproject.toml
```
Expected: empty.

---

## Task 6: Reinstall the package

```bash
uv sync --dev --reinstall-package pocketpaw
```

Expected: clean reinstall. If hatch complains about missing files or wrong layout, fix Task 3's config before continuing.

**Sanity import check:**
```bash
uv run python -c "import pocketpaw_ee; import pocketpaw_ee.cloud; print(pocketpaw_ee.__file__)"
```
Expected: prints the path inside `ee/pocketpaw_ee/__init__.py`.

```bash
uv run python -c "import ee" 2>&1 | tail -1
```
Expected: `ModuleNotFoundError: No module named 'ee'` — confirms the old namespace is gone.

---

## Task 7: Run lint + type + import-linter

```bash
uv run ruff check . --fix
uv run ruff format .
uv run mypy .
uv run lint-imports
```

Expected: all green. If any failures, they should be:
- **ruff/mypy:** missed import or syntax issue from the codemod — fix and re-run.
- **lint-imports:** missed contract path in Task 5 — fix and re-run.

If a failure is structural (e.g. mypy complains about a module path that the codemod missed), grep for it specifically:
```bash
grep -rn --include="*.py" "ee\." . | grep -v pocketpaw_ee | grep -v "# " | head
```

---

## Task 8: Run the test suite

**Step 1: Default suite (matches CI)**

```bash
uv run pytest --ignore=tests/e2e -q
```
Expected: same green count as the pre-flight baseline.

**Step 2: Cloud suite (currently excluded by default)**

```bash
uv run pytest tests/cloud -q
```
Expected: same pass/fail state as on `main` for `tests/cloud/` (Phase 1 must not change behavior — if cloud tests were red on main, they stay red; do not "fix" failing tests in this phase).

**Step 3: EE suite**

```bash
uv run pytest tests/ee -q
```
Same expectation.

---

## Task 9: Update active docs and CLAUDE.md

Phase 1 does not aim to retroactively edit every wiki page. Update the engineer-facing entry points only:

**Files:**
- Modify: `D:\paw\backend\CLAUDE.md` — every mention of `ee/` package import paths or `from ee.` examples becomes `pocketpaw_ee/` / `from pocketpaw_ee.`.
- Modify: `D:\paw\CLAUDE.md` — verify references match (the workspace-level file should still say `backend/ee/cloud/` since that's a **path**, not a Python import; only update Python import examples).
- Modify: `D:\paw\backend\docs\plans\2026-05-16-oss-ee-split-design.md` — add a "Phase 1 status: complete on chore/oss-ee-phase-1-rename" note at top.

**Wiki pages under `docs/wiki/` are deferred** — they're descriptive, not load-bearing, and a follow-up doc-sweep PR is cheaper than blocking this branch.

---

## Task 10: Cross-repo check (paw-enterprise + others)

Cheap sanity check that no other workspace project imports backend's `ee.*` directly.

```bash
grep -rn "from ee\.\|import ee\." D:/paw/paw-enterprise D:/paw/ripple D:/paw/side-projects --include="*.py" --include="*.ts" --include="*.svelte" 2>/dev/null | head
```
Expected: empty (these projects consume backend via HTTP). If matches found, list and decide before merging.

---

## Task 11: Commit and open PR

**Step 1: Stage all changes**

```bash
git status --short
git add -A
```

**Step 2: Commit**

```bash
git commit -m "$(cat <<'EOF'
chore(ee): rename ee.* namespace to pocketpaw_ee.*

Phase 1 of open-core split (see docs/plans/2026-05-16-oss-ee-split-design.md).

- Move ee/<subpkg>/ contents into ee/pocketpaw_ee/<subpkg>/
- Update hatch wheel includes/sources so pocketpaw_ee installs as a
  top-level distribution package
- Codemod all Python imports: from ee.* / import ee.* -> pocketpaw_ee.*
- Update import-linter contracts to new module paths
- Update CLAUDE.md import examples

No behavior change. Same wheel, same dependencies, same test outcomes.
Phases 2-5 (subpackage moves, extension points, pyproject split,
publish) follow in later branches.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

**Step 3: Push and open PR**

```bash
git push -u origin chore/oss-ee-phase-1-rename
gh pr create --title "chore(ee): rename ee.* namespace to pocketpaw_ee.* (Phase 1)" --body "$(cat <<'EOF'
## Summary
- Phase 1 of the open-core split documented in `docs/plans/2026-05-16-oss-ee-split-design.md`.
- Pure mechanical rename: every `from ee.X` / `import ee.X` becomes `from pocketpaw_ee.X` / `import pocketpaw_ee.X`.
- Directory move: `ee/<subpackage>/` → `ee/pocketpaw_ee/<subpackage>/`.
- Hatch wheel config updated so `pocketpaw_ee` is installed as a top-level distribution package.
- `import-linter` contracts updated to track the new module paths.

No behavior change. Same wheel, same deps, same test outcomes.

## Test plan
- [ ] `uv run pytest --ignore=tests/e2e` — same pass count as `main`
- [ ] `uv run pytest tests/cloud` — same pass/fail state as `main`
- [ ] `uv run pytest tests/ee` — same pass/fail state as `main`
- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy .`
- [ ] `uv run lint-imports`
- [ ] `uv build --wheel` succeeds; wheel contains `pocketpaw/` and `pocketpaw_ee/` top-level packages
- [ ] `python -c "import ee"` fails with ModuleNotFoundError
EOF
)"
```

---

## Rollback

If anything below fails after merge and isn't fixable forward:

```bash
git revert <merge-commit-sha> -m 1
```

The rename is purely additive at the codemod level; revert restores the prior `ee.*` namespace cleanly.

---

## Definition of done

- [ ] `ee/` contains only `LICENSE`, `docs/`, and `pocketpaw_ee/`
- [ ] `grep -rn "from ee\.\|import ee\." --include="*.py" .` returns empty
- [ ] `python -c "import pocketpaw_ee.cloud, pocketpaw_ee.agent, pocketpaw_ee.audit, pocketpaw_ee.fleet"` succeeds
- [ ] `uv run lint-imports` passes against updated contracts
- [ ] Pytest count on `chore/oss-ee-phase-1-rename` ≥ count on `main` for `tests/`, `tests/cloud/`, `tests/ee/`
- [ ] Wheel built by `uv build --wheel` contains `pocketpaw/` and `pocketpaw_ee/` at the top level (not `ee/pocketpaw_ee/`)
- [ ] `backend/CLAUDE.md` import examples reflect the new namespace
- [ ] PR opened, CI green, ready for review
