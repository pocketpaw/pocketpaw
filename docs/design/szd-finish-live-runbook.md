<!--
docs/design/szd-finish-live-runbook.md
Created: 2026-06-21 (F5 / feat/szd-finish-enforce). A hands-on checklist for the
captain to run the FINISHED Sovereign Zero-Setup Discovery feature against real
infra once — a real connector, a real on-box model, the live gate — and confirm
by eye what the F5 automated E2E (tests/ee/test_szd2_finish_e2e.py) proves with
mocks. Pairs with that test: the test pins the logic deterministically; this
runbook is the truly-live smoke pass that can't be automated cheaply (it needs a
running Ollama + a bound connector). No PII — use non-sensitive seed data only.
-->

# Sovereign Zero-Setup Discovery — live run runbook

A by-hand smoke pass for the finished feature (F1 trigger → F2 categorize → F3
refine → F4 edit → approve → F6 enforce). The automated E2E
(`tests/ee/test_szd2_finish_e2e.py`) proves the logic with mocks; this is the
one-time TRULY-LIVE confirmation against a real connector + a real on-box model.

The headline guarantee is **sovereignty**: no tenant text leaves the box. The
single most important thing to eyeball is the **network log** — there must be
zero calls to a cloud LLM host during the whole run.

## Rule 0 — no PII

Seed the connector with **non-sensitive** data only. Use synthetic tickets /
refunds / records (made-up names, fake invoice numbers). Discovery compiles the
exhaust on-box, but this is a manual run on a shared box — don't put anything
real-customer in it.

## Prerequisites

- [ ] A tenant box (or dev box) running the cloud backend with `pocketpaw-ee`.
- [ ] **Ollama running locally** with the configured model pulled:
  ```bash
  ollama serve                 # leave running
  ollama pull llama3.2         # or whatever POCKETPAW_OLLAMA_MODEL is set to
  ```
  Defaults: `POCKETPAW_OLLAMA_HOST=http://localhost:11434`,
  `POCKETPAW_OLLAMA_MODEL=llama3.2`.
- [ ] The `kb` (kb-go) binary on `PATH` — discovery shells out to it for the
  keyless on-box compile (`kb prepare` / `accept` / `convo` / `list` / `show` /
  `graph`). It must NEVER be invoked with `ingest` / `build` (those POST to
  Anthropic) — that's the sovereignty tripwire.
- [ ] A real connector bound + **enabled** in the target workspace, seeded with
  non-PII rows (see Rule 0). A correction history on the workspace helps the
  rule-discovery lane fire (3+ corrections on the same field with a constant
  target → one governed-rule proposal).

## Enable enforcement (default-OFF)

The live-rule gate is behind a default-off flag. Turn it on for this run:

```bash
export POCKETPAW_INSTINCT_ENFORCE_DISCOVERED_RULES=true
```

Restart the backend so the setting takes effect. (Leave it OFF in production
until a workspace has reviewed its discovered rules.)

## The live flow

### 1. Trigger discovery (F1)

```bash
curl -sS -X POST http://localhost:8000/api/v1/cloud/discovery/run \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{}'
```

- Expect **202 Accepted** + a `run_id`. The run is fire-and-forget; the
  proposals land asynchronously as PENDING Instinct Actions.
- Body can be `{}` (server enumerates the workspace's ENABLED connectors) or
  `{"connector_ids": ["<name>"], "sample_cap": 50}` to pin them.
- **Eyeball:** only ENABLED connectors should be sampled — a disabled connector
  must not appear in the run's logs.

### 2. Watch categorization (F2 + F3)

Tail the backend logs while the run executes. You should see:

- `kb-compile: label <x> compiled via prepare/accept (on-box categorization)` —
  the model path ran (NOT `via convo ingest (deterministic fallback)`).
- `discovery.run staged proposals run_id=... rules=N` — the run finished.

If Ollama is down you'll instead see `via convo ingest` + a refine
`unavailable` — discovery still produces a (degraded, objects-only) draft. That
is the graceful-degrade path; for a full live pass, Ollama must be up.

### 3. Review the proposals in the UI

Open **The Tray / Approvals panel** for the workspace. You should see three
pending discovery proposals sharing one run:

- a **Fabric objects** proposal (the discovered ontology),
- a **starter Pocket** proposal (a dashboard bound to the discovered types),
- one or more **governed rule** proposals (reverse-engineered from corrections).

**Eyeball — ≥2 domain categories:** the ontology proposal must show **two or
more DOMAIN object types** (e.g. `SupportTicket`, `RefundRequest`) — NOT a
single `Conversation` bucket. A single `conversation`-typed proposal means the
on-box model didn't run (check Ollama + the log line above).

### 4. Edit a proposal in review (F4)

On the governed-rule proposal, **tighten its condition** before approving
(e.g. narrow the CEL `when`), then save the edit (the PATCH endpoint, surfaced
as an edit affordance on the card).

- **Eyeball:** the edit persists, the proposal **stays PENDING** (Approve is a
  separate click), and the card reflects the tightened condition. A malformed
  condition should be refused (422) without changing the stored proposal.

### 5. Approve

Approve all three. The executors materialise them:

- the Fabric objects become queryable in the workspace,
- the starter Pocket is created and its `fabric.objects` source resolves to the
  discovered rows,
- the governed rule lands **active** (surfaces via `get_active_rules`).

### 6. Fire a governed action — enforcement on (F6)

In the new Pocket (or any pocket in the workspace), trigger a governed action on
a row that the approved rule **targets** (matches the rule's `when`).

- **Eyeball:** the verdict the rule declares fires — the action is **blocked**
  or **escalated to approval** (lands in the Approvals tray), not silently
  executed. A row that does NOT match the rule proceeds normally.

### 7. Flip the flag OFF — prove it gates

```bash
export POCKETPAW_INSTINCT_ENFORCE_DISCOVERED_RULES=false
# restart backend
```

Fire the SAME governed action on the SAME targeted row again.

- **Eyeball:** with the flag off, the action **proceeds** — the discovered rule
  is inert. This confirms the flag gates real behaviour end-to-end (and is the
  safe default state).

## What to confirm by eye (the checklist)

- [ ] **≥2 domain categories** in the ontology proposal (not one `Conversation`
      bucket) — proves F2 on-box categorization ran.
- [ ] **No cloud calls in the network log** for the whole run — open the box's
      outbound network log / a packet capture and confirm zero connections to
      `api.anthropic.com` / `api.openai.com` (or any cloud LLM host). The only
      LLM traffic should be local to `localhost:11434` (Ollama). This is the
      sovereignty guarantee.
- [ ] **No `kb ingest` / `kb build`** in the logs — only the keyless on-box kb
      commands (`prepare`/`accept`/`convo`/`list`/`show`/`graph`).
- [ ] **The rule actually blocks/escalates** the targeted governed action with
      the flag ON, and the SAME action **proceeds** with the flag OFF.
- [ ] The F4 edit persisted and kept the proposal PENDING until you approved.

## Cleanup

- Unset `POCKETPAW_INSTINCT_ENFORCE_DISCOVERED_RULES` (back to default OFF) on
  any non-test deploy.
- Archive the discovered rule / delete the smoke-test Pocket + Fabric objects if
  you don't want the seed data lingering.
