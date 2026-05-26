---
name: foresight-create-sim
description: |
  Create, edit, and run Foresight scenarios via the workspace's REST API.
  Triggered when the user asks to rehearse / simulate / project / forecast
  / branch a decision — "rehearse the renewal", "what if we cut price
  10%", "simulate the org change before we announce", "forecast Q3 churn",
  "branch the launch decision". The skill teaches the chat agent how to
  discover existing scenarios, synthesize the YAML body, save it via
  PUT/POST, and (on explicit user confirmation) execute the run. Calls the
  cloud's existing ``/api/v1/foresight/*`` endpoints with the internal
  loopback headers — no engine code is touched.
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

## The four-phase workflow

Every interaction with Foresight from chat follows this loop:

  1. **Discover** — does a scenario like this already exist?
  2. **Synthesize** — build (or modify) the YAML body.
  3. **Save** — POST (new) or PUT (replace) the custom scenario.
  4. **Run** — POST /scenarios with ``custom_scenario_id``, ONLY if the
     user explicitly confirms.

Skipping phases is the most common failure mode. If you jump straight to
"run" without saving, you can't re-run the same scenario. If you skip
"discover" and create a duplicate, the workspace fills with near-copies.

## STEP 1 — Discover (list before you create)

Always GET the workspace's saved scenarios first. The user may have
already built something close to what they want.

```bash
curl -s -X GET \
  -H "X-PocketPaw-Internal: true" \
  -H "X-PocketPaw-Workspace-Id: $WORKSPACE_ID" \
  -H "X-PocketPaw-User-Id: $USER_ID" \
  "http://localhost:8000/api/v1/foresight/scenarios/custom?limit=20"
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

Forward sims (POST /scenarios) don't carry inline anchors — the engine
fans personas across the ticks and emits one ProjectedDecision per
(tick, anchor inferred from role). **Backtests** (POST /backtests) are
where anchors are required:

  ```json
  {
    "anchors": [
      {
        "anchor_object_id": "decision:renewal_q2_2026",
        "actual_outcome": {"renewed": true, "discount_pct": 8},
        "scenario_template": "decision_forecast.yaml",
        "projection_confidence": 0.5
      }
    ]
  }
  ```

This skill focuses on **forward sims**. If the user asks for a backtest
("did we predict the Q2 renewals correctly?"), redirect to the backtest
endpoint and surface the ``gate_decision`` from the response. The chat
agent rarely needs to build backtests by hand — the UI's Aggregate panel
does that.

## STEP 3 — Save the scenario

### Create (POST)

```bash
YAML_BODY=$(cat <<'EOF'
name: rehearse-q3-renewals
sub_type: decision_forecast
n_ticks: 1
tier_mix:
  premium: 0.05
  mid: 0.15
  tail: 0.80
personas:
  - name: tenant-maria
    role: tenant
    ocean:
      conscientiousness: 0.4
      agreeableness: 0.5
  - name: approver-prakash
    role: approver
    ocean:
      conscientiousness: 1.2
EOF
)

# Escape the YAML for JSON
YAML_JSON=$(jq -Rs <<<"$YAML_BODY")

curl -s -X POST \
  -H "X-PocketPaw-Internal: true" \
  -H "X-PocketPaw-Workspace-Id: $WORKSPACE_ID" \
  -H "X-PocketPaw-User-Id: $USER_ID" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"Rehearse Q3 Renewals\",
    \"sub_type\": \"decision_forecast\",
    \"description\": \"Renewal cohort with one approver.\",
    \"yaml_body\": $YAML_JSON
  }" \
  http://localhost:8000/api/v1/foresight/scenarios/custom
