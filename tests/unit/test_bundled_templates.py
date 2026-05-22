# tests/unit/test_bundled_templates.py
# Created: 2026-05-22 (feat/bundled-templates, Increment 2a) — covers the
# built-in pocket templates: the SHA-256 mirror installer, the
# slug-keyed loader, the RFC 03 schema shape of each bundled
# template.pocket.yaml, the validity of each ripple_spec.json, and the
# create-specialist wiring (``_build_system_prompt`` template splice +
# ``AgentModeAdapter`` template short-circuit).
"""Tests for the ``pocketpaw.bundled_templates`` package and its wiring.

The installer + loader tests mirror into a ``tmp_path`` destination so
the user's real ``~/.pocketpaw/templates/`` is never touched. The
content tests assert each bundled template is well-formed against the
RFC 03 field set. The specialist-wiring tests confirm a ``template_id``
hint actually changes the create flow.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pocketpaw.bundled_templates.installer import (
    TemplateInstallResult,
    install_bundled_templates,
)
from pocketpaw.bundled_templates.loader import load_template

# The six built-in templates Increment 2a ships.
_EXPECTED_SLUGS = {
    "todo-task-tracker",
    "kanban-board",
    "metrics-dashboard",
    "crm-record-list",
    "calendar-planner",
    "activity-feed",
}

# RFC 03 Pocket Template Schema — the field set a seed template may
# carry. Seed templates ship ``actions: []`` and omit
# ``outcomes / instinct_rules / triggers / agents`` (Instinct + Outcomes
# are not wired yet — dead declarations are worse than none).
_RFC03_ALLOWED_FIELDS = {
    "name",
    "version",
    "vertical",
    "shape",
    "state",
    "actions",
    "connectors",
    "skills",
    "description",
}
_RFC03_REQUIRED_FIELDS = {"name", "version", "vertical", "shape", "state", "description"}
# RFC 03 shape enum — the fixed set of Ripple Layer 2 widget shapes.
_RFC03_SHAPE_ENUM = {
    "data-grid",
    "kanban",
    "calendar",
    "map",
    "timeline",
    "tree",
    "chart",
    "custom",
}

_BUNDLED_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "pocketpaw" / "bundled_templates" / "_bundled"
)


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------


def test_installer_copies_all_six_templates(tmp_path: Path) -> None:
    """First run mirrors every bundled template directory plus index.json."""
    results = install_bundled_templates(destination_root=tmp_path)

    installed_slugs = {r.name for r in results if r.name in _EXPECTED_SLUGS}
    assert installed_slugs == _EXPECTED_SLUGS

    for slug in _EXPECTED_SLUGS:
        slug_dir = tmp_path / slug
        assert (slug_dir / "template.pocket.yaml").is_file()
        assert (slug_dir / "ripple_spec.json").is_file()

    # The registry index sits beside the slug directories.
    assert (tmp_path / "index.json").is_file()
    index_result = next(r for r in results if r.name == "index.json")
    assert index_result.status == "installed"

    # Every per-template result is a frozen dataclass with a clean status.
    for r in results:
        assert isinstance(r, TemplateInstallResult)
        assert r.status in {"installed", "updated", "skipped", "failed"}
        if r.name in _EXPECTED_SLUGS:
            assert r.status == "installed"
            assert r.error is None


def test_installer_skips_unchanged_on_second_run(tmp_path: Path) -> None:
    """Second install with unchanged content is a no-op (SHA-256 match)."""
    install_bundled_templates(destination_root=tmp_path)
    results2 = install_bundled_templates(destination_root=tmp_path)

    for r in results2:
        assert r.status == "skipped", f"{r.name} should be skipped on the 2nd run, got {r.status}"


def test_installer_never_raises_on_oserror(tmp_path: Path, monkeypatch) -> None:
    """An OSError during copy yields a ``failed`` result, never a raise —
    template install is best-effort and must not block dashboard boot."""
    import pocketpaw.bundled_templates.installer as installer_mod

    def _explode(*args, **kwargs):  # noqa: ANN001 — test stub
        raise OSError("simulated permission denied on ~/.pocketpaw/templates/")

    monkeypatch.setattr(installer_mod.shutil, "copy2", _explode)

    results = install_bundled_templates(destination_root=tmp_path)
    assert results, "installer should still return per-entry results"
    assert all(r.status == "failed" for r in results)


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_loader_returns_meta_and_ripple_spec_for_known_slug(tmp_path: Path) -> None:
    """A known, installed slug loads to {meta, ripple_spec}, both dicts."""
    install_bundled_templates(destination_root=tmp_path)

    loaded = load_template("todo-task-tracker", templates_dir=tmp_path)
    assert loaded is not None
    assert set(loaded.keys()) == {"meta", "ripple_spec"}
    assert isinstance(loaded["meta"], dict)
    assert isinstance(loaded["ripple_spec"], dict)
    assert loaded["meta"]["name"] == "todo-task-tracker"
    assert "ui" in loaded["ripple_spec"]
    assert "state" in loaded["ripple_spec"]


def test_loader_returns_none_for_unknown_slug(tmp_path: Path) -> None:
    """An unknown slug returns None — the caller cold-generates instead."""
    install_bundled_templates(destination_root=tmp_path)
    assert load_template("does-not-exist", templates_dir=tmp_path) is None


def test_loader_honors_templates_dir_override(tmp_path: Path) -> None:
    """The ``templates_dir`` override is respected — nothing reads the
    default ~/.pocketpaw/templates/ when the override is supplied."""
    other_root = tmp_path / "elsewhere"
    install_bundled_templates(destination_root=other_root)

    # The override dir has the template; a sibling empty dir does not.
    assert load_template("kanban-board", templates_dir=other_root) is not None
    assert load_template("kanban-board", templates_dir=tmp_path / "empty") is None


def test_loader_rejects_path_traversal_slug(tmp_path: Path) -> None:
    """A slug with path separators is rejected — the slug is an untrusted
    hint and must not escape the templates root."""
    install_bundled_templates(destination_root=tmp_path)
    assert load_template("../etc/passwd", templates_dir=tmp_path) is None
    assert load_template("", templates_dir=tmp_path) is None


# ---------------------------------------------------------------------------
# Bundled-content shape — RFC 03 schema + valid rippleSpec JSON
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("slug", sorted(_EXPECTED_SLUGS))
def test_template_yaml_matches_rfc03_field_set(slug: str) -> None:
    """Each bundled template.pocket.yaml uses ONLY RFC 03 fields, carries
    every required field, ships ``actions: []``, and uses a valid shape."""
    import yaml

    meta_path = _BUNDLED_DIR / slug / "template.pocket.yaml"
    meta = yaml.safe_load(meta_path.read_text(encoding="utf-8"))

    assert isinstance(meta, dict)
    fields = set(meta.keys())
    extra = fields - _RFC03_ALLOWED_FIELDS
    assert not extra, f"{slug}: non-RFC03 fields {extra}"
    missing = _RFC03_REQUIRED_FIELDS - fields
    assert not missing, f"{slug}: missing required fields {missing}"

    # Seed templates ship empty actions; Instinct/Outcomes aren't wired.
    assert meta["actions"] == [], f"{slug}: seed templates ship actions: []"
    # Omit the not-yet-wired RFC 03 optional fields.
    for omitted in ("outcomes", "instinct_rules", "triggers", "agents"):
        assert omitted not in meta, f"{slug}: should omit {omitted}"

    assert meta["shape"] in _RFC03_SHAPE_ENUM, f"{slug}: bad shape {meta['shape']!r}"
    assert isinstance(meta["name"], str) and meta["name"] == slug
    assert isinstance(meta["state"], dict) and "entity_type" in meta["state"]


@pytest.mark.parametrize("slug", sorted(_EXPECTED_SLUGS))
def test_template_ripple_spec_is_valid_json(slug: str) -> None:
    """Each ripple_spec.json parses, carries ui + state, and includes the
    mandated top-level ``_placeholder_note`` field."""
    spec_path = _BUNDLED_DIR / slug / "ripple_spec.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    assert isinstance(spec, dict)
    assert "ui" in spec, f"{slug}: ripple_spec must carry a ui tree"
    assert "state" in spec, f"{slug}: ripple_spec must carry a state block"
    assert "_placeholder_note" in spec, f"{slug}: ripple_spec must carry _placeholder_note"


def test_crm_template_ships_sources_placeholder() -> None:
    """The CRM template ships a placeholder live-data ``sources`` entry —
    a GET /contacts binding into state.records."""
    spec = json.loads((_BUNDLED_DIR / "crm-record-list" / "ripple_spec.json").read_text())
    sources = spec.get("sources")
    assert isinstance(sources, dict) and "records" in sources
    records = sources["records"]
    assert records["method"] == "GET"
    assert records["path"] == "/contacts"
    assert records["bind"] == "state.records"
    assert "pocket_open" in records["refresh"]


def test_index_json_lists_all_six_templates() -> None:
    """index.json registers every bundled template with the registry
    row shape the chat agent's STEP 0 keyword match consumes."""
    index = json.loads((_BUNDLED_DIR / "index.json").read_text())
    rows = index["templates"]
    assert {r["slug"] for r in rows} == _EXPECTED_SLUGS
    for r in rows:
        assert {"slug", "title", "shape", "pattern", "keywords", "connectors_hint"} <= set(r.keys())
        assert isinstance(r["keywords"], list) and r["keywords"]

    # The slug of every index row must have a real template directory.
    for r in rows:
        assert (_BUNDLED_DIR / r["slug"]).is_dir()


