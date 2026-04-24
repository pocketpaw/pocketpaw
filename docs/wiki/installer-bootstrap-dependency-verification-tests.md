---
{
  "title": "Installer Bootstrap Dependency Verification Tests",
  "summary": "This test module reproduces and guards against a specific bootstrap bug where `uv install` succeeded but installed packages to the wrong Python interpreter, causing `_HAS_RICH` to be set `True` even though `rich` was not importable in the running process. Tests verify the `_verify_imports` helper logic and the uv command's Python targeting flags.",
  "concepts": [
    "installer bootstrap",
    "_verify_imports",
    "_HAS_RICH",
    "uv install",
    "Python targeting",
    "--python flag",
    "importlib.util.find_spec",
    "ImportError",
    "bootstrap bug",
    "plain-text fallback",
    "dependency detection"
  ],
  "categories": [
    "testing",
    "installer",
    "bootstrap",
    "dependency management",
    "bug regression",
    "test"
  ],
  "source_docs": [
    "fec6a368e65774fe"
  ],
  "backlinks": null,
  "word_count": 486,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Background: The Bootstrap Bug

The `installer.py` bootstrap script runs before PocketPaw is fully installed and must progressively enhance its own UI as dependencies become available. It sets flags like `_HAS_RICH = True` after attempting to install the `rich` library. The bug (introduced and reproduced in 2026-02-13 tests): `uv install` would succeed — exit code 0 — but install `rich` to a different Python interpreter than the one running the installer. The result: `_HAS_RICH` was set `True`, the installer tried to import `rich`, received an `ImportError`, and crashed instead of falling back to plain text output.

Because `installer.py` runs `_bootstrap_deps` at module import time, the file cannot be imported in tests without side effects. The test module therefore extracts the key functions as standalone implementations and tests those directly.

## `TestVerifyImports`

`_verify_imports_fn` is a standalone copy of the installer's `_verify_imports` function that uses `importlib.util.find_spec` to check whether packages are importable without actually importing them:

- **`test_returns_true_when_all_found`** — patches `find_spec` to return a mock spec for any package name. All packages found → returns `(True, True, True)` (all_ok, has_rich, has_inquirer).
- **`test_returns_false_for_missing_package`** — patches `find_spec` to return `None` for `rich`. The function must return `(False, False, True)` — not all_ok, has_rich=False, has_inquirer still True.
- **`test_partial_success`** — verifies that the per-package flags (`has_rich`, `has_inquirer`) are independent. A missing `inquirer` does not affect `has_rich`.

## `TestBootstrapBugReproduction`

Three tests directly document the bug and its fix:

- **`test_old_behavior_would_crash`** — constructs the failure scenario: `_verify_imports` says rich is available, but an actual `import rich` raises `ImportError`. This is the exact crash that users saw. The test demonstrates the failure mode rather than asserting the fix (it does not patch the module-level flag).

- **`test_uv_command_includes_python_flag`** — this is the fix verification. The installer's uv command must include `--python sys.executable` (or equivalent) to ensure packages are installed to the *same* Python that is running the installer. The test captures the actual subprocess command arguments and asserts the Python targeting flag is present. Without this flag, `uv` defaults to its own managed Python, which is a different interpreter.

- **`test_fallback_to_plain_text_when_all_cascades_fail`** — if both uv and pip fail to install dependencies, the installer must fall back to a plain-text UI rather than crashing. This is the last-resort resilience path that keeps the installer usable even in severely constrained environments.

## Design Notes

The test module explicitly documents that `installer.py` cannot be imported directly in tests due to the module-level `_bootstrap_deps()` call. This is a known architectural constraint of the installer design — the bootstrap logic is not structured for testability. The workaround (extracting functions as standalone copies) is fragile but necessary.

## Known Gaps

The standalone `_verify_imports_fn` copy in the test file may drift from the real implementation over time. No test verifies that the installer correctly detects the case where `find_spec` succeeds but the import itself fails (a subtler version of the original bug that could reappear if the spec check is insufficient).