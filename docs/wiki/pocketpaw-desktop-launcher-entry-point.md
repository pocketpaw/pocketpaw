---
{
  "title": "PocketPaw Desktop Launcher Entry Point",
  "summary": "The main entry point for the PocketPaw desktop launcher, handling first-run bootstrap (Python venv setup, pip install), subsequent-run startup (server launch, browser open, tray icon), and headless mode for server environments. Includes a PyInstaller package fixup that repairs relative imports when the frozen executable runs `__main__.py` as a top-level script.",
  "concepts": [
    "PyInstaller frozen executable",
    "package fixup",
    "sys.modules aliasing",
    "__package__ repair",
    "Bootstrap",
    "ServerManager",
    "system tray icon",
    "headless mode",
    "logging setup",
    "threading model",
    "first-run detection"
  ],
  "categories": [
    "launcher",
    "installer",
    "desktop-app",
    "cross-platform"
  ],
  "source_docs": [
    "installer/launcher/__main__.py"
  ],
  "backlinks": null,
  "word_count": 473,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`installer/launcher/__main__.py` is the entry point for PocketPaw's desktop launcher — the binary that non-technical users double-click to start PocketPaw. It orchestrates two distinct startup flows and contains a PyInstaller fixup that is critical for the frozen executable to function at all.

## The PyInstaller Package Fixup

The most technically significant block in this file addresses a frozen-executable import failure:

```python
if __package__ is None or __package__ == "":
    # Register the launcher package so relative imports work
    __package__ = "launcher"
    try:
        importlib.import_module("launcher")
    except ImportError:
        pkg = types.ModuleType("launcher")
        pkg.__path__ = [str(_this_dir)]
        sys.modules["launcher"] = pkg

    # Alias installer.launcher -> launcher
    if "installer" not in sys.modules:
        sys.modules["installer"] = types.ModuleType("installer")
    if "installer.launcher" not in sys.modules:
        sys.modules["installer.launcher"] = sys.modules["launcher"]
```

When PyInstaller runs `__main__.py` as a script entry point, `__package__` is `None`, which breaks all relative imports (`from .bootstrap import Bootstrap`). The fix manually registers the `launcher` package in `sys.modules` and aliases `installer.launcher` to the same module object. This ensures `from installer.launcher.common import POCKETPAW_HOME` works in both the installed-package path and the frozen-exe path.

## Startup Flows

**First run** — The `Bootstrap` module detects that no PocketPaw venv exists, shows a splash screen, creates the venv, runs `pip install pocketpaw`, and validates the install. Only after a successful bootstrap does it hand off to the server start path.

**Every run** — `ServerManager` starts the PocketPaw FastAPI server as a subprocess, waits for the health endpoint to respond, then opens the dashboard in the default browser. If `--tray` is passed (the default on macOS/Windows), a system tray icon is shown with start/stop/quit controls.

**Headless mode** — `_run_headless(server)` is used on Linux servers and CI environments where no display is available. It blocks on `server.wait()` and handles `KeyboardInterrupt` cleanly so the process exits with code 0 on Ctrl-C.

## Logging Setup

```python
LOG_DIR  = POCKETPAW_HOME / "logs"
LOG_FILE = LOG_DIR / "launcher.log"
```

Logging is configured to write to `~/.pocketpaw/logs/launcher.log` before any other imports run. This is deliberate: if the bootstrap or import machinery fails, the error should land in a log file rather than disappearing silently on a desktop double-click where stdout is not visible to the user.

## Tray Icon Threading

The system tray icon runs on the main thread (required by most OS tray APIs — AppKit on macOS, win32 on Windows). The PocketPaw server runs on a background thread (`threading.Thread`). The main thread's event loop is the tray's run loop. When the user quits via the tray menu, the main thread signals the server thread to stop and waits for it to join before exiting.

## Known Gaps

The `_run_headless` function uses a bare `time.sleep` poll loop to check server health rather than a proper async wait. On slow machines, the fixed sleep interval may either timeout too early or waste time. A future improvement would use the server's health endpoint with exponential backoff.