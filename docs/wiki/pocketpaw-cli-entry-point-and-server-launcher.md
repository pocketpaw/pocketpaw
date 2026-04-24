---
{
  "title": "PocketPaw CLI Entry Point and Server Launcher",
  "summary": "The `__main__.py` module is PocketPaw's CLI entry point, handling argument parsing, subcommand dispatch, server startup with automatic port fallback, and channel mode selection. It has evolved significantly from a simple server launcher into a full-featured CLI with subcommands for diagnostics, memory inspection, session management, configuration, and log tailing.",
  "concepts": [
    "CLI entry point",
    "argparse",
    "early commands",
    "port fallback",
    "SO_REUSEADDR",
    "_run_async",
    "event loop",
    "Windows UTF-8",
    "subcommand routing",
    "channel modes",
    "pocketpaw serve"
  ],
  "categories": [
    "CLI",
    "server startup",
    "deployment",
    "cross-platform"
  ],
  "source_docs": [
    "5228d0ce49bdffde"
  ],
  "backlinks": null,
  "word_count": 466,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Architecture Overview

`__main__.py` is invoked via `python -m pocketpaw` or the `pocketpaw` console script. It is intentionally structured as a collection of thin dispatch functions rather than a monolithic `main()` — each subcommand is a separate module under `pocketpaw.cli.*`, imported lazily only when needed.

## Early Command Pattern

Subcommands that don't need the full settings/health/LLM stack are called "early commands" and handled before any expensive initialization:

```python
_EARLY_COMMANDS = {
    "update", "doctor", "health", "skills",
    "memory", "sessions", "config", "errors", "logs",
}
```

This pattern exists because initializing the settings module, loading the LLM configuration, and running health checks takes 1-3 seconds. Commands like `pocketpaw logs --follow` or `pocketpaw memory search` should be instant. The `_handle_early_command` function dispatches these before `get_settings()` is called.

## Subcommand Routing via `_resolve_subargs`

Argparse positional arguments (`subargs`) are mapped to named attributes based on the command:

```python
# pocketpaw channels start discord
# → args.subaction = "start", args.query = "discord"
```

This allows commands with natural language structure (`pocketpaw sessions delete <key>`) without requiring nested subparsers for each command variant.

## Automatic Port Fallback

The `_serve` function wraps server startup with port-scanning logic:

```python
def _serve(fn, *args, port=8888, max_attempts=10, host="127.0.0.1", **kwargs):
```

It probes `max_attempts` consecutive ports starting from the requested one, using a raw socket bind with `SO_REUSEADDR` deliberately NOT set. This means ports in `TIME_WAIT` state (recently closed connections) are correctly detected as busy and skipped. The probe uses the same `host` the server will bind to, preventing the `0.0.0.0` vs `127.0.0.1` mismatch that caused false positives in earlier versions.

## Windows UTF-8 Fix

The file begins with a platform guard that runs before any other imports:

```python
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
```

This prevents `UnicodeEncodeError` on Windows when the Rich logging or status output includes emoji or Unicode box-drawing characters. The `errors="replace"` fallback ensures the process never crashes on unencodable characters.

## Event Loop Compatibility (`_run_async`)

```python
def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()
```

This helper runs a coroutine synchronously from code that may itself be called inside a running event loop (e.g., under `pytest-asyncio`). Calling `asyncio.run()` when a loop is already running raises a `RuntimeError`; the thread fallback avoids this.

## Channel Modes

The CLI supports `--telegram`, `--discord`, `--slack`, `--whatsapp`, `--signal`, `--matrix`, `--teams`, `--gchat` flags for headless channel-only operation. The default mode (no flags) launches the web dashboard. The `serve` subcommand starts the API server without opening a browser.

## Known Gaps

The `--doctor` flag is marked deprecated in favor of `pocketpaw doctor` subcommand but is still present for backward compatibility. The `channels` subcommand is listed in the argument parser but its handler in `_handle_early_command` is not shown in the visible source — it is likely handled in the main dispatch block after early-command processing.