```

Returns 201 with the full scenario object (id, parsed_meta, yaml_body).
**Capture the ``id``** — you need it for the run step.

### Edit (PUT — full replace)

```bash
curl -s -X PUT \
  -H "X-PocketPaw-Internal: true" \
  -H "X-PocketPaw-Workspace-Id: $WORKSPACE_ID" \
  -H "X-PocketPaw-User-Id: $USER_ID" \
  -H "Content-Type: application/json" \
  -d "{\"name\": ..., \"sub_type\": ..., \"yaml_body\": $YAML_JSON}" \
  "http://localhost:8000/api/v1/foresight/scenarios/custom/$SCENARIO_ID"
```

PUT is **full replace** — every field on the body overwrites the saved
doc. Read-modify-write: GET the current state first, modify only the
fields the user asked to change, then PUT. NEVER blank out a field the
user didn't mention.

### Delete

```bash
curl -s -X DELETE \
  -H "X-PocketPaw-Internal: true" \
  -H "X-PocketPaw-Workspace-Id: $WORKSPACE_ID" \
  -H "X-PocketPaw-User-Id: $USER_ID" \
  "http://localhost:8000/api/v1/foresight/scenarios/custom/$SCENARIO_ID"
```

Returns 204. **Always confirm with the user before deleting** — the
operation is irreversible (no audit log undo).

## STEP 4 — Run (only on explicit confirm)

After the save lands, ask the user:

  > "Saved as `<name>`. Want me to run it now?"

Wait for an explicit "yes" / "run it" / "go" before calling POST.
Foresight runs cost LLM tokens — never auto-run on save.

```bash
curl -s -X POST \
  -H "X-PocketPaw-Internal: true" \
  -H "X-PocketPaw-Workspace-Id: $WORKSPACE_ID" \
  -H "X-PocketPaw-User-Id: $USER_ID" \
  -H "Content-Type: application/json" \
  -d "{
    \"name\": \"$SCENARIO_NAME\",
    \"custom_scenario_id\": \"$SCENARIO_ID\"
  }" \
  http://localhost:8000/api/v1/foresight/scenarios
```

The v0.1 deterministic-fake backend completes synchronously — POST
returns the full run record (``status: complete``) on the same response.
Future versions return ``status: queued`` plus a websocket URL; this
skill assumes synchronous for now.

Response carries ``id``, ``status``, ``result.aggregates``,
``result.projected_decisions[]``. Surface the run id and a one-line
verdict drawn from ``result.aggregates``.

For richer details, GET the per-anchor projections:

```bash
curl -s -X GET \
  -H "X-PocketPaw-Internal: true" \
  -H "X-PocketPaw-Workspace-Id: $WORKSPACE_ID" \
  -H "X-PocketPaw-User-Id: $USER_ID" \
  "http://localhost:8000/api/v1/foresight/runs/$RUN_ID/projected-decisions"
```

## Endpoint reference

  - ``GET    /api/v1/foresight/scenarios/custom`` — list saved scenarios
    (workspace-scoped, paginated; optional ``?sub_type=`` filter).
  - ``GET    /api/v1/foresight/scenarios/custom/{id}`` — fetch one,
    returns full yaml_body + parsed_meta.
  - ``POST   /api/v1/foresight/scenarios/custom`` — create. Body:
    ``{name, sub_type, description, yaml_body}``. Returns 201 with the id.
  - ``PUT    /api/v1/foresight/scenarios/custom/{id}`` — full replace.
    Returns 200.
  - ``DELETE /api/v1/foresight/scenarios/custom/{id}`` — remove (204).
    Idempotency: a second DELETE on the same id returns 404.
  - ``POST   /api/v1/foresight/scenarios`` — run. Body: ``{name,
    custom_scenario_id, route_to_instinct?, precedent_seed?}`` OR the
    inline-personas grammar (skip custom_scenario_id and embed
    ``sub_type``, ``personas[]``, ``n_ticks`` directly).
  - ``GET    /api/v1/foresight/runs/{id}`` — fetch one run with full
    result blob.
  - ``GET    /api/v1/foresight/runs/{id}/projected-decisions`` —
    paginated list of per-anchor projections, optional
    ``?anchor_id=`` filter.

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

```bash
ID=$(curl -s -X POST ... /scenarios/custom | jq -r .id)
curl -s -X POST ... /scenarios -d "{\"name\":\"Q3 Renewals\",\"custom_scenario_id\":\"$ID\"}"
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

