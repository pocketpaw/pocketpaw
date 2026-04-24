---
{
  "title": "Channel Adapter Base Classes and Auto-Install Utility",
  "summary": "This module defines ChannelAdapter (a structural Protocol) and BaseChannelAdapter (an abstract base class with lifecycle management), providing the contracts that all channel adapters must satisfy. It also contains the auto_install() utility that lets adapters install their optional Python dependencies at runtime using uv or pip, with a restart-required path for packages that cannot be reloaded in-process.",
  "concepts": [
    "ChannelAdapter",
    "BaseChannelAdapter",
    "auto_install",
    "optional dependencies",
    "uv",
    "pip",
    "sys.modules",
    "lifecycle management",
    "Protocol",
    "ABC",
    "channel routing",
    "import cache invalidation"
  ],
  "categories": [
    "message-bus",
    "channel-adapters",
    "dependency-management"
  ],
  "source_docs": [
    "0000000000000011"
  ],
  "backlinks": null,
  "word_count": 414,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## ChannelAdapter Protocol

`ChannelAdapter` is a `typing.Protocol` declaring three methods:

- `channel() -> Channel` — Returns the enum value identifying which platform this adapter handles.
- `async start(bus: MessageBus) -> None` — Registers the adapter with the bus and begins receiving/sending messages.
- `async stop() -> None` — Disconnects cleanly and cancels any background tasks.

Using `Protocol` (structural subtyping) means the bus can accept any object with these methods, including mocks in tests, without requiring inheritance.

## BaseChannelAdapter

`BaseChannelAdapter` is an ABC that provides concrete `start()` and `stop()` implementations that wrap the adapter's lifecycle:

1. `start()` stores the bus reference, calls `_on_start()` (adapter-specific logic), and registers the adapter's `send()` method with the bus to receive outbound messages destined for its channel.
2. `stop()` de-registers the send handler and calls `_on_stop()`.

The `send()` method (abstract) is responsible for converting an `OutboundMessage` to the platform's native format and delivering it. Separating the base lifecycle from the platform-specific delivery logic means subclasses cannot accidentally skip the bus registration step.

## auto_install()

Channel adapters have optional dependencies: the Discord adapter needs `discli`, the Matrix adapter needs `matrix-nio`, the Google Chat adapter needs `google-api-python-client`. Requiring all of them at install time would bloat the base package for users who only use Telegram.

`auto_install(extra, verify_import)` handles runtime installation:

```python
def auto_install(extra: str, verify_import: str) -> dict[str, str]:
    pip_spec = f"pocketpaw[{extra}]"
    # Prefer uv (fast), fall back to pip
    # Detect venv to choose --system vs default install target
    # Run subprocess install
    # Invalidate import caches and clear stale sys.modules entries
```

### Why uv First?
uv is significantly faster than pip for package resolution and installation. The function detects whether it's in a virtual environment to choose the right install target (`--system` flag for uv in non-venv contexts).

### Why Clear sys.modules?
After a successful install, Python's import machinery may still have cached a failed import attempt in `sys.modules`. Without clearing these entries, a subsequent `import discord` would return the cached failure rather than the newly installed package. The function iterates `sys.modules` and removes any keys matching `verify_import` or its sub-modules.

### restart_required Path
For packages that cannot be reloaded in-process (e.g., C extensions with global state), `auto_install` returns `{"status": "restart_required", "message": "..."}` instead of raising. Callers can surface this to the user rather than crashing silently.

## Known Gaps

The `auto_install` function uses a 120-second timeout for the subprocess install. On slow networks or CI environments with cold pip caches, this may be insufficient for large optional dependencies.