---
name: foresight-create-sim
description: |
  Create, edit, and run Foresight scenarios via the workspace's typed
  MCP tools (with a curl fallback for power users / SDKs without the
  in-process server). Triggered when the user asks to rehearse /
  simulate / project / forecast / branch a decision — "rehearse the
  renewal", "what if we cut price 10%", "simulate the org change before
  we announce", "forecast Q3 churn", "branch the launch decision". The
  skill teaches the chat agent how to discover existing scenarios,
  synthesize the YAML body, save it via the ``save_scenario`` MCP tool,
  and (on explicit user confirmation) execute the run via
  ``run_scenario``. Workspace context is automatic — no env vars, no
  headers.
---

# Foresight Scenario Workflow

You're being asked to drive PocketPaw's **Foresight** module — the
population-scale decision rehearsal engine (RFC 08). The user wants to
**rehearse a decision before they make it**: model the personas, run
synthetic ticks, surface a forecast plus per-anchor projected outcomes,
and (optionally) compare against a known historical anchor.

This skill teaches you the 4-phase workflow and the YAML schema. It does
NOT replace the YAML editor in the UI — power users still edit there
directly. Reach for this skill only when the user is having the
conversation **in chat**.

## When to use

**Trigger phrases:** "rehearse", "simulate", "project", "forecast",
"branch", "what if", "model the impact of", "play forward", "stress test"
— combined with a decision the user is about to make or a change they're
planning.

  Examples that DO match:
  - "rehearse the price increase to enterprise customers"
  - "what if we push the renewal deadline back two weeks?"
  - "simulate the org change before we announce it"
  - "forecast how the founding team will react to the rebrand"
  - "branch the Q3 hiring plan — three personas accept, two resist"

**When NOT to use:**
- The user is editing an existing pocket on the canvas — that's the
  ``pocketpaw-edit-pocket`` skill, not this one.
- The user opens the **YAML editor panel** directly (paw-enterprise
  Foresight admin → New Scenario). The UI's Monaco buffer is the
  power-user path; the chat surface is the natural-language path. They
  cooperate via the same REST endpoints.
