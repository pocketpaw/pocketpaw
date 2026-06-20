# tests/test_skills_materialize.py
# Created: 2026-06-07 (feat/entity-pocket-profile-field, entity-rooms A2) —
# pins the per-run skill materializer that makes ``SurfaceProfile.skill_names``
# LIVE for the claude_agent_sdk backend. Covers: filters to the named subset,
# rejects unsafe slugs, produces a plugin-shaped dir (.claude-plugin/plugin.json
# + skills/<slug>/SKILL.md), returns None on no match, and cleans up.

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pocketpaw.skills import loader as loader_mod
from pocketpaw.skills.loader import Skill, SkillLoader
from pocketpaw.skills.materialize import cleanup_run_skills, materialize_run_skills


def _make_skill_dir(base: Path, name: str, extra_asset: str | None = None) -> Skill:
    """Create a real on-disk skill dir and return its parsed ``Skill``."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        f"---\nname: {name}\ndescription: test skill {name}\n---\nBody for {name}.\n",
        encoding="utf-8",
    )
    if extra_asset:
        (skill_dir / extra_asset).write_text("asset", encoding="utf-8")
    return Skill(
        name=name,
        description=f"test skill {name}",
        content=f"Body for {name}.",
        path=skill_md,
    )


@pytest.fixture
def installed_skills(tmp_path, monkeypatch):
    """Install a fresh SkillLoader populated with two on-disk skills."""
    src = tmp_path / "installed"
    src.mkdir()
    s_a = _make_skill_dir(src, "github", extra_asset="ref.md")
    s_b = _make_skill_dir(src, "calendar-sync")

    fake_loader = SkillLoader()
    fake_loader._skills = {s_a.name: s_a, s_b.name: s_b}
    fake_loader._loaded = True
    monkeypatch.setattr(loader_mod, "_skill_loader", fake_loader)
    return fake_loader


def test_filters_to_named_subset(installed_skills):
    """Only the requested names land in the plugin; others are excluded."""
    root = materialize_run_skills(["github"])
    try:
        assert root is not None
        skill_slugs = {p.name for p in (root / "skills").iterdir()}
        assert skill_slugs == {"github"}
        assert (root / "skills" / "github" / "SKILL.md").is_file()
        # Whole skill dir is copied, including assets.
        assert (root / "skills" / "github" / "ref.md").is_file()
    finally:
        cleanup_run_skills(root)


def test_produces_plugin_shaped_dir(installed_skills):
    """The root is a valid local plugin: .claude-plugin/plugin.json + skills/."""
    root = materialize_run_skills(["github", "calendar-sync"])
    try:
        assert root is not None
        manifest = root / ".claude-plugin" / "plugin.json"
        assert manifest.is_file()
        data = json.loads(manifest.read_text())
        assert data["name"]  # required key present
        assert data["version"]
        assert (root / "skills" / "github" / "SKILL.md").is_file()
        assert (root / "skills" / "calendar-sync" / "SKILL.md").is_file()
    finally:
        cleanup_run_skills(root)


def test_unknown_names_skipped(installed_skills):
    """Unknown names are dropped; known ones still materialize."""
    root = materialize_run_skills(["github", "does-not-exist"])
    try:
        assert root is not None
        slugs = {p.name for p in (root / "skills").iterdir()}
        assert slugs == {"github"}
    finally:
        cleanup_run_skills(root)


def test_no_match_returns_none(installed_skills):
    """When no requested name matches, return None (caller skips plugins=)."""
    assert materialize_run_skills(["nope", "still-nope"]) is None


def test_empty_input_returns_none(installed_skills):
    """Empty skill_names is the legacy/no-op path."""
    assert materialize_run_skills([]) is None
    assert materialize_run_skills(frozenset()) is None


def test_rejects_unsafe_slug(tmp_path, monkeypatch):
    """A skill whose name fails the slug guard is skipped, never written."""
    src = tmp_path / "installed"
    src.mkdir()
    good = _make_skill_dir(src, "github")
    # A registered name that would escape the plugin dir if used as a slug.
    evil = Skill(
        name="../evil",
        description="bad",
        content="x",
        path=good.path,  # reuse a real SKILL.md so only the slug guard rejects it
    )
    fake = SkillLoader()
    fake._skills = {good.name: good, evil.name: evil}
    fake._loaded = True
    monkeypatch.setattr(loader_mod, "_skill_loader", fake)

    root = materialize_run_skills(["github", "../evil"])
    try:
        assert root is not None
        slugs = {p.name for p in (root / "skills").iterdir()}
        assert slugs == {"github"}, "unsafe slug must not produce a directory"
    finally:
        cleanup_run_skills(root)


def test_cleanup_removes_dir(installed_skills):
    """cleanup_run_skills deletes the materialized dir and tolerates None."""
    root = materialize_run_skills(["github"])
    assert root is not None and root.exists()
    cleanup_run_skills(root)
    assert not root.exists()
    cleanup_run_skills(None)  # no raise
    cleanup_run_skills(root)  # idempotent
