---
{
  "title": "CLI Update Command: Self-Update via uv with PyPI Version Check",
  "summary": "The `update` command checks PyPI for a newer version of PocketPaw and, if one is available, installs it using the `uv` package manager targeting the current Python interpreter. It handles the Windows file-locking edge case where the running executable cannot be replaced while the process is active.",
  "concepts": [
    "self-update",
    "uv package manager",
    "PyPI",
    "version check",
    "Windows file lock",
    "update_check",
    "sys.executable",
    "subprocess",
    "supply chain security"
  ],
  "categories": [
    "CLI",
    "Package Management"
  ],
  "source_docs": [
    "54fb0d3184e40e9f"
  ],
  "backlinks": null,
  "word_count": 511,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`src/pocketpaw/cli/update.py` implements the `pocketpaw update` subcommand. It provides a single-command upgrade path that checks for updates, confirms there is something to install, and runs the upgrade — all without requiring the operator to remember `pip` or `uv` invocations.

## Why `uv` Instead of `pip`?

PocketPaw is distributed and managed via `uv`, a fast Rust-based package installer. Using `uv pip install --upgrade` ensures the upgrade happens through the same tool that manages the installation, respecting any `uv.toml` constraints (such as the workspace's minimum release age policy). Using `pip` directly would bypass those constraints and potentially install a version that violates the supply chain security policy.

## Detecting `uv`

```python
uv_bin = shutil.which("uv")
if not uv_bin:
    print(f"  {RED}uv not found.{RESET} ...")
    return 1
```

`shutil.which` searches `PATH` for the `uv` binary. If it is not found, the command fails immediately with installation instructions rather than falling back to `pip`. This early exit is important: running `pip install --upgrade pocketpaw` in a `uv`-managed environment can corrupt the virtual environment's lock state.

## PyPI Version Check

```python
info = check_for_updates(current_version, get_config_dir())
```

`check_for_updates` is imported from `pocketpaw.update_check` and handles the PyPI query, caching, and comparison logic. The cache directory is `get_config_dir()` — typically `~/.pocketpaw/` — to avoid hitting PyPI on every invocation. If the network request fails, `info` is `None` and the update fails gracefully with a network error message.

## The Upgrade Command

```python
cmd = [uv_bin, "pip", "install", "--upgrade", "pocketpaw", "--python", sys.executable]
```

The `--python sys.executable` flag is critical: it tells `uv` to install into the same Python environment that is currently running PocketPaw. Without this flag, `uv` might install into a different environment, leaving the running `pocketpaw` binary pointing at an older version.

A 120-second timeout prevents the update from hanging indefinitely on slow connections.

## Windows File Lock Workaround

```python
if sys.platform == "win32" and "os error 32" in stderr:
    print("Packages downloaded, but the running process locks the exe.")
    print("Stop pocketpaw first, then run: uv pip install --upgrade pocketpaw")
    return 1
```

On Windows, a running `.exe` holds a file lock on itself, preventing the installer from replacing it. `uv` downloads the packages successfully but fails at the final file-replace step with error code 32 (`ERROR_SHARING_VIOLATION`). Rather than reporting a generic failure, this guard detects that specific condition and tells the operator the workaround: stop the server first, then re-run the install. This prevents confusion from a cryptic OS error message.

## Known Gaps

- **No `--dry-run` flag**: There is no way to check what version would be installed without actually running the upgrade. Adding a dry-run mode would let operators script update checks in CI without triggering upgrades.
- **Restart not automated**: After a successful update, the operator must restart PocketPaw manually. The command prints a reminder but does not attempt to restart the service. On systems with a process supervisor (systemd, launchd), automation is possible but not implemented.
- **Stderr truncated to 10 lines**: The error output is capped at 10 lines on failure. For verbose error output from `uv`, the root cause may be cut off.
