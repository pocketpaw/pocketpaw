# tests/test_bundled_skills_installer.py
# Created: 2026-05-14 (feat/pocket-creator-skill) — verifies the
# auto-installer that mirrors bundled Claude Code skill files into
# ~/.claude/skills/. The installer is idempotent (SHA-256 hash compare
# per file), best-effort (errors logged not raised), and discovers
# new skills by directory iteration so adding a skill doesn't need
# code changes.
# Updated: 2026-06-03 (feat/sdk-bundled-skills-plugin) — bundled skills moved
# under _bundled/skills/ and _bundled became a Claude Code local plugin. The
# missing-dir test now patches _SKILLS_DIR; added coverage for
# bundled_skills_plugin_dir() (the path the claude_agent_sdk backend passes
# via plugins=) and for create-site landing in the mirror.
# Updated: 2026-06-03 (feat/sites-landing-brain, Task P2) — added coverage
# for the new pocketpaw-create-paw-site marketing brain: it ships in the
# mirror AND the local plugin, and carries its load-bearing SSR guardrails
# (flat lead form, tiers pricing, no accordion, anchor CTAs, marketing hero).
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
    bundled_skills_plugin_dir,
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

    # The installer iterates ``_SKILLS_DIR`` (``_bundled/skills/``); patching it
    # to a missing path simulates a corrupt package without touching the real
    # bundled tree.
    monkeypatch.setattr(installer_mod, "_SKILLS_DIR", tmp_path / "definitely-does-not-exist")
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


def test_install_includes_create_site(tmp_path: Path) -> None:
    """The site-publish skill ships in the mirror too. Regression guard for
    the /sites flow — a dropped create-site skill would leave the publish
    path with no skill body on the non-SDK backends."""
    install_bundled_skills(destination_root=tmp_path)
    site_file = tmp_path / "pocketpaw-create-site" / "SKILL.md"
    assert site_file.is_file()
    assert "name: pocketpaw-create-site" in site_file.read_text()


def test_install_includes_create_paw_site(tmp_path: Path) -> None:
    """The marketing landing brain ships in the mirror. A dropped
    create-paw-site skill would route every new-site request back to the
    dashboard create-pocket flow → the broken-dashboard render."""
    results = install_bundled_skills(destination_root=tmp_path)
    assert any(r.name == "pocketpaw-create-paw-site" for r in results)

    skill_file = tmp_path / "pocketpaw-create-paw-site" / "SKILL.md"
    assert skill_file.is_file()
    assert "name: pocketpaw-create-paw-site" in skill_file.read_text()


def test_create_paw_site_is_copy_only_deterministic_brain(tmp_path: Path) -> None:
    """The brain's body must keep its load-bearing DETERMINISTIC contract: the
    agent writes COPY ONLY and calls the deterministic ``create_landing_site``
    tool — it does NOT draft a rippleSpec and does NOT route through the
    pocket specialist's create/redraft loop (the path that silently downgraded
    landing pages to generic dashboard widgets). A silent edit that reintroduced
    spec-drafting or the specialist route would bring the downgrade back. We pin
    discriminating tokens, not prose, so wording can evolve."""
    install_bundled_skills(destination_root=tmp_path)
    body = (tmp_path / "pocketpaw-create-paw-site" / "SKILL.md").read_text()

    # The site identity it stamps (the published page renders as a landing page).
    assert 'pattern="landing"' in body
    assert 'type="site"' in body

    # The deterministic create tool is the path — the agent calls it, the tool
    # owns the structure.
    assert "create_landing_site" in body

    # Copy-only contract: the agent does NOT compose a rippleSpec. Pin the
    # explicit "copy only" steer AND the "do NOT compose" instruction.
    assert "COPY ONLY" in body
    assert "do NOT compose" in body

    # The old downgrade route must NOT be the active instruction. It may only
    # appear under a negative ("do not call pocket_specialist__create"), so we
    # assert the negative phrasing is present rather than banning the token.
    lowered = body.lower()
    assert "do not call" in lowered and "pocket_specialist__create" in body

    # The marketing widgets the page is built from are still named, so a silent
    # edit that drops the marketing steer (back toward a dashboard) is caught.
    for widget in ("navbar", "feature-grid", "testimonial", "pricing-table", "cta", "footer"):
        assert widget in body, f"marketing widget {widget!r} no longer named in the brain"

    # The dashboard anti-pattern is still warned against (no hero+grid KPI page).
    assert "hero + grid" in body or "hero+grid" in body
    # Pricing uses tiers; CTAs navigate by anchor not on_click.
    assert "tiers" in body
    assert "on_click" in body


# ---------------------------------------------------------------------------
# Local-plugin entry (the claude_agent_sdk path)
# ---------------------------------------------------------------------------


def test_plugin_dir_points_at_valid_local_plugin() -> None:
    """``bundled_skills_plugin_dir`` returns the ``_bundled`` directory, and
    that directory is a structurally valid Claude Code local plugin: a
    ``.claude-plugin/plugin.json`` manifest beside a ``skills/`` tree. This is
    the path the claude_agent_sdk backend hands to the SDK ``plugins=`` option
    — the only route bundled skills reach that backend under
    ``setting_sources=[]``."""
    import json

    plugin_dir = bundled_skills_plugin_dir()
    assert plugin_dir is not None
    assert plugin_dir.is_dir()

    manifest = plugin_dir / ".claude-plugin" / "plugin.json"
    assert manifest.is_file()
    data = json.loads(manifest.read_text())
    assert data["name"]  # a plugin must declare a name to load

    # The skills the plugin advertises live under skills/<name>/SKILL.md.
    skills_dir = plugin_dir / "skills"
    assert skills_dir.is_dir()
    names = {p.name for p in skills_dir.iterdir() if p.is_dir()}
    assert "pocketpaw-create-site" in names
    assert "pocketpaw-create-pocket" in names
    # The marketing landing brain must reach the claude_agent_sdk backend
    # too — it's the default backend, where new-site requests land.
    assert "pocketpaw-create-paw-site" in names


def test_plugin_dir_none_when_manifest_missing(monkeypatch, tmp_path: Path) -> None:
    """If the plugin manifest is absent (partial / corrupt package), the
    helper returns ``None`` so the SDK backend never gets an invalid plugin
    path — it just runs skill-free instead of failing to launch."""
    import pocketpaw.bundled_skills.installer as installer_mod

    monkeypatch.setattr(installer_mod, "_PLUGIN_MANIFEST", tmp_path / "nope" / "plugin.json")
    assert bundled_skills_plugin_dir() is None
