---
{
  "title": "PocketPaw Desktop Launcher Package Version Bootstrap",
  "summary": "Defines the `pocketpaw-launcher` package version using `importlib.metadata` with a fallback to the `POCKETPAW_VERSION` environment variable, supporting both installed-package and frozen-executable (PyInstaller) deployment scenarios. This thin init file establishes the package namespace for the launcher subsystem.",
  "concepts": [
    "importlib.metadata",
    "version resolution",
    "PyInstaller frozen executable",
    "POCKETPAW_VERSION env var",
    "package namespace",
    "bare except defensiveness",
    "desktop launcher",
    "dist-info",
    "build pipeline contract"
  ],
  "categories": [
    "installer",
    "launcher",
    "packaging",
    "cross-platform"
  ],
  "source_docs": [
    "installer/launcher/__init__.py"
  ],
  "backlinks": null,
  "word_count": 403,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`installer/launcher/__init__.py` is the package initialization file for PocketPaw's desktop launcher. It serves one primary function: establishing the `__version__` string that the rest of the launcher can reference for display, logging, and update checks.

## Version Resolution Strategy

The version resolution uses a two-stage fallback:

```python
try:
    from importlib.metadata import version as _meta_version
    __version__ = _meta_version("pocketpaw-launcher")
except Exception:
    import os
    __version__ = os.environ.get("POCKETPAW_VERSION", "0.1.0")
```

**Stage 1 — importlib.metadata:** When the launcher is installed as a proper Python package (via pip or uv), `importlib.metadata.version()` reads the version from the package's `METADATA` file. This is the authoritative source: it reflects exactly what was installed, not what was hardcoded.

**Stage 2 — Environment variable:** When the launcher is bundled as a frozen executable by PyInstaller, `importlib.metadata` may not find the package metadata because PyInstaller doesn't always bundle `dist-info` directories by default. The fallback reads `POCKETPAW_VERSION` from the environment — a variable that the build pipeline sets at bundle time. If neither source is available, the hardcoded `"0.1.0"` ensures the launcher can always report *some* version rather than crashing.

## Why a Bare Except

The `except Exception` (rather than `except PackageNotFoundError`) is intentional defensiveness. The launcher targets non-technical users on three platforms. Any version of the metadata lookup that fails — whether due to a missing `dist-info`, a corrupted package database, or a Python version that predates `importlib.metadata` (Python < 3.8) — should silently fall back rather than surface a traceback to a user who just double-clicked the app icon.

## Package Namespace Purpose

Beyond the version string, the `__init__.py` establishes the `installer.launcher` package namespace. The `__main__.py` entry point and `autostart.py` both use relative imports (`from .common import POCKETPAW_HOME`, `from installer.launcher.common import ...`). Without this `__init__.py`, those imports would fail in both installed and frozen modes.

## Relationship to the Build Pipeline

The build pipeline is expected to set `POCKETPAW_VERSION` as an environment variable before invoking PyInstaller. This creates a clear contract: the build system knows the version (from the git tag or release pipeline), and the binary reflects it at runtime without requiring a `dist-info` lookup that may not survive the PyInstaller bundle step.

## Known Gaps

The hardcoded fallback version `"0.1.0"` could mislead operators diagnosing production issues if neither the metadata nor the environment variable is set. A version like `"unknown"` might be more honest, but `"0.1.0"` was chosen to always be a valid semver string for downstream consumers that parse the version.