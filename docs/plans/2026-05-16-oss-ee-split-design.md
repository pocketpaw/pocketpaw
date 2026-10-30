# PocketPaw Open-Core Split — Design Proposal

**Status:** Draft — sections 1–2 agreed in design session, sections 3–7 proposed direction pending team review
**Date:** 2026-05-16
**Scope:** `backend/` only. Other workspace projects (`paw-enterprise`, `ripple`, `discli`, `soul-protocol`, `soul-site`) are unaffected.

---

## TL;DR

PocketPaw is structurally open-core already (`backend/` core + `backend/ee/`), but the boundary is unenforced: core code imports from `ee/` in seven places, `ee/` ships in every wheel, and the OSS package can't actually be built without enterprise code present. This proposal locks in the commercial line (single-tenant runtime free, multi-tenant cloud paid), splits the codebase into two installable packages (`pocketpaw` MIT + `pocketpaw-ee` FSL), and replaces the seven core→ee imports with an entry-points + Protocols plugin system. Net effect on `ee/`: shrinks from 11 subpackages to 4 (`cloud`, `agent/pocket_specialist`, `audit`, `fleet`); the rest moves into core.

---

## Why now

Three problems with the current state:

1. **No OSS distribution exists.** `pyproject.toml` ships `["src/pocketpaw", "ee"]` in every wheel. Anyone who `pip install pocketpaw` (hypothetically) gets FSL-licensed code on disk. We can't publish to PyPI today.
2. **The boundary leaks.** Core imports from `ee/` in `agents/`, `api/v1/`, `tools/builtin/`, `memory/`, `bootstrap/`, `dashboard/`, `features.py`. Removing `ee/` would break the OSS core.
3. **`ee/` accumulates non-commercial code.** Several `ee/` subpackages (`instinct`, `fabric`, `retrieval`, `automations`, `ripple`, `paw_print`, `widget`, `guards`) are core agent infrastructure, not multi-tenant cloud features. They landed in `ee/` by accident of when they were written. Putting an OSS license on them would expand adoption without affecting what we sell.

---

## Decisions locked

| Dimension | Decision |
|---|---|
| **Commercial line** | OSS = single-tenant agent runtime, channels, tools, local memory/retrieval. EE = multi-tenant cloud: auth, workspaces, team chat, billing, file storage, audit, fleet. (GitLab/Sentry/PostHog pattern.) |
| **Distribution** | One repo, two installable packages: `pocketpaw` (MIT, PyPI) + `pocketpaw-ee` (FSL-1.1 → Apache 2.0 after 4 years, private index or git+https). |
| **IoC pattern** | Core defines `typing.Protocol` extension points. EE registers implementations via Python entry-points. Import-linter forbids `pocketpaw → pocketpaw_ee` at CI time. |
| **Repo visibility** | `ee/` stays in the public repo under FSL. (Same as Sentry, Cal.com, CockroachDB.) |

---

## Section 1 — Subpackage allocation

The "multi-tenant cloud is paid" line applied to every current `ee/` subpackage:

| Current path | Decision | Rationale |
|---|---|---|
| `ee/cloud/` (all of it) | **Stays EE** | Multi-tenant: auth, workspaces, chat, sessions, files, KB, embeddings, composio, realtime, billing, uploads, notifications. |
| `ee/agent/pocket_specialist/` | **Stays EE** | Tightly coupled to cloud pockets + composio. OSS variant possible later if demanded. |
| `ee/audit/` | **Stays EE** | Compliance is an enterprise sell. |
| `ee/fleet/` | **Stays EE** | Multi-agent swarms = team feature. |
| `ee/automations/` | **Moves to core** | Triggers/schedules/workflows are basic agent infra. |
| `ee/fabric/` | **Moves to core** | Knowledge graph / ontology is a building block. |
| `ee/instinct/` | **Moves to core** | Decision pipeline. Without this, OSS is a toy. |
| `ee/retrieval/` | **Moves to core** | RAG primitives. Cloud KB stays EE; local retrieval doesn't. |
| `ee/paw_print/` | **Moves to core** | Agent identity; pairs naturally with Soul protocol (already OSS). |
| `ee/ripple/` (backend bits) | **Moves to core** | The `ripple/` repo is already public. Backend should match. |
| `ee/widget/` | **Moves to core** | UI primitives. |
| `ee/guards/` | **Moves to core** | Policy/guards are runtime concerns every install needs. |