## Error handling — the 422 envelope

The cloud returns errors in a stable envelope:

```json
{ "error": { "code": "foresight.invalid_yaml", "message": "..." } }
```

Four error codes you'll see:

  - **422 ``foresight.invalid_yaml``** — YAML failed to parse. Read the
    message, identify the field (often a colon / indentation issue),
    fix, retry. NEVER swallow the error and present a fake success.
  - **422 ``foresight.sub_type_mismatch``** — the ``sub_type`` in the
    request body differs from the ``sub_type:`` declared inside the
    YAML. Pick one; they must match. The body's sub_type wins as the
    intent declaration; rewrite the YAML to match.
  - **422 ``foresight.invalid_scenario``** — YAML parsed but engine
    grammar / cap failed (persona count > 100, n_ticks > 1000, tier_mix
    doesn't sum to 1.0, etc.). Read the message — it names the field —
    and adjust.
  - **404 ``foresight_custom_scenario.not_found``** — the scenario id
    is unknown or belongs to another workspace (tenancy collapse). On a
    PUT/DELETE/GET retry, this means the id is stale; refresh the list.

Surface the error message to the user verbatim — do not paraphrase. The
message names the field; the user can fix it directly. If the error
recurs after one retry, stop and ask for clarification rather than
looping.

## Auth headers

Calls go to ``http://localhost:8000`` (the local dashboard's loopback
address). The agent runs on the same host as the dashboard and uses
internal-trust headers — NO user JWT needed.

Required on every call:

  - ``X-PocketPaw-Internal: true``
  - ``X-PocketPaw-Workspace-Id: <id>``
  - ``X-PocketPaw-User-Id: <id>``

The workspace + user ids come from the **chat surface stamp** the
dashboard threads into the agent's context. Look for ``$WORKSPACE_ID``
and ``$USER_ID`` env vars or a system-prompt block named ``<surface>``
that carries them. If neither is present, ask the user to switch to a
specific workspace before continuing.

If the loopback bypass is misconfigured (wrong host, missing headers,
or feature flag off), the cloud returns 401 — surface the error and
tell the user the dashboard auth path needs attention.

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
  - For richer detail, optionally GET ``/runs/{id}/projected-decisions``
    and surface the highest-confidence projections.

## Edit pattern — read, modify, replace

When the user asks to change a saved scenario:

  1. GET the scenario by id to capture the current state.
  2. Parse the ``yaml_body`` into your working copy.
  3. Modify ONLY the fields the user named. Leave everything else
     verbatim — including comments, ordering, and tier_mix.
  4. PUT the full body back.

NEVER PUT a body assembled from memory — you'll lose fields the user
added through the UI. Always read first.

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

  - **NEVER** call POST /scenarios without first GETing the workspace
    scenario list. Discovery prevents duplicates.
  - **NEVER** run on save — always ask first.
  - **NEVER** PUT a partial body — full replace means full state.
  - **NEVER** invent error codes. If the cloud returns ``422
    foresight.invalid_scenario``, surface that exact code; don't
    paraphrase it as "validation failed".
  - **NEVER** call ``/api/v1/foresight/backtests`` from this skill. The
    backtest path needs ground-truth anchors and ships through the UI's
    Aggregate panel. If the user asks for a backtest, redirect them
    there.
  - **ALWAYS** echo the response shape verbatim when surfacing a run
    result — the operator's Live panel binds to the same field names.

## Related skills

  - ``pocketpaw-create-pocket`` — when the user wants a **dashboard** of
    foresight history (Aggregate / Insights), not a new sim.
  - ``pocketpaw-edit-pocket`` — when the user is on an existing canvas
    and wants to add a Foresight widget to it.
