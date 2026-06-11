<!--
README.md — the gated external-action Instinct proposal type.
Created: 2026-06-11 (feat/external-action-proposal).
Documents the third gated proposal kind (alongside `_pocket_write` and
`_code_change`): its blob contract, the propose/execute split, the
exactly-one-terminal Decision-Graph discipline, and the router wiring.
-->

# Gated external actions

A **gated external action** is a proposed call to an external system through a
**bound connector** — for example "call action `approveApplication` on connector
`crm` with params `{...}`". It is the **third gated Instinct proposal type**,
sitting alongside the two that shipped before it:

| Kind | Parameters key | Propose | Execute-on-approve |
|------|----------------|---------|--------------------|
| Pocket write | `_pocket_write` | `pockets/instinct_bridge.propose_pocket_write` | `pockets/instinct_bridge.execute_approved_write` |
| Code change (Belt) | `_code_change` | `agent/mcp_servers/belt.py` | `cloud/belt/executor.execute_approved_change` |
| **External action** | **`_external_action`** | **`external_actions.propose.propose_external_action`** | **`external_actions.executor.execute_approved_external_action`** |

Instinct is the approval gate: an agent **proposes**, a human **approves or
rejects** in The Tray, and every decision is audit-trailed as a Decision-Graph
chain. On approve the executor performs the connector call; on reject nothing
fires. The kind is **fully generic** — no domain-specific logic, no client
names.

## The blob (`Action.parameters._external_action`, schema 1)

The propose helper files an Instinct `Action` carrying this blob. No connector
secret is ever written — only the connector *name* + scope; the credential is
resolved fresh at execution from the workspace's saved connector config.

| Field | Meaning |
|-------|---------|
| `schema` / `kind` | version (`1`) + discriminator (`"external_action"`) |
| `workspace_id` | originating tenant — the executor's tenancy gate reads it here (an external action isn't bound to a pocket) |
| `connector_name` / `scope` / `pocket_id` | which bound connector, at which scope |
| `action` | the named connector action to call |
| `params` | the proposed call params (DATA — passed verbatim to the adapter) |
| `params_hash` | stable SHA-256 of `action` + `params`; re-checked at execute so a post-propose edit is refused |
| `idempotency_key` | so the executor never double-fires the call on re-invocation |
| `correlation_id` / `proposed_event_id` | Decision-Graph chain ids minted at propose time |
| `summary` | human-readable one-liner for the gate UI |
| `outcome` | `{status, response_summary, executed_at}` back-written by the executor after the call |

## Execute-on-approve contract

`execute_approved_external_action(action, *, human_event_id=None)` is fired
best-effort from the instinct router's approve / bulk-approve handlers. It:

1. Re-validates the **schema version** (fail loud on mismatch — a stale blob
   from an incompatible build never fires).
2. Re-validates the **params hash** (a human approved a *specific* call; a
   params edit between propose and approve is refused).
3. **Idempotency guard** — never re-fires the call if the Action already
   executed / failed or already carries a back-written outcome.
4. Resolves the connector and calls the named action with the proposed params
   through the **cloud connector path** (`connectors.service.execute`).
5. Back-writes the structured `outcome` via the direct-SQL pattern (the same one
   belt's `_persist_run_result` and the pocket-write bridge's
   `_persist_parked_policy_event_id` use).
6. Records the result on the Action (`mark_executed` / `mark_failed`) and closes
   the Decision-Graph chain.

It **never raises** into the router — a connector error (reported failure or a
raised exception) is captured as `status=failed` with a `failed` terminal, best
effort.

## Exactly-one-terminal discipline (Decision Graph)

The chain folds into **one** Decision per call:
`agent.proposed → human.corrected → decision.completed`.

- **On approve**, the **executor owns** the `decision.completed` close (success →
  `landed`, failure → `failed`). The router emits `human.corrected` only — it
  does **not** close, so the chain never double-closes.
- **On reject**, the **router owns** the close (`human.corrected` then
  `decision.completed(rejected)`); the executor never runs.

This mirrors the pocket-write bridge and the Belt executor exactly. Getting it
wrong double-closes the chain — the production-path integration test in
`tests/cloud/test_external_action_gate.py` walks the chain and asserts exactly
one terminal on both the approve and reject paths.

## Router wiring

`ee/instinct/router.py` dispatches on the `_external_action` blob in all four
handlers — `approve_action`, `bulk_approve_actions`, `reject_action`,
`bulk_reject_actions` — with the up-front `_assert_external_action_workspace`
tenancy gate in each. The blob branches are matched in priority order against
the `_pocket_write` / `_code_change` branches (mutually exclusive — an Action
carries exactly one kind).