**Net effect on `ee/`:** 11 subpackages → 4 (`cloud`, `agent/pocket_specialist`, `audit`, `fleet`).

**Risk flagged:** some "moves to core" subpackages may have internal dependencies on cloud models or composio. Each move requires confirming no hard cloud dependency before lifting. Where a dependency exists, options are (a) lift the dependency out, (b) accept the subpackage stays EE, or (c) split the subpackage. To be resolved in migration phase per-package.

---

## Section 2 — Repo and package layout

```
backend/
├── LICENSE                      # MIT (root, applies to src/pocketpaw)
├── pyproject.toml               # Defines package: pocketpaw
├── src/
│   └── pocketpaw/
│       ├── extensions.py        # NEW — Protocol definitions for plugin points
│       ├── agents/
│       ├── api/v1/              # OSS routes only
│       ├── bus/adapters/        # all channel transports
│       ├── connectors/
│       ├── tools/builtin/
│       ├── memory/
│       ├── bootstrap/
│       ├── dashboard/
│       ├── features.py          # entry-point discovery, no `from ee` imports
│       ├── automations/         # ← moved from ee/
│       ├── fabric/              # ← moved from ee/
│       ├── instinct/            # ← moved from ee/
│       ├── retrieval/           # ← moved from ee/
│       ├── paw_print/           # ← moved from ee/
│       ├── ripple/              # ← moved from ee/
│       ├── widget/              # ← moved from ee/
│       └── guards/              # ← moved from ee/
└── ee/
    ├── LICENSE                  # FSL-1.1 (exists today)
    ├── pyproject.toml           # NEW — defines package: pocketpaw-ee
    ├── src/
    │   └── pocketpaw_ee/        # NEW namespace (was bare `ee.*`)
    │       ├── entry_points.py  # registers all plugins
    │       ├── cloud/
    │       ├── agent/pocket_specialist/
    │       ├── audit/
    │       └── fleet/
    └── tests/
```

### Why rename `ee.*` → `pocketpaw_ee.*`

1. **Distinguishability.** Bare `ee` namespace collides with other packages and hides provenance in tracebacks.
2. **PyPI publishability.** `pocketpaw-ee` becomes a real distribution name.
3. **Mechanical refactor.** A repo-wide `ee.` → `pocketpaw_ee.` rewrite is easy; the import-linter rule becomes unambiguous.

### Install paths

```bash
# OSS user
pip install pocketpaw

# Enterprise / our own cloud
pip install pocketpaw pocketpaw-ee   # pocketpaw-ee from private index or git+https
```

### Two pyproject files

- `backend/pyproject.toml` — package `pocketpaw`, include `src/pocketpaw/` only. The current `enterprise` extra is removed; its deps move into `pocketpaw-ee`'s own dependencies. Channel/tool extras stay.
- `backend/ee/pyproject.toml` — package `pocketpaw-ee`, depends on `pocketpaw>=X.Y`, includes `ee/src/pocketpaw_ee/` only, declares all entry-points.

---

## Section 3 — Extension-point taxonomy *(proposed, pending review)*

Replace the seven current `core → ee` imports with these `typing.Protocol` extension points. Core defines the protocols; `pocketpaw-ee` registers implementations via entry-points.

Proposed extension-point groups:

