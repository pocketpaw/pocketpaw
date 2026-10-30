# tests/ee/test_extraction_closure_is_shipped.py — the document-extraction
# libraries have to be in the IMAGE, not just in pyproject.
# Created 2026-09-01 (feat/sites-public-asset-uploads). New file.
#
# WHY THIS EXISTS. `ee/pocketpaw_ee/cloud/extraction/local.py` lazy-imports
# pypdf / python-docx / pytesseract, and the attachment loop in
# `chat/agent_service._build_attachments_block` treats an adapter exception as
# "skip this file" — no entry, no error the user can see. So a missing library
# does not fail loudly at boot; it makes every attached PDF or .docx vanish, and
# the agent replies that no document was attached. That is exactly what happened
# on 2026-09-01, and the cause was not code: `.[all]` is a CURATED list that
# never references `[knowledge]`, and the Dockerfiles installed the ee package
# with NO extras, so the image shipped without any of them.
#
# THIS FILE IS DELIBERATELY NOT AN IMPORT TEST. Asserting `import pypdf` here
# would only prove the DEV environment has it — the dev venv and the image
# install different closures, which is the whole trap. So it reads the
# Dockerfiles as text and asserts the install lines actually request the extra.
# A pyproject-only fix leaves this red, which is the point.

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Every library the extraction adapters lazy-import. Adding one to the extra in
# ee/pyproject.toml without shipping it is the failure mode this guards.
_EXTRACTION_LIBS = {"pypdf", "python-docx", "trafilatura", "pymupdf"}


def _extra(name: str, pyproject: Path) -> list[str]:
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    return data["project"]["optional-dependencies"][name]


def test_the_extraction_extra_still_declares_what_the_adapters_import() -> None:
    """If a library moves out of the extra, shipping the extra stops being enough."""
    declared = {
        re.split(r"[><=\[]", d)[0].strip().lower()
        for d in _extra("extraction", REPO / "ee" / "pyproject.toml")
    }
    missing = {lib for lib in _EXTRACTION_LIBS if lib.lower() not in declared}
    assert not missing, f"ee[extraction] no longer declares: {sorted(missing)}"


@pytest.mark.parametrize("dockerfile", ["Dockerfile", "Dockerfile.enterprise"])
def test_the_image_installs_the_ee_package_with_the_extraction_extra(dockerfile: str) -> None:
    """THE guard: a bare ee install ships none of the extraction libraries.

    Both images install pocketpaw-ee — one from the source tree, one from a
    compiled wheel — and pip takes extras on either form. Whichever line does
    that install must carry ``[extraction]``.
    """
    text = (REPO / dockerfile).read_text(encoding="utf-8")

    # Comments in these files discuss `pip install ./ee` at length (the Cython
    # source-protection note), so a naive substring match reads prose as an
    # install line and fails on it. Only real RUN instructions count.
    ee_installs = [
        line
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
        and "pip install" in line
        and ("./ee" in line or "pocketpaw_ee-" in line)
    ]
    assert ee_installs, f"{dockerfile}: found no pip install of the ee package"

    for line in ee_installs:
        assert "[extraction]" in line, (
            f"{dockerfile}: ee is installed without [extraction] — an attached "
            f"PDF or .docx will be silently dropped in this image.\n  {line.strip()}"
        )


def test_all_is_not_mistaken_for_every_extra() -> None:
    """Pins the fact that made this invisible, so a reader meets it here.

    ``[all]`` reads like "everything" and is not: it never references
    ``[knowledge]``, where the OSS core keeps pypdf. If someone later makes
    ``[all]`` genuinely complete, this test fails and should simply be deleted —
    a red here means the trap is gone.
    """
    root = REPO / "pyproject.toml"
    all_extra = " ".join(_extra("all", root)).lower()
    knowledge = _extra("knowledge", root)

    assert "pypdf" in " ".join(knowledge).lower(), "knowledge extra changed shape"
    assert "pypdf" not in all_extra and "knowledge" not in all_extra, (
        "[all] now covers [knowledge] — good. Delete this test; its premise is stale."
    )
