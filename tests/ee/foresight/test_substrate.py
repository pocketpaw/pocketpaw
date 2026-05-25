# tests/ee/foresight/test_substrate.py
# Created: 2026-05-25 (feat/foresight-v02-oasis-camel-paw) — PR 2.
#
# Pin the v0.2 OASIS substrate vendoring contract:
#   - The vendored package at ee/pocketpaw_ee/foresight/substrate/oasis/
#     is importable as a namespace package (no camel-ai dep needed).
#   - When camel-ai is missing, OASIS_AVAILABLE=False and the package
#     records the underlying ImportError in OASIS_LOAD_ERROR.
#   - The package version mirrors upstream OASIS 0.2.5 (commit 46cdc8d).
#   - The LICENSE / NOTICE / README-FORK.md files survive vendoring.
#   - The upstream init lives at _upstream_init.py with rewritten
#     absolute imports (oasis.X -> pocketpaw_ee.foresight.substrate.oasis.X).
#
# This file does NOT try to instantiate any OASIS class — PR 3 wires
# the substrate into the engine and adds the integration tests that
# exercise OasisEnv.step / SocialAgent / AgentGraph. PR 2's smoke
# contract is just "the package is importable and well-formed."

from __future__ import annotations

from pathlib import Path

from pocketpaw_ee.foresight.substrate import oasis


def test_substrate_package_imports_cleanly():
    """The vendored OASIS package must import without raising."""
    assert oasis.__name__ == "pocketpaw_ee.foresight.substrate.oasis"


def test_substrate_version_mirrors_upstream():
    """We vendored at upstream SHA 46cdc8d which carries __version__ 0.2.5."""
    assert oasis.__version__ == "0.2.5"


def test_substrate_exposes_availability_flag():
    """The OASIS_AVAILABLE flag is the canonical PR 3 branch-predicate.
    Its presence is what matters here, not its value (which depends on
    whether ``pocketpaw-ee[foresight]`` was installed)."""
    assert hasattr(oasis, "OASIS_AVAILABLE")
    assert isinstance(oasis.OASIS_AVAILABLE, bool)


def test_substrate_records_load_error_when_unavailable():
    """If OASIS_AVAILABLE is False, the underlying ImportError should
    be captured for debugging — never swallowed silently."""
    if not oasis.OASIS_AVAILABLE:
        assert oasis.OASIS_LOAD_ERROR is not None
        assert isinstance(oasis.OASIS_LOAD_ERROR, Exception)


def test_substrate_directory_contains_license_and_notice():
    """Apache-2.0 §4(d) requires we ship LICENSE + NOTICE alongside
    redistributed code. Lock that in test."""
    pkg_dir = Path(oasis.__file__).parent
    assert (pkg_dir / "LICENSE").exists(), "OASIS LICENSE file missing"
    assert (pkg_dir / "NOTICE").exists(), "OASIS NOTICE file missing"
    assert (pkg_dir / "README-FORK.md").exists(), "README-FORK.md missing"


def test_substrate_upstream_init_preserved_verbatim():
    """The upstream OASIS __init__.py should be preserved as _upstream_init.py
    so PR 3 wiring can re-import from it cleanly."""
    pkg_dir = Path(oasis.__file__).parent
    upstream = pkg_dir / "_upstream_init.py"
    assert upstream.exists(), "_upstream_init.py missing — vendoring incomplete"
    content = upstream.read_text()
    # Upstream's verbatim copyright header (modulo import-path rewrite)
    assert "Copyright 2023 @ CAMEL-AI.org" in content
    # The full set of upstream re-exports should still be present (the
    # import path rewrite touches only the module prefix).
    assert "SocialAgent" in content
    assert "AgentGraph" in content
    assert "Platform" in content
    assert "ActionType" in content


def test_substrate_subpackages_are_present():
    """The four substrate subpackages OASIS ships with — clock,
    environment, social_agent, social_platform, testing — must all
    be vendored as directories."""
    pkg_dir = Path(oasis.__file__).parent
    for sub in ["clock", "environment", "social_agent", "social_platform", "testing"]:
        assert (pkg_dir / sub).is_dir(), f"missing substrate subpackage: {sub}"
        # Each subpackage carries its own __init__.py
        assert (pkg_dir / sub / "__init__.py").exists(), f"missing {sub}/__init__.py"


def test_substrate_imports_use_nested_namespace_not_top_level_oasis():
    """The mechanical import-path rewrite in PR 2 (from oasis.X to
    pocketpaw_ee.foresight.substrate.oasis.X) must touch every vendored
    .py file. A lingering ``from oasis.X`` would mean PR 3 wiring
    breaks when it tries to load OasisEnv."""
    pkg_dir = Path(oasis.__file__).parent
    offenders: list[str] = []
    for py in pkg_dir.rglob("*.py"):
        if py.name == "__init__.py" and py.parent == pkg_dir:
            # Our wrapper __init__.py uses the new nested name explicitly.
            continue
        text = py.read_text()
        # Match line starts; ignore strings/comments/logger names that
        # incidentally contain "oasis.".
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith("from oasis.") or stripped.startswith("import oasis."):
                offenders.append(f"{py.relative_to(pkg_dir)}:{lineno}: {stripped[:80]}")
    assert not offenders, (
        "Found vendored OASIS files still using top-level 'oasis.' imports — "
        "PR 2 mechanical rewrite incomplete:\n" + "\n".join(offenders)
    )
