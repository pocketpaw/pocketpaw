# OASIS vendored fork

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
| `__init__.py` | Replaced with a safe wrapper that lazy-imports upstream re-exports | Lets the package be importable on machines without `camel-ai` installed (OSS-only install path). Upstream's verbatim re-exports moved to `_upstream_init.py`. |
| `_upstream_init.py` | New file — verbatim copy of upstream's `__init__.py` with the import-path rewrite above | Preserves provenance; PR 3 wires the re-exports into the engine via the `OASIS_AVAILABLE` flag the new `__init__.py` exposes. |

No upstream functional code was modified. Every algorithm, schema, and
control-flow path comes from upstream as-is. The Apache-2.0 §4(b)
modified-file marker is reserved for *future* per-file behavioural
edits; the import-path rewrite is a packaging-only adaptation and is
documented here at the module level rather than per-file.

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
