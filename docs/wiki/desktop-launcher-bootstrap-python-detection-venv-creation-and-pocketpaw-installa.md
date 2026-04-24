---
{
  "title": "Desktop Launcher Bootstrap: Python Detection, venv Creation, and PocketPaw Installation",
  "summary": "The Bootstrap module is the entry point for the PocketPaw desktop launcher's first-run experience. It detects a suitable Python interpreter, creates an isolated virtual environment, and installs the PocketPaw package — handling Windows edge cases by downloading an embeddable Python build when none is found.",
  "concepts": [
    "Bootstrap",
    "virtual environment",
    "venv",
    "uv",
    "pip",
    "Python detection",
    "Windows embeddable Python",
    "CREATE_NO_WINDOW",
    "progress callback",
    "idempotency",
    "dev mode",
    "PyPI installation",
    "subprocess flags"
  ],
  "categories": [
    "installer",
    "launcher",
    "desktop",
    "environment-setup"
  ],
  "source_docs": [
    "df111fd23a9ae866"
  ],
  "backlinks": null,
  "word_count": 591,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

The `Bootstrap` class in `installer/launcher/bootstrap.py` orchestrates the full environment setup that must succeed before PocketPaw can run. It bridges the gap between a raw user machine — which may have no Python at all — and a working PocketPaw installation. The module is deliberately self-contained, avoiding imports from outside the launcher package so it can operate before the full venv exists.

## Key Responsibilities

### Python Discovery and Version Gating

`_check_python_version()` guards against Python versions below `MIN_PYTHON = (3, 11)`. This threshold exists because PocketPaw uses `match` statements, `tomllib`, and modern `asyncio` features that simply don't exist in older releases. Rather than surfacing cryptic import errors at runtime, the bootstrap fails fast with a readable message.

### Windows Embeddable Python Fallback

One of the most defensive patterns in the codebase is `_create_manual_venv_from_embedded()`. On Windows, users who install PocketPaw may not have Python on their PATH at all. The module downloads a specific Python embeddable ZIP from `PYTHON_EMBED_URL`, extracts it to `EMBEDDED_PYTHON_DIR`, and uses it as the venv base. `PYTHON_EMBED_VERSION` and `PYTHON_EMBED_URL` are constants that must be kept in sync — if they drift, the download succeeds but the venv creation fails silently.

### uv-First, pip-Fallback Installation

The module strongly prefers `uv` for package installation because it is dramatically faster than pip for first-run scenarios. `_uv_path()` resolves the uv binary from `UV_DIR` (inside `~/.pocketpaw/`). If uv is unavailable, the module falls back to pip inside the new venv. `_resolve_uv_version()` fetches the latest uv release from the GitHub API and caches the result for 24 hours — this prevents hammering the API on repeated bootstrap attempts while still tracking upstream releases.

### Progress Callbacks

All long-running steps accept a `progress(msg, pct)` callback. This is injected by the `SplashWindow` so the UI progress bar stays accurate. The `_noop_progress` function acts as a null object when no UI is present (headless installs, CI).

### Idempotency via `check_status()`

`check_status()` returns a `BootstrapStatus` describing whether the venv exists and whether PocketPaw is already installed at an acceptable version. The `run()` method checks this first and skips work that is already complete. This means re-running the launcher after a successful install is fast and non-destructive.

### Subprocess Safety on Windows

`_SUBPROCESS_FLAGS` injects `creationflags=0x08000000` (`CREATE_NO_WINDOW`) on Windows only. Without this flag, every `subprocess.run()` call during bootstrap causes a brief console window flash — noticeable and confusing in a GUI context (Tauri launcher). The flag is a Windows API constant, not a Python abstraction, so the comment explaining it is load-bearing.

### Error Formatting

`_format_pip_error()` parses pip's stderr to extract the most human-readable line. pip's error output is verbose; surfacing the raw 40-line traceback in the splash window would be unreadable. This method exists specifically to produce a one-line error message suitable for display.

## Data Flow

```python
Bootstrap(progress_cb)
  .check_status()         # fast path — already installed?
  .run(extras, branch)    # detect python → download if needed → uv/pip install
```

The `run()` method accepts `branch` and `local_path` for dev-mode installs — instead of pulling from PyPI, the bootstrap installs from a git branch or a local directory. This is gated by `DEV_MODE_MARKER` from `common.py`.

## Known Gaps

- `UV_OVERRIDES` and `UV_PINNED_VERSION` are defined as constants but their interaction is not fully documented — it is unclear whether pinned always wins or whether overrides can supersede it.
- The 24-hour uv version cache (`_resolve_uv_version`) has no invalidation mechanism beyond time; a corrupted cache file will persist for a full day.
- `_create_manual_venv_from_embedded` is only implemented for Windows; Linux/macOS users without Python receive a generic error rather than a platform-specific download.