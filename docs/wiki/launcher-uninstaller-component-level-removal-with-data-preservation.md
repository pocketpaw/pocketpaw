---
{
  "title": "Launcher Uninstaller: Component-Level Removal with Data Preservation",
  "summary": "The launcher uninstall test suite validates PocketPaw's structured uninstall logic, covering component discovery, selective removal of runtime artifacts, and preservation of user data by default. Tests verify that the `Uninstaller` class correctly reflects filesystem state and handles missing or partially-installed environments without raising exceptions.",
  "concepts": [
    "uninstaller",
    "component-based removal",
    "POCKETPAW_HOME",
    "venv",
    "selective uninstall",
    "PID file",
    "autostart",
    "data preservation",
    "filesystem fixture",
    "pystray mock"
  ],
  "categories": [
    "installer",
    "lifecycle management",
    "test"
  ],
  "source_docs": [
    "de4a8eaae998967a"
  ],
  "backlinks": null,
  "word_count": 553,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

PocketPaw's uninstall system is built around a `Component`/`Uninstaller` model that treats each piece of the installation (venv, uv, config, memory, logs, audit, pid) as a named, discoverable unit. This granular approach prevents the accidental nuking of user data — a critical concern for any AI companion that accumulates conversation history and settings.

## Component Discovery

The `Uninstaller.get_components()` method returns a list of `Component` objects. Each component has a `name` and an `exists` attribute that reflects the actual filesystem state at call time. Tests confirm the expected set of components — `venv`, `uv`, `config`, `memory`, `logs`, `audit`, and `pid` — and verify that `exists` is only `True` when the corresponding directory or file is actually present under `POCKETPAW_HOME`.

This design matters because uninstall tools that do not check existence before removal can crash on partially-installed environments (e.g., a failed first-run setup). By surfacing existence as a property on the component object, the UI layer can present accurate checkboxes without additional filesystem probing.

## Selective Removal

The `Uninstaller.uninstall()` method accepts keyword flags (`remove_venv`, `remove_uv`, `remove_config`, `remove_memory`, `remove_python`, `remove_logs`) and only deletes what is explicitly requested. The PID file is always removed as it is a transient runtime artifact — leaving a stale PID would interfere with future launch attempts.

The default invocation (`uninstall()` with no arguments) is deliberately conservative: it removes the runtime artifacts (venv, uv, pid) but leaves config and memory intact. This default is validated by `test_preserves_config_and_memory_by_default`, which asserts that `config.json` and the `memory/` directory survive a default uninstall. The philosophical reason is that a user who reinstalls PocketPaw should not have to reconfigure their API keys and lose their conversation history.

## Handling Missing Components

`test_handles_missing_components` creates an empty temp directory and calls `uninstall()`. The system must not raise an exception — it should silently skip missing items and report them as "not found" in its result list. This guard prevents the uninstaller from crashing on a broken installation, where a user might be trying to clean up a half-failed setup.

## Autostart Integration

`test_disables_autostart` confirms that the uninstaller disables any system autostart registration (e.g., a launch agent or systemd service) as part of the uninstall flow. This prevents the app from attempting to relaunch after removal, which would cause confusing error dialogs.

## Return Values

The `uninstall()` method returns a list of human-readable result messages. Tests assert that component names appear in these messages, enabling the GUI to display a per-component uninstall report.

## Test Setup Pattern

The test suite uses a `setup_home` pytest fixture that creates a realistic fake `~/.pocketpaw` directory — including nested `venv/bin/`, `uv/`, `config.json`, and `audit.jsonl` — and patches `POCKETPAW_HOME` to point at it. This isolates tests from the developer's actual home directory and allows parallel test runs without collision.

Note the module-level mock injection of `pystray` and `PIL` via `sys.modules.setdefault`. These are GUI dependencies that may not be installed in the test environment; the stubs prevent `ImportError` on import.

## Known Gaps

- The test for `test_disables_autostart` patches the autostart call but does not verify the exact platform-specific mechanism (e.g., `launchctl unload` on macOS vs. `systemctl disable` on Linux). Cross-platform correctness of the autostart disable path is not covered here.
- `test_handles_missing_components` only asserts no exception is raised; it does not validate the exact wording of "not found" messages, leaving the result format under-specified.