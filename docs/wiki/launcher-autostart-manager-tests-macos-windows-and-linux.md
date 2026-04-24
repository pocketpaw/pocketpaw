---
{
  "title": "Launcher Autostart Manager Tests: macOS, Windows, and Linux",
  "summary": "This test module verifies the `AutoStartManager` class, which enables the PocketPaw launcher to start automatically at system login across macOS (Launch Agents plist), Windows (registry key), and Linux (XDG autostart .desktop file). Tests cover enable, disable, and status detection for each platform, with appropriate mocking of platform-specific APIs.",
  "concepts": [
    "AutoStartManager",
    "autostart",
    "Launch Agents",
    "plist",
    "Windows registry",
    "XDG autostart",
    ".desktop file",
    "platform abstraction",
    "get_executable_path",
    "PyInstaller",
    "launchd",
    "idempotent disable"
  ],
  "categories": [
    "testing",
    "launcher",
    "autostart",
    "cross-platform",
    "system integration",
    "test"
  ],
  "source_docs": [
    "5797991f8d0a2e50"
  ],
  "backlinks": null,
  "word_count": 447,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Autostart is a critical UX feature: users expect their AI companion to be available immediately after login without manually launching an app. The `AutoStartManager` abstracts the platform-specific mechanisms behind a uniform `enable()`, `disable()`, and `is_enabled()` interface.

Because the tests must run on any platform (CI runs on Linux even when testing macOS behavior), all platform-specific APIs are mocked. The test file also pre-stubs `pystray` and `PIL.Image` at module level before importing the autostart module, preventing `ImportError` in environments where those GUI libraries are absent.

## `TestGetExecutablePath`

`get_executable_path()` returns the path that will be registered in the platform autostart entry:

- **`test_frozen_returns_sys_executable`** — when `sys.frozen` is `True` (PyInstaller bundle), the path is `sys.executable` — the bundled binary.
- **`test_source_returns_sys_executable`** — when running from source, `sys.executable` is still the correct path (the Python interpreter). There is currently no distinction between frozen and source modes in the return value; both return `sys.executable`. This reflects the implementation as shipped.

## `TestMacOSAutoStart`

On macOS, autostart uses `~/Library/LaunchAgents/<bundle_id>.plist`. Tests use `tmp_path` to redirect the plist directory:

- **`test_is_enabled_false_by_default`** — no plist file exists initially → `is_enabled()` returns `False`.
- **`test_enable_creates_plist`** — calling `enable()` writes a plist file to the expected path. The plist must exist for launchd to pick it up at next login.
- **`test_disable_removes_plist`** — after `enable()`, `disable()` must delete the plist. A stale plist after disable would continue autostarting the app.
- **`test_disable_when_not_enabled`** — calling `disable()` when no plist exists must not raise. Idempotent disable prevents crashes during uninstall flows where the plist may already be absent.
- **`test_enable_then_is_enabled`** — full round-trip: enable → `is_enabled()` returns `True`.

## `TestWindowsAutoStart`

On Windows, autostart uses the `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` registry key. The `winreg` module is mocked (it only exists on Windows):

- **`test_is_enabled_no_winreg`** — if `winreg` is unavailable (non-Windows platform or mocked absent), `is_enabled()` returns `False` gracefully.
- **`test_enable_sets_registry_key`** — `enable()` calls `winreg.SetValueEx` with the application name and executable path.
- **`test_disable_deletes_registry_key`** — `disable()` calls `winreg.DeleteValue`. If the key does not exist, Windows raises `FileNotFoundError`; this must be caught silently.

## `TestLinuxAutoStart`

On Linux, autostart uses `~/.config/autostart/<app>.desktop` (XDG autostart spec):

- **`test_is_enabled_false_by_default`** — no `.desktop` file → not enabled.
- **`test_enable_creates_desktop_file`** — `enable()` writes a `.desktop` file with the correct `[Desktop Entry]` format, `Exec=` pointing to the executable, and `X-GNOME-Autostart-enabled=true`.
- **`test_disable_removes_desktop_file`** — `disable()` deletes the file.
- **`test_disable_when_not_enabled`** — idempotent, no exception.
- **`test_enable_then_is_enabled`** — round-trip verification.

## Known Gaps

No tests verify the plist or .desktop file content beyond existence — a malformed plist would pass these tests but fail at launchd load time. No tests cover the case where the autostart directory itself is missing or non-writable. The Windows registry mock does not verify the exact registry path used.