- The user asks for **historical accuracy** ("how accurate were our
  forecasts last quarter?") — that's the Aggregate / Insights panel
  read path, not the scenario create path.
- The user wants a **dashboard / canvas** of metrics — that's the
  ``pocketpaw-create-pocket`` skill.

## MCP tool reference (PREFER THESE)

The dashboard runs an in-process MCP server (``pocketpaw_foresight``)
that closes over the chat session's workspace id. **Always use these
tools instead of curl** — they cannot save to the wrong workspace
because the workspace context is read from the active chat stream, not
from env vars or headers you have to plumb yourself.

  - ``mcp__pocketpaw_foresight__list_scenarios({limit?, offset?, sub_type?})``
    — list saved custom scenarios in the active workspace. Returns
    ``{items[], total, limit, offset, has_more}``.
  - ``mcp__pocketpaw_foresight__get_scenario({scenario_id})`` — full
    detail (yaml_body + parsed_meta).
  - ``mcp__pocketpaw_foresight__save_scenario({name, sub_type,
    yaml_body, description?})`` — create. Returns the new scenario
    object with its ``id``. **CAPTURE THE ID** for the run step.
  - ``mcp__pocketpaw_foresight__update_scenario({scenario_id, name,
    sub_type, yaml_body, description?})`` — PUT-style full replace.
  - ``mcp__pocketpaw_foresight__delete_scenario({scenario_id})`` —
    remove. Ask the user first; no undo.
  - ``mcp__pocketpaw_foresight__run_scenario({name, custom_scenario_id,
    route_to_instinct?, precedent_seed?})`` — execute a saved scenario.
    ``custom_scenario_id`` is REQUIRED (the chat surface only supports
    the saved-scenario path so the run stays re-runnable from the
    dashboard).
  - ``mcp__pocketpaw_foresight__list_runs({limit?, offset?})`` —
    recent runs, newest first.
  - ``mcp__pocketpaw_foresight__get_run({run_id})`` — single run with
    full result blob.

Each tool returns either a JSON body (success) or an MCP error envelope
carrying the cloud error code + message (e.g.
``foresight.invalid_yaml``, ``foresight.sub_type_mismatch``,
``foresight_custom_scenario.not_found``). Surface those codes to the
user verbatim — they name the field to fix.

## Read tools (results, accuracy, insights)

Three additional tools cover the *result* side — what the run produced,
how accurate the workspace has been, and what's worth flagging. Same
workspace-context guarantee as the write tools (closed over the chat
stream's workspace id; no env vars, no headers).

  - ``mcp__pocketpaw_foresight__list_projected_decisions({run_id,
    anchor_id?, limit?, offset?})`` — per-anchor, per-persona verdicts
    for a single run. Returns ``{items[], total, limit, offset,
    has_more}``. 404 (``foresight_run.not_found``) for unknown or
    cross-tenant ids.

    **When to use:** "show me the projections for run X", "what did
    each persona decide", "break down the renewal sim by anchor".

  - ``mcp__pocketpaw_foresight__get_aggregate({window_days?})`` —
    workspace-level rolling accuracy + confidence drift + modal outcome
    distribution over the trailing window. Defaults to 30 days, caps at
    90; above the cap surfaces ``foresight.invalid_window``. Empty
    workspaces return zeros + empty arrays (never 404).

    **When to use:** "how accurate were we", "did we predict X
    correctly", "show me our hit rate". Not the same surface as the
    Backtest panel — backtests are per-scenario; this is the
    workspace-wide rollup.

  - ``mcp__pocketpaw_foresight__get_insights({})`` — narrative insights
    synthesized over recent PredictionRecords + backtests (accuracy
    drops, persona outliers, tier imbalances, threshold misses). Empty
    workspaces yield ``items=[]`` — the synthesizer fires no rows when
    no patterns match. Each item carries ``severity`` (``info`` |
    ``warning`` | ``critical``) — surface it verbatim so the user sees
    the same colour the dashboard renders. Response carries
    ``synth_source`` (``"pattern"`` | ``"llm"``) — surface this when
    reporting findings so the user knows whether they came from the
    deterministic five-rule synthesizer or the v1.0 LLM synth.

    **When to use:** "what mattered in the last run", "explain the
    insights", "anything worth flagging".

## Backtest reads + gate state

Three more tools cover the backtest *read* side and the onboarding
gate. **These three tools are read-only.** Backtest creation still
ships through the dashboard Aggregate panel — it needs ground-truth
anchors the chat surface can't reliably produce. The hard rule below
("NEVER call ``/api/v1/foresight/backtests`` from this skill") stays
in force; these tools don't contradict it — they expose only the
list / get / gate reads.

  - ``mcp__pocketpaw_foresight__list_backtests({limit?, offset?})`` —
    trailing list of past backtests, most recent first. Each item
    carries ``id, scenario_name, status, gate_decision, threshold,
    created_at``. Pagination shape mirrors ``list_runs``.

    **When to use:** "did we backtest yet", "show me past backtests",
    "when was the last backtest".

  - ``mcp__pocketpaw_foresight__get_backtest({backtest_id})`` — single
    backtest with the full result blob, gate decision, and per-anchor
    calibration. Find ids via ``list_backtests``. 404
    (``foresight_backtest.not_found``) for unknown / cross-tenant ids.

    **When to use:** "what was the gate decision on backtest X", "show
    me the details of that backtest", "why did backtest X fail the
    gate".

  - ``mcp__pocketpaw_foresight__get_onboarding_gate({})`` — workspace's
    onboarding gate state (unlock status + reason + last backtest
    reference + effective threshold). Empty workspaces return
    ``unlocked=False, reason='no_backtest'`` (not a 404). ``reason`` is
    one of ``no_backtest`` | ``in_flight`` | ``below_threshold`` |
    ``unlocked`` — surface it verbatim.

    **When to use:** "are we unlocked", "what's the gate", "why is
    foresight gated".

If the user asks to *create* a backtest, redirect them to the
dashboard's Aggregate panel — that surface anchors the historical
pairing the engine needs.

## The four-phase workflow

Every interaction with Foresight from chat follows this loop:

  1. **Discover** — does a scenario like this already exist?
  2. **Synthesize** — build (or modify) the YAML body.
  3. **Save** — call ``save_scenario`` (new) or ``update_scenario``
     (replace).
  4. **Run** — call ``run_scenario`` with ``custom_scenario_id``, ONLY
     if the user explicitly confirms.

Skipping phases is the most common failure mode. If you jump straight to
"run" without saving, you can't re-run the same scenario. If you skip
"discover" and create a duplicate, the workspace fills with near-copies.

## STEP 1 — Discover (list before you create)

Always list the workspace's saved scenarios first. The user may have
already built something close to what they want.

```
mcp__pocketpaw_foresight__list_scenarios({"limit": 20})
```

Returns ``{items, total, limit, offset, has_more}``. Each item carries
``id, name, sub_type, num_personas, num_ticks, updated_at``.

**Decision branch:**
- A close match exists → ask the user "I found `<name>` from `<date>`.
  Edit that, or start fresh?" Don't silently overwrite.
- Nothing close → proceed to STEP 2 with a new scenario.

## STEP 2 — Synthesize the YAML

Foresight scenarios are YAML documents the engine parses into a
``ScenarioConfig``. The schema below is the v1.0 wire grammar. Anything
NOT in this list is silently ignored by the loader (intentional —
forward-compat for v2.0 fields).

### Required fields

  - ``name`` (string, ≤120 chars) — human label for the scenario.
  - ``sub_type`` (enum) — one of:
      - ``decision_forecast`` — single-decision projection (renewals,
        approvals, go/no-go gates).
      - ``market_sim`` — competitive market dynamics across segments
        (pricing, launches, churn).
      - ``org_change_rehearsal`` — internal rollouts staged across
        ticks (re-org, tooling, policy).
  - ``n_ticks`` (int, 1-1000) — number of simulation steps. Decision
    forecasts usually want 1; market sims 2-5; org change rehearsals
    match the rollout event count (often 4).
  - ``personas`` (list, 1-100 items) — each persona is:
      - ``name`` (string) — identifier inside the scenario.
      - ``role`` (string) — bucket the adapter aggregates against.
        Common roles: ``approver``, ``tenant``, ``manager``, ``ic``,
        ``ops``, ``customer_success``, ``enterprise``, ``smb``,
        ``channel``, ``competitor``, ``property_manager``, ``agent``.
      - ``ocean`` (map, optional) — OCEAN trait deltas in [-2, 2]:
        ``openness``, ``conscientiousness``, ``extraversion``,
        ``agreeableness``, ``neuroticism``. Values are deltas off the
        baseline 0 — positive = stronger trait. Omit traits the user
        didn't specify; the engine defaults them to 0.

### Optional fields

  - ``tier_mix`` (map) — share of personas routed to each LLM tier.
    Must sum to 1.0 ± 0.001. Captain-locked default 5/15/80:

    ```yaml
    tier_mix:
      premium: 0.05  # Claude Sonnet 4.7 — strategic / approver personas
      mid:     0.15  # Claude Haiku 4.7 — mid-fidelity cohort
      tail:    0.80  # Llama-3.1-8B via vLLM — bulk synthesized personas
    ```

    Overriding the mix triggers a cost-estimator warning in the UI —
    only deviate when the user explicitly asked.

  - ``precedent_seed`` (string) — global forward-precedent seed. When
    set, every projected decision gets a synthetic, deterministic
    ``forward_precedent_decision_id``. Omit unless the user mentions
    "link to past decision".

  - ``precedent_seeds`` (map) — per-anchor overrides keyed by anchor id.

### Anchors (for backtests, not forward sims)

Forward sims (the ``run_scenario`` tool) don't carry inline anchors —
the engine fans personas across the ticks and emits one
ProjectedDecision per (tick, anchor inferred from role). **Backtests**
are where anchors are required, and they currently ship through the
REST surface only (``POST /api/v1/foresight/backtests``); see the
"Endpoint reference (fallback)" section below.

This skill focuses on **forward sims**. If the user asks for a backtest
("did we predict the Q2 renewals correctly?"), redirect to the
Aggregate panel in the UI and surface the ``gate_decision`` from the
response. The chat agent rarely needs to build backtests by hand.

## STEP 3 — Save the scenario

### Create

Build the YAML body as a string and call ``save_scenario``. The tool
returns the full scenario object including the new ``id`` — **capture
it** for the run step.

```
mcp__pocketpaw_foresight__save_scenario({
  "name": "Rehearse Q3 Renewals",
  "sub_type": "decision_forecast",
  "description": "Renewal cohort with one approver.",
  "yaml_body": "name: rehearse-q3-renewals\nsub_type: decision_forecast\nn_ticks: 1\ntier_mix:\n  premium: 0.05\n  mid: 0.15\n  tail: 0.80\npersonas:\n  - name: tenant-maria\n    role: tenant\n    ocean:\n      conscientiousness: 0.4\n      agreeableness: 0.5\n  - name: approver-prakash\n    role: approver\n    ocean:\n      conscientiousness: 1.2\n"
})
```

Returns the scenario object: ``{id, name, sub_type, description,
yaml_body, parsed_meta, created_at, updated_at, ...}``. **Capture the
``id``** — you need it for the run step. The cloud rejects bodies
where the request ``sub_type`` doesn't match the YAML's
``sub_type:`` (422 ``foresight.sub_type_mismatch``); keep them in sync.

### Edit (PUT — full replace)

```
mcp__pocketpaw_foresight__update_scenario({
  "scenario_id": "<id>",
  "name": "...",
  "sub_type": "...",
  "yaml_body": "<full yaml>",
  "description": "..."
})
```

``update_scenario`` is **full replace** — every field on the body
overwrites the saved doc. Read-modify-write: ``get_scenario`` first,
modify only the fields the user asked to change, then call
``update_scenario`` with the complete body. NEVER blank out a field the
user didn't mention.

### Delete

```
mcp__pocketpaw_foresight__delete_scenario({"scenario_id": "<id>"})
```

**Always confirm with the user before deleting** — the operation is
irreversible (no audit log undo).

## STEP 4 — Run (only on explicit confirm)

After the save lands, ask the user:

  > "Saved as `<name>`. Want me to run it now?"

Wait for an explicit "yes" / "run it" / "go" before calling
``run_scenario``. Foresight runs cost LLM tokens — never auto-run on
save.

```
mcp__pocketpaw_foresight__run_scenario({
  "name": "Q3 Renewals",
  "custom_scenario_id": "<id-from-save_scenario>",
  "route_to_instinct": false
})
```

The v0.1 deterministic-fake backend completes synchronously — the call
returns the full run record (``status: complete``) on the same
response. Future versions return ``status: queued`` plus a websocket
URL; this skill assumes synchronous for now.

Response carries ``id``, ``status``, ``result.aggregates``,
``result.projected_decisions[]``. Surface the run id and a one-line
verdict drawn from ``result.aggregates``.

For richer details, look up the run via ``get_run`` (or use the
``/api/v1/foresight/runs/{id}/projected-decisions`` REST endpoint for
the per-anchor list).

```
mcp__pocketpaw_foresight__get_run({"run_id": "<id>"})
```

## Three worked examples

### Example 1 — Decision Forecast

User: "Rehearse the Q3 enterprise renewal — 5 customers, one approver,
single decision."

```yaml
name: q3-enterprise-renewals
sub_type: decision_forecast
n_ticks: 1
tier_mix:
  premium: 0.05
  mid: 0.15
  tail: 0.80
personas:
  - name: customer-acme
    role: tenant
    ocean:
      conscientiousness: 0.3
      agreeableness: 0.5
  - name: customer-globex
    role: tenant
    ocean:
      openness: 0.4
      neuroticism: -0.2
  - name: customer-initech
    role: tenant
    ocean:
      conscientiousness: 0.6
  - name: customer-umbrella
    role: tenant
    ocean:
      agreeableness: 0.7
      neuroticism: 0.3
  - name: customer-tyrell
    role: tenant
    ocean:
      openness: 0.5
      extraversion: 0.4
  - name: approver-prakash
    role: approver
    ocean:
      conscientiousness: 1.2
```

Save then run:

```
saved = mcp__pocketpaw_foresight__save_scenario({
  "name": "Q3 Enterprise Renewals",
  "sub_type": "decision_forecast",
  "yaml_body": "<the yaml above>"
})
mcp__pocketpaw_foresight__run_scenario({
  "name": "Q3 Renewals",
  "custom_scenario_id": saved.id
})
```

### Example 2 — Market Sim (pricing stress test)

User: "What happens if we raise enterprise pricing 12% and SMB stays
flat? Two ticks — announce, then observe the competitive reaction."

```yaml
name: pricing-stress-2026q3
sub_type: market_sim
n_ticks: 2
tier_mix:
  premium: 0.05
  mid: 0.15
  tail: 0.80
personas:
  - name: enterprise-acme
    role: enterprise
    ocean:
      conscientiousness: 0.6
      neuroticism: -0.2
  - name: enterprise-globex
    role: enterprise
    ocean:
      openness: 0.4
  - name: smb-quickserve
    role: smb
    ocean:
      extraversion: 0.5
      openness: 0.6
  - name: smb-corner-coffee
    role: smb
    ocean:
      agreeableness: 0.7
  - name: channel-partner-east
    role: channel
    ocean:
      extraversion: 0.8
  - name: competitor-alpha
    role: competitor
    ocean:
      openness: 0.9
      conscientiousness: 0.4
```

After save + run, surface ``aggregates.per_segment`` per role bucket.

### Example 3 — Org Change Rehearsal

User: "Model the engineering re-org — 2 managers, 3 ICs, ops + CS. Four
rollout events: announce, training, deadline, escalation."

```yaml
name: eng-reorg-2026q3
sub_type: org_change_rehearsal
n_ticks: 4  # one tick per rollout event
tier_mix:
  premium: 0.05
  mid: 0.15
  tail: 0.80
personas:
  - name: eng-manager-anne
    role: manager
    ocean:
      conscientiousness: 0.8
      agreeableness: 0.4
  - name: eng-manager-priya
    role: manager
    ocean:
      conscientiousness: 0.6
      openness: 0.3
  - name: ic-alex
    role: ic
    ocean:
      openness: 0.7
      neuroticism: -0.3
  - name: ic-blake
    role: ic
    ocean:
      conscientiousness: 0.5
      agreeableness: 0.6
  - name: ic-carmen
    role: ic
    ocean:
      neuroticism: 0.4   # higher resistance tilt
      openness: -0.2
  - name: ops-david
    role: ops
    ocean:
      conscientiousness: 0.7
  - name: cs-elena
    role: customer_success
    ocean:
      extraversion: 0.6
  - name: cs-frank
    role: customer_success
    ocean:
      agreeableness: 0.7
```

After the run, surface ``aggregates.per_event`` (adoption / resistance /
exit / escalation rates) and ``totals.queue_depth``.

## Error handling — the codes you'll see

When an MCP tool fails, the envelope carries ``is_error: true`` and the
text starts with ``Error: <code>``. Four codes matter:

  - **``foresight.invalid_yaml``** — YAML failed to parse. Read the
    message, identify the field (often a colon / indentation issue),
    fix, retry. NEVER swallow the error and present a fake success.
  - **``foresight.sub_type_mismatch``** — the ``sub_type`` in the
    request differs from the ``sub_type:`` declared inside the YAML.
    Pick one; they must match. The request's sub_type wins as the
    intent declaration; rewrite the YAML to match.
  - **``foresight.invalid_scenario``** — YAML parsed but engine
    grammar / cap failed (persona count > 100, n_ticks > 1000,
    tier_mix doesn't sum to 1.0, etc.). Read the message — it names
    the field — and adjust.
  - **``foresight_custom_scenario.not_found``** — the scenario id is
    unknown or belongs to another workspace (tenancy collapse). On a
    PUT/DELETE/GET retry, this means the id is stale; refresh the list.

Surface the error message to the user verbatim — do not paraphrase. The
message names the field; the user can fix it directly. If the error
recurs after one retry, stop and ask for clarification rather than
looping.

## Endpoint reference (fallback)

The MCP tools above are the preferred surface. The cloud also exposes a
REST API for power users, SDKs without the in-process MCP server, and
backtests (which the chat surface doesn't yet expose):

  - ``GET    /api/v1/foresight/scenarios/custom`` — list saved scenarios
    (workspace-scoped, paginated; optional ``?sub_type=`` filter).
  - ``GET    /api/v1/foresight/scenarios/custom/{id}`` — fetch one.
  - ``POST   /api/v1/foresight/scenarios/custom`` — create.
  - ``PUT    /api/v1/foresight/scenarios/custom/{id}`` — full replace.
  - ``DELETE /api/v1/foresight/scenarios/custom/{id}`` — remove (204).
  - ``POST   /api/v1/foresight/scenarios`` — run. Body: ``{name,
    custom_scenario_id, route_to_instinct?, precedent_seed?}`` OR the
    inline-personas grammar.
  - ``GET    /api/v1/foresight/runs/{id}`` — fetch one run.
  - ``GET    /api/v1/foresight/runs/{id}/projected-decisions`` —
    paginated per-anchor projections, optional ``?anchor_id=`` filter.
  - ``POST   /api/v1/foresight/backtests`` — retroactive run scored
    against known historical anchors. Not exposed via MCP; ship
    backtests through the UI's Aggregate panel.

## Auth headers (REST fallback only)

The MCP tools handle workspace identity automatically — no headers
required. The REST fallback is loopback-only and uses internal-trust
headers:

  - ``X-PocketPaw-Internal: true``
  - ``X-PocketPaw-Workspace-Id: <id>``
  - ``X-PocketPaw-User-Id: <id>``

Prefer the MCP tools whenever possible; the REST surface exists for
SDK callers, the backtest path, and edge cases where the in-process
server isn't available.

## Run pattern — ask, then go

After every save, ALWAYS ask before running:

  > "Saved as `<name>`. Want me to run it now? Forward sims cost LLM
  > tokens; the run takes ~5s on the deterministic backend, 30-120s on
  > the live LLM tier pool."

Wait for explicit confirmation. Acceptable confirms: "yes", "run it",
"go ahead", "ship it", "send it". Anything else → wait.

After the run completes (synchronous in v0.1):

  - One-line verdict drawn from ``result.aggregates``.
  - The run id + a hint: "Open the Live panel for the full breakdown."
  - For richer detail, call ``get_run`` (or hit
    ``/runs/{id}/projected-decisions``) and surface the
    highest-confidence projections.

## Edit pattern — read, modify, replace

When the user asks to change a saved scenario:

  1. Call ``get_scenario`` by id to capture the current state.
  2. Parse the ``yaml_body`` into your working copy.
  3. Modify ONLY the fields the user named. Leave everything else
     verbatim — including comments, ordering, and tier_mix.
  4. Call ``update_scenario`` with the full body back.

NEVER call ``update_scenario`` with a body assembled from memory —
you'll lose fields the user added through the UI. Always read first.

## Conversation conventions

  - **Concise + active voice.** "Saved as q3-renewals. Run it?" beats
    "I have successfully created the scenario for you. Would you like me
    to execute it?"
  - **Surface YAML in code-fence blocks** so the user can copy / edit it
    in their own editor if they want.
  - **Ask before deleting.** "Want me to delete the old `q2-renewals`
    scenario?" — never silently overwrite or remove.
  - **Admit when the request doesn't fit.** Four sub_types are deferred
    to future RFC waves (``cycle_planner``, ``portfolio_sim``,
    ``crisis_branch``, ``calibration_drift``). If the user asks for one
    of those, say so and offer to map to the closest v1.0 shape:
      - "cycle planner" → ``decision_forecast`` with n_ticks matched to
        the cycle length
      - "portfolio sim" → ``market_sim`` with persona segments per
        portfolio bucket
      - "crisis branch" → ``decision_forecast`` with one anchor per
        crisis scenario
      - "calibration drift" → not supported; redirect to the
        ``/aggregate`` and ``/insights`` read endpoints

## Hard rules

  - **PREFER** ``mcp__pocketpaw_foresight__*`` tools over curl — they
    always use the correct workspace_id.
  - **NEVER** call ``run_scenario`` without first calling
    ``list_scenarios`` (or ``get_scenario`` on a known id). Discovery
    prevents duplicates.
  - **NEVER** run on save — always ask first.
  - **NEVER** call ``update_scenario`` with a partial body — PUT
    semantics mean full replace.
  - **NEVER** invent error codes. If a tool returns
    ``foresight.invalid_scenario``, surface that exact code; don't
    paraphrase it as "validation failed".
  - **NEVER** call ``/api/v1/foresight/backtests`` (POST) from this
    skill. The backtest path needs ground-truth anchors and ships
    through the UI's Aggregate panel. If the user asks to *create* a
    backtest, redirect them there. The ``list_backtests`` /
    ``get_backtest`` / ``get_onboarding_gate`` MCP tools above are
    read-only and don't contradict this rule — they answer "did we
    backtest yet" / "what was the gate decision" / "are we unlocked"
    without touching the create path.
  - **ALWAYS** echo the response shape verbatim when surfacing a run
    result — the operator's Live panel binds to the same field names.

## Related skills

  - ``pocketpaw-create-pocket`` — when the user wants a **dashboard** of
    foresight history (Aggregate / Insights), not a new sim.
  - ``pocketpaw-edit-pocket`` — when the user is on an existing canvas
    and wants to add a Foresight widget to it.
