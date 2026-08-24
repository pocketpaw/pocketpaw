<!-- Conformance fixture format + harness contract for the paw composition kernel.
     Created 2026-08-24. Read with ../SEMANTICS.md — this file defines HOW the
     normative rules are tested; SEMANTICS.md defines WHAT they are. -->

# Conformance fixtures — format and harness contract

Every paw composition runtime executes these 17 fixtures. Same JSON, same expected traces,
every language. This is the mechanical enforcement the workspace charter asks for: two
runtimes that both claim to implement the semantics either agree here or one of them is
wrong.

## Fixture shape

```json
{
  "id": "load-order-inject",
  "asserts": "one-line statement of the rule under test",
  "semantics": "§2",
  "plugins": {
    "a": { "provides": ["svcA"] },
    "b": { "inject": { "required": ["svcA"] } }
  },
  "steps": [
    { "op": "mount", "plugin": "b" },
    { "expect_state": { "b": "PENDING" } },
    { "op": "mount", "plugin": "a" },
    { "expect_state": { "a": "ACTIVE", "b": "ACTIVE" } }
  ],
  "expect_trace": [
    "b:PENDING", "a:LOADING", "a:provide:svcA", "a:ACTIVE", "b:LOADING", "b:ACTIVE"
  ]
}
```

### Plugin declarations

A plugin entry is declarative. The harness turns it into a real plugin.

| Field | Meaning |
|---|---|
| `provides` | service keys published during `apply` (each is an effect) |
| `inject.required` | services that gate activation |
| `inject.optional` | services used if present, never gating |
| `effects` | effect ids to create during `apply`, in order |
| `effects_after_delay` | effect ids created **after** `apply_delay_ms` elapses. Exists so a dispose-during-load test can distinguish a runtime that awaits `apply` from one that tears down concurrently. |
| `record_resolved` | service keys whose resolved value the plugin records during `apply`, emitting `<plugin>:resolved:<svc>:<value>` |
| `effect_during_dispose` | an effect id the plugin attempts to register **from inside a disposer**. Must be rejected (§3). Attach it to the **first-registered** effect's disposer, which runs last under LIFO. |
| `listeners` | see the listener table below |
| `children` | plugin names to mount as children during `apply`. Children load **inline** — awaited inside the parent's `apply`. |
| `apply_throws` | if true, `apply` raises after creating its declared effects |
| `apply_delay_ms` | `apply` awaits this long before completing. Yields the event loop mid-apply. |
| `dispose_delay_ms` | this plugin's disposers await this long (async cleanup) |
| `disposer_throws` | an effect id whose disposer raises after emitting its `:dispose` token. The chain MUST continue and the fiber MUST still reach its target state. |

### Listener declarations

`{ "event": ..., "mode": ..., "id": ..., "action": ..., ... }`

| `action` | Behavior |
|---|---|
| `observe` | enter, emit tokens, return nothing. For `emit` / `parallel`. |
| `delegate` | call `next()` and return its result unchanged (waterfall) |
| `wrap` | call `next()` and wrap the result as `<wrap>(<result>)`, using the `wrap` field (waterfall) |
| `shortcircuit` | return the `value` field **without** calling `next()` (waterfall) |
| `absent` | return the runtime's absent value, so a `serial` chain continues past it |
| `value` | return the `value` field (serial: this wins and stops the chain) |

Optional listener field `delay_ms` — the listener awaits this long, used to prove `parallel` really fans out.

### Operations

| `op` | Effect |
|---|---|
| `mount` | mount `plugin`. Optional `under` = parent plugin name; `scope` = an isolated scope name; `nowait: true` begins the mount without awaiting it. |
| `dispose` | dispose the named plugin's fiber, awaited |
| `dispose_nowait` | begin disposal **without** awaiting — pairs with `apply_delay_ms` to open the dispose-during-load window |
| `provide` | publish `service` on the root context, or in `scope` if given. Optional `value` (defaults to a runtime-chosen marker). |
| `withdraw` | withdraw `service` from the root context, or from `scope` |
| `isolate` | create an isolated `scope` for `service` (§1). Later `provide`/`mount` steps may target it by name. |
| `dispatch` | dispatch `event` with `mode`. Emits no token of its own; the listener tokens and `expect_result` are what's checked. |
| `settle` | await the runtime's quiescence — all pending lifecycle work drained |

### Assertions

- `expect_state` — map of plugin name → expected fiber state at that point.
- `expect_trace` — the **complete, ordered** trace for the whole fixture. Exact match.
- `expect_trace_unordered` — used **instead of** `expect_trace` when the fixture contains genuinely concurrent work (`parallel` dispatch). Compared as a multiset.
- `expect_result` — on a `dispatch` step, the expected returned value.

### Ordering inside `apply`

Unspecified ordering is how two runtimes drift while both passing. The order is:

```
provides → record_resolved → effects → listeners → children
         → apply_delay_ms → effects_after_delay → apply_throws
```

And two rules that a single mechanism cannot satisfy together:

- **Child mounts during `apply` load inline** — awaited within the parent's `apply` (`nested-recursive-dispose`).
- **Activation triggered by a newly provided service is deferred** — queued and drained only after the providing plugin settles (`load-order-inject`).

## Trace vocabulary

The trace is the observable record of lifecycle. A runtime MUST emit exactly these tokens,
in this form:

The `<owner>` slot is a plugin name, or one of two pseudo-owners: **`root`** for operations
performed directly on the root context by a step (not by a plugin), and **the scope name**
(e.g. `s1`) for operations against an isolated scope.

