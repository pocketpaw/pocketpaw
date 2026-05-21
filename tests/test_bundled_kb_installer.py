# tests/test_bundled_kb_installer.py
# Created: 2026-05-14 (feat/ripple-recipes-poc) — verifies the
# kb-go scope auto-installer that mirrors bundled scopes from
# pocketpaw/bundled_kb/_bundled/ into ~/.knowledge-base/. Symmetric
# with tests/test_bundled_skills_installer.py — same state machine
# (installed / updated / skipped / failed), same hash-compare
# semantics, separate target directory.
"""Tests for ``pocketpaw.bundled_kb.install_bundled_kb_scopes``.

Each test installs into a tmp_path destination (no touching the
user's real ``~/.knowledge-base/`` directory) and exercises one
branch of the installer's status state machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.bundled_kb.installer import (
    KbInstallResult,
    install_bundled_kb_scopes,
)

# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_install_creates_kb_scope_files_in_destination(tmp_path: Path) -> None:
    """First run mirrors the bundled scope directory tree."""
    results = install_bundled_kb_scopes(destination_root=tmp_path)

    # ripple-recipes is the cornerstone bundle the recipe-library PR
    # ships. Future scopes (paw-runtime-patterns, kb-cookbook, …)
    # will sit beside it.
    assert any(r.name == "ripple-recipes" for r in results)

    scope_dir = tmp_path / "ripple-recipes"
    assert scope_dir.is_dir()
    # kb-go scope layout = cache/ + raw/ + wiki/ + index.json.
    # If a future kb-go upgrade changes the layout the install
    # itself still succeeds (we mirror whatever's there), but the
    # search-result check below would fail — that's the right place
    # to catch the regression.
    assert (scope_dir / "index.json").is_file()
    assert (scope_dir / "wiki").is_dir()
    assert (scope_dir / "raw").is_dir()


def test_first_install_returns_installed_status(tmp_path: Path) -> None:
    """When the destination didn't exist, status is ``installed``."""
    results = install_bundled_kb_scopes(destination_root=tmp_path)
    rr_result = next(r for r in results if r.name == "ripple-recipes")
    assert rr_result.status == "installed"
    assert rr_result.error is None


def test_install_is_idempotent_on_same_content(tmp_path: Path) -> None:
    """Second install with unchanged source/destination is a no-op
    (``skipped`` per result). Idempotent boots are the steady state."""
    install_bundled_kb_scopes(destination_root=tmp_path)
    results2 = install_bundled_kb_scopes(destination_root=tmp_path)
    rr_result = next(r for r in results2 if r.name == "ripple-recipes")
    assert rr_result.status == "skipped"


def test_install_updates_when_destination_content_drifts(tmp_path: Path) -> None:
    """If a file in the user's destination drifts from the bundled
    source (e.g., older recipe set from a prior PocketPaw release),
    the installer overwrites it and flips status to ``updated``."""
    scope_dir = tmp_path / "ripple-recipes"
    scope_dir.mkdir(parents=True)
    stale_index = scope_dir / "index.json"
    stale_index.write_text('{"stale": "drift from prior pocketpaw release"}')

    results = install_bundled_kb_scopes(destination_root=tmp_path)
    rr_result = next(r for r in results if r.name == "ripple-recipes")
    assert rr_result.status == "updated"
    body = stale_index.read_text()
    assert "stale" not in body


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_install_never_raises_on_oserror(tmp_path: Path, monkeypatch) -> None:
    """OSError during copy returns a ``failed`` result rather than
    propagating. KB retrieval is a non-critical enhancement — a
    permission error must not block dashboard boot."""
    import pocketpaw.bundled_kb.installer as installer_mod

    def _explode(*args, **kwargs):  # noqa: ANN001 — test stub
        raise OSError("simulated permission denied on ~/.knowledge-base/")

    monkeypatch.setattr(installer_mod.shutil, "copy2", _explode)

    results = install_bundled_kb_scopes(destination_root=tmp_path)
    rr_result = next(r for r in results if r.name == "ripple-recipes")
    assert rr_result.status == "failed"
    assert "permission denied" in (rr_result.error or "")


def test_install_skips_when_bundled_dir_missing(monkeypatch, tmp_path: Path) -> None:
    """If the package's ``_bundled`` dir vanishes (corrupt install /
    bad package), the installer returns an empty list rather than
    crashing boot."""
    import pocketpaw.bundled_kb.installer as installer_mod

    monkeypatch.setattr(installer_mod, "_BUNDLED_DIR", tmp_path / "definitely-does-not-exist")
    results = install_bundled_kb_scopes(destination_root=tmp_path)
    assert results == []


def test_result_is_frozen_dataclass(tmp_path: Path) -> None:
    """``KbInstallResult`` is frozen — callers can't mutate the
    status after the installer returned."""
    results = install_bundled_kb_scopes(destination_root=tmp_path)
    r = results[0]
    assert isinstance(r, KbInstallResult)
    with pytest.raises(Exception):
        r.status = "tampered"  # type: ignore[misc]
