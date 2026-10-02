# tests/test_ripple_action_verbs_vendored.py — the ripple action-verb set is
# generated from ripple, not hand-kept.
# Created 2026-10-02 (fix/canon-cross-repo-pins, CN-8). The hand list in
# pocketpaw.ripple.manifest missed ``animate`` (ripple's EventAction has had it
# since RFC 12), so validate_action_verbs flagged valid animate handlers. The set
# now comes from scripts/vendor_ripple_verbs.py; these tests pin it.
"""Pins for the generated ripple action-verb set."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import subprocess
from pathlib import Path

import pytest

from pocketpaw.ripple import _action_verbs
from pocketpaw.ripple.manifest import _KNOWN_ACTION_VERBS, validate_action_verbs

ROOT = Path(__file__).resolve().parents[1]


def _generator():
    spec = importlib.util.spec_from_file_location(
        "vendor_ripple_verbs", ROOT / "scripts" / "vendor_ripple_verbs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_generated_set_matches_its_pinned_hash():
    """A hand edit to _action_verbs.py fails here. Refresh by re-running
    scripts/vendor_ripple_verbs.py, which rewrites the hash with the set."""
    joined = "\n".join(sorted(_action_verbs.ACTION_VERBS))
    assert hashlib.sha256(joined.encode()).hexdigest() == _action_verbs.VERBS_SHA256
    assert _KNOWN_ACTION_VERBS is _action_verbs.ACTION_VERBS


def test_animate_is_a_known_verb():
    spec = {"ui": {"type": "button", "props": {"on_click": {"action": "animate"}}}}
    assert "animate" in _KNOWN_ACTION_VERBS
    assert validate_action_verbs(spec) == []


def _ripple_checkout() -> Path | None:
    if override := os.environ.get("RIPPLE_DIR"):
        return Path(override)
    return next((p / "ripple" for p in ROOT.parents if (p / "ripple" / ".git").exists()), None)


def test_the_generated_set_is_what_ripple_declared_at_the_pinned_commit():
    """Re-reads event-handler.ts at SOURCE_COMMIT and re-parses it, so the file is
    provably the generator's output for that commit. Skips without a checkout."""
    ripple = _ripple_checkout()
    if ripple is None:
        pytest.skip("ripple is not checked out beside this repo (set RIPPLE_DIR)")
    gen = _generator()
    try:
        commit, source = gen.read_source(ripple, _action_verbs.SOURCE_COMMIT)
    except subprocess.CalledProcessError:
        pytest.skip(f"{_action_verbs.SOURCE_COMMIT[:7]} is not in {ripple}; fetch it")
    assert hashlib.sha256(source.encode()).hexdigest() == _action_verbs.SOURCE_SHA256
    assert frozenset(gen.parse_verbs(source)) == _action_verbs.ACTION_VERBS
    assert gen.render(commit, source) == Path(_action_verbs.__file__).read_text("utf-8")
