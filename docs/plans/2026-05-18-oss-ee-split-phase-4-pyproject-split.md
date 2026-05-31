# OSS/EE Split — Phase 4: Split into Two Packages (`pocketpaw` + `pocketpaw-ee`) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Produce two installable wheels — `pocketpaw` (MIT, OSS, no `pocketpaw_ee` code on disk) and `pocketpaw-ee` (FSL-1.1, depends on `pocketpaw`). After this phase, `pip install pocketpaw` installs only the core; `pip install pocketpaw-ee` adds the enterprise layer.

**Architecture:** Two `pyproject.toml` files. Root one defines `pocketpaw`, includes only `src/pocketpaw`, drops all EE dependencies and the `enterprise` extra. `ee/pyproject.toml` defines `pocketpaw-ee`, includes `ee/pocketpaw_ee`, depends on `pocketpaw>=<this version>`, owns all EE-specific dependencies, and declares all `[project.entry-points]` previously in the root file.

**Tech Stack:** `hatch` for both packages. Same Python/uv/ruff/mypy stack.

**Reference:** Design doc Sections 2 and 5. Depends on Phases 1, 2, 3 being merged.

---

## Pre-flight

```bash
git checkout main
git pull
git checkout -b chore/oss-ee-phase-4-pyproject-split
uv sync --dev
uv run pytest --ignore=tests/e2e -q
```

---

## Task 1: Inventory current root `pyproject.toml`

Read end-to-end. Identify three groups of items:

- **Stays in core** — dependencies and extras used by anything under `src/pocketpaw/`.
- **Moves to EE** — dependencies under the current `enterprise` extra (MongoDB, fastapi-users, Socket.IO, slowapi, S3/GCS clients, LiveKit, Redis, Composio, Soul protocol if cloud-only); plus any other deps used only inside `ee/pocketpaw_ee/`.
- **Shared dev tooling** — `ruff`, `mypy`, `pytest`, `import-linter` — keep in both packages' dev deps, or in core dev deps only with EE inheriting (simpler).

Cross-check by grepping:
```bash
# For each dependency name, find which side it's used on
DEP=mongodb
grep -rn "$DEP\|$(echo $DEP | sed 's/-/_/g')" src/pocketpaw ee/pocketpaw_ee --include="*.py" -l | sort -u
```

Output: write `.phase-4-deps.md` with the verdict per dependency. Delete before commit.

---

## Task 2: Restructure `ee/` for hatch

The final layout for the EE package:

```
ee/
├── LICENSE
├── README.md             # short EE-specific readme (NEW)
├── pyproject.toml        # NEW
├── pocketpaw_ee/         # was ee/pocketpaw_ee/, but hatch needs it under a src-style path
└── docs/                 # stays
```

Easiest hatch config: keep `pocketpaw_ee/` at `ee/pocketpaw_ee/` and let `ee/pyproject.toml`'s `tool.hatch.build.targets.wheel` declare `packages = ["pocketpaw_ee"]` with the working directory set by the file's location.

**Step 1: Write `ee/pyproject.toml`**

```toml
[build-system]
requires = ["hatchling>=1.18"]
build-backend = "hatchling.build"

[project]
name = "pocketpaw-ee"
dynamic = ["version"]
description = "PocketPaw enterprise extensions (multi-tenant cloud, audit, fleet, pocket specialist)"
readme = "README.md"
requires-python = ">=3.11"
license = { file = "LICENSE" }  # FSL-1.1 → Apache-2.0
authors = [{ name = "PocketPaw" }]

dependencies = [
    "pocketpaw>=__SAME_VERSION__",
    # All EE-only deps, from Task 1 inventory
    "beanie>=...",
    "fastapi-users>=...",
    "python-socketio>=...",
    "boto3>=...",
    "google-cloud-storage>=...",
    "composio-core>=...",
    "redis>=...",
    # ... rest from inventory
]

[project.optional-dependencies]
# EE-specific extras only (e.g. livekit, sso providers if optional)
livekit = ["livekit>=..."]
sso = ["..."]

[project.entry-points."pocketpaw.tools"]
composio = "pocketpaw_ee.cloud.composio:CloudComposioTools"

[project.entry-points."pocketpaw.models"]
cloud = "pocketpaw_ee.cloud.models:CloudModelProvider"

[project.entry-points."pocketpaw.routes"]
cloud = "pocketpaw_ee.cloud:CloudRouteProvider"

[project.entry-points."pocketpaw.lifecycle"]
cloud = "pocketpaw_ee.cloud.lifecycle:CloudLifecycle"

[project.entry-points."pocketpaw.agent_extensions"]
chat_agent = "pocketpaw_ee.cloud.chat.agent_service:ChatAgentExtension"
pocket_specialist = "pocketpaw_ee.agent.pocket_specialist:PocketSpecialistExtension"

[project.entry-points."pocketpaw.embeddings"]
cloud = "pocketpaw_ee.cloud.embeddings:CloudEmbeddingProvider"

[project.entry-points."pocketpaw.memory_backends"]
mongo = "pocketpaw_ee.cloud.memory.mongo_store:MongoMemoryBackend"

[project.entry-points."pocketpaw.storage_backends"]
s3 = "pocketpaw_ee.cloud.files.providers.s3:S3StorageBackend"
gcs = "pocketpaw_ee.cloud.files.providers.gcs:GCSStorageBackend"

[project.entry-points."pocketpaw.events"]
in_process = "pocketpaw_ee.cloud.shared.events:InProcessEventBus"

[project.entry-points."pocketpaw.auth"]
cloud = "pocketpaw_ee.cloud.auth:CloudAuthProvider"

[project.entry-points."pocketpaw.features"]
cloud = "pocketpaw_ee.cloud.features:CloudFeatures"

[tool.hatch.version]
path = "pocketpaw_ee/__init__.py"

[tool.hatch.build.targets.wheel]
packages = ["pocketpaw_ee"]
```

