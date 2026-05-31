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
# Modified 2026-05-28 (feat/wave-4a-cli-registry): added the Wave 4a
# Registry subactions:
#   * ``publish``  — pack a template directory / YAML into a signed,
#                    content-addressed ``<slug>-<version>.template.tar.gz``.
#                    No Registry transport in v0 — bundles are local files.
#   * ``install``  — unpack a bundle, verify hash + (optional) signature,
#                    materialize into ``~/.pocketpaw/templates/<slug>/``.
#   * ``upgrade``  — diff an installed template against a new bundle
#                    (or installed-slug pair), prompt for destructive
#                    changes, apply non-destructive updates silently.
# Modified 2026-05-28 (feat/wave-4b-lint-fabric): ``lint`` now also
# calls ``validate_template_with_registry`` after Pydantic validation
# passes. Default registry is :class:`NullFabricRegistry` (synthetic-
# tier templates lint clean; registered-tier templates surface errors —
# the correct loud signal that Fabric isn't wired). New
# ``registry_path`` parameter (CLI ``--registry <path>``) loads a JSON-
# backed mock via :class:`JSONFileFabricRegistry` so developers can
# lint registered-tier templates without standing up the EE
# FabricRegistry. ``--json`` output gains a ``fabric_validations``
# array; warnings keep exit 0, any severity=error fails with exit 1.
"""``pocketpaw template`` — author-side template tooling.

Seven sub-subcommands: ``lint``, ``migrate``, ``diff``, ``compile``,
``publish``, ``install``, ``upgrade``. Dispatched from the top-level
argparse parser via ``__main__._handle_early_command`` so the template
subcommand never pays the agent / settings boot cost.

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
    output_path: str | None = None,
    key_path: str | None = None,
    unsigned: bool = False,
    destination: str | None = None,
    verify_key_path: str | None = None,
    no_prompt: bool = False,
    registry_path: str | None = None,
) -> int:
    """Dispatch ``pocketpaw template <subaction>`` to the right handler.

    Returns an exit code: 0 on success, 1 on validation / I/O failure,
    2 on usage error (unknown subaction, missing required positional) or
    on a destructive upgrade attempted under ``--no-prompt``.
    """
    valid = {"lint", "migrate", "diff", "compile", "publish", "install", "upgrade"}
    if subaction not in valid:
        msg = f"unknown subcommand {subaction!r}. Expected one of: {', '.join(sorted(valid))}."
        if as_json:
            output_json({"error": msg})
        else:
            print_fail(msg)
        return 2

    if subaction == "lint":
        if not file1:
            _usage_error("pocketpaw template lint <file> [--registry FILE]", as_json)
            return 2
        return _run_lint(
            Path(file1),
            as_json=as_json,
            registry_path=Path(registry_path) if registry_path else None,
        )

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

    if subaction == "publish":
        if not file1:
            _usage_error(
                "pocketpaw template publish <file-or-dir> [--output DIR] [--key FILE | --unsigned]",
                as_json,
            )
            return 2
        return _run_publish(
            Path(file1),
            output_path=Path(output_path) if output_path else None,
            key_path=Path(key_path) if key_path else None,
            unsigned=unsigned,
            as_json=as_json,
        )

    if subaction == "install":
        if not file1:
            _usage_error(
                "pocketpaw template install <bundle.tar.gz> [--dest DIR] [--verify-key FILE]",
                as_json,
            )
            return 2
        return _run_install(
            Path(file1),
            destination=Path(destination) if destination else None,
            verify_key_path=Path(verify_key_path) if verify_key_path else None,
            as_json=as_json,
        )

    if subaction == "upgrade":
        if not file1:
            _usage_error(
                "pocketpaw template upgrade <slug-or-bundle> [--dest DIR] [--no-prompt]",
                as_json,
            )
            return 2
        return _run_upgrade(
            file1,
            destination=Path(destination) if destination else None,
            no_prompt=no_prompt,
            as_json=as_json,
        )

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


def _run_lint(
    path: Path,
    *,
    as_json: bool,
    registry_path: Path | None = None,
) -> int:
    """Validate one template file.

    Steps:
      1. Read + parse (YAML or JSON, by extension).
      2. If v1, run the loader's private ``_promote_v1_to_v2`` and remember
         that a rewrite would apply.
      3. Try ``PocketTemplate.model_validate(merged)``.
      4. Load the Fabric registry — :class:`NullFabricRegistry` by
         default, :class:`JSONFileFabricRegistry` when ``registry_path``
         is supplied — and call ``validate_template_with_registry``.
      5. Compute heuristic warnings (pattern × shape pairings).
      6. Print human-readable or JSON output. Exit code: 0 when every
         Fabric finding has severity=warning (and Pydantic passed); 1
         on any severity=error finding.
    """

    # Phase 1 — read + parse
    try:
        meta = _parse_file(path)
    except ValueError as exc:
        return _lint_fail(path, [str(exc)], [], [], False, as_json)

    promoted_from_v1 = _is_v1(meta)

    # Lazy imports keep the chokepoint off the hot path.
    from pocketpaw.bundled_templates.loader import _promote_v1_to_v2  # noqa: PLC0415

    merged = _promote_v1_to_v2(meta) if promoted_from_v1 else meta

    # Phase 3 — Pydantic validation
    try:
        from pydantic import ValidationError  # noqa: PLC0415

        from pocketpaw.bundled_templates.schema import PocketTemplate  # noqa: PLC0415

        template = PocketTemplate.model_validate(merged)
    except ValidationError as exc:
        errors = _format_pydantic_errors(exc)
        return _lint_fail(path, errors, [], [], promoted_from_v1, as_json)
    except Exception as exc:  # noqa: BLE001 — defensive
        return _lint_fail(path, [f"unexpected error: {exc}"], [], [], promoted_from_v1, as_json)

    # Phase 4 — Fabric tier:registered validation
    from pocketpaw.bundled_templates.fabric_registry import (  # noqa: PLC0415
        NullFabricRegistry,
    )

    registry: Any
    if registry_path is None:
        registry = NullFabricRegistry()
    else:
        from pocketpaw.bundled_templates.json_registry import (  # noqa: PLC0415
            JSONFileFabricRegistry,
            JSONFileFabricRegistryError,
        )

        try:
            registry = JSONFileFabricRegistry(registry_path)
        except JSONFileFabricRegistryError as exc:
            return _lint_fail(path, [str(exc)], [], [], promoted_from_v1, as_json)

    from pocketpaw.bundled_templates.fabric_validator import (  # noqa: PLC0415
        validate_template_with_registry,
    )

    fabric_findings = validate_template_with_registry(template, registry)
    fabric_payload = [
        {
            "severity": f.severity,
            "message": f.message,
            "path": f.path,
            "data": dict(f.data),
        }
        for f in fabric_findings
    ]
    fabric_errors = [f for f in fabric_findings if f.severity == "error"]
    fabric_warnings = [f for f in fabric_findings if f.severity == "warning"]

    # Phase 5 — heuristic warnings (Pydantic already passed)
    warnings = _compute_warnings(merged)

    # Phase 6 — output. Exit 1 only when Fabric surfaces a severity=error
    # finding; warnings stay informational.
    if fabric_errors:
        return _lint_fail(
            path,
            [_format_fabric_error(f) for f in fabric_errors],
            warnings,
            fabric_payload,
            promoted_from_v1,
            as_json,
        )

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
                "fabric_validations": fabric_payload,
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
        for f in fabric_warnings:
            print_warn(f"fabric: {_format_fabric_error(f)}")
    return 0


def _lint_fail(
    path: Path,
    errors: list[str],
    warnings: list[str],
    fabric_payload: list[dict[str, Any]],
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
                "fabric_validations": fabric_payload,
            }
        )
        return 1
    print_fail(f"{path} failed validation:")
    for e in errors:
        print(f"    - {e}")
    for w in warnings:
        print_warn(w)
    return 1


def _format_fabric_error(finding: Any) -> str:
    """Render a :class:`FabricValidationError` as a one-line lint string
    matching the Pydantic-error format (``"at <path>: <message>"``)."""
    return f"at {finding.path!r}: {finding.message}"


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


# ---------------------------------------------------------------------------
# Subcommand: publish
# ---------------------------------------------------------------------------


def _default_install_root() -> Path:
    """The default per-user templates dir (``~/.pocketpaw/templates``)."""

    return Path.home() / ".pocketpaw" / "templates"


def _read_key_bytes(path: Path, *, label: str) -> bytes:
    """Read a key file. Accepts raw 32 bytes or 64 hex chars."""
    data = path.read_bytes().strip()
    return data


def _run_publish(
    source: Path,
    *,
    output_path: Path | None,
    key_path: Path | None,
    unsigned: bool,
    as_json: bool,
) -> int:
    """Pack a template into a content-addressed, optionally-signed tarball.

    Wave 4a ships no Registry transport — the bundle is written to the
    local filesystem. Operators ship the resulting ``.template.tar.gz``
    however they like; ``pocketpaw template install`` reads it back.
    """

    if key_path and unsigned:
        msg = "--key and --unsigned are mutually exclusive"
        if as_json:
            output_json({"error": msg})
        else:
            print_fail(msg)
        return 2

    signing_key: bytes | None = None
    if key_path is not None:
        try:
            signing_key = _read_key_bytes(key_path, label="signing key")
        except OSError as exc:
            msg = f"failed to read signing key {key_path}: {exc}"
            if as_json:
                output_json({"error": msg})
            else:
                print_fail(msg)
            return 1

    from pocketpaw.bundled_templates.bundler import (  # noqa: PLC0415
        BundleError,
        pack_template,
    )

    try:
        bundle = pack_template(
            source,
            output_path=output_path,
            signing_key=signing_key,
        )
    except BundleError as exc:
        if as_json:
            output_json({"error": str(exc)})
        else:
            print_fail(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001 — surface unexpected errors cleanly
        if as_json:
            output_json({"error": f"unexpected error: {exc}"})
        else:
            print_fail(f"unexpected error: {exc}")
        return 1

    if signing_key is None:
        if not as_json:
            print_warn(
                "bundle is unsigned — consumers will install on hash trust only. "
                "Pass --key <file> to sign with an Ed25519 private key."
            )

    if as_json:
        output_json(
            {
                "bundle": str(bundle),
                "signed": signing_key is not None,
            }
        )
    else:
        print_ok(f"wrote {bundle}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: install
# ---------------------------------------------------------------------------


def _run_install(
    bundle_path: Path,
    *,
    destination: Path | None,
    verify_key_path: Path | None,
    as_json: bool,
) -> int:
    """Unpack + verify a bundle, materialize into ``destination/<slug>/``."""

    if destination is None:
        destination = _default_install_root()

    verify_key: bytes | None = None
    if verify_key_path is not None:
        try:
            verify_key = _read_key_bytes(verify_key_path, label="verify key")
        except OSError as exc:
            msg = f"failed to read verify key {verify_key_path}: {exc}"
            if as_json:
                output_json({"error": msg})
            else:
                print_fail(msg)
            return 1

    from pocketpaw.bundled_templates.bundler import (  # noqa: PLC0415
        BundleError,
        unpack_template,
    )

    try:
        result = unpack_template(bundle_path, destination, verify_key=verify_key)
    except BundleError as exc:
        if as_json:
            output_json({"error": str(exc)})
        else:
            print_fail(str(exc))
        return 1
    except Exception as exc:  # noqa: BLE001
        if as_json:
            output_json({"error": f"unexpected error: {exc}"})
        else:
            print_fail(f"unexpected error: {exc}")
        return 1

    if as_json:
        output_json(
            {
                "slug": result.slug,
                "version": result.version,
                "destination": str(result.destination),
                "hash_verified": result.hash_verified,
                "signature_verified": result.signature_verified,
            }
        )
    else:
        print_ok(f"installed {result.slug}@{result.version} -> {result.destination}")
        if result.signature_verified is True:
            print("  signature: verified")
        elif result.signature_verified is False:
            print_warn("bundle is unsigned or signature did not match the supplied verify key")
        # signature_verified is None -> no verify key supplied; stay quiet

    # Fail-closed when the operator explicitly supplied --verify-key but the
    # bundle either lacks a signature or carries one that doesn't match the
    # key. An explicit verify-key is an assertion ("this bundle should be
    # signed by THIS key") — if we can't satisfy it, the install should not
    # silently succeed. Without --verify-key, signature_verified is None
    # and we stay exit 0 (hash-trust install).
    if verify_key is not None and result.signature_verified is False:
        return 1
    return 0


# ---------------------------------------------------------------------------
# Subcommand: upgrade
# ---------------------------------------------------------------------------


def _run_upgrade(
    target: str,
    *,
    destination: Path | None,
    no_prompt: bool,
    as_json: bool,
) -> int:
    """Diff an installed template against a new copy, apply or prompt.

    ``target`` is either:
      - A bundle path (ends with ``.tar.gz``) — diff against the
        installed slug recorded in the bundle's manifest.
      - A bare slug (e.g. ``demo-pocket``) — looks up
        ``<destination>/<slug>/`` and re-reads the template. Useful for
        comparing after manual edits.

    Destructive changes (removed action / outcome, changed instinct
    policy) prompt unless ``--no-prompt`` is set. Under ``--no-prompt``
    a destructive upgrade fails with exit code 2 so CI scripts can
    detect the case without hanging.
    """

    if destination is None:
        destination = _default_install_root()

    target_path = Path(target)
    is_bundle = target_path.suffix == ".gz" or target_path.suffix == ".tgz"

    from pocketpaw.bundled_templates.bundler import (  # noqa: PLC0415
        BundleError,
        compute_template_diff,
        unpack_template,
    )

    new_yaml: dict[str, Any]
    slug: str

    if is_bundle:
        # Inspect the bundle without permanently installing — unpack to
        # a sibling staging dir, read its YAML, then either commit or
        # roll back.
        if not target_path.is_file():
            _usage_error(f"bundle not found: {target_path}", as_json)
            return 2

        staging = destination / ".__upgrade_staging__"
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        try:
            result = unpack_template(target_path, staging)
        except BundleError as exc:
            if as_json:
                output_json({"error": str(exc)})
            else:
                print_fail(str(exc))
            shutil.rmtree(staging, ignore_errors=True)
            return 1

        slug = result.slug
        new_yaml_path = result.destination / "template.pocket.yaml"
        try:
            new_yaml = _parse_file(new_yaml_path)
        except ValueError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if as_json:
                output_json({"error": str(exc)})
            else:
                print_fail(str(exc))
            return 1

        installed_yaml_path = destination / slug / "template.pocket.yaml"
        if not installed_yaml_path.is_file():
            shutil.rmtree(staging, ignore_errors=True)
            msg = (
                f"no installed template at {installed_yaml_path} — run "
                f"`pocketpaw template install` first"
            )
            if as_json:
                output_json({"error": msg})
            else:
                print_fail(msg)
            return 1
        try:
            installed_yaml = _parse_file(installed_yaml_path)
        except ValueError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            if as_json:
                output_json({"error": str(exc)})
            else:
                print_fail(str(exc))
            return 1
    else:
        # Slug-mode: caller already installed two versions side by side,
        # or pointed us at a bare slug. We don't support that in v1 to
        # keep the surface small — surface a clear usage error.
        msg = (
            "upgrade by slug requires a bundle path; pass a "
            "<slug>-<version>.template.tar.gz instead"
        )
        if as_json:
            output_json({"error": msg})
        else:
            print_fail(msg)
        return 2

    diff = compute_template_diff(installed_yaml, new_yaml)

    # Decision time
    if not diff.is_destructive:
        # Non-destructive — apply silently
        _apply_upgrade(staging_src=result.destination, dest=destination / slug)
        shutil.rmtree(staging, ignore_errors=True)
        if as_json:
            output_json(
                {
                    "slug": slug,
                    "applied": True,
                    "destructive": False,
                    "diff": _diff_to_dict(diff),
                }
            )
        else:
            print_ok(f"upgraded {slug} (non-destructive)")
            _render_diff_summary(diff)
        return 0

    # Destructive — render diff + prompt
    if not as_json:
        print_warn("destructive changes detected:")
        _render_diff_summary(diff)

    if no_prompt:
        shutil.rmtree(staging, ignore_errors=True)
        if as_json:
            output_json(
                {
                    "slug": slug,
                    "applied": False,
                    "destructive": True,
                    "diff": _diff_to_dict(diff),
                    "reason": "destructive change refused under --no-prompt",
                }
            )
        else:
            print_fail("refusing to apply destructive upgrade under --no-prompt")
        return 2

    # Interactive prompt
    sys.stdout.write(f"Apply destructive upgrade to {slug}? [y/N] ")
    sys.stdout.flush()
    try:
        reply = input("")
    except EOFError:
        reply = ""
    if reply.strip().lower() not in {"y", "yes"}:
        shutil.rmtree(staging, ignore_errors=True)
        if as_json:
            output_json(
                {
                    "slug": slug,
                    "applied": False,
                    "destructive": True,
                    "diff": _diff_to_dict(diff),
                    "reason": "user declined",
                }
            )
        else:
            print("  Aborted — no changes applied.")
        return 0

    _apply_upgrade(staging_src=result.destination, dest=destination / slug)
    shutil.rmtree(staging, ignore_errors=True)
    if as_json:
        output_json(
            {
                "slug": slug,
                "applied": True,
                "destructive": True,
                "diff": _diff_to_dict(diff),
            }
        )
    else:
        print_ok(f"upgraded {slug} (destructive — confirmed)")
    return 0


def _apply_upgrade(*, staging_src: Path, dest: Path) -> None:
    """Replace ``dest``'s contents with the contents of ``staging_src``.

    We intentionally remove + recopy rather than overlay so removed
    files actually disappear (e.g. a screenshot dropped from the new
    version shouldn't linger). Both paths are inside the user's
    templates dir.
    """

    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)
    for src in staging_src.rglob("*"):
        if not src.is_file():
            continue
        relative = src.relative_to(staging_src)
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(src.read_bytes())


def _render_diff_summary(diff: Any) -> None:
    """Print a compact summary of the structured diff to stdout."""

    def _line(label: str, items: list[Any]) -> None:
        if not items:
            return
        rendered = ", ".join(str(x) for x in items)
        print(f"  {label}: {rendered}")

    _line("actions added", diff.actions_added)
    _line("actions removed", diff.actions_removed)
    _line("triggers added", diff.triggers_added)
    _line("triggers removed", diff.triggers_removed)
    _line("outcomes added", diff.outcomes_added)
    _line("outcomes removed", diff.outcomes_removed)
    _line("instinct rules added", diff.instinct_rules_added)
    _line("instinct rules removed", diff.instinct_rules_removed)
    for entry in diff.actions_changed:
        tag = " [destructive]" if entry.get("destructive") else ""
        print(f"  changed{tag}: {entry['path']}: {entry['old']!r} -> {entry['new']!r}")
    for entry in diff.triggers_changed:
        tag = " [destructive]" if entry.get("destructive") else ""
        print(f"  changed{tag}: {entry['path']}: {entry['old']!r} -> {entry['new']!r}")
    for entry in diff.instinct_rules_changed:
        tag = " [destructive]" if entry.get("destructive") else ""
        print(f"  changed{tag}: {entry['path']}: {entry['old']!r} -> {entry['new']!r}")


def _diff_to_dict(diff: Any) -> dict[str, Any]:
    """Serialize the TemplateDiff dataclass for ``--json`` output."""

    return {
        "added_fields": diff.added_fields,
        "removed_fields": diff.removed_fields,
        "changed_fields": diff.changed_fields,
        "actions_added": diff.actions_added,
        "actions_removed": diff.actions_removed,
        "actions_changed": diff.actions_changed,
        "triggers_added": diff.triggers_added,
        "triggers_removed": diff.triggers_removed,
        "triggers_changed": diff.triggers_changed,
        "instinct_rules_added": diff.instinct_rules_added,
        "instinct_rules_removed": diff.instinct_rules_removed,
        "instinct_rules_changed": diff.instinct_rules_changed,
        "outcomes_added": diff.outcomes_added,
        "outcomes_removed": diff.outcomes_removed,
        "is_destructive": diff.is_destructive,
    }


__all__ = ["run_template_cmd"]