| Token | Emitted when |
|---|---|
| `<plugin>:PENDING` | fiber enters PENDING |
| `<plugin>:LOADING` | fiber enters LOADING |
| `<plugin>:ACTIVE` | fiber enters ACTIVE |
| `<plugin>:UNLOADING` | fiber enters UNLOADING |
| `<plugin>:DISPOSED` | fiber enters DISPOSED |
| `<plugin>:FAILED` | fiber enters FAILED |
| `<owner>:provide:<svc>` | a service is published |
| `<owner>:withdraw:<svc>` | a service is withdrawn |
| `<plugin>:resolved:<svc>:<value>` | a plugin records which provider it resolved (from `record_resolved`) |
| `<plugin>:effect:<id>:setup` | an effect's setup body runs |
| `<plugin>:effect:<id>:dispose` | an effect's disposer runs |
| `<plugin>:effect:<id>:rejected` | effect creation refused because the owner is UNLOADING |
| `<plugin>:effect:<id>:dispose:error` | that disposer raised; the chain continues regardless (§3) |
| `<plugin>:listener:<id>:enter` | a waterfall/serial listener is entered |
| `<plugin>:listener:<id>:exit` | that listener returns |
| `<plugin>:apply:throw` | `apply` raises |

Only listed tokens appear. A runtime that emits extra tokens fails — the trace is an exact
match, because "it did something else too" is exactly the class of bug this catches.

**Not in the trace:** mounting a plugin whose deps are already satisfied goes straight to
`LOADING` — there is no `PENDING` token in that case. `PENDING` appears only when the
plugin actually waits.

## Harness contract

Each runtime ships a harness that:

1. Loads every `*.json` in this directory (excluding `README.md`).
2. For each fixture: builds a fresh root context, constructs the declared plugins, runs the
   steps in order, and records the trace.
3. Compares the recorded trace to `expect_trace` **exactly** — same tokens, same order.
4. Checks each `expect_state` at the point it appears.
5. Fails loudly on any unknown `op`, unknown field, or missing fixture file. **A fixture
   that the harness does not understand is a failure, never a skip.** This is the rule that
   stops a runtime from quietly "passing" by ignoring what it hasn't built.

Ordering note: where two tokens are genuinely concurrent (`parallel` dispatch), the fixture
declares them under `expect_trace_unordered` instead, and the harness compares as a
multiset. Only `parallel` uses this.

## Amendment log

**2026-08-24 — two fixtures were vacuous.** Found by mutation-testing during the
first runtime build, not by review. Both are recorded in the fixtures' own
`regression_note` field.

- `dispose-during-load` created its only effect *before* `apply_delay_ms`, so a
  runtime that ran cleanup concurrently with the rest of `apply` produced an
  identical trace. It now creates a second effect *after* the delay.
- `load-order-inject` had a provider that never awaited after publishing, so
  `apply` ran to completion before any spawned re-check could run — a raw task
  spawn passed exactly as well as a correct deferred queue. The provider now
  awaits after publishing.

**2026-08-24 — three of four dispatch modes were untested.** §5 declares `emit`,
`waterfall`, `parallel` and `serial` as MUSTs; only `waterfall` had coverage.
Added `emit-fire-and-forget`, `parallel-awaits-all`, `serial-first-non-absent`.

**2026-08-24 (second pass) — `parallel-awaits-all` could not see concurrency.**
Its multiset compare proved "awaits all" but not "fans out concurrently": a
runtime awaiting each listener strictly in turn produced the same bag of tokens
and passed. Now an ordered trace, relying on the deliberate 4x delay margin
(20ms vs 5ms) to make the concurrent interleaving deterministic. This is the one
fixture with a timing dependency — that trade is worth it against a check that
could not fail.

No fixture currently uses `expect_trace_unordered`. It stays documented as an
option, but note what this episode showed: an unordered compare hides mechanism,
so reach for it only when order is genuinely undefined AND nothing important
depends on it.

**2026-08-24 — cancellation moved out of scope, deliberately.** Removing
`asyncio.shield` from the Python runtime's `dispose()` left all 16 fixtures
green. It cannot be fixed here: JS promises have no cancellation, so a `cancel`
op would be meaningless for the TypeScript runtime. It is now a **runtime-specific
obligation** in `SEMANTICS.md` §7a, tested natively by each runtime.

**2026-08-24 (third pass) — a throwing disposer aborted the whole chain.**
Found by the TypeScript runtime while writing its §7a obligation tests, then
reproduced independently on Python. In BOTH runtimes a disposer that raised
prevented every earlier disposer from running and stranded the fiber in
`UNLOADING` — and the two **disagreed** on whether `dispose()` raised (Python) or
resolved silently (TypeScript). Neither was wrong: §3 never defined disposer-error
behavior. It does now, and `disposer-throws-still-unwinds` enforces it.

That divergence is the clearest argument for this suite existing. Two runtimes,
built the same night from the same document, drifted on a real teardown guarantee,
and nothing but a shared fixture would have surfaced it.

The lesson is in the suite now: **a fixture that passes on a deliberately broken
runtime is worse than no fixture**, because it converts an untested rule into a
green check. Mutation-test every new fixture before trusting it — every amendment
above was found that way, and not one by review.

## The two that matter

`dispose-during-load` and `failed-apply-rolls-back` encode DeepSeek's divergence entry #6 —
reentrant disposal gaps found in a production JS runtime. Python's asyncio makes them
harder, not easier: a disposer that awaits must survive `CancelledError` in the enclosing
task. If these two fail, the runtime is not done, regardless of the other ten.