@pytest.mark.parametrize("slug", sorted(_EXPECTED_SLUGS))
def test_template_ripple_spec_passes_manifest_validation_if_reachable(slug: str) -> None:
    """Each ripple_spec.json passes the Ripple manifest validator when a
    manifest is reachable. The validator needs a network-fetched manifest;
    when offline (CI default) it returns no manifest and the check is
    skipped — never a hard failure on infrastructure."""
    import asyncio

    from pocketpaw.config import get_settings
    from pocketpaw.ripple.manifest import get_manifest, validate_against_manifest

    settings = get_settings()
    manifest = asyncio.run(
        get_manifest(settings.ripple_manifest_url, ttl_seconds=settings.ripple_manifest_ttl_seconds)
    )
    if manifest is None:
        pytest.skip("ripple manifest not reachable — skipping manifest validation")

    spec = json.loads((_BUNDLED_DIR / slug / "ripple_spec.json").read_text())
    issues = validate_against_manifest(spec, manifest, apply_aliases=True)
    assert issues == [], f"{slug}: manifest validation issues {issues}"


# ---------------------------------------------------------------------------
# Create-specialist wiring — _build_system_prompt template splice
# ---------------------------------------------------------------------------


def _install_to_default(tmp_path: Path, monkeypatch) -> Path:
    """Install the bundled templates into a tmp dir and point the loader's
    default templates root at it — so code paths that call
    ``load_template`` with no override (the runtime / adapter wiring)
    resolve against the tmp install."""
    import pocketpaw.bundled_templates.loader as loader_mod

    root = tmp_path / "templates"
    install_bundled_templates(destination_root=root)
    monkeypatch.setattr(loader_mod, "_DEFAULT_TEMPLATES_DIR", root)
    return root


