<!-- Normative semantics for the paw composition kernel. Created 2026-08-24.
     This file is the SOURCE OF TRUTH. A runtime is conformant iff it satisfies
     every MUST here and passes every fixture in conformance/. -->

# Paw Composition Kernel — Normative Semantics

**Status:** v0.1.0 · **Date:** 2026-08-24

This document defines the composition semantics every paw runtime implements. It is
language-neutral and normative. Where a runtime and this document disagree, this document
wins — or this document is amended, deliberately, with the conformance fixtures updated in
the same change.

Key words **MUST**, **MUST NOT**, **SHOULD**, **MAY** are used in the RFC 2119 sense.

Design lineage: these semantics follow [Cordis](https://github.com/cordiverse/cordis) (MIT,
Shigma). We port the *semantics*, not the code. Where DeepSeek's hardened fork logged a
divergence that fixes a real bug, we adopt the fixed behavior — those places are marked
**[dragon]** and each has a conformance fixture.

---

## 1. Context

A **context** is a repository of services.

- A context MUST resolve a service by **key**, never by concrete type or import.
- A context MUST support creating a **child context** that inherits its parent's services.
- Resolving an absent key MUST yield the runtime's "absent" value (`None` / `undefined` /
  zero value) rather than raising, so optional injection is expressible.
- `isolate(key)` MUST return a child context in which reads and writes of `key` resolve
  against a fresh scope. The parent MUST be unaffected. Other keys MUST still resolve
  through the parent.

A **service** is any value published under a key. Publishing is an effect (§3) and
therefore reversible.

**One authority per key per scope.** Publishing a key that is already live **in the same
scope** MUST be rejected — the publish raises, and (when it happens inside `apply`) the
offending plugin rolls back to `FAILED` under §3 while the incumbent service and its
dependents are left completely undisturbed. Sequential publication is fine: once a provider
unloads and its key goes absent, another may claim it.

`isolate(key)` is the sanctioned way to run a different implementation of a key — that is
what it is *for*. Without this rule, overlapping providers force a runtime to invent a
restore policy, and the plausible ones are all wrong: restoring unconditionally on unload
clobbers a newer provider and resurrects a dead one, while never restoring silently
downgrades a key to absent while an older provider is still live. Both were observed in
first-generation runtimes, in opposite directions.

## 2. Plugins and injection

A **plugin** is a unit of composition with a `name`, an optional `inject` declaration, and
an `apply` body.

- `inject.required` names services the plugin cannot run without.
- `inject.optional` names services it will use if present but does not wait for.
- A plugin whose required services are not all present MUST remain `PENDING` and MUST NOT
  run `apply`.
- When the last missing required service appears, the plugin MUST transition to `LOADING`
  and run `apply`.
- When a required service is withdrawn, the plugin MUST unload (§4) and return to
  `PENDING` — not `DISPOSED`. It MUST re-activate if the service returns.
- Load order MUST be **derived from injection**, never from declaration order. A runtime
  MUST NOT require callers to hand-sequence mounts.

`apply` receives the plugin's own context. Anything it registers is an effect.

## 3. Effects — the reversibility rule

**Every registration MUST be reversible.** This is the load-bearing guarantee of the whole
kernel; a runtime that gets this wrong is not conformant regardless of what else passes.

- `effect(setup)` runs `setup`, which MAY return a **disposer**.
- On unload, collected disposers MUST run in **reverse registration order** (LIFO).
- A disposer MUST run **at most once**. Repeated disposal MUST be a no-op, not an error.
- A disposer MAY be asynchronous. Unload MUST NOT be considered complete until every
  disposer, including async ones, has settled.
- **[dragon]** If `setup` throws, every effect already collected by that plugin MUST be
  rolled back, and the plugin MUST enter `FAILED`.
- **[dragon]** Creating a new effect while the owner is `UNLOADING` MUST be rejected.
  Creation while `PENDING` or `LOADING` remains legal. Without this, a cleanup-time
  registration escapes the unload snapshot and leaks.
- **[dragon]** A **throwing disposer MUST NOT abort the chain.** Disposer errors are
  contained per disposer: every remaining disposer still runs, in order, and the fiber
  still reaches its target state (§4). The error MUST be observable — reported after
  unwinding completes, never swallowed. If several disposers throw, all errors are
  reported, not just the first.

  Without this, one badly-behaved plugin leaks every effect registered before it and
  strands the fiber mid-teardown. Both first-generation runtimes had this bug, and they
  disagreed about whether `dispose()` raised or resolved silently — which made a caller
  awaiting cleanup believe it had finished when it had not.

Publishing a service, registering an event listener, and mounting a child plugin are all
effects and all obey the rules above.

## 4. Fiber lifecycle

Each mounted plugin instance owns a **fiber** — the runtime handle for that instance.

