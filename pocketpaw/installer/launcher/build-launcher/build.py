#!/usr/bin/env python3
"""PocketPaw Desktop Launcher — Build Script.

Builds the launcher into a native app for the current platform.
Handles icon generation, PyInstaller, code signing (macOS), DMG creation,
and Inno Setup invocation (Windows).

Usage:
  python installer/launcher/build-launcher/build.py [--version VERSION]

Requirements:
  pip install pyinstaller pystray Pillow
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent.parent  # repo root
BUILD_DIR = Path(__file__).parent  # build-launcher/
SPEC_FILE = BUILD_DIR / "launcher.spec"
ASSETS_DIR = BUILD_DIR.parent / "assets"
DIST_DIR = ROOT / "dist" / "launcher"


def check_deps() -> bool:
    """Verify build dependencies are installed."""
    missing = []
    for pkg in ("PyInstaller", "pystray", "PIL"):
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg.lower() if pkg != "PIL" else "Pillow")

    if missing:
        print(f"Missing build dependencies: {', '.join(missing)}")
        print(f"Run: pip install {' '.join(missing)}")
        return False
    return True


def ensure_icons() -> None:
    """Generate .ico/.icns if they don't exist."""
    ico = ASSETS_DIR / "icon.ico"
    icns = ASSETS_DIR / "icon.icns"

    if ico.exists() and icns.exists():
        return

    print("Generating platform icons...")
    make_icons = BUILD_DIR / "make_icons.py"
    if not make_icons.exists():
        print("Warning: make_icons.py not found, skipping icon generation")
        return

    result = subprocess.run(
        [sys.executable, str(make_icons)],
        cwd=str(ROOT),
    )
    if result.returncode != 0:
        print("Warning: Icon generation failed, build may use fallback icons")


def codesign_macos(app_path: Path) -> None:
    """Ad-hoc or identity-based code signing on macOS."""
    identity = os.environ.get("MACOS_SIGNING_IDENTITY", "-")
    label = "production" if identity != "-" else "ad-hoc"
    print(f"\nCode signing ({label}): {app_path}")

    cmd = [
        "codesign",
        "--force",
        "--deep",
        "--sign",
        identity,
        "--options",
        "runtime",
        str(app_path),
    ]
    # Ad-hoc doesn't need --options runtime
    if identity == "-":
        cmd = [
            "codesign",
            "--force",
            "--deep",
            "--sign",
            "-",
            str(app_path),
        ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print(f"  Signed successfully ({label})")
    else:
        print(f"  Warning: codesign failed: {result.stderr.strip()}")


def create_dmg(app_path: Path, output_path: Path) -> bool:
    """Create a .dmg with the .app and an Applications symlink."""
    print(f"\nCreating DMG: {output_path}")

    with tempfile.TemporaryDirectory() as tmpdir:
        staging = Path(tmpdir) / "dmg_staging"
        staging.mkdir()

        # Copy .app bundle
        dest_app = staging / app_path.name
        shutil.copytree(str(app_path), str(dest_app), symlinks=True)

        # Create Applications symlink for drag-to-install
        apps_link = staging / "Applications"
        apps_link.symlink_to("/Applications")

        # Create temporary read/write DMG
        temp_dmg = Path(tmpdir) / "temp.dmg"
        result = subprocess.run(
            [
                "hdiutil",
                "create",
                "-volname",
                "PocketPaw",
                "-srcfolder",
                str(staging),
                "-ov",
                "-format",
                "UDRW",
                str(temp_dmg),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  Error creating temp DMG: {result.stderr.strip()}")
            return False

        # Convert to compressed read-only DMG
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if output_path.exists():
            output_path.unlink()

        result = subprocess.run(
            [
                "hdiutil",
                "convert",
                str(temp_dmg),
                "-format",
                "UDZO",
                "-imagekey",
                "zlib-level=9",
                "-o",
                str(output_path),
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  Error converting DMG: {result.stderr.strip()}")
            return False

    print(f"  DMG created: {output_path}")
    return True


def run_inno_setup(iss_path: Path, version: str) -> bool:
    """Run Inno Setup compiler if available."""
    iscc = shutil.which("ISCC") or shutil.which("iscc")
    if not iscc:
        # Try common install locations on Windows
        for candidate in [
            r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
            r"C:\Program Files\Inno Setup 6\ISCC.exe",
        ]:
            if Path(candidate).exists():
                iscc = candidate
                break

    if not iscc:
        print("  Inno Setup not found — skipping Windows installer creation")
        return False

    print(f"\nRunning Inno Setup: {iss_path}")
    result = subprocess.run(
        [iscc, f"/DVERSION={version}", str(iss_path)],
        cwd=str(ROOT),
    )
    if result.returncode == 0:
        print("  Windows installer created successfully")
        return True

    print(f"  Inno Setup failed (exit code {result.returncode})")
    return False


def build(version: str) -> int:
    """Run the full build pipeline."""
    if not check_deps():
        return 1

    # Set version env var for the spec file
    os.environ["POCKETPAW_VERSION"] = version

    print(f"Building PocketPaw Launcher v{version} for {platform.system()}...")
    print(f"Spec file: {SPEC_FILE}")
    print(f"Output: {DIST_DIR}")
    print()

    # Step 1: Generate icons if missing
    ensure_icons()

    # Step 2: Clean previous build
    build_work = ROOT / "build" / "launcher"
    if build_work.exists():
        shutil.rmtree(build_work)

    # Step 3: Run PyInstaller
    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        str(SPEC_FILE),
        "--distpath",
        str(DIST_DIR),
        "--workpath",
        str(build_work),
        "--clean",
        "--noconfirm",
    ]

    print(f"Running: {' '.join(cmd)}\n")
    result = subprocess.run(cmd, cwd=str(ROOT))

    if result.returncode != 0:
        print("\nBuild failed!")
        return result.returncode

    print("\nPyInstaller build successful!")

    # Step 4: Platform-specific post-processing
    if platform.system() == "Darwin":
        app_path = DIST_DIR / "PocketPaw.app"
        if app_path.exists():
            print(f"macOS app: {app_path}")

            # Code sign
            codesign_macos(app_path)

            # Create DMG
            dmg_path = DIST_DIR / "PocketPaw.dmg"
            create_dmg(app_path, dmg_path)

    elif platform.system() == "Windows":
        exe_path = DIST_DIR / "PocketPaw" / "PocketPaw.exe"
        if exe_path.exists():
            print(f"Windows exe: {exe_path}")

            # Try Inno Setup
            iss_path = BUILD_DIR / "pocketpaw.iss"
            if iss_path.exists():
                run_inno_setup(iss_path, version)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Build PocketPaw Desktop Launcher")
    parser.add_argument(
        "--version",
        default=os.environ.get("POCKETPAW_VERSION", "0.1.0"),
        help="Version string to embed (default: $POCKETPAW_VERSION or 0.1.0)",
    )
    args = parser.parse_args()
    return build(args.version)


if __name__ == "__main__":
    sys.exit(main())
