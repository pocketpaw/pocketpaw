# Foresight v0.1 — internal handover

Created: 2026-05-25 (feat/foresight-v01-scaffold). Companion to RFC 08
at `temp/brain-storming/to-build/08-foresight-module-rfc.md`. This is
the post-PR-1 status note for the next crew to pick up.

## What this PR does

Lands the Foresight module scaffold and the minimum end-to-end loop
RFC 08 §13.1 calls v0.1 — but as the FIRST PR of that cut, intentionally
narrower than the 30-day v0.1 envelope. Captures the public surface
shape (World / Persona / Backend / Scenario / RunResult / REST) so
follow-up PRs can fill in bodies without churning APIs.

Files shipped:

- `ee/pocketpaw_ee/foresight/__init__.py` — lazy-importing public surface
- `ee/pocketpaw_ee/foresight/world.py` — `ForesightWorld` + `WorldSnapshot`
- `ee/pocketpaw_ee/foresight/persona.py` — `SoulSeededPersona` + `OceanDrift` + `MemoryTierStub`
- `ee/pocketpaw_ee/foresight/llm/adapter.py` — `ClaudeCodeBackend` + `DeterministicFakeBackend`
- `ee/pocketpaw_ee/foresight/scenarios/runner.py` — `ScenarioConfig` + `run_scenario` + `RunResult`
- `ee/pocketpaw_ee/foresight/scenarios/decision_forecast.yaml` — one scenario
- `ee/pocketpaw_ee/foresight/api/run_store.py` — in-memory run store
- `ee/pocketpaw_ee/foresight/substrate/oasis/{LICENSE, NOTICE, README-FORK.md}` — vendoring placeholder
- `ee/pocketpaw_ee/cloud/foresight/{dto.py, router.py}` — cloud surface (not yet mounted)
- `tests/ee/foresight/` — targeted tests on every touched module
- `ee/pocketpaw_ee/foresight/README.md` — module quick-start

## RFC ambiguities resolved in this PR

### 1. Package layout — `ee/pocketpaw_ee/foresight/`, not `ee/foresight/`

RFC 08 §6.1 specifies the engine at `ee/foresight/`. The actual ee/
package on dev is `ee/pocketpaw_ee/` (open-core split per
`pocketpaw/CLAUDE.md` "Two packages" section). All RFC §6.1 path
references map to `ee/pocketpaw_ee/foresight/`.

The cloud surface mirrors `ee/pocketpaw_ee/cloud/<entity>/` — the RFC's
`ee/cloud/foresight/router.py` lives at
`ee/pocketpaw_ee/cloud/foresight/router.py`.

### 2. OASIS src-copy deferred to PR 2

RFC §6.1 says vendor the ~3,500 LOC OASIS fork at
`ee/pocketpaw_ee/foresight/substrate/oasis/`. v0.1 ships only the
LICENSE + NOTICE + README-FORK.md placeholder. Three reasons:

- The first PR is a SCAFFOLD; ~3,500 LOC of dropped-in upstream would
  dominate the diff and obscure review of our own engineering.
- Apache-2.0 vendoring needs an explicit captain-level call on the
  copy-vs-submodule axis (RFC §6.1 prefers src-copy; the captain may
  want to revisit). v0.1's placeholder leaves both paths open.
- The v0.1 engine surfaces are protocol-shaped, not subclass-shaped —
  `ForesightWorld` doesn't inherit from `oasis.social_platform.Platform`,
  and `SoulSeededPersona` doesn't inherit from `oasis.social_agent.SocialAgent`.
  So the loop works end-to-end *without* OASIS on disk. The v1.0 swap
  is "subclass + use, don't rewrite."

PR 2 will: vendor the OASIS modules listed in RFC §6.3 ("What we inherit"),
add CAMEL as a PyPI dep (`camel-ai==0.2.78`), and switch `SoulSeededPersona`
to subclass `oasis.social_agent.SocialAgent` while keeping the
`decide(observation)` entrypoint.

### 3. Backend surface — `complete(prompt)` not full `BaseModelBackend.run`

RFC §6.4 specifies `BaseModelBackend.run(messages, response_format, tools)`.
v0.1 ships a narrower `await backend.complete(prompt: str) -> str` because
that's the surface the persona's `decide` needs and it stays usable
without CAMEL on disk (CAMEL lands with OASIS in PR 2).

v1.0 promotes `complete` to a convenience wrapper over the full
BaseModelBackend.run shape so CAMEL's tool / response_format machinery
becomes available.

### 4. Cloud router not yet mounted

RFC §13.1 lists "router under `ee/cloud/foresight/`" in the v0.1 build.
This PR ships the router but does NOT add it to
`ee/pocketpaw_ee/cloud/__init__.py:mount_cloud`. Reason: the cloud
4-file rule (per `pocketPaw/CLAUDE.md` "pocketpaw_ee/cloud Code Rules"
section) wants `domain.py + service.py` alongside `router.py`, and
those land cleanly in PR 7 when Beanie persistence replaces the
in-memory `RunStore`. Mounting the router now would establish a
public URL bound to an in-memory store — a worse drift surface than
the deferred mount.