def test_build_system_prompt_includes_template_block_when_template_id_set(
    tmp_path: Path, monkeypatch
) -> None:
    """``_build_system_prompt`` splices the template skeleton + the
    customization rules in when ``hints.template_id`` is set."""
    pytest.importorskip("pocketpaw_ee")  # EE-only: skips on an OSS-only install
    _install_to_default(tmp_path, monkeypatch)

    from pocketpaw_ee.agent.pocket_specialist.runtime import (
        PocketSpecialistHints,
        _build_system_prompt,
    )

    hints = PocketSpecialistHints(template_id="kanban-board")
    prompt = _build_system_prompt(hints)

    assert "BUILT-IN TEMPLATE — INSTANTIATE AND CUSTOMIZE" in prompt
    assert "kanban-board" in prompt
    assert "CUSTOMIZATION RULES" in prompt
    # The actual skeleton JSON is spliced in.
    assert '"columnKey": "status"' in prompt


def test_build_system_prompt_excludes_template_block_without_template_id(
    tmp_path: Path, monkeypatch
) -> None:
    """Without a ``template_id`` hint the template block is absent —
    the specialist cold-generates as before."""
    pytest.importorskip("pocketpaw_ee")  # EE-only: skips on an OSS-only install
    _install_to_default(tmp_path, monkeypatch)

    from pocketpaw_ee.agent.pocket_specialist.runtime import (
        PocketSpecialistHints,
        _build_system_prompt,
    )

    # No hints at all.
    assert "BUILT-IN TEMPLATE — INSTANTIATE AND CUSTOMIZE" not in _build_system_prompt(None)

    # Hints with no template_id.
    hints = PocketSpecialistHints(layout="hero+grid", focal_widget="data-grid")
    prompt = _build_system_prompt(hints)
    assert "BUILT-IN TEMPLATE — INSTANTIATE AND CUSTOMIZE" not in prompt
    # The structural-plan hints still land.
    assert "STRUCTURAL PLAN FROM PARENT AGENT" in prompt


def test_build_system_prompt_ignores_unknown_template_id(tmp_path: Path, monkeypatch) -> None:
    """An unknown ``template_id`` slug is ignored — no template block,
    no crash. The specialist falls back to cold generation."""
    pytest.importorskip("pocketpaw_ee")  # EE-only: skips on an OSS-only install
    _install_to_default(tmp_path, monkeypatch)

    from pocketpaw_ee.agent.pocket_specialist.runtime import (
        PocketSpecialistHints,
        _build_system_prompt,
    )

    hints = PocketSpecialistHints(template_id="no-such-template")
    prompt = _build_system_prompt(hints)
    assert "BUILT-IN TEMPLATE — INSTANTIATE AND CUSTOMIZE" not in prompt


