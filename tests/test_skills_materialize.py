# tests/test_skills_materialize.py
# Created: 2026-06-07 (feat/entity-pocket-profile-field, entity-rooms A2) —
# pins the per-run skill materializer that makes ``SurfaceProfile.skill_names``
# LIVE for the claude_agent_sdk backend. Covers: filters to the named subset,
# rejects unsafe slugs, produces a plugin-shaped dir (.claude-plugin/plugin.json
# + skills/<slug>/SKILL.md), returns None on no match, cleans up, resolves
# bundled skills, and the shared content-addressed, reference-counted dir.

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


# --- P1: bundled skills are resolvable by name ---------------------------
# Added 2026-07-25 (feat/bundled-skills-per-surface): a surface that names
# skills must be able to name a BUNDLED one. The SkillLoader only scans
# SKILL_PATHS (~/.agents, ~/.claude, ~/.pocketpaw), so a bundled skill was
# reachable by name only via the boot-time ~/.claude/skills mirror — which
# POCKETPAW_AUTO_INSTALL_BUNDLED_SKILLS=false turns off. These pin the
# packaged _bundled/skills/ dir as a resolution source of LAST resort.


@pytest.fixture
def fake_bundled(tmp_path, monkeypatch):
    """Point ``bundled_skills_plugin_dir()`` at a throwaway packaged bundle."""
    from pocketpaw.skills import materialize as materialize_mod

    bundle_root = tmp_path / "_bundled"
    (bundle_root / "skills").mkdir(parents=True)
    _make_skill_dir(bundle_root / "skills", "code-react")
    _make_skill_dir(bundle_root / "skills", "github")  # name clash on purpose
    monkeypatch.setattr(materialize_mod, "_bundled_plugin_root", lambda: bundle_root, raising=False)
    return bundle_root


def test_resolves_bundled_skill_absent_from_loader(installed_skills, fake_bundled):
    """A bundled skill the SkillLoader never saw still materializes by name."""
    root = materialize_run_skills(["code-react"])
    try:
        assert root is not None, "bundled skill must resolve without the ~/.claude mirror"
        assert (root / "skills" / "code-react" / "SKILL.md").is_file()
    finally:
        cleanup_run_skills(root)


def test_installed_skill_wins_over_bundled_on_name_clash(installed_skills, fake_bundled):
    """An installed skill overrides a bundled one of the same name."""
    root = materialize_run_skills(["github"])
    try:
        assert root is not None
        body = (root / "skills" / "github" / "SKILL.md").read_text(encoding="utf-8")
        assert "Body for github." in body
        # The installed fixture carries ref.md; the bundled one does not.
        assert (root / "skills" / "github" / "ref.md").is_file(), (
            "installed skill dir must win, assets and all"
        )
    finally:
        cleanup_run_skills(root)


def test_bundled_fallback_is_inert_when_no_bundle_present(installed_skills, monkeypatch):
    """No packaged bundle → unchanged behavior, no crash."""
    from pocketpaw.skills import materialize as materialize_mod

    monkeypatch.setattr(materialize_mod, "_bundled_plugin_root", lambda: None, raising=False)
    assert materialize_run_skills(["code-react"]) is None


# --- shared, content-addressed dirs ---------------------------------------
# A warm Claude CLI is bound to the plugin path it connected with, so the dir
# must not depend on the run: the same skills give the same path, references
# are counted, and the dir goes only when the last holder releases it. A run id
# (a session key full of ``:``) never reaches the path.

_RUN_DIR_RE = r"^paw-run-skills-[0-9a-f]+$"

_CLOUD_SESSION_KEY = (
    "cloud:session:6ad37f5d6c7b32c8c231987c:69f88339dc8997d1f758cb60:u:69f88339dc8997d1f758cb5d"
)


def test_session_key_run_id_yields_legal_shared_dir(installed_skills):
    """A colon-bearing session key still produces a legal dir, and the run id
    does not change which dir the same skills land in."""
    import re

    root_a = materialize_run_skills(["github"], run_id="cloud:session:6ad37f5d:u:69f8")
    root_b = materialize_run_skills(["github"], run_id=_CLOUD_SESSION_KEY)
    try:
        assert root_a is not None and root_a.is_dir()
        assert ":" not in str(root_a.relative_to(root_a.parent.parent))
        assert re.match(_RUN_DIR_RE, root_a.name), root_a.name
        assert root_a == root_b
    finally:
        cleanup_run_skills(root_a)
        cleanup_run_skills(root_b)
    assert not root_a.exists()


def test_dir_survives_until_last_reference_released(installed_skills):
    """Two holders of the same skills share one dir; releasing one keeps it."""
    first = materialize_run_skills(["github", "calendar-sync"])
    second = materialize_run_skills(["calendar-sync", "github"])
    assert first is not None and first == second
    cleanup_run_skills(first)
    assert (first / "skills" / "github" / "SKILL.md").is_file(), (
        "releasing one reference removed a dir another holder still uses"
    )
    cleanup_run_skills(second)
    assert not first.exists()


def test_different_skill_sets_get_different_dirs(installed_skills):
    a = materialize_run_skills(["github"])
    b = materialize_run_skills(["github", "calendar-sync"])
    try:
        assert a is not None and b is not None and a != b
    finally:
        cleanup_run_skills(a)
        cleanup_run_skills(b)


def test_changed_skill_files_get_a_new_dir(installed_skills):
    """Editing a skill yields a fresh dir; the old holder's dir is untouched."""
    old = materialize_run_skills(["github"])
    md = installed_skills.get_all()["github"].path
    md.write_text(md.read_text(encoding="utf-8") + "\nmore\n", encoding="utf-8")
    new = materialize_run_skills(["github"])
    try:
        assert old is not None and new is not None and old != new
        assert old.exists() and "more" in (new / "skills" / "github" / "SKILL.md").read_text(
            encoding="utf-8"
        )
    finally:
        cleanup_run_skills(old)
        cleanup_run_skills(new)


def test_damaged_dir_is_rebuilt_in_place(installed_skills):
    """A dir whose manifest vanished (partial tmp reaping) is rebuilt at the
    same path, so clients bound to that path see their skills again."""
    root = materialize_run_skills(["github"])
    try:
        assert root is not None
        (root / ".claude-plugin" / "plugin.json").unlink()
        again = materialize_run_skills(["github"])
        assert again == root
        assert (root / ".claude-plugin" / "plugin.json").is_file()
        cleanup_run_skills(again)
    finally:
        cleanup_run_skills(root)
    assert not list(root.parent.glob(".staging-*")), "a staging dir leaked"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("abc-123_x.y", "abc-123_x.y"),
        ("cloud:session:abc:u:def", "cloud-session-abc-u-def"),
        (r'a/b\c*d?e"f<g>h|i', "a-b-c-d-e-f-g-h-i"),
        ("::a::::b::", "a-b"),
        ("..", ""),
        ("", ""),
        ("ünïcode key", "n-code-key"),
    ],
)
def test_fs_safe_component(raw, expected):
    from pocketpaw.skills.materialize import fs_safe_component

    assert fs_safe_component(raw) == expected


def test_fs_safe_component_caps_with_stable_hash():
    from pocketpaw.skills.materialize import fs_safe_component

    long_a = "x" * 100
    long_b = "x" * 99 + "y"
    out_a = fs_safe_component(long_a, max_len=48)
    assert len(out_a) == 48
    assert out_a == fs_safe_component(long_a, max_len=48)  # stable
    assert out_a != fs_safe_component(long_b, max_len=48)  # distinguishable
    assert fs_safe_component("short", max_len=48) == "short"
