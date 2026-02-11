#!/usr/bin/env python3
"""Generate platform-specific icon files from icon.png.

Produces:
  - icon.ico  (Windows: multi-resolution 16/32/48/64/128/256)
  - icon.icns (macOS: via iconutil if available, otherwise Pillow fallback)

Usage:
  python installer/launcher/build-launcher/make_icons.py
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Error: Pillow is required.  pip install Pillow")
    sys.exit(1)

ASSETS_DIR = Path(__file__).parent.parent / "assets"
SOURCE_PNG = ASSETS_DIR / "icon.png"
ICO_SIZES = [16, 32, 48, 64, 128, 256]
ICNS_SIZES = {
    "icon_16x16.png": 16,
    "icon_16x16@2x.png": 32,
    "icon_32x32.png": 32,
    "icon_32x32@2x.png": 64,
    "icon_128x128.png": 128,
    "icon_128x128@2x.png": 256,
    "icon_256x256.png": 256,
    "icon_256x256@2x.png": 512,
    "icon_512x512.png": 512,
    "icon_512x512@2x.png": 1024,
}


def make_ico(source: Path, output: Path) -> None:
    """Create a multi-resolution .ico file."""
    img = Image.open(source)
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    sizes = [(s, s) for s in ICO_SIZES]
    img.save(str(output), format="ICO", sizes=sizes)
    print(f"  Created {output} ({', '.join(f'{s}px' for s in ICO_SIZES)})")


def make_icns(source: Path, output: Path) -> None:
    """Create an .icns file, preferring iconutil on macOS."""
    img = Image.open(source)
    if img.mode != "RGBA":
        img = img.convert("RGBA")

    # Try macOS iconutil first (produces proper icns)
    if platform.system() == "Darwin" and shutil.which("iconutil"):
        _make_icns_iconutil(img, output)
    else:
        _make_icns_pillow(img, output)


def _make_icns_iconutil(img: Image.Image, output: Path) -> None:
    """Use macOS iconutil to create .icns from an iconset."""
    with tempfile.TemporaryDirectory() as tmpdir:
        iconset = Path(tmpdir) / "icon.iconset"
        iconset.mkdir()

        for name, size in ICNS_SIZES.items():
            resized = img.resize((size, size), Image.Resampling.LANCZOS)
            resized.save(str(iconset / name), format="PNG")

        result = subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(output)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"  iconutil failed: {result.stderr.strip()}, falling back to Pillow")
            _make_icns_pillow(img, output)
            return

    print(f"  Created {output} (via iconutil)")


def _make_icns_pillow(img: Image.Image, output: Path) -> None:
    """Fallback: save .icns using Pillow (limited but functional)."""
    img.save(str(output), format="ICNS")
    print(f"  Created {output} (via Pillow)")


def main() -> int:
    if not SOURCE_PNG.exists():
        print(f"Error: Source icon not found: {SOURCE_PNG}")
        return 1

    print(f"Generating icons from {SOURCE_PNG}")
    ASSETS_DIR.mkdir(parents=True, exist_ok=True)

    make_ico(SOURCE_PNG, ASSETS_DIR / "icon.ico")
    make_icns(SOURCE_PNG, ASSETS_DIR / "icon.icns")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
