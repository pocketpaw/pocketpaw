# tests/unit/test_cli_template.py
# Created: 2026-05-25 (feat/rfc-03-v2-cli) — RED-first tests for the
# new ``pocketpaw template`` CLI subcommand (lint / migrate / diff).
# Modified 2026-05-25 (feat/rfc-03-v2-compile): added tests for the
# new ``compile`` subaction — JSON (default) + YAML (--yaml) output,
# end-to-end through the dispatch path, plus argparse wiring sanity.
# Modified 2026-05-28 (feat/wave-4b-lint-fabric): added Wave 4b
# ``lint`` Fabric-tier coverage. ``_run_lint`` now calls
# ``validate_template_with_registry`` after the Pydantic chokepoint
# passes, defaulting to ``NullFabricRegistry`` and accepting a
# ``--registry <path>`` JSON override. The new tests cover:
#   * synthetic-tier templates (no joins) lint clean against Null;
#   * registered-tier templates fail against Null (correct loud signal);
#   * a JSON registry mock unblocks the same template;
#   * ``--json`` output gains a ``fabric_validations`` array.
# These cover the six contract pillars from RFC 03 v2 "Style and tooling
# notes":
#   1. lint on a clean v2 fixture exits 0 and reports schema_version.
#   2. lint on a v1 file auto-promotes and notes the rewrite.
#   3. lint on an invalid file (missing field / bad enum / bad CEL)
#      exits 1 and surfaces a per-field message.
#   4. lint heuristically warns on implausible pattern x shape pairs
#      but still exits 0.
#   5. migrate is idempotent on v2, prompts unless --yes, writes a
#      .v1.bak unless --no-backup, and preserves YAML vs JSON format.
#   6. diff returns a semantic dict-walked diff (+ / - / ~) grouped
#      under top-level fields, empty for identical inputs.
#   7. compile emits a runtime-shaped rippleSpec dict to stdout — JSON
#      by default, YAML under --yaml; round-trips through argparse.
# All commands also support --json for scripting.
"""Tests for the ``pocketpaw template`` CLI subcommand.

These tests invoke the entry function ``run_template_cmd`` directly so
they exercise the subcommand dispatch without going through argparse.
The argparse wiring lives in ``pocketpaw.__main__`` and is covered by
its own (existing) test_cli_flags shape — repeating it here would only
test argparse itself.
"""

from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest
import yaml

# The new module under test. RED on import until Phase 2 lands it.
from pocketpaw.cli.template import run_template_cmd

_FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "templates"
_LEASE_V2 = _FIXTURES_DIR / "lease-renewal-v2.yaml"
_BUNDLED_DIR = (
    Path(__file__).resolve().parents[2] / "src" / "pocketpaw" / "bundled_templates" / "_bundled"
)
_TODO_V2 = _BUNDLED_DIR / "todo-task-tracker" / "template.pocket.yaml"
_KANBAN_V2 = _BUNDLED_DIR / "kanban-board" / "template.pocket.yaml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _capture(fn, /, *args, **kwargs) -> tuple[int, str]:
    """Call a CLI entry function and return ``(exit_code, stdout)``."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = fn(*args, **kwargs)
    return rc, buf.getvalue()


def _minimal_v2_dict() -> dict:
    """Smallest dict that round-trips through the Pydantic chokepoint."""
    return {
        "schema_version": "2",
        "name": "minimal-template",
        "version": "1.0.0",
        "pattern": "app",
        "vertical": "productivity",
        "description": "A minimal template used only by CLI tests.",
        "shape": "data-grid",
        "state": {
            "entity_type": "Thing",
            "columns": [
                {"field": "title", "widget": "text"},
                {"field": "status", "widget": "badge"},
            ],
        },
    }


def _minimal_v1_dict() -> dict:
    """v1-shaped dict that mirrors the shipped bundled templates."""
    return {
        "name": "todo-task-tracker",
        "version": "1.0.0",
        "vertical": "productivity",
        "shape": "data-grid",
        "description": "A personal task tracker.",
        "state": {
            "entity_type": "Task",
            "columns": [{"field": "title", "widget": "text"}],
        },
        "actions": [],
        "connectors": [],
        "skills": [],
    }


def _write_yaml(path: Path, data: dict) -> Path:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def _write_json(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


# ===========================================================================
# pocketpaw template lint <file>
# ===========================================================================


def test_lint_clean_v2_fixture_exits_zero_and_reports_schema_version(tmp_path: Path) -> None:
    # Wave 4b: lease-renewal-v2 declares joined_entities (registered
    # tier), so the default NullFabricRegistry would fail it. Pass an
    # explicit JSON registry mock that satisfies the joins so the test
    # exercises the "clean v2 fixture" path it originally targeted.
    reg = tmp_path / "fabric.json"
    reg.write_text(
        json.dumps(
            {
                "entity_types": ["Lease", "Tenant", "Property"],
                "links": [
                    {"from": "Lease", "to": "Tenant", "name": "lease_tenant"},
                    {"from": "Lease", "to": "Property", "name": "lease_property"},
                ],
            }
        ),
        encoding="utf-8",
    )
    rc, out = _capture(
        run_template_cmd,
        subaction="lint",
        file1=str(_LEASE_V2),
        registry_path=str(reg),
    )
    assert rc == 0
    assert "valid" in out.lower()
    # surfaces both the schema version and the slug name
    assert "v2" in out or "schema_version=v2" in out or '"2"' in out
    assert "lease-renewal" in out


def test_lint_bundled_v2_template_exits_zero(tmp_path: Path) -> None:
    rc, out = _capture(run_template_cmd, subaction="lint", file1=str(_TODO_V2))
    assert rc == 0, f"bundled template lint failed: {out}"
    assert "todo-task-tracker" in out


def test_lint_missing_required_field_exits_one(tmp_path: Path) -> None:
    bad = _minimal_v2_dict()
    del bad["version"]  # required field
    f = _write_yaml(tmp_path / "bad.yaml", bad)
    rc, out = _capture(run_template_cmd, subaction="lint", file1=str(f))
    assert rc == 1
    # human-readable error must point at the missing field
    assert "version" in out.lower()


def test_lint_invalid_enum_value_exits_one(tmp_path: Path) -> None:
    bad = _minimal_v2_dict()
    bad["shape"] = "not-a-real-shape"
    f = _write_yaml(tmp_path / "bad-enum.yaml", bad)
    rc, out = _capture(run_template_cmd, subaction="lint", file1=str(f))
    assert rc == 1
    assert "shape" in out.lower()


def test_lint_bad_cel_expression_exits_one(tmp_path: Path) -> None:
    bad = _minimal_v2_dict()
    bad["state"]["saved_views"] = [{"name": "broken", "filter": "this is :: not :: cel"}]
    f = _write_yaml(tmp_path / "bad-cel.yaml", bad)
    rc, out = _capture(run_template_cmd, subaction="lint", file1=str(f))
    assert rc == 1


def test_lint_v1_input_auto_promotes_and_notes_rewrite(tmp_path: Path) -> None:
    v1 = _minimal_v1_dict()
    f = _write_yaml(tmp_path / "old.yaml", v1)
    rc, out = _capture(run_template_cmd, subaction="lint", file1=str(f))
    # v1 still passes Pydantic *after* promotion — exit 0, with a
    # human-readable note that the rewrite was applied.
    assert rc == 0, f"v1 lint after promote should pass; got: {out}"
    lower = out.lower()
    assert "v1" in lower
    assert "promot" in lower or "rewrite" in lower or "migrat" in lower


def test_lint_pattern_shape_warning_is_warning_not_error(tmp_path: Path) -> None:
    """`dashboard` x `kanban` is documented as a heuristic warning, not
    a hard error. Exit code stays 0; output includes WARN."""
    odd = _minimal_v2_dict()
    odd["pattern"] = "dashboard"
    odd["shape"] = "kanban"
    odd["state"]["default_view"] = "kanban"  # required for kanban shape
    f = _write_yaml(tmp_path / "odd.yaml", odd)
    rc, out = _capture(run_template_cmd, subaction="lint", file1=str(f))
    assert rc == 0
    lower = out.lower()
    assert "warn" in lower or "unusual" in lower


def test_lint_json_output_shape(tmp_path: Path) -> None:
    rc, out = _capture(
        run_template_cmd,
        subaction="lint",
        file1=str(_TODO_V2),
        as_json=True,
    )
    assert rc == 0
    data = json.loads(out)
    assert isinstance(data, dict)
    for key in ("file", "valid", "errors", "warnings", "schema_version"):
        assert key in data, f"missing key {key!r} in --json output"
    assert data["valid"] is True
    assert data["errors"] == []


def test_lint_json_output_on_invalid(tmp_path: Path) -> None:
    bad = _minimal_v2_dict()
    del bad["version"]
    f = _write_yaml(tmp_path / "bad.yaml", bad)
    rc, out = _capture(
        run_template_cmd,
        subaction="lint",
        file1=str(f),
        as_json=True,
    )
    assert rc == 1
    data = json.loads(out)
    assert data["valid"] is False
    assert isinstance(data["errors"], list)
    assert len(data["errors"]) >= 1


def test_lint_json_marks_promoted_from_v1(tmp_path: Path) -> None:
    v1 = _minimal_v1_dict()
    f = _write_yaml(tmp_path / "old.yaml", v1)
    rc, out = _capture(
        run_template_cmd,
        subaction="lint",
        file1=str(f),
        as_json=True,
    )
    assert rc == 0
    data = json.loads(out)
    assert data["promoted_from_v1"] is True


def test_lint_accepts_json_input(tmp_path: Path) -> None:
    f = _write_json(tmp_path / "minimal.json", _minimal_v2_dict())
    rc, out = _capture(run_template_cmd, subaction="lint", file1=str(f))
    assert rc == 0


# ===========================================================================
# pocketpaw template migrate <file>
# ===========================================================================


def test_migrate_v1_to_v2_happy_path_with_backup(tmp_path: Path) -> None:
    v1 = _minimal_v1_dict()
    f = _write_yaml(tmp_path / "lease.yaml", v1)
    rc, out = _capture(
        run_template_cmd,
        subaction="migrate",
        file1=str(f),
        yes=True,  # skip prompt
    )
    assert rc == 0
    # 1. file is now v2
    parsed = yaml.safe_load(f.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == "2"
    # 2. backup exists with original v1 content
    backup = f.with_suffix(f.suffix + ".v1.bak")
    assert backup.exists()
    backup_parsed = yaml.safe_load(backup.read_text(encoding="utf-8"))
    assert "schema_version" not in backup_parsed  # original v1 had none
    # 3. stdout mentions migration + backup
    assert "migrate" in out.lower() or "v2" in out.lower()


def test_migrate_already_v2_is_noop(tmp_path: Path) -> None:
    v2 = _minimal_v2_dict()
    f = _write_yaml(tmp_path / "already.yaml", v2)
    original = f.read_text(encoding="utf-8")
    rc, out = _capture(
        run_template_cmd,
        subaction="migrate",
        file1=str(f),
        yes=True,
    )
    assert rc == 0
    assert f.read_text(encoding="utf-8") == original
    # no backup created for a noop
    backup = f.with_suffix(f.suffix + ".v1.bak")
    assert not backup.exists()
    assert "already" in out.lower() or "noop" in out.lower() or "no change" in out.lower()


def test_migrate_no_backup_flag_skips_backup(tmp_path: Path) -> None:
    v1 = _minimal_v1_dict()
    f = _write_yaml(tmp_path / "lease.yaml", v1)
    rc, _out = _capture(
        run_template_cmd,
        subaction="migrate",
        file1=str(f),
        yes=True,
        no_backup=True,
    )
    assert rc == 0
    backup = f.with_suffix(f.suffix + ".v1.bak")
    assert not backup.exists()


def test_migrate_preserves_json_format(tmp_path: Path) -> None:
    """A .json input must be rewritten as JSON, not YAML."""
    v1 = _minimal_v1_dict()
    f = _write_json(tmp_path / "lease.json", v1)
    rc, _out = _capture(
        run_template_cmd,
        subaction="migrate",
        file1=str(f),
        yes=True,
    )
    assert rc == 0
    # json.loads succeeds = output is JSON
    parsed = json.loads(f.read_text(encoding="utf-8"))
    assert parsed["schema_version"] == "2"


def test_migrate_json_output_already_v2(tmp_path: Path) -> None:
    v2 = _minimal_v2_dict()
    f = _write_yaml(tmp_path / "v2.yaml", v2)
    rc, out = _capture(
        run_template_cmd,
        subaction="migrate",
        file1=str(f),
        yes=True,
        as_json=True,
    )
    assert rc == 0
    data = json.loads(out)
    assert data["was_already_v2"] is True
    assert data["migrated"] is False


def test_migrate_json_output_with_migration(tmp_path: Path) -> None:
    v1 = _minimal_v1_dict()
    f = _write_yaml(tmp_path / "v1.yaml", v1)
    rc, out = _capture(
        run_template_cmd,
        subaction="migrate",
        file1=str(f),
        yes=True,
        as_json=True,
    )
    assert rc == 0
    data = json.loads(out)
    assert data["migrated"] is True
    assert data["was_already_v2"] is False
    assert data["backup_path"]
    assert Path(data["backup_path"]).exists()


def test_migrate_prompt_aborts_without_yes(tmp_path: Path, monkeypatch) -> None:
    """No --yes and user types anything other than y/Y aborts."""
    v1 = _minimal_v1_dict()
    f = _write_yaml(tmp_path / "v1.yaml", v1)
    monkeypatch.setattr("builtins.input", lambda _prompt="": "n")
    rc, out = _capture(
        run_template_cmd,
        subaction="migrate",
        file1=str(f),
        yes=False,
    )
    assert rc == 0  # graceful abort, not error
    # file unchanged — still v1
    parsed = yaml.safe_load(f.read_text(encoding="utf-8"))
    assert "schema_version" not in parsed
    assert "abort" in out.lower() or "cancel" in out.lower() or "skipp" in out.lower()


# ===========================================================================
# pocketpaw template diff <file1> <file2>
# ===========================================================================


def test_diff_identical_files_yields_empty(tmp_path: Path) -> None:
    v2 = _minimal_v2_dict()
    a = _write_yaml(tmp_path / "a.yaml", v2)
    b = _write_yaml(tmp_path / "b.yaml", v2)
    rc, out = _capture(
        run_template_cmd,
        subaction="diff",
        file1=str(a),
        file2=str(b),
    )
    assert rc == 0
    # no +/-/~ lines should be present
    body_lines = [ln for ln in out.splitlines() if ln.startswith(("+ ", "- ", "~ "))]
    assert body_lines == []


def test_diff_added_field(tmp_path: Path) -> None:
    base = _minimal_v2_dict()
    added = _minimal_v2_dict()
    added["icon"] = "home"
    a = _write_yaml(tmp_path / "a.yaml", base)
    b = _write_yaml(tmp_path / "b.yaml", added)
    rc, out = _capture(
        run_template_cmd,
        subaction="diff",
        file1=str(a),
        file2=str(b),
    )
    assert rc == 0
    assert any("+ " in ln and "icon" in ln for ln in out.splitlines())


def test_diff_removed_field(tmp_path: Path) -> None:
    base = _minimal_v2_dict()
    base["icon"] = "home"
    removed = _minimal_v2_dict()
    a = _write_yaml(tmp_path / "a.yaml", base)
    b = _write_yaml(tmp_path / "b.yaml", removed)
    rc, out = _capture(
        run_template_cmd,
        subaction="diff",
        file1=str(a),
        file2=str(b),
    )
    assert rc == 0
    assert any("- " in ln and "icon" in ln for ln in out.splitlines())


def test_diff_changed_value(tmp_path: Path) -> None:
    base = _minimal_v2_dict()
    changed = _minimal_v2_dict()
    changed["version"] = "2.0.0"
    a = _write_yaml(tmp_path / "a.yaml", base)
    b = _write_yaml(tmp_path / "b.yaml", changed)
    rc, out = _capture(
        run_template_cmd,
        subaction="diff",
        file1=str(a),
        file2=str(b),
    )
    assert rc == 0
    assert any("~ " in ln and "version" in ln for ln in out.splitlines())
    assert "1.0.0" in out and "2.0.0" in out


def test_diff_normalises_v1_against_v2(tmp_path: Path) -> None:
    """A v1 file diffed against the equivalent v2 file should be (mostly)
    empty after the internal promotion pass."""
    v1 = _minimal_v1_dict()
    # build the v2 equivalent — same data, post-promotion
    v2 = dict(v1)
    v2["schema_version"] = "2"
    v2["display_name"] = "Todo Task Tracker"
    v2["pattern"] = "app"
    # rename skills -> skill_refs
    v2["skill_refs"] = v2.pop("skills")
    a = _write_yaml(tmp_path / "v1.yaml", v1)
    b = _write_yaml(tmp_path / "v2.yaml", v2)
    rc, out = _capture(
        run_template_cmd,
        subaction="diff",
        file1=str(a),
        file2=str(b),
    )
    assert rc == 0
    body_lines = [ln for ln in out.splitlines() if ln.startswith(("+ ", "- ", "~ "))]
    assert body_lines == []


def test_diff_json_output(tmp_path: Path) -> None:
    base = _minimal_v2_dict()
    other = _minimal_v2_dict()
    other["version"] = "2.0.0"
    a = _write_yaml(tmp_path / "a.yaml", base)
    b = _write_yaml(tmp_path / "b.yaml", other)
    rc, out = _capture(
        run_template_cmd,
        subaction="diff",
        file1=str(a),
        file2=str(b),
        as_json=True,
    )
    assert rc == 0
    data = json.loads(out)
    assert isinstance(data, list)
    assert any(entry.get("op") == "change" and entry.get("path") == "version" for entry in data)


# ===========================================================================
# Argument-error surface — bad usage exits non-zero with a friendly message
# ===========================================================================


def test_unknown_subaction_returns_error() -> None:
    rc, _out = _capture(
        run_template_cmd,
        subaction="frobnicate",
        file1="ignored",
    )
    assert rc != 0


def test_lint_missing_file_returns_error(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    rc, _out = _capture(run_template_cmd, subaction="lint", file1=str(missing))
    assert rc != 0


def test_diff_requires_two_files(tmp_path: Path) -> None:
    f = _write_yaml(tmp_path / "a.yaml", _minimal_v2_dict())
    rc, _out = _capture(
        run_template_cmd,
        subaction="diff",
        file1=str(f),
        file2=None,
    )
    assert rc != 0


# ===========================================================================
# Argparse wiring — sanity that `pocketpaw template lint` parses
# ===========================================================================


def test_argparse_accepts_template_subcommand(monkeypatch: pytest.MonkeyPatch) -> None:
    """`pocketpaw template lint <file>` resolves to the template entry point."""
    from pocketpaw.__main__ import _build_parser, _resolve_subargs

    parser = _build_parser()
    args = parser.parse_args(["template", "lint", str(_TODO_V2)])
    _resolve_subargs(args)
    assert args.command == "template"
    assert args.subaction == "lint"
    # the file path must be plumbed through to args.file1
    assert getattr(args, "file1", None) == str(_TODO_V2)


def test_argparse_template_diff_two_files(monkeypatch: pytest.MonkeyPatch) -> None:
    from pocketpaw.__main__ import _build_parser, _resolve_subargs

    parser = _build_parser()
    args = parser.parse_args(["template", "diff", str(_TODO_V2), str(_KANBAN_V2)])
    _resolve_subargs(args)
    assert args.subaction == "diff"
    assert args.file1 == str(_TODO_V2)
    assert args.file2 == str(_KANBAN_V2)


# ===========================================================================
# pocketpaw template compile <file>
# ===========================================================================

_LEASE_V2_FIXTURE = _FIXTURES_DIR / "lease-renewal-v2.yaml"


def test_compile_emits_json_by_default() -> None:
    """``compile <file>`` prints the runtime-shaped rippleSpec as JSON
    on stdout. Exit 0 on a clean v2 fixture."""
    rc, out = _capture(run_template_cmd, subaction="compile", file1=str(_LEASE_V2_FIXTURE))
    assert rc == 0
    data = json.loads(out)
    assert isinstance(data, dict)
    # data_sources -> sources translation must have happened
    assert "sources" in data
    assert set(data["sources"].keys()) == {"expiring_leases", "tenant_responses"}
    # name passthrough at the top level
    assert data["name"] == "lease-renewal-v1"


def test_compile_yaml_flag_emits_yaml() -> None:
    """``compile --yaml`` emits YAML instead of JSON. Round-trip via
    ``yaml.safe_load`` must produce the same dict shape as the JSON
    output."""
    rc, out = _capture(
        run_template_cmd,
        subaction="compile",
        file1=str(_LEASE_V2_FIXTURE),
        as_yaml=True,
    )
    assert rc == 0
    # YAML output should parse cleanly
    data = yaml.safe_load(out)
    assert isinstance(data, dict)
    assert "sources" in data
    assert set(data["sources"].keys()) == {"expiring_leases", "tenant_responses"}


def test_compile_bundled_template_produces_empty_sources() -> None:
    """The bundled todo template carries no ``data_sources`` — compile
    must produce ``{"sources": {}}`` and still exit 0."""
    rc, out = _capture(run_template_cmd, subaction="compile", file1=str(_TODO_V2))
    assert rc == 0
    data = json.loads(out)
    assert data["sources"] == {}


def test_compile_invalid_template_exits_one(tmp_path: Path) -> None:
    """A template with a missing required field cannot be compiled —
    must fail validation and exit non-zero."""
    bad = _minimal_v2_dict()
    del bad["version"]
    f = _write_yaml(tmp_path / "bad.yaml", bad)
    rc, out = _capture(run_template_cmd, subaction="compile", file1=str(f))
    assert rc == 1
    assert "version" in out.lower() or "error" in out.lower()


def test_compile_v1_input_auto_promotes(tmp_path: Path) -> None:
    """A v1 template on disk is auto-promoted through the loader's
    ``_promote_v1_to_v2`` translation before compile runs."""
    v1 = _minimal_v1_dict()
    f = _write_yaml(tmp_path / "old.yaml", v1)
    rc, out = _capture(run_template_cmd, subaction="compile", file1=str(f))
    assert rc == 0, f"v1 compile after promote should pass; got: {out}"
    data = json.loads(out)
    # promoted_from_v1 schema_version visible
    assert data.get("schema_version") == "2"


def test_compile_missing_file_exits_one(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    rc, _out = _capture(run_template_cmd, subaction="compile", file1=str(missing))
    assert rc != 0


def test_compile_requires_file_arg() -> None:
    rc, _out = _capture(run_template_cmd, subaction="compile", file1=None)
    assert rc != 0


def test_argparse_template_compile_subcommand() -> None:
    """``pocketpaw template compile <file>`` resolves to the right
    dispatch."""
    from pocketpaw.__main__ import _build_parser, _resolve_subargs

    parser = _build_parser()
    args = parser.parse_args(["template", "compile", str(_TODO_V2)])
    _resolve_subargs(args)
    assert args.command == "template"
    assert args.subaction == "compile"
    assert getattr(args, "file1", None) == str(_TODO_V2)


def test_argparse_template_compile_yaml_flag() -> None:
    """The ``--yaml`` top-level flag parses and surfaces via
    ``args.yaml``. Uses ``parse_known_args`` to mirror what the real
    ``main()`` does — this is what allows
    ``pocketpaw template compile --yaml <file>`` to work despite ``--yaml``
    appearing between two positionals."""
    from pocketpaw.__main__ import _build_parser, _resolve_subargs

    parser = _build_parser()
    args, unknown = parser.parse_known_args(["template", "compile", "--yaml", str(_TODO_V2)])
    # Mirror the main()'s unknown-positional folding behaviour
    if unknown:
        args.subargs = list(args.subargs or []) + [a for a in unknown if not a.startswith("-")]
    _resolve_subargs(args)
    assert args.subaction == "compile"
    assert args.file1 == str(_TODO_V2)
    assert getattr(args, "yaml", False) is True


# ===========================================================================
# pocketpaw template publish <file-or-dir>
#
# Wave 4a — content-addressed bundles, Ed25519 signatures, local-only
# (no Registry server). The CLI just dispatches to
# ``pocketpaw.bundled_templates.bundler.pack_template``.
# ===========================================================================


def _bundler_minimal_v2_dict() -> dict:
    """Local copy of the bundler-test fixture — keeps the two test files
    independent (so reordering or skipping one doesn't break the other)."""
    return {
        "schema_version": "2",
        "name": "demo-pocket",
        "version": "1.0.0",
        "pattern": "app",
        "vertical": "productivity",
        "display_name": "Demo Pocket",
        "description": "A minimal template used only by publish/install/upgrade tests.",
        "shape": "data-grid",
        "icon": "list",
        "color": "#7c9c63",
        "state": {
            "entity_type": "Task",
            "columns": [
                {"field": "title", "widget": "text"},
                {"field": "status", "widget": "badge"},
            ],
        },
        "actions": [],
        "connectors": [],
        "skill_refs": [],
    }


def _write_publishable_dir(root: Path, data: dict, slug: str = "demo-pocket") -> Path:
    source = root / slug
    source.mkdir(parents=True, exist_ok=True)
    (source / "template.pocket.yaml").write_text(
        yaml.safe_dump(data, sort_keys=False), encoding="utf-8"
    )
    return source


class TestPublishSubcommand:
    def test_publish_directory_writes_bundle(self, tmp_path: Path) -> None:
        source = _write_publishable_dir(tmp_path, _bundler_minimal_v2_dict())
        rc, out = _capture(
            run_template_cmd,
            subaction="publish",
            file1=str(source),
            output_path=str(tmp_path / "dist"),
        )
        assert rc == 0, out
        bundle = tmp_path / "dist" / "demo-pocket-1.0.0.template.tar.gz"
        assert bundle.exists()
        assert str(bundle) in out

    def test_publish_yaml_file_writes_bundle(self, tmp_path: Path) -> None:
        source = _write_publishable_dir(tmp_path, _bundler_minimal_v2_dict())
        yaml_file = source / "template.pocket.yaml"
        rc, out = _capture(
            run_template_cmd,
            subaction="publish",
            file1=str(yaml_file),
            output_path=str(tmp_path / "dist"),
        )
        assert rc == 0, out
        assert (tmp_path / "dist" / "demo-pocket-1.0.0.template.tar.gz").exists()

    def test_publish_invalid_template_exits_one(self, tmp_path: Path) -> None:
        bad = _bundler_minimal_v2_dict()
        del bad["version"]
        source = _write_publishable_dir(tmp_path, bad)
        rc, _out = _capture(
            run_template_cmd,
            subaction="publish",
            file1=str(source),
            output_path=str(tmp_path / "dist"),
        )
        assert rc == 1

    def test_publish_missing_file_exits_one(self, tmp_path: Path) -> None:
        rc, _out = _capture(
            run_template_cmd,
            subaction="publish",
            file1=str(tmp_path / "nope"),
            output_path=str(tmp_path / "dist"),
        )
        assert rc == 1


# ===========================================================================
# pocketpaw template install <bundle.tar.gz>
# ===========================================================================


class TestInstallSubcommand:
    def _make_bundle(self, tmp_path: Path) -> Path:
        from pocketpaw.bundled_templates.bundler import pack_template

        source = _write_publishable_dir(tmp_path, _bundler_minimal_v2_dict())
        return pack_template(source, output_path=tmp_path / "dist")

    def test_install_unpacks_into_destination(self, tmp_path: Path) -> None:
        bundle = self._make_bundle(tmp_path)
        dest = tmp_path / "installed"
        rc, out = _capture(
            run_template_cmd,
            subaction="install",
            file1=str(bundle),
            destination=str(dest),
        )
        assert rc == 0, out
        assert (dest / "demo-pocket" / "template.pocket.yaml").exists()
        assert (dest / "demo-pocket" / "manifest.json").exists()

    def test_install_rejects_tampered_bundle(self, tmp_path: Path) -> None:
        import tarfile as _tar

        bundle = self._make_bundle(tmp_path)
        tampered_dir = tmp_path / "tampered"
        tampered_dir.mkdir()
        with _tar.open(bundle, "r:gz") as tar:
            tar.extractall(tampered_dir, filter="data")
        (tampered_dir / "template.pocket.yaml").write_text(
            "schema_version: '2'\nname: nope\n", encoding="utf-8"
        )
        tampered_bundle = tmp_path / "tampered.tar.gz"
        with _tar.open(tampered_bundle, "w:gz") as tar:
            for path in sorted(tampered_dir.rglob("*")):
                if path.is_file():
                    tar.add(path, arcname=str(path.relative_to(tampered_dir)))

        rc, _out = _capture(
            run_template_cmd,
            subaction="install",
            file1=str(tampered_bundle),
            destination=str(tmp_path / "installed"),
        )
        assert rc == 1

    def test_install_missing_bundle_exits_one(self, tmp_path: Path) -> None:
        rc, _out = _capture(
            run_template_cmd,
            subaction="install",
            file1=str(tmp_path / "missing.tar.gz"),
            destination=str(tmp_path / "installed"),
        )
        assert rc == 1

    def test_install_unsigned_bundle_with_verify_key_fails_closed(self, tmp_path: Path) -> None:
        """When --verify-key is supplied AND the bundle is unsigned (or the
        signature doesn't match), the install must fail-closed. Explicit
        --verify-key is an assertion that the bundle is signed by that key;
        if we can't satisfy it, refuse to install. Smoke-finding fix for
        pocketpaw#1283."""
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
        )

        bundle = self._make_bundle(tmp_path)  # unsigned
        verify_key_file = tmp_path / "verify.key"
        pub = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
        verify_key_file.write_bytes(pub)

        rc, out = _capture(
            run_template_cmd,
            subaction="install",
            file1=str(bundle),
            destination=str(tmp_path / "installed"),
            verify_key_path=str(verify_key_file),
        )
        assert rc == 1, f"expected fail-closed exit 1, got {rc}; out={out!r}"


