---
{
  "title": "Launcher Bootstrap: Python Detection, Venv Creation, and Installation Tests",
  "summary": "This test module covers the `Bootstrap` class responsible for the launcher's dependency setup: detecting a suitable Python 3.11+ interpreter, creating a virtual environment, installing PocketPaw with optional extras, and reporting progress. It tests both the happy path and critical failure scenarios including missing Python, uv fallback, and pip failures.",
  "concepts": [
    "Bootstrap",
    "Python detection",
    "venv creation",
    "pip install",
    "uv fallback",
    "progress callback",
    "check_status",
    "Python version requirement",
    "launcher bootstrap",
    "_install_pocketpaw",
    "extras",
    "subprocess timeout"
  ],
  "categories": [
    "testing",
    "launcher",
    "bootstrap",
    "installation",
    "dependency management",
    "test"
  ],
  "source_docs": [
    "d3b5d4cde3a97820"
  ],
  "backlinks": null,
  "word_count": 476,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

Before the PocketPaw GUI launcher can start the server, it must ensure PocketPaw itself is installed in a virtual environment. The `Bootstrap` class automates this: it finds Python, creates a venv, installs pocketpaw, and reports progress back to the UI. Tests use `tmp_path` to isolate filesystem operations and mock subprocess calls to avoid real installations.

## `TestCheckStatus`

`check_status()` inspects the venv directory and returns the current bootstrap state:

- **`test_no_python_no_venv`** — neither Python nor a venv exists → status is `"not_installed"`.
- **`test_venv_exists_with_pocketpaw`** — the expected venv Python binary exists and pip reports pocketpaw as installed → status is `"installed"`.
- **`test_venv_exists_no_pocketpaw`** — venv Python exists but pip does not find pocketpaw → status is `"venv_only"`.

`_make_venv_python()` is a helper that creates the platform-correct Python binary path inside the venv directory (Unix: `venv/bin/python3`, Windows: `venv/Scripts/python.exe`).

## `TestCheckPythonVersion`

`_check_python_version()` runs `python --version` and parses the output:

- **`test_valid_python_312`** and **`test_valid_python_311`** — Python 3.11 and 3.12 are both accepted.
- **`test_old_python_310`** — Python 3.10 must be rejected. PocketPaw requires 3.11+ for certain asyncio features.
- **`test_python_not_found`** — `FileNotFoundError` from subprocess is caught and returns `None` (no usable Python found).
- **`test_python_timeout`** — `subprocess.TimeoutExpired` is caught and returns `None`. A frozen `python --version` call must not block the launcher UI thread.

## `TestGetInstalledVersion`

`_get_installed_version()` uses `pip show pocketpaw` to check the installed version:

- **`test_package_installed`** — returns the version string from pip output.
- **`test_package_not_installed`** — pip exits non-zero → returns `None`.
- **`test_pip_timeout`** — timeout is caught → returns `None`. Used to detect whether an update is needed.

## `TestBootstrapRun`

`run()` orchestrates the full bootstrap flow and accepts a `progress_callback(msg, pct)` function:

- **`test_successful_install`** — mocks Python detection and pip install; asserts the progress callback received percentage updates and the final status is `"installed"`.
- **`test_no_python_found`** — when no system Python is found and uv is also unavailable, `run()` must fail gracefully with a descriptive error message rather than crashing.
- **`test_no_python_but_has_uv`** — uv can create a venv using its own bundled Python. `fake_create_venv` simulates uv creating the venv structure. This is the fallback path for systems where only uv is installed.
- **`test_install_failure`** — pip returns a non-zero exit code during install. `run()` must surface the error and not leave the venv in a partially-installed state.

## `TestInstallPocketpaw`

`_install_pocketpaw()` returns `None` on success or an error string on failure:

- **`test_install_with_extras`** — when extras are configured (e.g., `pocketpaw[soul,vision]`), they must appear in the pip command.
- **`test_install_no_extras`** — plain `pocketpaw` install without extras.
- **`test_install_pip_failure`** — non-zero pip exit code → returns an error string containing the stderr content.

## Known Gaps

No tests cover the case where the venv creation step itself fails (e.g., insufficient disk space). No tests verify that the progress callback receives *monotonically increasing* percentage values — a regressing progress bar would be confusing but would not cause a test failure.