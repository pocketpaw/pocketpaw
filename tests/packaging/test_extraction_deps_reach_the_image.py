# tests/packaging/test_extraction_deps_reach_the_image.py — the image must
# actually install what the extraction adapters import.
#
# Updated 2026-08-29 (files-intelligence Track 1): extended to cover the new
# lazy imports added to local.py — python-pptx (.pptx), openpyxl (.xlsx) and
# pillow-heif (.heic/.heif). Each is now asserted in BOTH the root `knowledge`
# extra and ee's `extraction` extra, because those are two independent routes
# into the image (Dockerfile installs `.[knowledge]` AND `./ee[extraction]`)
# and only the root extra is reachable by `uv sync --all-extras` locally. The
# per-extra assertions are now table-driven off DUAL_HOMED_DEPS so adding a
# dep to one extra and forgetting the other fails here rather than at runtime.
#
# Created 2026-08-29. pypdf was absent from the production image: the main
# Dockerfile installs '.[all]', and `all` is a HAND-MAINTAINED list that never
# gained the `knowledge` extra (trafilatura, bm25s, pypdf); ee was installed
# bare, without its `extraction` extra. The adapters lazy-import those, so the
# build was green and every PDF failed at runtime — inside a listener except
# that logs and continues. File comprehension and the book agent therefore
# produced nothing for the most common upload type, and it read as "the
# feature is off" rather than "the build is missing a dependency".
#
# These tests read the Dockerfile as text on purpose. The failure was a
# PACKAGING fact, invisible to every import-level test in the suite: the code
# was correct, the dependency simply was not there.
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# What ee/cloud/extraction imports at call time. Each maps to the extra that
# must be installed for it to resolve.
#
# NOTE: these are DISTRIBUTION names as they appear in pyproject, not import
# names — `python-pptx` imports as `pptx`, `pillow-heif` as `pillow_heif`.
# The assertions below grep the pyproject TOML, so the distribution name is
# what has to match.
LAZY_IMPORTS = {
    "pypdf": "PDF text extraction (local.py)",
    "python-docx": "DOCX text extraction (local.py)",
    "trafilatura": "HTML extraction / kb ingest",
    "python-pptx": "PPTX slide + speaker-notes extraction (local.py)",
    "openpyxl": "XLSX sheet text extraction (local.py)",
    "pillow-heif": "HEIC/HEIF decoding for the OCR path (local.py)",
}

# Deps that must be present in BOTH extras. Two independent install routes
# reach the image (`.[knowledge]` and `./ee[extraction]`), and only the root
# `knowledge` extra is reachable by a local `uv sync --all-extras` — ee is a
# path dependency whose extras that flag does not resolve. A dep missing from
# `knowledge` is untestable locally; one missing from `extraction` breaks an
# ee-only install. Both are real failures, so both are asserted.
DUAL_HOMED_DEPS = ("pypdf", "trafilatura", "python-pptx", "openpyxl", "pillow-heif")


def _dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text()


def test_the_image_installs_the_knowledge_extra():
    """`.[all]` does not contain it — it must be named explicitly."""
    df = _dockerfile()
    install_lines = [ln for ln in df.splitlines() if "pip install" in ln and ".[" in ln]
    joined = " ".join(install_lines)
    assert "knowledge" in joined, (
        "Dockerfile installs '.[all]' but not '.[knowledge]'. `all` is a "
        "hand-maintained list that omits pypdf/trafilatura/bm25s, so document "
        "extraction dies at runtime while the build stays green."
    )


def test_the_image_installs_ee_with_its_extraction_extra():
    df = _dockerfile()
    assert re.search(r"pip install[^\n]*\./ee\[extraction\]", df), (
        "ee is installed without [extraction]; pypdf, python-docx and pymupdf "
        "are lazy-imported by the extraction adapters and would be missing."
    )


def _extra(pyproject: Path, name: str) -> str:
    data = tomllib.loads(pyproject.read_text())
    return " ".join(data["project"]["optional-dependencies"][name])


@pytest.mark.parametrize("dep", DUAL_HOMED_DEPS)
def test_knowledge_extra_still_carries_the_document_deps(dep: str):
    """If someone empties `knowledge`, naming it in the Dockerfile buys nothing.

    This extra is also the only one a local `uv sync --all-extras` installs,
    so a dep dropped from here silently downgrades every real-fixture
    extraction test into a mock-only test that proves nothing.
    """
    pkgs = _extra(ROOT / "pyproject.toml", "knowledge")
    assert dep in pkgs, (
        f"the knowledge extra no longer carries {dep} — needed for {LAZY_IMPORTS[dep]}"
    )


@pytest.mark.parametrize("dep", DUAL_HOMED_DEPS)
def test_ee_extraction_extra_still_carries_the_document_deps(dep: str):
    pkgs = _extra(ROOT / "ee" / "pyproject.toml", "extraction")
    assert dep in pkgs, f"ee[extraction] no longer carries {dep} — needed for {LAZY_IMPORTS[dep]}"


def test_python_docx_is_carried_by_the_ee_extraction_extra():
    """python-docx is ee-only (the OSS core never reads docx)."""
    pkgs = _extra(ROOT / "ee" / "pyproject.toml", "extraction")
    assert "python-docx" in pkgs, "ee[extraction] no longer carries python-docx"


def test_every_lazy_import_lives_in_at_least_one_installed_extra():
    """Catch-all: a new lazy import that lands in NEITHER extra is invisible.

    The pypdf outage was exactly this shape — correct code, dependency absent,
    build green, every PDF silently empty. Adding a name to LAZY_IMPORTS
    without adding the dep to an extra fails here.
    """
    knowledge = _extra(ROOT / "pyproject.toml", "knowledge")
    extraction = _extra(ROOT / "ee" / "pyproject.toml", "extraction")
    for dep, why in LAZY_IMPORTS.items():
        assert dep in knowledge or dep in extraction, (
            f"{dep} is lazy-imported for {why} but appears in neither "
            f"pyproject `knowledge` nor ee `extraction` — it will not be in "
            f"the image, and the failure will read as 'the feature is off'."
        )


def test_fal_client_is_a_BASE_dependency_of_ee_not_an_extra():
    """T2 (2026-08-29): media transcription needs it, and it must not become
    optional.

    ``fal-client`` is lazy-imported by ``uploads/transcription.py`` and
    ``studio/fal_edit.py``, so moving it into an extra would build green and
    then fail at runtime — the same shape as the pypdf hole above, and with
    the same symptom: audio and video uploads silently produce no transcript,
    no summary and no search hit, and it reads as "transcription is off".

    Asserted on the BASE ``dependencies`` list specifically. The Dockerfile
    installs ``./ee[extraction]``, which carries the base deps too, so being
    in an extra that nobody names is the only way to lose it.
    """
    data = tomllib.loads((ROOT / "ee" / "pyproject.toml").read_text())
    base = " ".join(data["project"]["dependencies"])
    assert "fal-client" in base, (
        "fal-client left pocketpaw-ee's base dependencies. Media transcription "
        "and the /studio edit ops both lazy-import it, so the image would build "
        "green and every media upload would silently produce nothing."
    )