class TestArgKeyResolutionRegression:
    """Regression guard for the smoke-finding ``--key`` drop bug.

    ``_resolve_subargs`` used to unconditionally reset ``args.key = None``
    after argparse parsed ``--key <file>``. Effect: ``pocketpaw template
    publish --key ed25519.seed`` silently wrote an UNSIGNED bundle. Fixed
    by removing the unconditional reset; the ``config`` branch still
    sets ``args.key`` from positional subargs when needed.
    """

    def test_template_publish_key_flag_survives_resolve_subargs(self) -> None:
        from pocketpaw.__main__ import _build_parser, _resolve_subargs

        parser = _build_parser()
        args = parser.parse_args(["template", "publish", "/tmp/some.yaml", "--key", "/tmp/key.bin"])
        assert args.key == "/tmp/key.bin"
        _resolve_subargs(args)
        assert args.key == "/tmp/key.bin", (
            f"--key was dropped by _resolve_subargs; got {args.key!r}"
        )

    def test_config_set_positional_key_still_works(self) -> None:
        """Sibling check: the config command's positional key/value path
        must still populate args.key from subargs[1]. This is the case
        the original reset was trying to cover."""
        from pocketpaw.__main__ import _build_parser, _resolve_subargs

        parser = _build_parser()
        args = parser.parse_args(["config", "set", "POCKETPAW_FOO", "bar"])
        _resolve_subargs(args)
        assert args.subaction == "set"
        assert args.key == "POCKETPAW_FOO"
        assert args.value == "bar"


