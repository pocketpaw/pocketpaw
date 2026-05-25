# src/pocketpaw/cli/template.py
# Created: 2026-05-25 (feat/rfc-03-v2-cli) — ``pocketpaw template``
# subcommand. Implements the three "author-facing" RFC 03 v2 commands:
#   * ``lint``    — validate a template against the Pydantic chokepoint;
#                   auto-promote v1 input and surface heuristic warnings.
#   * ``migrate`` — rewrite v1 to v2 on disk with a .v1.bak backup and a
#                   y/N confirmation prompt (idempotent on v2 input).
#   * ``diff``    — semantic dict-walked diff between two templates,
#                   internally promoted to v2 so v1 / v2 compare cleanly.
# Modified 2026-05-25 (feat/rfc-03-v2-compile): added ``compile <file>``
# subaction. Prints the runtime-shaped rippleSpec dict that
# ``compile_template`` produces (JSON by default, YAML under --yaml).
# Author-facing inspection only — installation is NOT part of this
# path (that's Bucket C / Registry).
# Publish / install / upgrade are intentionally NOT here — they belong
# to the registry PR (Bucket C). Fabric tier:registered + via_link
# enforcement in lint is deferred to the Fabric integration PR (PR 2g).
"""``pocketpaw template`` — author-side template tooling.

Four sub-subcommands: ``lint``, ``migrate``, ``diff``, ``compile``.
Dispatched from the top-level argparse parser via
``__main__._handle_early_command`` so the template subcommand never
pays the agent / settings boot cost.

Imports of ``pocketpaw.bundled_templates`` are lazy — invoking ``--help``
or any of the unrelated CLI commands must not pull Pydantic, YAML, or
the schema module into memory.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from pocketpaw.cli.utils import output_json, print_fail, print_ok, print_warn

# ---------------------------------------------------------------------------
# Heuristic ``pattern × shape`` matrix — used only by lint for soft
# warnings. "Unusual" means we don't know of a sane pocket that uses
# the pair; flag it but don't fail the lint. Anything not in this map
# is treated as plausible.
# ---------------------------------------------------------------------------

_UNUSUAL_PATTERN_SHAPE_PAIRS: set[tuple[str, str]] = {
    # dashboards are read-mostly summary surfaces; a kanban implies
    # writable state-per-row, which makes a dashboard kanban an odd
    # combination — flag for author review.
    ("dashboard", "kanban"),
    # feeds are vertically scrolling event streams; a data-grid is a
    # columnar table. The two cohabit awkwardly — flag it.
    ("feed", "data-grid"),
    # wizards are single-step linear flows; a network / treemap shape
    # has no concept of "next step", so the pairing is suspect.
    ("wizard", "network"),
    ("wizard", "treemap"),
}


# ---------------------------------------------------------------------------
# Format detection + I/O
# ---------------------------------------------------------------------------


def _detect_format(path: Path) -> str:
    """Return ``"json"`` for ``.json`` files, ``"yaml"`` otherwise.

    The RFC's canonical format is YAML; JSON is accepted as input but
    everything else (no extension, ``.yml``, ``.yaml``) routes through
    the YAML parser.
    """
    return "json" if path.suffix.lower() == ".json" else "yaml"


def _parse_file(path: Path) -> dict[str, Any]:
    """Read + parse one template file. Raises ``ValueError`` on failure."""
    if not path.is_file():
        raise ValueError(f"file not found: {path}")
    raw = path.read_text(encoding="utf-8")
    fmt = _detect_format(path)
    try:
        if fmt == "json":
            data = json.loads(raw)
        else:
            import yaml  # noqa: PLC0415 — lazy

            data = yaml.safe_load(raw)
    except Exception as exc:
        raise ValueError(f"failed to parse {path} as {fmt}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at top level, got {type(data).__name__}")
    return data


def _write_file(path: Path, data: dict[str, Any]) -> None:
    """Write a template dict back to disk, preserving the input format."""
    fmt = _detect_format(path)
    if fmt == "json":
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        return
    import yaml  # noqa: PLC0415 — lazy

    path.write_text(
        yaml.safe_dump(data, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def _is_v1(meta: dict[str, Any]) -> bool:
    """True when ``meta`` lacks a ``schema_version`` or declares ``"1"``."""
    sv = meta.get("schema_version")
    return sv is None or str(sv) == "1"


# ---------------------------------------------------------------------------
# Top-level entry point — wired from ``__main__._handle_early_command``.
# ---------------------------------------------------------------------------


def run_template_cmd(
    subaction: str | None = None,
    file1: str | None = None,
    file2: str | None = None,
    as_json: bool = False,
    yes: bool = False,
    no_backup: bool = False,
    as_yaml: bool = False,
) -> int:
    """Dispatch ``pocketpaw template <subaction>`` to the right handler.

    Returns an exit code: 0 on success, 1 on validation / I/O failure,
    2 on usage error (unknown subaction, missing required positional).
    """
    if subaction not in {"lint", "migrate", "diff", "compile"}:
        msg = f"unknown subcommand {subaction!r}. Expected one of: lint, migrate, diff, compile."
        if as_json:
            output_json({"error": msg})
        else:
            print_fail(msg)
        return 2

    if subaction == "lint":
        if not file1:
            _usage_error("pocketpaw template lint <file>", as_json)
            return 2
        return _run_lint(Path(file1), as_json=as_json)

    if subaction == "migrate":
        if not file1:
            _usage_error("pocketpaw template migrate <file>", as_json)
            return 2
        return _run_migrate(
            Path(file1),
            as_json=as_json,
            yes=yes,
            no_backup=no_backup,
        )

    if subaction == "compile":
        if not file1:
            _usage_error("pocketpaw template compile <file> [--yaml]", as_json)
            return 2
        return _run_compile(Path(file1), as_yaml=as_yaml)

    # subaction == "diff"
    if not file1 or not file2:
        _usage_error("pocketpaw template diff <file1> <file2>", as_json)
        return 2
    return _run_diff(Path(file1), Path(file2), as_json=as_json)


def _usage_error(usage: str, as_json: bool) -> None:
    if as_json:
        output_json({"error": f"usage: {usage}"})
    else:
        print_fail(f"usage: {usage}")


# ---------------------------------------------------------------------------
# Subcommand: lint
# ---------------------------------------------------------------------------


def _run_lint(path: Path, *, as_json: bool) -> int:
    """Validate one template file.

    Steps:
      1. Read + parse (YAML or JSON, by extension).
      2. If v1, run the loader's private ``_promote_v1_to_v2`` and remember
         that a rewrite would apply.
      3. Try ``PocketTemplate.model_validate(merged)``.
      4. Compute heuristic warnings (pattern × shape only — Fabric
         ``via_link`` registry enforcement is out of scope).
      5. Print human-readable or JSON output.
    """

    # Phase 1 — read + parse
    try:
        meta = _parse_file(path)
    except ValueError as exc:
        return _lint_fail(path, [str(exc)], [], False, as_json)

    promoted_from_v1 = _is_v1(meta)

    # Lazy imports keep the chokepoint off the hot path.
    from pocketpaw.bundled_templates.loader import _promote_v1_to_v2  # noqa: PLC0415

    merged = _promote_v1_to_v2(meta) if promoted_from_v1 else meta

    # Phase 3 — Pydantic validation
    try:
        from pydantic import ValidationError  # noqa: PLC0415

        from pocketpaw.bundled_templates.schema import PocketTemplate  # noqa: PLC0415

        PocketTemplate.model_validate(merged)
    except ValidationError as exc:
        errors = _format_pydantic_errors(exc)
        return _lint_fail(path, errors, [], promoted_from_v1, as_json)
    except Exception as exc:  # noqa: BLE001 — defensive
        return _lint_fail(path, [f"unexpected error: {exc}"], [], promoted_from_v1, as_json)

    # Phase 4 — heuristic warnings (Pydantic already passed)
    warnings = _compute_warnings(merged)

    # Phase 5 — output
    name = merged.get("name", "<unknown>")
    if as_json:
        output_json(
            {
                "file": str(path),
                "valid": True,
                "errors": [],
                "warnings": warnings,
                "schema_version": "v2",
                "promoted_from_v1": promoted_from_v1,
            }
        )
    else:
        print_ok(f"{path} is valid (schema_version=v2, name={name})")
        if promoted_from_v1:
            print(
                "  Note: input was v1; the runtime loader would auto-promote "
                "it to v2 on read. Run `pocketpaw template migrate` to apply "
                "the rewrite on disk."
            )
        for w in warnings:
            print_warn(w)
    return 0


def _lint_fail(
    path: Path,
    errors: list[str],
    warnings: list[str],
    promoted_from_v1: bool,
    as_json: bool,
) -> int:
    if as_json:
        output_json(
            {
                "file": str(path),
                "valid": False,
                "errors": errors,
                "warnings": warnings,
                "schema_version": "v2",
                "promoted_from_v1": promoted_from_v1,
            }
        )
        return 1
    print_fail(f"{path} failed validation:")
    for e in errors:
        print(f"    - {e}")
    for w in warnings:
        print_warn(w)
    return 1


def _format_pydantic_errors(exc: Any) -> list[str]:
    """Render a Pydantic ``ValidationError`` as a list of one-line
    ``"at <path>: <message>"`` strings."""
    out: list[str] = []
    for err in exc.errors():
        loc_parts = [str(p) for p in err.get("loc", ())]
        loc = ".".join(loc_parts) if loc_parts else "<root>"
        msg = err.get("msg", "validation error")
        out.append(f"at {loc!r}: {msg}")
    return out


def _compute_warnings(meta: dict[str, Any]) -> list[str]:
    """Emit heuristic warnings — non-fatal flags for the author."""
    warnings: list[str] = []
    pattern = meta.get("pattern")
    shape = meta.get("shape")
    if isinstance(pattern, str) and isinstance(shape, str):
        if (pattern, shape) in _UNUSUAL_PATTERN_SHAPE_PAIRS:
            warnings.append(
                f"unusual pattern x shape pairing: pattern={pattern!r} with "
                f"shape={shape!r} (heuristic — not enforced; double-check intent)"
            )
    return warnings


# ---------------------------------------------------------------------------
# Subcommand: migrate
# ---------------------------------------------------------------------------


def _run_migrate(
    path: Path,
    *,
    as_json: bool,
    yes: bool,
    no_backup: bool,
) -> int:
    """Rewrite a v1 template to v2 on disk. Idempotent on v2 input.

    Steps:
      1. Read + parse.
      2. If already v2, print a noop message and exit 0.
      3. Confirm with the user (unless --yes).
      4. Back up the original to ``<file>.v1.bak`` (unless --no-backup).
      5. Promote in memory and write back, preserving JSON vs YAML.
    """

    try:
        meta = _parse_file(path)
    except ValueError as exc:
        if as_json:
            output_json({"file": str(path), "migrated": False, "error": str(exc)})
        else:
            print_fail(str(exc))
        return 1

    if not _is_v1(meta):
        if as_json:
            output_json(
                {
                    "file": str(path),
                    "migrated": False,
                    "was_already_v2": True,
                    "backup_path": None,
                }
            )
        else:
            print_ok(f"{path} is already v2 — no changes")
        return 0

    # Confirm
    if not yes:
        sys.stdout.write(f"Migrate {path} from v1 to v2? [y/N] ")
        sys.stdout.flush()
        try:
            reply = input("")
        except EOFError:
            reply = ""
        if reply.strip().lower() not in {"y", "yes"}:
            if as_json:
                output_json(
                    {
                        "file": str(path),
                        "migrated": False,
                        "was_already_v2": False,
                        "backup_path": None,
                        "aborted": True,
                    }
                )
            else:
                print("  Aborted — no changes written.")
            return 0

    # Backup
    backup_path: Path | None = None
    if not no_backup:
        backup_path = path.with_suffix(path.suffix + ".v1.bak")
        try:
            shutil.copy2(path, backup_path)
        except OSError as exc:
            msg = f"failed to write backup at {backup_path}: {exc}"
            if as_json:
                output_json({"file": str(path), "migrated": False, "error": msg})
            else:
                print_fail(msg)
            return 1

    # Promote + write
    from pocketpaw.bundled_templates.loader import _promote_v1_to_v2  # noqa: PLC0415

    promoted = _promote_v1_to_v2(meta)
    try:
        _write_file(path, promoted)
    except Exception as exc:  # noqa: BLE001
        msg = f"failed to write {path}: {exc}"
        if as_json:
            output_json({"file": str(path), "migrated": False, "error": msg})
        else:
            print_fail(msg)
        return 1

    if as_json:
        output_json(
            {
                "file": str(path),
                "migrated": True,
                "was_already_v2": False,
                "backup_path": str(backup_path) if backup_path else None,
            }
        )
    else:
        print_ok(f"migrated {path} from v1 to v2")
        if backup_path:
            print(f"  backup: {backup_path}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: diff
# ---------------------------------------------------------------------------


def _run_diff(file1: Path, file2: Path, *, as_json: bool) -> int:
    """Semantic diff between two templates.

    Both inputs are promoted to v2 before comparison so v1 vs v2 files
    compare cleanly. The output is grouped by top-level field; within a
    group, each line uses one of:

      ``+ <path>: <value>``         (added in file2)
      ``- <path>: <value>``         (removed from file1)
      ``~ <path>: <old> -> <new>``  (value changed)
    """

    try:
        a_raw = _parse_file(file1)
        b_raw = _parse_file(file2)
    except ValueError as exc:
        if as_json:
            output_json({"error": str(exc)})
        else:
            print_fail(str(exc))
        return 1

    from pocketpaw.bundled_templates.loader import _promote_v1_to_v2  # noqa: PLC0415

    a = _promote_v1_to_v2(a_raw) if _is_v1(a_raw) else a_raw
    b = _promote_v1_to_v2(b_raw) if _is_v1(b_raw) else b_raw

    entries: list[dict[str, Any]] = []
    _walk_diff("", a, b, entries)

    if as_json:
        output_json(entries)
        return 0

    if not entries:
        print_ok(f"no semantic differences between {file1} and {file2}")
        return 0

    # Group by top-level field (the segment before the first ``.`` or ``[``)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for entry in entries:
        top = _top_level_key(entry["path"])
        grouped.setdefault(top, []).append(entry)

    print(f"  diff: {file1} -> {file2}")
    for top in sorted(grouped):
        print(f"\n  [{top}]")
        for entry in grouped[top]:
            sym = {"add": "+ ", "remove": "- ", "change": "~ "}[entry["op"]]
            if entry["op"] == "add":
                print(f"  {sym}{entry['path']}: {_short(entry['new'])}")
            elif entry["op"] == "remove":
                print(f"  {sym}{entry['path']}: {_short(entry['old'])}")
            else:
                print(f"  {sym}{entry['path']}: {_short(entry['old'])} -> {_short(entry['new'])}")
    return 0


def _walk_diff(
    prefix: str,
    a: Any,
    b: Any,
    out: list[dict[str, Any]],
) -> None:
    """Recurse over two parsed templates and append diff entries.

    Lists are diffed positionally. Dicts are diffed by key. Scalars are
    diffed by equality.
    """
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a) | set(b)
        for key in sorted(keys):
            sub_prefix = f"{prefix}.{key}" if prefix else key
            if key not in a:
                out.append({"op": "add", "path": sub_prefix, "new": b[key]})
            elif key not in b:
                out.append({"op": "remove", "path": sub_prefix, "old": a[key]})
            else:
                _walk_diff(sub_prefix, a[key], b[key], out)
        return

    if isinstance(a, list) and isinstance(b, list):
        max_len = max(len(a), len(b))
        for idx in range(max_len):
            sub_prefix = f"{prefix}[{idx}]"
            if idx >= len(a):
                out.append({"op": "add", "path": sub_prefix, "new": b[idx]})
            elif idx >= len(b):
                out.append({"op": "remove", "path": sub_prefix, "old": a[idx]})
            else:
                _walk_diff(sub_prefix, a[idx], b[idx], out)
        return

    if a != b:
        out.append({"op": "change", "path": prefix, "old": a, "new": b})


def _top_level_key(path: str) -> str:
    """Return the path segment before the first ``.`` or ``[``."""
    if not path:
        return "<root>"
    for i, ch in enumerate(path):
        if ch in (".", "["):
            return path[:i]
    return path


def _short(value: Any, limit: int = 60) -> str:
    """Render a scalar / collection compactly for the diff output."""
    try:
        s = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        s = str(value)
    if len(s) > limit:
        return s[: limit - 3] + "..."
    return s


# ---------------------------------------------------------------------------
# Subcommand: compile
# ---------------------------------------------------------------------------


def _run_compile(path: Path, *, as_yaml: bool) -> int:
    """Print the runtime-shaped rippleSpec dict the compile pass produces.

    Author-facing inspection only — this command does NOT install or
    persist anything. The output is what the runtime executors would
    consume if this template were instantiated (RFC 04 sources block,
    plus the passthrough fields PRs 2c-2g will translate).

    Steps:
      1. Read + parse the input (YAML or JSON, by extension).
      2. Auto-promote v1 input via the loader's ``_promote_v1_to_v2``.
      3. Validate through the Pydantic chokepoint.
      4. Compile via ``compile_template``.
      5. Emit the result as JSON (default) or YAML (--yaml).
    """

    # Phase 1 — read + parse
    try:
        meta = _parse_file(path)
    except ValueError as exc:
        print_fail(str(exc))
        return 1

    # Phase 2 — v1 -> v2 promotion (idempotent on v2 input)
    from pocketpaw.bundled_templates.loader import _promote_v1_to_v2  # noqa: PLC0415

    merged = _promote_v1_to_v2(meta) if _is_v1(meta) else meta

    # Phase 3 — Pydantic validation
    try:
        from pydantic import ValidationError  # noqa: PLC0415

        from pocketpaw.bundled_templates.schema import PocketTemplate  # noqa: PLC0415

        template = PocketTemplate.model_validate(merged)
    except ValidationError as exc:
        errors = _format_pydantic_errors(exc)
        print_fail(f"{path} failed validation:")
        for e in errors:
            print(f"    - {e}")
        return 1
    except Exception as exc:  # noqa: BLE001 — defensive
        print_fail(f"unexpected error validating {path}: {exc}")
        return 1

    # Phase 4 — compile
    from pocketpaw.bundled_templates.compile import compile_template  # noqa: PLC0415

    try:
        spec = compile_template(template)
    except Exception as exc:  # noqa: BLE001 — surface compile failures cleanly
        print_fail(f"failed to compile {path}: {exc}")
        return 1

    # Phase 5 — emit
    if as_yaml:
        import yaml  # noqa: PLC0415

        sys.stdout.write(yaml.safe_dump(spec, sort_keys=False, default_flow_style=False))
    else:
        sys.stdout.write(json.dumps(spec, indent=2, default=str) + "\n")
    return 0


__all__ = ["run_template_cmd"]
