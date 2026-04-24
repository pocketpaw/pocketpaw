---
{
  "title": "Desktop Launcher Build Pipeline: PyInstaller, Code Signing, DMG, and Inno Setup",
  "summary": "The build script packages the PocketPaw desktop launcher into a distributable native application for macOS and Windows. It chains icon generation, PyInstaller bundling, platform-specific code signing, DMG creation for macOS, and Inno Setup invocation for Windows into a single `build()` function.",
  "concepts": [
    "PyInstaller",
    "code signing",
    "DMG",
    "Inno Setup",
    "launcher build",
    "macOS packaging",
    "Windows packaging",
    "icon generation",
    "pystray",
    "hdiutil",
    "APPLE_DEVELOPER_ID",
    "ad-hoc signing",
    "distribution artifact"
  ],
  "categories": [
    "installer",
    "build-tooling",
    "desktop",
    "packaging"
  ],
  "source_docs": [
    "a17e99ae0e2705fd"
  ],
  "backlinks": null,
  "word_count": 519,
  "compiled_at": "2026-04-24T04:26:10Z",
  "compiled_with": "agent",
  "version": 1,
  "audience": "human",
  "depth": "deep",
  "target_words": 500
}
---

## Overview

`installer/launcher/build-launcher/build.py` is the CI-facing entry point for producing distributable launcher binaries. It is designed to run on the host platform — macOS produces a `.app` and optionally a `.dmg`; Windows produces an `.exe` installer via Inno Setup. The script uses `argparse` for a `--version` flag so CI can inject the release version without editing source.

## Pipeline Stages

### Dependency Check

`check_deps()` verifies that `PyInstaller`, `pystray`, and `Pillow` are installed before any work starts. This prevents mid-build failures where PyInstaller succeeds but the tray icon import crashes at freeze time. The function returns a boolean so `main()` can exit cleanly rather than raise.

### Icon Generation

`ensure_icons()` calls into `make_icons.py` (the adjacent script) to produce `icon.ico` and `icon.icns` from `icon.png` if they don't already exist. The guard avoids regenerating icons on every build — icon generation is slow on macOS because `iconutil` must be invoked. This is an idempotency optimization, not just laziness: the output files are deterministic given the same source PNG, so skipping them is always safe.

### PyInstaller

The core freeze step runs `pyinstaller launcher.spec` from `BUILD_DIR`. The `.spec` file (tracked in the repo) controls what gets bundled: the pystray backend, Pillow, tkinter, and the launcher package itself. Using a spec file rather than command-line flags ensures reproducible builds — flag changes don't silently alter the bundle.

### macOS Code Signing

`codesign_macos()` attempts signing with a Developer ID if `APPLE_DEVELOPER_ID` is set in the environment; otherwise it falls back to ad-hoc signing (`-`). Ad-hoc signing allows the app to run on the build machine without Gatekeeper warnings, which is sufficient for development. The function does not hard-fail on signing errors — it logs a warning and continues — because unsigned builds are still useful for local testing.

### DMG Creation

`create_dmg()` shells out to `hdiutil` to produce a `.dmg` containing the `.app` and a symlink to `/Applications`. This is the standard macOS distribution artifact. The function returns `bool` so the caller can decide whether to treat a DMG failure as fatal; currently `build()` logs and continues, meaning a successful `.app` is still produced even if DMG creation fails.

### Windows Inno Setup

`run_inno_setup()` looks for `ISCC.exe` on the PATH and runs the `.iss` spec if found. This is optional: Windows CI agents may not have Inno Setup installed, in which case the function returns `False` and the build produces only the raw `.exe` from PyInstaller. The `.iss` file handles start menu shortcuts, uninstall registry entries, and version stamping.

## Path Layout

```
ROOT/
  dist/launcher/          ← PyInstaller output (DIST_DIR)
  installer/launcher/
    assets/               ← icon.png, icon.ico, icon.icns (ASSETS_DIR)
    build-launcher/
      build.py            ← this script
      launcher.spec       ← PyInstaller spec (SPEC_FILE)
```

All paths derive from `Path(__file__)` anchored to the repo root, so the script can be invoked from any working directory.

## Known Gaps

- `codesign_macos()` does not verify the signature after signing — a failed signing that exits 0 would go undetected.
- DMG layout (window size, icon positions) is not configured; `hdiutil` uses defaults, which produces a functional but unstyled DMG.
- Linux packaging (AppImage, `.deb`) is not implemented; the build script only handles macOS and Windows.