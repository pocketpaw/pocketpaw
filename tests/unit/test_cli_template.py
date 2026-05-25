# tests/unit/test_cli_template.py
# Created: 2026-05-25 (feat/rfc-03-v2-cli) — RED-first tests for the
# new ``pocketpaw template`` CLI subcommand (lint / migrate / diff).
# Modified 2026-05-25 (feat/rfc-03-v2-compile): added tests for the
# new ``compile`` subaction — JSON (default) + YAML (--yaml) output,
# end-to-end through the dispatch path, plus argparse wiring sanity.
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


def test_lint_clean_v2_fixture_exits_zero_and_reports_schema_version() -> None:
    rc, out = _capture(run_template_cmd, subaction="lint", file1=str(_LEASE_V2))
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