# ===========================================================================
# pocketpaw template upgrade <slug-or-bundle>
# ===========================================================================


class TestUpgradeSubcommand:
    def _install_v1(self, tmp_path: Path) -> Path:
        """Pack and install a v1 template, return the destination root."""
        from pocketpaw.bundled_templates.bundler import (
            pack_template,
            unpack_template,
        )

        source = _write_publishable_dir(tmp_path / "src1", _bundler_minimal_v2_dict())
        bundle = pack_template(source, output_path=tmp_path / "dist1")
        dest = tmp_path / "installed"
        unpack_template(bundle, dest)
        return dest

    def _make_v2_bundle_with_added_outcome(self, tmp_path: Path) -> Path:
        """Pack a non-destructive upgrade (adds an outcome)."""
        from pocketpaw.bundled_templates.bundler import pack_template

        data = _bundler_minimal_v2_dict()
        data["version"] = "1.1.0"
        data["actions"] = [
            {
                "name": "do_it",
                "label": "Do it",
                "kind": "single-row",
                "instinct_policy": "auto",
                "outcomes_emitted": ["finished"],
            }
        ]
        data["outcomes"] = ["finished"]
        source = _write_publishable_dir(tmp_path / "src2", data)
        return pack_template(source, output_path=tmp_path / "dist2")

    def _make_v2_bundle_destructive(self, tmp_path: Path) -> Path:
        """Pack a destructive upgrade — adds an action, then upgrade to a
        version that REMOVES it. We use a two-step setup."""
        from pocketpaw.bundled_templates.bundler import pack_template

        data = _bundler_minimal_v2_dict()
        data["version"] = "2.0.0"
        # Original installed copy will be re-installed first to have an action;
        # this bundle removes it.
        source = _write_publishable_dir(tmp_path / "src3", data)
        return pack_template(source, output_path=tmp_path / "dist3")

    def test_upgrade_non_destructive_applies(self, tmp_path: Path) -> None:
        dest = self._install_v1(tmp_path)
        bundle = self._make_v2_bundle_with_added_outcome(tmp_path)
        rc, out = _capture(
            run_template_cmd,
            subaction="upgrade",
            file1=str(bundle),
            destination=str(dest),
            no_prompt=True,
        )
        assert rc == 0, out
        new_yaml = yaml.safe_load(
            (dest / "demo-pocket" / "template.pocket.yaml").read_text(encoding="utf-8")
        )
        assert new_yaml["version"] == "1.1.0"

    def test_upgrade_destructive_without_prompt_exits_two(self, tmp_path: Path) -> None:
        """An upgrade that removes an action without --no-prompt would
        prompt; in non-interactive mode the contract is exit 2."""
        from pocketpaw.bundled_templates.bundler import pack_template, unpack_template

        # Install a copy WITH an action first
        data_with_action = _bundler_minimal_v2_dict()
        data_with_action["actions"] = [
            {
                "name": "kill_me",
                "label": "Kill me",
                "kind": "single-row",
                "instinct_policy": "auto",
            }
        ]
        source = _write_publishable_dir(tmp_path / "src_a", data_with_action)
        bundle_a = pack_template(source, output_path=tmp_path / "dist_a")
        dest = tmp_path / "installed"
        unpack_template(bundle_a, dest)

        # Now create a NEW bundle that removes the action — destructive
        data_clean = _bundler_minimal_v2_dict()
        data_clean["version"] = "2.0.0"
        source_b = _write_publishable_dir(tmp_path / "src_b", data_clean)
        bundle_b = pack_template(source_b, output_path=tmp_path / "dist_b")

        rc, out = _capture(
            run_template_cmd,
            subaction="upgrade",
            file1=str(bundle_b),
            destination=str(dest),
            no_prompt=True,
        )
        assert rc == 2, f"expected exit 2 on destructive --no-prompt; got {rc}: {out}"
        # Original still in place
        yaml_now = yaml.safe_load(
            (dest / "demo-pocket" / "template.pocket.yaml").read_text(encoding="utf-8")
        )
        assert yaml_now["actions"], "destructive upgrade must not apply under --no-prompt"