| Entry-point group | Protocol | Used by core | EE implementation |
|---|---|---|---|
| `pocketpaw.tools` | `ToolProvider` | `tools/builtin` (replaces `instinct_tools`, `fabric_tools`, `ee.guards.policy` once moved; replaces `ee.cloud.composio` tools after split) | Composio tool factory |
| `pocketpaw.models` | `ModelProvider` (Beanie document registration) | `bootstrap`, `dashboard`, `memory` (replaces direct `ee.cloud.models` imports) | All `pocketpaw_ee.cloud.models` registered |
| `pocketpaw.routes` | `RouteProvider` (FastAPI router) | `api/v1` (replaces `ee.cloud.pockets`, `ee.cloud.chat` routes) | Mounts `pocketpaw_ee.cloud.*` routers |
| `pocketpaw.agent_extensions` | `AgentExtension` | `agents/` (replaces `ee.cloud.chat.agent_service`, `ee.agent.pocket_specialist.mcp_tool`) | Cloud chat agent, pocket specialist |
| `pocketpaw.storage_backends` | `FileStorageBackend` | `connectors/`, `memory/` | S3/GCS backends from `pocketpaw_ee.cloud.files` |
| `pocketpaw.license` | `LicenseValidator` | startup | License key check for EE features |

`features.py` becomes a registry of which extension points have implementations registered, replacing today's `ee`-availability sniffing.

**Open questions for review:**
- Do we want a single composite `Plugin` protocol that exposes multiple capabilities, or many narrow protocols (above)? Recommendation: many narrow protocols — keeps OSS-only test paths simpler.
- How are agent extensions ordered when multiple are registered? Recommendation: explicit `priority: int` field on the Protocol.
- Should `pocketpaw_ee.entry_points.py` register everything at import, or lazy-load per extension point? Recommendation: lazy via `importlib.metadata.entry_points()` — avoids importing all of cloud just to start a single-tenant install with `pocketpaw-ee` accidentally present.

---

## Section 4 — Migrating the seven current violations *(proposed)*

Each known core→ee import and its replacement extension point:

| Core file | Currently imports | Replacement |
|---|---|---|
| `pocketpaw/agents/` | `ee.cloud.composio`, `ee.cloud.chat.agent_service`, `ee.agent.pocket_specialist.mcp_tool` | `pocketpaw.tools` (composio) + `pocketpaw.agent_extensions` (chat agent, pocket specialist) |
| `pocketpaw/api/v1/` | `ee.cloud.pockets`, `ee.cloud.chat` | `pocketpaw.routes` (EE mounts its routers) |
| `pocketpaw/tools/builtin/` | `ee.guards.policy`, `instinct_tools`, `fabric_tools` | After Section 1 moves, these are *core* — no entry-point needed |
| `pocketpaw/memory/` | `ee.cloud.models` | `pocketpaw.models` (EE registers Beanie docs at startup) |
| `pocketpaw/bootstrap/` | `ee.cloud.models` | Same |
| `pocketpaw/dashboard/` | `ee.cloud.models` | Same |
| `pocketpaw/features.py` | `ee` availability check | Replaced by `entry_points()` registry inspection |

After migration, `grep -r "from ee\|import ee" src/pocketpaw/` returns empty. CI enforces this.

---

## Section 5 — CI and tooling *(proposed)*

1. **`import-linter`** config at repo root. Contract: `pocketpaw` (and any submodule) may not import from `pocketpaw_ee`. Run on every PR.
2. **Two-build CI matrix:**
   - Job A: install `pocketpaw` only, run `tests/` (excluding `tests/cloud`, `tests/ee`). Proves OSS works standalone.
   - Job B: install `pocketpaw` + `pocketpaw-ee`, run full test suite. Proves integration.
3. **Pre-commit hook** mirroring import-linter so violations are caught before push.
4. **`pytest` config**: split `testpaths` so OSS tests live under `tests/` and EE tests under `ee/tests/`. Both addressable from repo root.
5. **Release pipeline**: build wheels for both packages; `pocketpaw` publishes to PyPI, `pocketpaw-ee` publishes to private index (or attached to GitHub release for the EE customers list).

