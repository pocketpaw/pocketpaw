---
{
  "title": "Desktop Launcher Uninstaller: Selective Component Removal with Interactive Confirmation",
  "summary": "The `Uninstaller` class manages the removal of PocketPaw's desktop installation components — venv, uv binary, embedded Python, logs, config, memory, and audit data — with per-component granularity. Each component can be individually included or excluded, and the interactive mode prompts the user before removing sensitive data like memory and audit logs.",
  "concepts": [
    "Uninstaller",
    "Component",
    "shutil.rmtree",
    "selective uninstall",
    "memory preservation",
    "audit logs",
    "interactive confirmation",
    "venv removal",
    "embedded Python",
    "config removal"
  ],
  "categories": [
    "installer",
    "launcher",
    "lifecycle",
    "desktop"
  ],
  "source_docs": [
    "abc847bb6387bbaa"
  ],
  "backlinks": null,
  "word_count": 532,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`installer/launcher/uninstall.py` exists because PocketPaw's installer writes to multiple locations under `~/.pocketpaw/`. A naive "delete the folder" approach would work technically but would destroy data the user might want to keep — particularly chat memory and audit logs. The uninstaller models each installable artifact as a `Component` and gives the caller (and interactive user) fine-grained control over what gets removed.

## Component Model

```python
@dataclass
class Component:
    name: str
    description: str
    path: Path | None
    exists: bool
```

`get_components()` returns a list of all known components with their current existence status. The `exists` field is computed at call time by checking whether the path is present on disk — this prevents the uninstaller from reporting a component as removable when it was never installed (e.g., embedded Python only exists on Windows machines that lacked a system Python).

The seven components are:

| Component | Path | Notes |
|-----------|------|-------|
| `venv` | `~/.pocketpaw/venv/` | The Python venv — removing this breaks the server |
| `uv` | `~/.pocketpaw/uv/` | The uv binary downloaded by bootstrap |
| `python` | `~/.pocketpaw/python/` | Embedded Python (Windows only) |
| `logs` | `~/.pocketpaw/logs/` | Server and launcher logs |
| `config` | `~/.pocketpaw/config/` | User configuration files |
| `memory` | `~/.pocketpaw/memory/` | Persistent conversation memory |
| `audit` | `~/.pocketpaw/audit/` | Audit trail for security-relevant events |

## The uninstall() Method

`uninstall()` accepts boolean flags for each component. Each flag that is `True` triggers a `shutil.rmtree()` on the corresponding path (after confirming it exists). The function returns a list of strings describing what was removed — this is used by the tray's uninstall dialog to show a summary.

The memory and audit components are intentionally separated from the venv and config. A user reinstalling PocketPaw may want to keep their conversation history (`memory`) and security trail (`audit`) while wiping the package installation. The separate flags make this possible.

## Interactive Mode

`interactive_uninstall()` walks through each component present on disk and calls `_confirm()` to prompt the user. `_confirm()` accepts a default answer so that pressing Enter without typing gives the safer option (default is `False` — do not remove — for memory and audit; `True` for the technical components).

The interactive mode is designed for CLI use (e.g., running `python -m installer.launcher.uninstall`), not for the tray icon path. The tray icon calls `uninstall()` directly with pre-determined flags after showing its own confirmation dialog.

## Error Handling

Each `shutil.rmtree()` call is wrapped in a try/except that logs the error and continues. This ensures a partially-failed uninstall does not leave the process in an error state — the user sees which components were successfully removed in the returned list, and can re-run for the ones that failed.

## Known Gaps

- The uninstaller does not remove OS-level entries added by the auto-start feature (macOS LaunchAgent plist, Windows registry key). A complete uninstall requires the tray's `_on_toggle_autostart()` to be called first.
- `shutil.rmtree()` is not atomic; a crash mid-removal leaves a partially deleted directory that may confuse the next bootstrap run.
- There is no dry-run mode — `get_components()` shows what exists, but there is no way to preview exactly what would be deleted without running `uninstall()`.