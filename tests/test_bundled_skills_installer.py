# tests/test_bundled_skills_installer.py
# Created: 2026-05-14 (feat/pocket-creator-skill) — verifies the
# auto-installer that mirrors bundled Claude Code skill files into
# ~/.claude/skills/. The installer is idempotent (SHA-256 hash compare
# per file), best-effort (errors logged not raised), and discovers
# new skills by directory iteration so adding a skill doesn't need
# code changes.
"""Tests for ``pocketpaw.bundled_skills.installer.install_bundled_skills``.

Each test installs into a tmp_path destination (no touching the user's
real ``~/.claude/skills/`` directory) and exercises one branch of
the installer's status state machine: installed / updated / skipped /
failed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pocketpaw.bundled_skills.installer import (
    InstallResult,
    install_bundled_skills,
)

# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_install_creates_skill_files_in_destination(tmp_path: Path) -> None:
    """First run mirrors every bundled SKILL.md into the destination."""
    results = install_bundled_skills(destination_root=tmp_path)

    # Both shipping skills must land. Adding a new bundled skill is a
    # drop-a-directory operation — the test grows by one assert each.
    assert any(r.name == "pocketpaw-create-pocket" for r in results)
    assert any(r.name == "pocketpaw-edit-pocket" for r in results)

    # ---- create skill: frontmatter + STEP 1 marker ----
    create_file = tmp_path / "pocketpaw-create-pocket" / "SKILL.md"
    assert create_file.is_file()
    create_body = create_file.read_text()
    assert "name: pocketpaw-create-pocket" in create_body
    assert "STEP 1 — Pick the pattern" in create_body

    # ---- edit skill: frontmatter + Type A/B/C decision tree ----
    # The decision tree is the load-bearing content for edit
    # delegation — a regression that drops it would silently route
    # every edit through the wrong shape of specialist call.
    edit_file = tmp_path / "pocketpaw-edit-pocket" / "SKILL.md"
    assert edit_file.is_file()
    edit_body = edit_file.read_text()
    assert "name: pocketpaw-edit-pocket" in edit_body
    assert "Type A — Simple state edit" in edit_body
    assert "Type B — Structural" in edit_body
    assert "Type C — Open-ended redesign" in edit_body
    assert "pocket_specialist__edit" in edit_body


def test_first_install_returns_installed_status(tmp_path: Path) -> None:
    """When the destination didn't exist, status is ``installed``."""
    results = install_bundled_skills(destination_root=tmp_path)
    pocket_result = next(r for r in results if r.name == "pocketpaw-create-pocket")
    assert pocket_result.status == "installed"
    assert pocket_result.error is None


def test_install_is_idempotent_on_same_content(tmp_path: Path) -> None:
    """Second install with unchanged source/destination is a no-op
    (``skipped`` per result). Idempotent boots are the steady state."""
    install_bundled_skills(destination_root=tmp_path)
    results2 = install_bundled_skills(destination_root=tmp_path)
    pocket_result = next(r for r in results2 if r.name == "pocketpaw-create-pocket")
    assert pocket_result.status == "skipped"


def test_install_updates_when_destination_content_drifts(tmp_path: Path) -> None:
    """When the destination file exists but its content differs from
    the bundled source (e.g., older skill version), the installer
    overwrites it and the status flips to ``updated``."""
    skill_dir = tmp_path / "pocketpaw-create-pocket"
    skill_dir.mkdir(parents=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text("--- stale content from prior PocketPaw version ---")

    results = install_bundled_skills(destination_root=tmp_path)
    pocket_result = next(r for r in results if r.name == "pocketpaw-create-pocket")

    assert pocket_result.status == "updated"
    # Stale content is overwritten with the bundled body.
    body = skill_file.read_text()
    assert "stale content" not in body
    assert "name: pocketpaw-create-pocket" in body


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


def test_install_never_raises_on_oserror(tmp_path: Path, monkeypatch) -> None:
    """OSError during copy returns a ``failed`` result rather than
    propagating. The chat agent path is best-effort — a single failed
    skill must not block other skills (or the rest of dashboard boot)."""
    import pocketpaw.bundled_skills.installer as installer_mod

    def _explode(*args, **kwargs):  # noqa: ANN001 - test stub
        raise OSError("simulated permission denied")

    monkeypatch.setattr(installer_mod.shutil, "copy2", _explode)

    results = install_bundled_skills(destination_root=tmp_path)
    pocket_result = next(r for r in results if r.name == "pocketpaw-create-pocket")
    assert pocket_result.status == "failed"
    assert "simulated permission denied" in (pocket_result.error or "")


def test_install_skips_when_bundled_dir_missing(monkeypatch, tmp_path: Path) -> None:
    """If the package's ``_bundled`` dir vanishes (corrupt install /
    bad package), the installer logs and returns an empty list rather
    than crashing the boot."""
    import pocketpaw.bundled_skills.installer as installer_mod

    monkeypatch.setattr(installer_mod, "_BUNDLED_DIR", tmp_path / "definitely-does-not-exist")
    results = install_bundled_skills(destination_root=tmp_path)
    assert results == []


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


def test_install_result_is_frozen_dataclass(tmp_path: Path) -> None:
    """``InstallResult`` is frozen — callers can't accidentally mutate
    a status after the installer returned. Catches a regression where
    a caller might try to ``r.status = ...`` and silently succeed."""
    results = install_bundled_skills(destination_root=tmp_path)
    r = results[0]
    assert isinstance(r, InstallResult)
    with pytest.raises(Exception):
        # ``frozen=True`` raises ``dataclasses.FrozenInstanceError``,
        # subclass of ``AttributeError``. We just want it to refuse.
        r.status = "tampered"  # type: ignore[misc]