The router is fully testable today via `TestClient(app).include_router(
foresight_router)` — see `tests/ee/foresight/` patterns when we add
the API test in PR 2.

### 5. Persona ↔ `PawAgent` deferred to v1.0

RFC §7.2 says the persona wraps a real `PawAgent` (the live runtime
agent at `src/pocketpaw/agents/`). v0.1's `SoulSeededPersona` instead
takes a generic backend handle. Reason: PR 1 stays inside `ee/foresight/`
and `ee/cloud/foresight/`; touching `src/pocketpaw/agents/` would
expand the diff and cross the OSS/EE boundary — which is fine, but
not the right move for "scaffold + minimum loop."

PR 2 will swap the constructor to take a `PawAgent` and delegate
`decide()` to the agent's runtime decision path, preserving the fidelity
floor the captain locked.

## What was deferred (vs RFC §13.1's full v0.1)

| Deferred | Why | Lands in |
|----------|-----|----------|
| OASIS src-copy (~3,500 LOC) | Audit / review surface | PR 2 |
| `PawAgent`-wrapped persona | Cross-boundary diff | PR 2 |
| Tier-pool builder (5/15/80) | Needs CAMEL + multiple backends | PR 3 |
| LiteLLM fallback | Needs CAMEL on disk | PR 3 |
| Aggregator primitives | No useful signal at N=5 | PR 4 |
| `ProjectedDecision` emission | Needs RFC 07 Decision projection wired | PR 5 |
| Cloud `mount_cloud` wiring | Needs Beanie + service module | PR 7 |
| `pyproject.toml` for foresight | Stays inside `ee/pocketpaw_ee/` for now | PR 7 |

The v0.1 30-day envelope still lands inside the captain's 30-day
window; this PR carves off ~3-4 agent-hours of the 8-12 budget.

## Open follow-ups for the next PR

1. **PR 2 (substrate vendoring).** Captain to confirm src-copy vs git
   submodule before the actual vendoring agent runs. Pin upstream at
   `46cdc8d` (verified 2026-05-25 still upstream main HEAD).
2. **PR 3 (calibration scaffold).** Stand up `prediction_buffer.py` +
   `pair_against_reality.py` + `score.py` per RFC §9. Hook to RFC 07's
   `decision.outcome_attached` event when that event ships.
3. **PR 7 (cloud wiring).** Add `domain.py` + `service.py` per the
   cloud 4-file rule, swap the in-memory store for Beanie, add to
   `mount_cloud`, write `tests/cloud/test_foresight_router.py`.
4. **Open question — `ProjectedDecision` storage.** RFC §7.7 says
   "projected_decisions mirrors decisions schema." With RFC 07's
   `DecisionProjection` not yet shipped, we need to decide whether
   to spin up a parallel collection in PR 5 or wait for RFC 07's
   landing PR. Captain's call.
5. **Open question — variation engine.** RFC §7.2 specifies an
   OCEAN-slider sampler that produces persona drift across a
   population. v0.1 ships the per-persona OceanDrift dataclass but
   not the sampler. Lands cleanly in PR 3 alongside the calibration
   scaffold — same N=100-1000 scale boundary.

## Test coverage at v0.1

- `tests/ee/foresight/test_world.py` — registration, single-tick
  execution, snapshot shape, error capture, action application,
  concurrent fan-out.
- `tests/ee/foresight/test_persona.py` — OceanDrift rendering,
  MemoryTierStub roundtrip, decide() happy path, decide() backend
  error capture, response parser tolerance.
- `tests/ee/foresight/test_adapter.py` — `DeterministicFakeBackend`
  cycle, `ClaudeCodeBackend` factory injection, terminal-event
  draining for the three SDK shape variants v0.1 handles.
- `tests/ee/foresight/test_scenario_smoke.py` — end-to-end
  `run_scenario` against `decision_forecast.yaml`; verifies the
  loop closes and the RunResult is JSON-serializable.

No `tests/cloud/test_foresight_router.py` in this PR — the router
isn't mounted yet, so the canonical test pattern (via
`pocketpaw_ee.cloud.api:create_app`) doesn't fit. Router tests
land in PR 7 with the mount.

## Performance budget (informational)

The DeterministicFakeBackend loop runs at ~10 ms per persona-tick on
an M2 Pro (single core, no I/O). 5 personas × 1 tick smoke run
completes in well under 100 ms. The semaphore in `ClaudeCodeBackend`
is set to the v0.1 Sonnet-tier default of 128, sized for the v1.0
fan-out target (10K personas × p=0.1 activation = 1000 concurrent
calls, capped to 128 by the semaphore).

## Cross-references

- RFC 08 — `temp/brain-storming/to-build/08-foresight-module-rfc.md`
- RFC 07 — `temp/brain-storming/to-build/07-decision-graph-query-layer-rfc.md`
  (projected Decisions will land here)
- `pocketPaw/CLAUDE.md` — cloud rules section governs the cloud surface
- `pocketPaw/docs/internal/2026-05-mission-control.md` — mirror format
  this doc follows
- Soul memories: search shared soul for "Foresight pocketpaw ee" and
  personal soul for "RFC 08 Foresight OASIS implementation gates"
