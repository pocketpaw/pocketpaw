# tests/packaging/test_extraction_deps_reach_the_image.py — the image must
# actually install what the extraction adapters import.
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

ROOT = Path(__file__).resolve().parents[2]

# What ee/cloud/extraction imports at call time. Each maps to the extra that
# must be installed for it to resolve.
LAZY_IMPORTS = {
    "pypdf": "PDF text extraction (local.py)",
    "python-docx": "DOCX text extraction (local.py)",
    "trafilatura": "HTML extraction / kb ingest",
}


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


def test_knowledge_extra_still_carries_the_document_deps():
    """If someone empties `knowledge`, naming it in the Dockerfile buys nothing."""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text())
    pkgs = " ".join(data["project"]["optional-dependencies"]["knowledge"])
    assert "pypdf" in pkgs, "the knowledge extra no longer carries pypdf"
    assert "trafilatura" in pkgs, "the knowledge extra no longer carries trafilatura"


def test_ee_extraction_extra_still_carries_the_document_deps():
    data = tomllib.loads((ROOT / "ee" / "pyproject.toml").read_text())
    pkgs = " ".join(data["project"]["optional-dependencies"]["extraction"])
    for dep, why in LAZY_IMPORTS.items():
        if dep == "trafilatura":
            continue  # lives in the main knowledge extra, asserted above
        assert dep in pkgs, f"ee[extraction] no longer carries {dep} — needed for {why}"
