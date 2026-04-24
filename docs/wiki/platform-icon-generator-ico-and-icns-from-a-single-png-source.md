---
{
  "title": "Platform Icon Generator: .ico and .icns from a Single PNG Source",
  "summary": "The `make_icons.py` script generates platform-specific application icon files for Windows (`.ico`) and macOS (`.icns`) from a single source `icon.png`. It uses the native macOS `iconutil` tool when available and falls back to a Pillow-based implementation for cross-platform compatibility.",
  "concepts": [
    "ico",
    "icns",
    "Pillow",
    "iconutil",
    "icon generation",
    "multi-resolution icons",
    "RGBA mode",
    "iconset",
    "Retina display",
    "cross-platform icons",
    "Windows shell",
    "macOS packaging"
  ],
  "categories": [
    "installer",
    "build-tooling",
    "desktop",
    "assets"
  ],
  "source_docs": [
    "4a51bed759cb3afd"
  ],
  "backlinks": null,
  "word_count": 501,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`installer/launcher/build-launcher/make_icons.py` exists because application icons are not portable across platforms. Windows expects a multi-resolution `.ico` bundle; macOS expects an `.icns` archive. Generating both from the same PNG source keeps the design source of truth in one file while producing the correct artifact for each platform's native loader.

## Windows .ico Generation

`make_ico()` opens the source PNG with Pillow, ensures the image is in RGBA mode (preserving the alpha channel for transparency), and saves it as a multi-resolution `.ico` file. The `ICO_SIZES` constant defines the six resolutions: `[16, 32, 48, 64, 128, 256]`. This set matches the Windows Shell icon cache sizes — omitting any of these would cause Windows to upscale from the nearest available size, producing blurry icons at common display DPIs.

The RGBA mode check prevents a subtle failure: Pillow's `.ico` encoder raises an error if the source image is in palette or RGB mode. Forcing RGBA before encoding makes the function robust to source images that were saved without an alpha channel.

## macOS .icns Generation

The `.icns` path has two implementations, selected at runtime:

### Primary: iconutil

`_make_icns_iconutil()` builds an `iconset` directory, populates it with the required resolution variants defined in `ICNS_SIZES`, and calls `iconutil --convert icns`. This is Apple's official tool and produces the most compatible `.icns` files, including Retina (`@2x`) variants. The `ICNS_SIZES` dict maps filenames to pixel dimensions — the `@2x` entries use doubled pixel counts (e.g., `icon_16x16@2x.png` is 32×32 pixels).

A temporary directory is used for the iconset so cleanup is guaranteed even on failure. `shutil.which('iconutil')` gates this path — `iconutil` only ships on macOS, so the function never attempts it on Linux or Windows CI agents.

### Fallback: Pillow

`_make_icns_pillow()` saves the image directly as `.icns` using Pillow's built-in encoder. This works on any platform but produces a lower-fidelity result — Pillow's `.icns` support is limited and does not generate all required resolution variants. The fallback is documented as "limited but functional", meaning it is acceptable for development builds but not for production releases where macOS Gatekeeper and Spotlight need the full resolution set.

## Usage Pattern

```python
# Invoked by build.py's ensure_icons():
make_ico(SOURCE_PNG, ASSETS_DIR / "icon.ico")
make_icns(SOURCE_PNG, ASSETS_DIR / "icon.icns")
```

Both functions are also exposed as a CLI via `main()`, allowing manual regeneration:

```python
python installer/launcher/build-launcher/make_icons.py
```

## Error Handling

The script imports Pillow at the top level and calls `sys.exit(1)` if it is missing. This is intentional: unlike other optional dependencies, Pillow is required for both paths and there is no meaningful fallback without it.

## Known Gaps

- The `@2x` Retina variants in the Pillow fallback path are not generated; only a single resolution is saved. This means macOS apps built on non-macOS CI will have lower-quality dock and Spotlight icons.
- There is no validation that the source PNG is large enough (at least 512×512) to produce all required sizes without upscaling artifacts.
- Windows High DPI (`256@2x` = 512px) is not included in `ICO_SIZES`; very high-DPI Windows displays may show slightly blurry taskbar icons.