(Entry-point list above is illustrative — copy the actual set Phase 3 created in the root `pyproject.toml`.)

**Step 2: Move all `[project.entry-points.\"pocketpaw.*\"]` blocks from root `pyproject.toml` into `ee/pyproject.toml`.**

After this, the root `pyproject.toml` declares no entry-points (or only OSS-side ones, if any).

**Step 3: Write `ee/README.md`** — a short one-page description of the enterprise edition, link to FSL license, install instructions.

**Step 4: Verify the EE build**

```bash
cd ee
uv build --wheel
unzip -l dist/pocketpaw_ee-*.whl | head -30
```
Expected: wheel contents at `pocketpaw_ee/...` (no `ee/` prefix).

```bash
cd ..
```

---

## Task 3: Strip EE from root `pyproject.toml`

**Modify: `pyproject.toml`**

Changes:
1. `[tool.hatch.build.targets.wheel]` — `only-include = ["src/pocketpaw"]` (drop the `ee/pocketpaw_ee` entry from Phase 1).
2. `[tool.hatch.build.targets.wheel.sources]` — drop the `"ee" = ""` line.
3. `[project.optional-dependencies]` — remove the `enterprise` extra entirely. Channel/tool extras stay.
4. `[project.dependencies]` — remove any dep that's only used inside `ee/pocketpaw_ee/` per Task 1 inventory.
5. `[project.entry-points]` — verify empty (or OSS-side only).
6. `[tool.importlinter]` — split contracts: those scoped to `pocketpaw.*` stay; those scoped to `pocketpaw_ee.*` move into `ee/pyproject.toml`'s `[tool.importlinter]`.

**Build core to verify:**
```bash
uv build --wheel
unzip -l dist/pocketpaw-*.whl | head -20
```
Expected: only `pocketpaw/...` entries, no `pocketpaw_ee/`.

---

## Task 4: Local install matrix

Three local installs, three sanity checks.

**A. OSS-only install:**
```bash
uv venv .venv-oss && . .venv-oss/Scripts/activate
uv pip install -e .   # core only
python -c "import pocketpaw; print(pocketpaw.__file__)"
python -c "import pocketpaw_ee" 2>&1 | grep ModuleNotFoundError
pytest --ignore=tests/e2e --ignore=tests/cloud --ignore=tests/ee -q
deactivate
```
Expected: pocketpaw imports, pocketpaw_ee raises ModuleNotFoundError, OSS tests pass.

**B. Full install:**
```bash
uv venv .venv-full && . .venv-full/Scripts/activate
uv pip install -e . -e ./ee
python -c "import pocketpaw, pocketpaw_ee; print(pocketpaw_ee.__file__)"
pytest --ignore=tests/e2e -q
deactivate
```
Expected: both import, all tests pass.

**C. EE-only smoke (should fail clearly):**
```bash
uv venv .venv-ee-only && . .venv-ee-only/Scripts/activate
uv pip install -e ./ee   # without -e .
# Expected: pulls pocketpaw from PyPI if available, OR fails because pocketpaw>=X isn't published yet.
# For this phase pre-publish, the expectation is: pip install error referencing pocketpaw dep.
deactivate
```

