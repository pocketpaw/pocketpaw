---
{
  "title": "Paw Agent Factory: Wiring Soul, Bootstrap, and Tools Together",
  "summary": "get_paw_agent() is the single composition root for a fully configured paw agent: it loads PawConfig, awakens or births a Soul, wires SoulBridge and SoulBootstrapProvider, registers the four core soul tools, and returns a PawAgent dataclass ready for CLI commands. A startup guard raises a clear error if soul-protocol is not installed.",
  "concepts": [
    "PawAgent",
    "get_paw_agent",
    "Soul.awaken",
    "Soul.birth",
    "SoulBridge",
    "SoulBootstrapProvider",
    "ToolRegistry",
    "soul-protocol",
    "composition root",
    "factory function"
  ],
  "categories": [
    "paw",
    "agent-factory",
    "soul-protocol"
  ],
  "source_docs": [
    "efb62d34d2af0022"
  ],
  "backlinks": null,
  "word_count": 342,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`agent.py` is the composition root for the `paw` integration layer. Rather than scattering wiring logic across CLI command handlers, all construction logic lives in the single async factory function `get_paw_agent()`. CLI commands call it and get back a `PawAgent` dataclass containing every wired-up component.

## Startup Guard

The very first operation is a soul-protocol availability check:

```python
def _require_soul_protocol() -> None:
    try:
        import soul_protocol
    except ImportError:
        raise ImportError(
            "soul-protocol is required for paw. Install it with:\n"
            "  pip install pocketpaw[soul]\n"
            "  # or\n"
            "  pip install soul-protocol"
        ) from None
```

The `from None` suppresses the original `ImportError` chain so the user sees only the helpful install instructions rather than a Python traceback pointing at an internal import.

## Assembly Sequence

```python
async def get_paw_agent(project_root: Path | None = None) -> PawAgent:
    _require_soul_protocol()
    config = PawConfig.load(project_root)
    config.paw_dir.mkdir(parents=True, exist_ok=True)

    soul_path = config.soul_path or config.default_soul_path
    if soul_path.exists():
        soul = await Soul.awaken(soul_path)
    else:
        soul = await Soul.birth(name=config.soul_name, ...)

    bridge = SoulBridge(soul)
    bootstrap_provider = SoulBootstrapProvider(soul)

    registry = ToolRegistry()
    registry.register(SoulRememberTool(soul))
    registry.register(SoulRecallTool(soul))
    registry.register(SoulEditCoreTool(soul))
    registry.register(SoulStatusTool(soul))

    return PawAgent(soul=soul, bridge=bridge,
                    bootstrap_provider=bootstrap_provider,
                    registry=registry, config=config)
```

The `awaken` / `birth` branch prevents accidental re-initialization. If a `.soul` file already exists, the agent picks up where it left off with all existing memories intact.

## PawAgent Dataclass

`PawAgent` uses `Any`-typed fields for soul, bridge, bootstrap provider, and registry to avoid importing soul-protocol types at module level, which would defeat the lazy-import strategy.

## Why Four Soul Tools?

The four registered tools (`SoulRememberTool`, `SoulRecallTool`, `SoulEditCoreTool`, `SoulStatusTool`) represent the minimal set an agent needs to be self-aware. Extended tools (`SoulEvaluateTool`, `SoulReloadTool`) require `SoulManager` and are wired separately in the full PocketPaw daemon.

## Known Gaps

- **No soul save on agent creation**: After `Soul.birth()`, the new soul is not saved to disk. The CLI's `paw init` command saves it, but calling `get_paw_agent()` directly on a freshly initialized project will re-birth the soul on every invocation until a `.soul` file exists.
- **`PawAgent.registry` is assembled but not used in CLI commands**: The `ToolRegistry` exists for future programmatic use but CLI commands route through `AgentRouter` directly.