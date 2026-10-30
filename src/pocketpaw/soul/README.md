# soul/

PocketPaw's integration layer with [soul-protocol](https://github.com/OCEAN/soul-protocol) — the persistent identity + memory system. This module owns the soul lifecycle for the running agent: birth/awaken from a `.soul` file, observe interactions, periodically save, and feed soul state into the agent's bootstrap context.

The public interface is everything exported from `__init__.py`. Implementation lives in private files (`_manager.py`, `_cognitive.py`, `_bridge.py`) and may be reorganized freely without affecting callers, as long as the public surface keeps the same behavior. Consumers — agent loop, agent pool, tool bridge, bootstrap context builder, the API router, the instinct correction loop — only import from `pocketpaw.soul`.

## Public surface

| Name | What it is | Used by |
|---|---|---|
| `SoulManager` | Lifecycle owner: birth/awaken/save/observe/reload/import/forget. Holds the active `Soul` instance plus its `bridge` and `bootstrap_provider`. | Agent loop on startup; API router for HTTP endpoints |
| `get_soul_manager()` | Returns the global `SoulManager` (singleton) or `None` if not initialized yet. | Anywhere that wants to consume soul state without having a direct reference |
| `set_soul_manager(manager)` | Registers (or clears) the global singleton. `SoulManager.initialize()` already calls this — exposed for callers that want explicit registration. | Agent loop |
| `PocketPawCognitiveEngine` | `CognitiveEngine` impl that routes soul-protocol's cognitive calls (significance, fact extraction, reflection) through PocketPaw's active agent backend, with optional direct-Anthropic fast path for cheaper models. | Agent loop, builds and passes to `SoulManager.initialize(engine=...)` |
| `SoulBootstrapProvider` | Maps soul state → `BootstrapContext` for the agent context builder. Wraps the default provider so `INSTRUCTIONS.md` and `USER.md` are still loaded. | `bootstrap.context_builder` |
| `SoulBridge` | Thin async helper for `observe(user_input, agent_output)` and `recall(query)`. | Agent loop's per-turn observation hook; tools that read/write memory |

## Lifecycle

1. **Startup.** Agent loop creates a `PocketPawCognitiveEngine` and a `SoulManager`, then calls `manager.initialize(engine=...)`. Manager births or awakens the `.soul` file, creates a `SoulBridge` and a `SoulBootstrapProvider`, and registers itself as the global singleton.
2. **Per turn.** Agent loop calls `manager.observe(user_input, agent_output)` once per completed turn. The bridge forwards an `Interaction` into the soul.
3. **Periodic.** A background `_auto_save_loop` saves dirty state, detects external `.soul` changes (and reloads), and consolidates memory via `soul.reflect()` every 10 observations.
4. **Shutdown.** Agent loop calls `manager.shutdown()` — cancels the auto-save task, saves once more.

## Tests

`tests/test_soul_manager.py`, `tests/test_soul_cognitive_engine.py`, `tests/test_paw_bridge.py`, `tests/test_soul_integration.py`, `tests/test_soul_v024_smoke.py`. Behavioral tests import from `pocketpaw.soul`; tests that need to flip the singleton state import `_reset_manager` from `pocketpaw.soul._manager` directly.

## Out of scope for this folder

- HTTP routes — `src/pocketpaw/api/v1/soul.py`
- The instinct correction → soul handoff — `ee/instinct/correction_soul_bridge.py`
- The standalone `paw` CLI's soul bootstrap — `src/pocketpaw/paw/agent.py`

These all consume `pocketpaw.soul` (the public surface) but live elsewhere because the dependency direction is theirs-to-soul, not the other way around.