Document any rough edges. Clean up temp venvs:
```bash
rm -rf .venv-oss .venv-full .venv-ee-only
```

---

## Task 5: Update CI matrix

CI now runs **three** jobs against `main`/PRs:

1. **OSS-only build & test** (already added in Phase 3 — keep it, update paths).
2. **Full build & test** (current default — but `uv pip install -e . -e ./ee` instead of just `uv pip install -e .`).
3. **`import-linter`** — run twice: once with core contracts, once with EE contracts.

Update `.github/workflows/*.yml` accordingly. Cache pip installs across jobs.

Add wheel build jobs that produce both wheels on tag pushes:
```yaml
build-wheels:
  if: startsWith(github.ref, 'refs/tags/v')
  steps:
    - uses: actions/checkout@v4
    - uses: astral-sh/setup-uv@v3
    - run: uv build --wheel       # produces dist/pocketpaw-*.whl
    - run: cd ee && uv build --wheel  # produces ee/dist/pocketpaw_ee-*.whl
    - uses: actions/upload-artifact@v4
      with:
        name: wheels
        path: |
          dist/*.whl
          ee/dist/*.whl
```

---

## Task 6: Update `uv sync` workflow for developers

Developers want one command that installs both. Add a Makefile or a script:

```makefile
# Makefile
dev:
\tuv sync --dev
\tuv pip install -e ./ee --no-deps   # ee deps already pulled by uv sync via dev group

test:
\tuv run pytest --ignore=tests/e2e -q

test-oss-only:
\trm -rf .venv-oss
\tuv venv .venv-oss
\t.venv-oss/Scripts/python -m pip install -e .
\t.venv-oss/Scripts/python -m pytest --ignore=tests/e2e --ignore=tests/cloud --ignore=tests/ee -q
```

Or simpler: a `[dependency-groups]` `dev` entry in root `pyproject.toml` that includes `-e ./ee` via `uv`'s editable path support (verify uv syntax at execution time).

Document the dev setup in `backend/CLAUDE.md`.

---

## Task 7: Update `import-linter` contracts split

Root `pyproject.toml` contracts (OSS) — keep:
- "Core may not import from EE"
- any layered contracts inside `pocketpaw.*`

`ee/pyproject.toml` contracts (EE) — receive:
- "EE may not import from itself in disallowed order" (the 9 existing `ee.cloud.*` contracts)

Both run in CI; both must pass.

---

## Task 8: Update CLAUDE.md and onboarding docs

`backend/CLAUDE.md` install commands:

```bash
# OSS dev (no enterprise code)
uv pip install -e .

# Full dev (everything)
uv pip install -e . -e ./ee
# or: uv sync --dev   (if [dependency-groups] is set up)
```

Mention both wheels in the "Quick Reference" table. Mention the OSS-only test invocation.

`docs/wiki/` sweep — defer to a separate doc-cleanup branch; not blocking.

---

## Task 9: Final verification and PR

```bash
uv sync --dev
uv run ruff check . && uv run ruff format --check .
uv run mypy .
uv run lint-imports
uv run pytest --ignore=tests/e2e -q
uv run pytest tests/cloud -q
uv run pytest tests/ee -q
uv build --wheel && ( cd ee && uv build --wheel )
```

Open PR:
- Title: `chore(ee): split into pocketpaw + pocketpaw-ee packages (Phase 4)`
- Body lists the two wheels, the install matrix, the CI matrix, and that publishing is Phase 5.

---

## Definition of done

- [ ] `ee/pyproject.toml` exists and `uv build --wheel` in `ee/` produces a valid `pocketpaw_ee-*.whl`
- [ ] Root `uv build --wheel` produces `pocketpaw-*.whl` containing only `pocketpaw/...`
- [ ] `pip install pocketpaw` (no `pocketpaw-ee`) yields a venv where `import pocketpaw` works and `import pocketpaw_ee` fails with ModuleNotFoundError
- [ ] `pip install pocketpaw pocketpaw-ee` yields a venv where everything works
- [ ] All three CI jobs (OSS-only, full, import-linter) are green
- [ ] `enterprise` extra removed from root pyproject; its deps are now in EE's `[project.dependencies]`
- [ ] All entry-points migrated from root pyproject to EE pyproject
- [ ] All test suites at parity
- [ ] `backend/CLAUDE.md` reflects the two-wheel dev workflow
- [ ] PR opened, CI green