def test_argparse_template_publish_subcommand(tmp_path: Path) -> None:
    """``pocketpaw template publish <file>`` resolves through argparse."""
    from pocketpaw.__main__ import _build_parser, _resolve_subargs

    parser = _build_parser()
    args, unknown = parser.parse_known_args(["template", "publish", str(_TODO_V2)])
    if unknown:
        args.subargs = list(args.subargs or []) + [a for a in unknown if not a.startswith("-")]
    _resolve_subargs(args)
    assert args.command == "template"
    assert args.subaction == "publish"
    assert args.file1 == str(_TODO_V2)


def test_argparse_template_install_subcommand(tmp_path: Path) -> None:
    from pocketpaw.__main__ import _build_parser, _resolve_subargs

    parser = _build_parser()
    bundle = "demo-1.0.0.template.tar.gz"
    args, unknown = parser.parse_known_args(["template", "install", bundle])
    if unknown:
        args.subargs = list(args.subargs or []) + [a for a in unknown if not a.startswith("-")]
    _resolve_subargs(args)
    assert args.subaction == "install"
    assert args.file1 == bundle


def test_argparse_template_upgrade_subcommand(tmp_path: Path) -> None:
    from pocketpaw.__main__ import _build_parser, _resolve_subargs

    parser = _build_parser()
    args, unknown = parser.parse_known_args(["template", "upgrade", "demo-pocket"])
    if unknown:
        args.subargs = list(args.subargs or []) + [a for a in unknown if not a.startswith("-")]
    _resolve_subargs(args)
    assert args.subaction == "upgrade"
    assert args.file1 == "demo-pocket"