```
          ┌──────────────────────────────────────────┐
          │                                          │
          ▼                                          │  required service
     ┌─────────┐   deps met   ┌─────────┐            │  withdrawn
     │ PENDING ├─────────────►│ LOADING │            │
     └─────────┘              └────┬────┘            │
          ▲                        │                 │
          │                  apply │ ok              │
          │                        ▼                 │
          │                   ┌────────┐             │
          └───────────────────┤ ACTIVE ├─────────────┘
                              └───┬────┘
                                  │ dispose()
                                  ▼
                            ┌───────────┐      ┌──────────┐
                            │ UNLOADING ├─────►│ DISPOSED │
                            └───────────┘      └──────────┘

     apply throws / config invalid
     LOADING ──────────────────────────────────────► FAILED
```

- `dispose()` MUST resolve only after **all** cleanup has settled, including async
  disposers and the recursive disposal of child plugins.
- Child plugins MUST be disposed **before** the parent's own effects. A parent's effect may
  own a resource a child still depends on.
- **[dragon]** `dispose()` called while the fiber is still `LOADING` MUST await `apply`'s
  completion, then run the full cleanup for everything `apply` collected. It MUST NOT
  abandon partially-collected effects, and it MUST NOT run cleanup concurrently with the
  remainder of `apply`.
- A fiber in `FAILED` MUST hold no live effects.
- **`dispose()` is total.** Disposing a `FAILED` fiber MUST retire the handle and enter
  `DISPOSED`. There is nothing to unwind — `FAILED` already holds nothing live — but the
  caller asked for disposal and disposal completed, so the terminal state is `DISPOSED`.
  The originating error remains available on the fiber. Disposing a `PENDING` fiber
  likewise enters `DISPOSED` without unwinding. Disposing an already-`DISPOSED` fiber is a
  no-op, not an error.

  Without this rule, `FAILED` has no outgoing edge and two runtimes will disagree about
  whether a failed fiber can ever be retired — which is exactly what happened.

## 5. Events

Events are typed and identified by name. Every event has exactly one **dispatch mode**,
which is part of its public contract and MUST NOT vary by call site.

| Mode | Awaited | Returns a value | Listener order | Semantics |
|---|---|---|---|---|
| `emit` | no | no | registration | fire-and-forget observation |
| `waterfall` | no | yes | registration | around-middleware (see below) |
| `parallel` | yes | no | concurrent | fan out, await all |
| `serial` | yes | yes | registration | ordered; first non-absent result wins |

**Waterfall** is around-middleware. A listener receives the arguments plus a `next`
callable.

- Calling `next()` delegates to the remaining listeners and returns their result, which the
  listener MAY wrap before returning.
- Returning **without** calling `next()` short-circuits the chain. Downstream listeners
  MUST NOT run.
- A listener that only observes MUST delegate.

Listener registration is an effect: a listener MUST be removed when its owning plugin
unloads (§3).

## 6. Manifest

A plugin's static declaration is described by `manifest.schema.json`. The manifest carries
identity (`name`, `version`), what it publishes (`provides`), what it needs (`inject`), and
free-form `config`. Manifests are data — they MUST be readable without executing plugin
code.

## 7. Conformance

A runtime is **conformant** iff it passes every fixture in `conformance/`.

Fixtures are language-neutral JSON, executed by a per-runtime harness (see
`conformance/README.md` for the fixture format and the harness contract). A runtime MUST
NOT skip a fixture; an unimplemented feature is a failure, not an exemption.

The two fixtures that matter most are `dispose-during-load` and `failed-apply-rolls-back`.
They encode DeepSeek divergence entry #6 — the reentrant-disposal gaps found in production.
A runtime that passes everything except those two is not close to done; it is missing the
part that bites.

## 7a. Runtime-specific obligations (NOT in the shared suite)

Some hazards are real but not language-neutral, so they cannot live in
`conformance/`. A runtime is **not** conformant merely by passing the shared fixtures; it
must also cover the obligations below **in its own test suite**, and say so in its README.

**Python / asyncio — cancellation.** A disposer that awaits MUST survive
`asyncio.CancelledError` in the enclosing task. Unload MUST be idempotent under repeated
cancellation, and cleanup MUST NOT be abandoned partway. Use `asyncio.shield` where
required.

This is not expressible in the shared fixtures: JavaScript promises have no cancellation,
so a `cancel` op would be meaningless for the TypeScript runtime. It was found by
mutation-testing during the first build — removing `asyncio.shield` from `dispose()` left
**all 16 fixtures green**. That is exactly the class of bug the shared suite cannot see, and
the reason this section exists.

**Any runtime with a concurrent scheduler** — `parallel` dispatch MUST genuinely fan out,
not await listeners in turn. `parallel-awaits-all` checks this via a deliberate 4x delay
margin; a runtime whose scheduler differs should add its own check.

The rule: **when a runtime discovers a hazard the shared fixtures cannot express, it adds a
native test AND records the obligation here.** A gap that lives only in one runtime's test
file is a gap the next runtime will rediscover the hard way.

## 8. Explicit non-requirements

To keep runtimes small, the kernel deliberately does **not** specify:

- Configuration file format, patch overlays, or hot module replacement.
- Plugin discovery, distribution, or installation.
- Any transport. The kernel is in-process. Composition MUST NOT require IPC.
- Approval, permission, or trust policy — those compose *on top* (paw's Instinct layer).
