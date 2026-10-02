# vetted_pins.py — the toolchain dependency pins, read from the generator instead of
# hand-mirrored.
#
# Updated: 2026-10-02 (fix/canon-cross-repo-pins, CN-8). The fallback is no longer a
# hand dict. ``paw-sites-allowlist.json`` (beside this file) is ``paw-sites-gen
# allowlist``'s output vendored byte-for-byte at a recorded paw-sites commit
# (``paw-sites-allowlist.pin.json``, hash-pinned by
# tests/ee/sites/test_paw_sites_allowlist_vendored.py; refresh with
# scripts/vendor-paw-sites-allowlist.sh). ``FALLBACK_PINS`` is its ``vetted`` map,
# and ``VENDORED_ALLOWLIST`` hands its reserved-name and package-cap rules to
# ``dependency_manifest`` so those stop being a second hand copy too. The old hand
# dict matched the 11 names it carried; the vendored map adds the rest of
# VETTED_DEPENDENCIES (valibot, gsap, ...), so ``pin_for`` now answers for them.
#
# Created: 2026-09-24 (feat/sites-author-dependencies, PP-1). Two places in this
# package named versions that belong to paw-sites' ``VETTED_DEPENDENCIES``:
# ``project_zip._VETTED_PINS`` (the downloadable build shell) and
# ``generator_client._ripple_motion_dep`` (motion, added to the ripple track after
# generate). Both were copies, kept in step by a drift test and good intentions.
# ``scripts/vendor-paw-sites.sh`` (and the image's paw-sites stage) now write
# ``paw-sites-gen allowlist``'s JSON next to the vendored generator, and both
# callers read the pins from there. The constants below stay as the FALLBACK for a
# checkout that never vendored the generator, and a test compares them to the sibling
# paw-sites allowlist so the fallback cannot silently rot either.
"""Toolchain version pins for generated Paw Site projects."""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

#: Overrides where the allowlist JSON is read from.
ALLOWLIST_ENV = "PAW_SITES_ALLOWLIST_JSON"

#: File name the vendor script and the image write next to the generator.
ALLOWLIST_FILE = "allowlist.json"

#: ``paw-sites-gen allowlist`` output, vendored into the package at a pinned
#: paw-sites commit (see the header). Committed package data, so always present.
VENDORED_ALLOWLIST: dict = json.loads(
    Path(__file__).with_name("paw-sites-allowlist.json").read_text(encoding="utf-8")
)

#: Used for any name the deployed allowlist does not carry: the vendored ``vetted``
#: map (paw-sites ``VETTED_DEPENDENCIES``).
FALLBACK_PINS: dict[str, str] = dict(VENDORED_ALLOWLIST["vetted"])


def _candidate_paths() -> list[Path]:
    paths: list[Path] = []
    if override := os.environ.get(ALLOWLIST_ENV, "").strip():
        paths.append(Path(override))
    # The image's install location (Dockerfile.enterprise COPYs the stage to here).
    paths.append(Path("/opt/paw-sites") / ALLOWLIST_FILE)
    # A dev checkout that ran scripts/vendor-paw-sites.sh.
    paths.append(Path(__file__).resolve().parents[3] / "deploy" / "paw-sites" / ALLOWLIST_FILE)
    return paths


def _read_allowlist(path: Path) -> dict[str, str] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    pins: dict[str, str] = {}
    # ``pinned`` is what ``paw-sites-gen allowlist`` prints today (the
    # author-declarable set); ``vetted`` is read too so a generator that starts
    # printing its whole map is picked up without a change here.
    for key in ("vetted", "pinned"):
        block = data.get(key)
        if isinstance(block, dict):
            pins.update(
                {k: v for k, v in block.items() if isinstance(k, str) and isinstance(v, str)}
            )
    return pins


@lru_cache(maxsize=1)
def vetted_pins() -> dict[str, str]:
    """``FALLBACK_PINS`` overlaid with the vendored allowlist, when one is found."""
    merged = dict(FALLBACK_PINS)
    for path in _candidate_paths():
        pins = _read_allowlist(path)
        if pins is not None:
            merged.update(pins)
            logger.debug("sites.vetted_pins: read %d pins from %s", len(pins), path)
            break
    return merged


def pin_for(name: str) -> str:
    """The vetted version spec for ``name``. ``KeyError`` for an unvetted name."""
    return vetted_pins()[name]


__all__ = ["ALLOWLIST_ENV", "FALLBACK_PINS", "VENDORED_ALLOWLIST", "pin_for", "vetted_pins"]