def test_argparse_template_no_prompt_flag(tmp_path: Path) -> None:
    """The new ``--no-prompt`` flag parses at the top level."""
    from pocketpaw.__main__ import _build_parser, _resolve_subargs

    parser = _build_parser()
    args, unknown = parser.parse_known_args(["template", "upgrade", "--no-prompt", "demo-pocket"])
    if unknown:
        args.subargs = list(args.subargs or []) + [a for a in unknown if not a.startswith("-")]
    _resolve_subargs(args)
    assert args.subaction == "upgrade"
    assert getattr(args, "no_prompt", False) is True


# ===========================================================================
# Wave 4b — pocketpaw template lint Fabric ``tier: registered`` enforcement
# ===========================================================================
#
# After Pydantic validation succeeds, ``_run_lint`` now calls
# ``validate_template_with_registry``. The default registry is
# ``NullFabricRegistry`` — synthetic-tier templates (no dot-paths, no
# joined entities) lint clean; registered-tier templates surface errors,
# which is the correct loud signal that Fabric isn't wired. A
# ``--registry <path>`` flag accepts a JSON file describing entity
# types + links so a developer can lint against a mock Fabric before
# the EE registry ships.
# ===========================================================================


_BUNDLED_DECISION_GRAPH = _BUNDLED_DIR / "decision-graph" / "template.pocket.yaml"