---

## Section 6 — License headers and contribution guidelines *(proposed)*

1. Every file under `ee/src/pocketpaw_ee/` gets a header pointing to `ee/LICENSE` (FSL-1.1). Helps drive-by readers know what they're looking at.
2. `CONTRIBUTING.md` gains a section: "Where does my change go?" — decision tree distinguishing OSS-eligible from EE-only changes. External contributors are asked to submit OSS contributions only.
3. `README.md` at repo root gets a short "Editions" section linking to both licenses.
4. A `NOTICE` file at repo root if any moved code needs attribution.

---

## Section 7 — Phased migration plan *(proposed)*

Five phases, each independently shippable and revertable.

**Phase 1 — Rename `ee.*` → `pocketpaw_ee.*`** *(mechanical, ~1 day)*
- Repo-wide rewrite. Update imports, conftest paths, entry-point references.
- Keep one wheel; no behavioural change. Ships immediately.

**Phase 2 — Move 8 subpackages from `ee/` to `src/pocketpaw/`** *(per-package, ~1–3 days each)*
- For each of `automations, fabric, instinct, retrieval, paw_print, ripple, widget, guards`:
  - Audit for cloud/composio dependencies.
  - `git mv`, fix imports, run tests.
  - If hard cloud dep found, decide: lift, leave, or split.
- After this phase, `ee/` contains only the four enterprise subpackages.

**Phase 3 — Introduce extension points** *(~1 week)*
- Add `src/pocketpaw/extensions.py` with Protocols.
- Wire core to use the registry instead of direct imports.
- Register existing EE implementations via entry-points in `ee/pyproject.toml`.
- Remove the seven `from ee` imports from core.
- Add `import-linter` rule + CI job A (OSS-only build).

**Phase 4 — Split pyproject files** *(~2 days)*
- Create `ee/pyproject.toml` for `pocketpaw-ee`.
- Remove `ee/` from core wheel inclusion.
- Verify `pip install pocketpaw` produces a working OSS install with no `pocketpaw_ee` on disk.

**Phase 5 — Publish** *(operational, dates per release plan)*
- Cut `pocketpaw 1.0` to PyPI.
- Cut `pocketpaw-ee 1.0` to private index.
- Update `paw-enterprise` (desktop client) and any deployment configs that referenced the old `ee.*` namespace.
- Update marketing site / docs to advertise the OSS edition.

Each phase ends in a green CI and a tagged release. Rolling back any phase is a single revert.

---

## Risks and non-goals

**Risks:**
- A moved subpackage turns out to have a hard cloud dependency we missed → Phase 2 stalls per-package; decide lift/leave/split.
- External contributors get confused by the boundary → mitigate with CONTRIBUTING.md and clear PR template.
- `paw-enterprise` and other clients break on Phase 1 rename → coordinate the rename across all workspace repos in the same week.
- Composio licensing — confirm Composio terms permit redistribution in either edition before placing it.

**Non-goals:**
- Re-licensing anything outside `backend/`. `ripple/`, `soul-protocol/`, `soul-site/` are independent.
- Building a paid feature-gating runtime ("license server") in this round. The license-key check in Section 3 is for activation only, not metering.
- Changing the commercial line later. If we decide more goes free (or more goes paid) in future, that's a separate proposal.

---

## Decisions needed from the team

1. Confirm Section 1 allocation (especially: `pocket_specialist`, `automations`, `instinct` — any to flip?).
2. Approve the `ee.*` → `pocketpaw_ee.*` rename.
3. Approve extension-point taxonomy in Section 3 or propose changes.
4. Approve phased plan in Section 7 or propose alternate sequencing.
5. Confirm distribution channel for `pocketpaw-ee` (private PyPI index vs git+https vs GitHub releases).

Once these are settled, the next step is producing a per-phase implementation plan with file-level diffs.
