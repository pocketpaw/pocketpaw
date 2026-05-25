# Foresight

> Rehearse the future before living it.

Foresight is the IS-era primitive for forward-simulating real Paw agents
against a real org's Fabric snapshot under a real Instinct policy. One
engine, seven simulation sub-types, projected Decisions that land in
the Decision Graph (RFC 07) as forward-precedents.

The full design lives in RFC 08
(`temp/brain-storming/to-build/08-foresight-module-rfc.md`).
The v0.1 cut and internal handover live at
`pocketPaw/docs/internal/2026-05-foresight.md`.

## v0.2 scope (this PR)

PR 2 lands the three items PR 1 deferred:

| In (PR 2) | Out (later PRs) |
|----|-----|
| Vendored OASIS fork at `substrate/oasis/` (~6000 LOC, upstream SHA `46cdc8d`) | Calibration loop (PR 3) |
| `camel-ai==0.2.90` optional dep (`pocketpaw-ee[foresight]`) | Retroactive backtest gate |
| `ClaudeCodeBackend.run(messages, response_format, tools)` — CAMEL surface | UI rail (5 panels) |
| `LiteLLMFallbackBackend` stub (PR 3 wires the proxy) | Tier mix beyond stub config |
| `SoulSeededPersona(paw_agent=...)` — wraps a real `PawAgent` | Sub-types beyond Decision Forecast |
| `SoulSeededPersona.from_paw_agent(...)` convenience factory | vLLM hosting |
| `OASIS_AVAILABLE` flag on the substrate package | Go port (long-horizon) |

## v0.1 scope (PR 1, merged)

| In | Out |
|----|-----|
| Module scaffold (`world.py`, `persona.py`, `llm/adapter.py`, `scenarios/`, `api/`) | Calibration loop |
| OASIS substrate placeholder + LICENSE/NOTICE | Retroactive backtest gate |
| `ForesightWorld` Fabric-backed stub (in-memory) | UI rail (5 panels) |
| `SoulSeededPersona` with OCEAN drift + memory-tier stub | Tier mix beyond stub config |
| `ClaudeCodeBackend` (~120 LOC) + `DeterministicFakeBackend` | Sub-types beyond Decision Forecast |
| One scenario template (`decision_forecast.yaml`) | vLLM hosting |
| REST: `POST /scenarios`, `GET /runs/{id}` | Go port (long-horizon) |
| Tests: world stub, persona, adapter, scenario smoke-test | |

## Run the smoke test

```bash
# From the worktree root
cd /Users/prakash-1/Documents/paw-workspace/pocketPaw/.claude/worktrees/foresight-v01
uv sync --dev --group ee
uv run pytest tests/ee/foresight/ -v
```

A passing run looks like:

```
tests/ee/foresight/test_world.py ............    [ 50%]
tests/ee/foresight/test_persona.py ........     [ 70%]
tests/ee/foresight/test_adapter.py ......       [ 85%]
tests/ee/foresight/test_scenario_smoke.py .. .  [100%]
```

## Use the runner programmatically

```python
import asyncio
from pocketpaw_ee.foresight import run_scenario, ScenarioConfig
from pocketpaw_ee.foresight.scenarios.runner import PersonaSpec
from pocketpaw_ee.foresight.persona import OceanDrift

config = ScenarioConfig(
    name="my-first-forecast",
    sub_type="decision_forecast",
    n_ticks=3,
    personas=[
        PersonaSpec(name="tenant-a", role="tenant", ocean=OceanDrift(agreeableness=0.5)),
        PersonaSpec(name="approver-prakash", role="approver", ocean=OceanDrift(conscientiousness=1.2)),
    ],
)
result = asyncio.run(run_scenario(config))
print(result.as_wire_dict())
```

## Load from YAML

```python
from pocketpaw_ee.foresight import run_scenario, ScenarioConfig

config = ScenarioConfig.from_yaml(
    "ee/pocketpaw_ee/foresight/scenarios/decision_forecast.yaml"
)
result = asyncio.run(run_scenario(config))
```

## Module layout

```
ee/pocketpaw_ee/foresight/
├── __init__.py              ← public surface (lazy re-exports)
├── README.md                ← this file
├── world.py                 ← ForesightWorld + WorldSnapshot
├── persona.py               ← SoulSeededPersona + OceanDrift + MemoryTierStub
├── llm/
│   ├── __init__.py
│   └── adapter.py           ← ClaudeCodeBackend + DeterministicFakeBackend
├── scenarios/
│   ├── __init__.py
│   ├── runner.py            ← ScenarioConfig + run_scenario + RunResult
│   └── decision_forecast.yaml
├── api/
│   ├── __init__.py
│   └── run_store.py         ← in-memory run store (v0.1 stand-in for Mongo)
└── substrate/
    └── oasis/
        ├── LICENSE          ← upstream Apache-2.0 (Copyright 2023 CAMEL-AI.org)
        ├── NOTICE           ← attribution per Apache-2.0 §4(d)
        └── README-FORK.md   ← v0.1 placeholder; src-copy lands in follow-up PR

ee/pocketpaw_ee/cloud/foresight/
├── __init__.py
├── dto.py                   ← CreateScenarioRequest + ScenarioRunResponse
└── router.py                ← POST /scenarios + GET /runs/{id}
```

## What lands next (v1.0 ramp)

| PR | Cut |
|----|-----|
| ~~2~~ | ~~OASIS src-copy at `substrate/oasis/`~~ — shipped (this PR) |
| 3 | OASIS wired into `ForesightWorld.tick()` + `PawSocialAgent(SocialAgent)` subclass + calibration loop scaffold (prediction buffer + pair-against-reality + score) |
| 4 | Backtest gate at onboarding (retroactive run, accuracy report) |
| 5 | Three sub-types complete (Market Sim, Org Change Rehearsal) |
| 6 | UI rail (Scenarios / Live / Results / Aggregate / Insights) |
| 7 | Cloud `mount_cloud` wiring + Beanie persistence + service module |

The exact ordering is captain's call. See `docs/internal/2026-05-foresight.md`
for the rationale.