def _lease_registry_json() -> dict:
    """A JSON manifest that satisfies the lease-renewal fixture."""
    return {
        "entity_types": ["Lease", "Tenant", "Property"],
        "links": [
            {"from": "Lease", "to": "Tenant", "name": "lease_tenant"},
            {"from": "Lease", "to": "Property", "name": "lease_property"},
        ],
    }


def test_lint_synthetic_v2_passes_with_null_registry() -> None:
    """``todo-task-tracker`` declares no joins / dot-paths — clean
    against the default NullFabricRegistry."""
    rc, out = _capture(
        run_template_cmd,
        subaction="lint",
        file1=str(_TODO_V2),
        as_json=True,
    )
    assert rc == 0, out
    data = json.loads(out)
    assert data["valid"] is True
    assert data["fabric_validations"] == []


def test_lint_decision_graph_template_passes_with_null_registry() -> None:
    """``decision-graph`` ships ``shape: custom`` with no joins — must
    stay clean against the Null default."""
    rc, out = _capture(
        run_template_cmd,
        subaction="lint",
        file1=str(_BUNDLED_DECISION_GRAPH),
        as_json=True,
    )
    assert rc == 0, out
    data = json.loads(out)
    assert data["valid"] is True
    assert data["fabric_validations"] == []


def test_lint_registered_tier_template_fails_with_null_registry() -> None:
    """The lease-renewal fixture declares ``joined_entities`` — Null
    can't satisfy that, so lint must exit 1 with Fabric errors."""
    rc, out = _capture(
        run_template_cmd,
        subaction="lint",
        file1=str(_LEASE_V2),
        as_json=True,
    )
    assert rc == 1, out
    data = json.loads(out)
    assert data["valid"] is False
    fab = data["fabric_validations"]
    assert isinstance(fab, list)
    assert len(fab) > 0
    # Each entry exposes the documented contract surface.
    for entry in fab:
        assert set(entry) >= {"severity", "message", "path", "data"}
        assert entry["severity"] == "error"


