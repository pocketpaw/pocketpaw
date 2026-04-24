---
{
  "title": "Desktop Launcher Updater: PyPI Version Checks and In-Place venv Upgrades",
  "summary": "The `Updater` class polls PyPI for newer PocketPaw releases and applies updates by running `uv pip install --upgrade` (or falling back to pip) inside the existing venv. Dev mode is detected via a marker file and switches the update path from PyPI to a git branch install.",
  "concepts": [
    "Updater",
    "UpdateInfo",
    "PyPI JSON API",
    "uv pip install",
    "dev mode",
    "DEV_MODE_MARKER",
    "version comparison",
    "git branch install",
    "in-place upgrade",
    "venv upgrade",
    "update notification"
  ],
  "categories": [
    "installer",
    "launcher",
    "update-management",
    "desktop"
  ],
  "source_docs": [
    "20596c9fd4a1d43d"
  ],
  "backlinks": null,
  "word_count": 498,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`installer/launcher/updater.py` provides the update lifecycle that the tray icon's periodic check and "Check for Updates" menu item use. It is designed to be non-disruptive: updates are downloaded into the existing venv without touching the server process. The server must be restarted separately (the tray handles this) to pick up the new code.

## UpdateInfo Dataclass

```python
@dataclass
class UpdateInfo:
    current_version: str | None
    latest_version: str | None
    update_available: bool
    dev_mode: bool
    dev_branch: str | None
    error: str | None
```

Returning a structured object rather than raising exceptions allows the tray to distinguish between "no update available", "update available", "dev mode", and "check failed" without catching exception types. The `error` field carries a human-readable message for display in the notification.

## Version Check

`check()` fetches `https://pypi.org/pypi/pocketpaw/json` and reads the `info.version` field. It compares against the currently installed version via `get_installed_version()` from `common.py`. `_version_newer()` does a tuple comparison after splitting the version string on `.` — this handles the standard `MAJOR.MINOR.PATCH` scheme but would misorder pre-release versions (e.g., `1.0.0b1` vs `1.0.0`).

The PyPI JSON API is used rather than the simple index because it returns structured data including the latest version string directly, avoiding HTML parsing.

## Dev Mode

`is_dev_mode()` checks for `DEV_MODE_MARKER`. When in dev mode, `check()` returns an `UpdateInfo` with `dev_mode=True` and skips the PyPI fetch entirely. `apply()` in dev mode calls `_update_from_branch()` instead of `uv pip install`.

`_update_from_branch()` reads the branch name from the marker file and runs:

```python
uv pip install "git+{GIT_REPO_URL}@{branch}"
```

This allows developers to test launcher updates against feature branches without publishing to PyPI. The marker file is written by the bootstrap's `--branch` mode.

## Update Application

`apply()` runs `uv pip install --upgrade pocketpaw` inside the venv (falling back to `pip install --upgrade pocketpaw` if uv is unavailable). It uses `on_status` callbacks to report progress to the tray, which forwards them to the OS notification system.

The update does not stop the server before upgrading. Python imports are file-based; the new package files are written into the venv's `site-packages` directory while the old ones remain in use by the running process. The next server restart will load the updated code. This is safe because Python does not hold locks on `.py` files.

## Error Propagation

All network and subprocess errors are caught and stored in `UpdateInfo.error`. The tray displays the error in a notification and does not retry automatically — the next periodic check (24 hours later) will try again. This avoids retry storms on network outages.

## Known Gaps

- `_version_newer()` uses a simple string split comparison that fails for pre-release version strings. PocketPaw's release process should avoid pre-release tags in the public package to work around this.
- The PyPI fetch has no timeout specified; on a slow network the update check could block for a very long time before the default urllib timeout kicks in.
- There is no rollback mechanism; if the updated package has a startup bug, the user must manually reinstall the previous version.