# ---------------------------------------------------------------------------
# Create-specialist wiring — AgentModeAdapter template short-circuit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_mode_skips_draft_kit_when_template_id_set(tmp_path: Path, monkeypatch) -> None:
    """``AgentModeAdapter.create`` short-circuits a ``template_id`` hint:
    it loads the template and goes straight to validate-and-persist,
    SKIPPING the draft-kit round-trip."""
    pytest.importorskip("pocketpaw_ee")  # EE-only: skips on an OSS-only install
    _install_to_default(tmp_path, monkeypatch)

    import pocketpaw_ee.agent.pocket_specialist.adapters as adapters_mod
    from pocketpaw_ee.agent.pocket_specialist.runtime import (
        PocketSpecialistCreateInput,
        PocketSpecialistCreateOutput,
        PocketSpecialistHints,
    )

    persisted_specs: list[dict] = []
    draft_kit_calls: list[object] = []

    async def _fake_validate_and_persist(input, **kwargs):  # noqa: ANN001 — test stub
        persisted_specs.append(input.spec)
        return PocketSpecialistCreateOutput(
            ok=True, action="created", pocket={"id": "p1"}, duration_ms=1, backend_used="agent_mode"
        )

    def _fake_draft_kit(input, *, started):  # noqa: ANN001 — test stub
        draft_kit_calls.append(input)
        return PocketSpecialistCreateOutput(
            ok=False, action="draft_kit", duration_ms=1, backend_used="agent_mode"
        )

    monkeypatch.setattr(adapters_mod, "_validate_and_persist", _fake_validate_and_persist)
    monkeypatch.setattr(adapters_mod, "_draft_kit_response", _fake_draft_kit)

    adapter = adapters_mod.AgentModeAdapter()
    payload = PocketSpecialistCreateInput(
        brief="a kanban board for the team sprint",
        hints=PocketSpecialistHints(template_id="kanban-board"),
    )
    out = await adapter.create(payload, workspace_id="w1", user_id="u1", settings=object())

    assert out.ok is True
    assert out.action == "created"
    # Draft kit was skipped; validate-and-persist ran with the template spec.
    assert draft_kit_calls == []
    assert len(persisted_specs) == 1
    spec = persisted_specs[0]
    assert "ui" in spec and "state" in spec
    # Authoring-only keys are stripped before persist.
    assert "_placeholder_note" not in spec


@pytest.mark.asyncio
async def test_agent_mode_falls_back_to_draft_kit_on_unknown_slug(
    tmp_path: Path, monkeypatch
) -> None:
    """An unknown ``template_id`` slug falls back to the normal draft-kit
    flow — never persists, never crashes."""
    pytest.importorskip("pocketpaw_ee")  # EE-only: skips on an OSS-only install
    _install_to_default(tmp_path, monkeypatch)

    import pocketpaw_ee.agent.pocket_specialist.adapters as adapters_mod
    from pocketpaw_ee.agent.pocket_specialist.runtime import (
        PocketSpecialistCreateInput,
        PocketSpecialistCreateOutput,
        PocketSpecialistHints,
    )

    persisted_specs: list[dict] = []
    draft_kit_calls: list[object] = []

    async def _fake_validate_and_persist(input, **kwargs):  # noqa: ANN001 — test stub
        persisted_specs.append(input.spec)
        return PocketSpecialistCreateOutput(
            ok=True, action="created", pocket={"id": "p1"}, duration_ms=1, backend_used="agent_mode"
        )

    def _fake_draft_kit(input, *, started):  # noqa: ANN001 — test stub
        draft_kit_calls.append(input)
        return PocketSpecialistCreateOutput(
            ok=False, action="draft_kit", duration_ms=1, backend_used="agent_mode"
        )

    monkeypatch.setattr(adapters_mod, "_validate_and_persist", _fake_validate_and_persist)
    monkeypatch.setattr(adapters_mod, "_draft_kit_response", _fake_draft_kit)

    adapter = adapters_mod.AgentModeAdapter()
    payload = PocketSpecialistCreateInput(
        brief="something the templates do not cover at all",
        hints=PocketSpecialistHints(template_id="no-such-template"),
    )
    out = await adapter.create(payload, workspace_id="w1", user_id="u1", settings=object())

    assert out.action == "draft_kit"
    assert len(draft_kit_calls) == 1
    assert persisted_specs == []
