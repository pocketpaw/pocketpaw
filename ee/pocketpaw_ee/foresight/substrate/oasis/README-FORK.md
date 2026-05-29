# OASIS vendored fork

Updated: 2026-05-25 (feat/foresight-v03-calibration) — RFC 08 PR 3.
Updated: 2026-05-25 (feat/foresight-v02-oasis-camel-paw) — RFC 08 v0.2 PR.
Created: 2026-05-25 (feat/foresight-v01-scaffold) — RFC 08 v0.1 first PR.

## What is here

This is the **vendored fork** of
[camel-ai/oasis](https://github.com/camel-ai/oasis) at upstream
commit `46cdc8d31496b93706ce3d95d7eddc637c0678e2` (master branch,
fetched 2026-05-25).

The fork lives at `ee/pocketpaw_ee/foresight/substrate/oasis/` and is
under our PR review process from day one. We do NOT pull from upstream
automatically — see "Drift policy" below.

### What was copied verbatim

- `LICENSE` (Apache-2.0, Copyright 2023 @ CAMEL-AI.org)
- `clock/` (Clock primitive)
- `environment/` (OasisEnv, EnvAction, LLMAction, ManualAction, make)
- `social_agent/` (SocialAgent, AgentGraph, agents_generator)
- `social_platform/` (Platform, Channel, database, recsys, schema/*.sql, typing)
- `testing/` (show_db debugging helper)

### What we modified

| File / region | Change | Why |
|---|---|---|
| All `*.py` files | `from oasis.X` → `from pocketpaw_ee.foresight.substrate.oasis.X` (mechanical rewrite) | Upstream uses absolute imports rooted at top-level `oasis`. Vendoring inside our namespace package requires the rewrite. No semantic change. |
| `__init__.py` | Replaced with a tiered import wrapper (`OASIS_AVAILABLE` for the core / `OASIS_RECSYS_AVAILABLE` for the recsys+torch tier) | Lets the package be importable without torch / pandas / neo4j. Upstream's verbatim re-exports moved to `_upstream_init.py`. |
| `_upstream_init.py` | New file — verbatim copy of upstream's `__init__.py` with the import-path rewrite above | Preserves provenance; the recsys tier loads from here via `oasis.__init__.py`'s second try block. |
| `social_platform/__init__.py` | **(PR 3)** Made `Platform` re-export lazy via module-level `__getattr__` | Upstream eagerly imports `.platform` → `.recsys` → `torch`. Foresight per RFC 08 §6.2 drops Platform entirely (replaced with `ForesightWorld`). Lazy import keeps `Channel` cheap. |
| `social_agent/__init__.py` | **(PR 3)** Made `generate_*_agent_graph` / `generate_agents_100w` re-exports lazy via module-level `__getattr__` | Upstream eagerly imports `agents_generator` → `pandas`. Foresight v0.1 uses `.soul`-file persona pools, not CSV imports. |
| `social_agent/agent.py` | **(PR 3)** Guarded the eager `FileHandler` setup with try/except + `makedirs(exist_ok=True)` | Upstream unconditionally writes to `./log/social.agent-<ts>.log` at import; fails in read-only sandboxes (CI, /tmp). Guard preserves stream-handler logging when file logging fails. |
| `social_agent/agent_graph.py` | **(PR 3)** Lazy-imported `neo4j.GraphDatabase` inside `Neo4jHandler.__init__` | Upstream eagerly imports it; AgentGraph defaults to `igraph` (the v0.1 backend), so the neo4j driver stays optional. |
| `environment/env.py` | **(PR 3)** Same log-dir guard as `social_agent/agent.py` | Same rationale. |

The Apache-2.0 §4(b) modified-file markers are at the top of each PR-3
file listed above, with a "Modified by PocketPaw, 2026-05-25" notice
plus the per-change justification. PR 2's mechanical import-path
rewrite is documented at the module level (no behavioural change).

### What was NOT copied (intentional)

- `examples/`, `docs/`, `assets/`, `test/`, `data/`, `generator/`,
  `visualization/`, `deploy.py` — not needed for our engine. The
  upstream README references them; we link out instead of vendoring.
- `pyproject.toml`, `poetry.lock` — we manage deps via our own
  `ee/pyproject.toml`. `camel-ai==0.2.78` is added there to match
  upstream's pin.
- `.github/`, `.container/`, `.pre-commit-config.yaml`,
  `CONTRIBUTING.md` — upstream project infra, not ours.

## What this enables

PR 2 (this PR) lands the substrate but does NOT yet exercise it. The
v0.1 engine surfaces (`ForesightWorld`, `SoulSeededPersona`,
`ClaudeCodeBackend`) remain protocol-shaped, so the smoke loop still
runs without touching this code. PR 3 will:

1. Replace `SoulSeededPersona`'s direct backend call with a
   `PawSocialAgent(SocialAgent)` subclass that uses
   `substrate.oasis.social_agent.SocialAgent` as its base.
2. Wire `substrate.oasis.environment.OasisEnv.step` into
   `ForesightWorld.tick()` (or replace `tick()` with a thin shim).
3. Plug `substrate.oasis.AgentGraph` into the relationship layer.

We will NOT use `substrate.oasis.social_platform.Platform` — RFC 08
§6.2 explicitly replaces it with `ForesightWorld` (Fabric-backed).

## Known issues at vendor time

| Issue | Severity | Plan |
|---|---|---|
| Upstream pins `camel-ai==0.2.78` (released 2025-10-15); CAMEL is at 0.2.90 stable as of 2026-05-25. | Low | We mirror the pin in `ee/pyproject.toml`. v1.0 may rebase forward; v0.1 stays on what OASIS was tested against. |
| Upstream requires `python = ">=3.10.0,<3.12"`. PocketPaw runs on Python 3.11+. | Low | We are inside the supported window. Drop `<3.12` once we move off OASIS (long-horizon). |
| Some OASIS modules pull in heavy optional deps (`igraph`, `cairocffi`, `sentence-transformers`, `neo4j`). | Medium | We do NOT add those to our deps at v0.2. Anyone touching the affected subpackages in PR 3+ adds them then; today's smoke test only validates the package is importable as a namespace. |
| `social_platform/recsys.py` requires the SQLite recsys schema OASIS was designed around. | N/A (not used) | RFC 08 §6.2 drops the TWHIN-BERT recommender. We will never call into `recsys.py`. |
| Logger name `oasis.env` is left as-is (string literal, not an import). | None | Pure log namespace; can rename in PR 3+ if desired. |

If a PR 3+ wiring path uncovers a bug in vendored code that blocks
forward motion, fix it in a dedicated PR against `substrate/oasis/`
with a justification line in the PR body (per the Drift policy below).

## License

OASIS is Apache-2.0 with no CLA and no trademark blockers. The
`LICENSE` file is a verbatim copy of
`github.com/camel-ai/oasis@46cdc8d/LICENSE`. The `NOTICE` file in this
directory carries the attribution required by Apache-2.0 §4(d).

When we modify a vendored OASIS file behaviourally (not the mechanical
import-path rewrite above), the modified file will carry a
"Modified by PocketPaw, YYYY-MM-DD" notice at the top per Apache-2.0
§4(b). PR 3 is expected to start that practice when it wires in the
substrate proper.

## Why a fork (not a pip dep)

Audit-locked reasons, reproduced from RFC 08 §6.1:

- Upstream is soft-dormant (last meaningful commit Mar 13, 2026; 13 open
  PRs unmerged at audit time).
- We need 100% control over the modules we use — bug fixes and API
  extensions land in our fork on our review cadence.
- The audit cap is ~3,500 LOC of vendored code, auditable end-to-end.
- CAMEL itself stays a PyPI dep (`camel-ai==0.2.78`, see
  `ee/pyproject.toml`) — the `BaseModelBackend` protocol comes from
  there, not from OASIS.

## Drift policy

- We **do not auto-pull** from upstream. Vendoring is one-way.
- We **monitor** the upstream commit log monthly. If a useful fix
  lands, we cherry-pick into the fork via an explicit PR; if upstream
  stays dormant, we are still operational.
- The fork is treated as **frozen at first**, with explicit edits over
  time. Any future change to `substrate/oasis/` requires a
  justification line in the PR description (e.g. "raises semaphore
  default from 128 to 1024 for vLLM pool runs").
- **Trademark.** Apache-2.0 has no trademark clause on the substrate.
  We ship our product as **Paw Foresight**, never as "OASIS" or
  "OASIS-powered." `LICENSE` and `NOTICE` retain CAMEL-AI authorship
  per Apache-2.0 §4(d).
