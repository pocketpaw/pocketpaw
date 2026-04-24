---
{
  "title": "PocketPaw Interactive Installer",
  "summary": "A self-contained, single-file installer that guides users through PocketPaw setup with optional rich prompts, falling back to plain text input when the terminal environment cannot support InquirerPy. Supports both interactive and headless (non-interactive) installation profiles, with a robust dependency bootstrap cascade that verifies each step before proceeding.",
  "concepts": [
    "standalone installer",
    "InquirerPy",
    "OSError(22) macOS fix",
    "terminal fallback",
    "dependency bootstrap cascade",
    "verify imports",
    "UTF-8 Windows fix",
    "SystemCheck",
    "installation profiles",
    "non-interactive mode",
    "curl pipe install"
  ],
  "categories": [
    "installer",
    "cli",
    "cross-platform",
    "bootstrap"
  ],
  "source_docs": [
    "installer/installer.py"
  ],
  "backlinks": null,
  "word_count": 530,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`installer/installer.py` is PocketPaw's primary installation script. It is intentionally a standalone file with no local imports so it can be piped directly from the web (`curl | sh`) without requiring a pre-existing PocketPaw installation.

## Design Principles

**No local imports** — the installer must work before PocketPaw exists on the system. All imports are stdlib or dynamically bootstrapped.

**Graceful degradation** — it attempts to install InquirerPy and Rich for a polished terminal UI, but falls back to `input()` calls if they cannot be installed or if the terminal doesn't support them.

**Two modes** — interactive (guided prompts) and `--non-interactive --profile <name>` (headless, for CI and enterprise deployments).

## Dependency Bootstrap Cascade

The installer cannot assume InquirerPy or Rich are present. `_bootstrap_deps()` attempts installation in order:

1. `uv pip install InquirerPy rich` (fastest, preferred)
2. `pip install InquirerPy rich` (fallback)
3. System package manager (last resort)

Critically, after each attempt, `_verify_imports(packages)` calls `importlib.util.find_spec` for each package rather than blindly setting `_HAS_RICH = True`. This fix (PR #184 follow-up, 2026-02-13) prevents the installer from proceeding with a broken UI state where it believes Rich is available but the import fails at runtime.

## The OSError(22) macOS Bug Fix

Fix #184 (2026-02-17) addresses a crash specific to macOS when the installer is run via `curl | sh`:

```
OSError: [Errno 22] Invalid argument
```

prompt_toolkit (InquirerPy's terminal engine) uses `termios` and `fcntl` calls that fail when stdin is a pipe rather than a TTY. The fix wraps every InquirerPy prompt call in a `try/except OSError` and falls back to `input()`. Crucially, the first `OSError` globally disables InquirerPy (`_HAS_INQUIRER = False`) so subsequent prompts don't try and fail again — the user gets a consistent plain-text experience for the entire session rather than alternating between rich and plain.

## Windows UTF-8 Fix

Windows defaults to the system code page (e.g., cp1252) which cannot encode the Unicode/emoji characters used in installer output. The installer forces UTF-8 before any output:

```python
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
```

The `errors="replace"` guard prevents crashes if a character genuinely cannot be encoded even with UTF-8 — it substitutes a replacement character instead of raising.

## System Detection

`SystemCheck.run_all()` populates a `SystemInfo` dataclass with:
- Python version (minimum 3.10 required)
- Available package manager (`uv` preferred over `pip`)
- Platform and architecture
- Available disk space and memory

The check runs before any installation attempt. If Python is below the minimum version, the installer prints a clear error and exits with code 1 rather than proceeding and failing cryptically later.

## Installation Profiles

Profiles bundle a set of optional dependencies and configuration defaults:

- `minimal` — core PocketPaw only
- `recommended` — core + voice + dashboard UI dependencies
- `developer` — recommended + dev tooling

In non-interactive mode, `--profile recommended` is the standard headless path used by enterprise deployment scripts.

## Known Gaps

The installer does not validate the PocketPaw version installed after `pip install pocketpaw` — it assumes the latest published version is correct. In environments where package manager age restrictions are enforced (see supply chain security policy), the install may fail with an opaque error if the latest version is too new.