def test_lint_registered_tier_template_passes_with_json_registry(tmp_path: Path) -> None:
    """The same lease fixture, against a JSON-registry mock that knows
    the entity types + via_links, lints clean."""
    reg_path = tmp_path / "fabric.json"
    reg_path.write_text(json.dumps(_lease_registry_json()), encoding="utf-8")
    rc, out = _capture(
        run_template_cmd,
        subaction="lint",
        file1=str(_LEASE_V2),
        registry_path=str(reg_path),
        as_json=True,
    )
    assert rc == 0, out
    data = json.loads(out)
    assert data["valid"] is True
    assert data["fabric_validations"] == []


def test_lint_human_output_surfaces_fabric_errors() -> None:
    """Human-readable lint must render Fabric errors when they fire —
    the JSON path is for scripting; the default path needs operator-
    legible failure lines."""
    rc, out = _capture(
        run_template_cmd,
        subaction="lint",
        file1=str(_LEASE_V2),
    )
    assert rc == 1, out
    lower = out.lower()
    assert "fabric" in lower or "via_link" in lower or "registered" in lower


def test_lint_missing_registry_file_exits_one(tmp_path: Path) -> None:
    """``--registry <path>`` where the path doesn't exist surfaces a
    clean lint failure (exit 1) rather than crashing."""
    rc, _out = _capture(
        run_template_cmd,
        subaction="lint",
        file1=str(_TODO_V2),
        registry_path=str(tmp_path / "missing.json"),
    )
    assert rc == 1


def test_lint_malformed_registry_file_exits_one(tmp_path: Path) -> None:
    """A registry file with malformed JSON also surfaces as a lint
    failure rather than a crash."""
    bad = tmp_path / "bad.json"
    bad.write_text("{ this is :: not json", encoding="utf-8")
    rc, out = _capture(
        run_template_cmd,
        subaction="lint",
        file1=str(_TODO_V2),
        registry_path=str(bad),
        as_json=True,
    )
    assert rc == 1
    data = json.loads(out)
    # The error surfaces in the top-level errors list — registry-load
    # failures are not Fabric-validation failures (different lifecycle).
    assert data["valid"] is False
    assert any("registry" in e.lower() or "json" in e.lower() for e in data["errors"])


def test_argparse_template_lint_registry_flag(tmp_path: Path) -> None:
    """``--registry <path>`` parses through argparse alongside the
    template subcommand."""
    from pocketpaw.__main__ import _build_parser, _resolve_subargs

    parser = _build_parser()
    reg = tmp_path / "fabric.json"
    args, unknown = parser.parse_known_args(
        ["template", "lint", str(_LEASE_V2), "--registry", str(reg)]
    )
    if unknown:
        args.subargs = list(args.subargs or []) + [a for a in unknown if not a.startswith("-")]
    _resolve_subargs(args)
    assert args.subaction == "lint"
    assert args.file1 == str(_LEASE_V2)
    assert getattr(args, "registry", None) == str(reg)
