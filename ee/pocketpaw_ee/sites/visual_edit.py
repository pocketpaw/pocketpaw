# ee/pocketpaw_ee/sites/visual_edit.py — visual-edit write-back for project sites: the
# owner clicks an element in the draft preview and the edit lands in the source file
# that element came from, as one ordinary project file write.
#
# Three pieces, all keyed off the vendored paw-sites generator (``project_tools.cli_argv``):
#   * feature detection: :func:`visual_edit_supported`, :func:`stamp_html_supported` and
#     :func:`source_tags_supported` read the generator's ``dist/cli.js`` for the
#     subcommand / flag, so an older generator turns the feature off instead of failing;
#     ``PAW_SITES_VISUAL_EDIT=0`` turns all three off;
#   * :func:`apply_visual_edit`: parse ``src`` (``<file>:<line>:<col>``, the
#     ``data-paw-src`` stamp), run ``paw-sites-gen visual-edit`` over that one file, and
#     save the result through ``project_tools.save_changes`` (the same writer as every
#     project file write: history, undo, Review changes) and queue the draft build;
#   * :func:`stamp_html`: ``paw-sites-gen stamp-html`` over a no-build draft's pages,
#     so a ``static`` preview carries the same ``data-paw-src`` stamps a framework draft
#     build gets from ``--source-tags``.
#
# Invariants: both subcommands run on the API host and only parse and splice text; no
# author code runs here. A src path passes ``project_tools.normalize_path`` (no
# absolute path, ``..`` or reserved path), must already be in the source map and must
# have an editable extension. Publish output is never stamped or tagged.
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, NotFound, ValidationError

logger = logging.getLogger(__name__)

#: Turns visual editing (write-back, static stamping, draft source tags) off when "0".
ENV_SWITCH = "PAW_SITES_VISUAL_EDIT"

#: Files a visual edit may patch (the languages the generator's patchers parse).
EDITABLE_EXTENSIONS = (".tsx", ".jsx", ".svelte", ".astro", ".html")

KINDS = ("text", "class", "attr", "delete", "duplicate", "move")
ATTR_NAMES = ("src", "href", "alt", "title")
MOVES = ("up", "down")

#: Generator refusal codes and the HTTP status each maps to.
REFUSALS = {"stale": 409, "not_editable": 422, "parse_error": 422, "unsupported": 422}

MAX_TEXT_CHARS = 20_000
MAX_VALUE_CHARS = 4_000
#: One visual edit or stamp: a parse and a splice, so seconds, not minutes.
CLI_TIMEOUT_SEC = 30

_BAD_URL_SCHEMES = ("javascript:", "vbscript:")

# ---------------------------------------------------------------------------
# Feature detection
# ---------------------------------------------------------------------------

_cli_cache: dict[str, Any] = {"key": None, "text": b""}


def _cli_js_path() -> Path | None:
    """The generator's ``dist/cli.js``: the vendored copy, else a ``.js`` file named in
    ``PAW_SITES_GEN_CMD``. ``None`` when neither is on this host."""
    from pocketpaw_ee.sites import project_build

    root = project_build.generator_dir()
    if root is not None:
        return root / "dist" / "cli.js"
    from pocketpaw_ee.sites.generator_client import _gen_cmd_argv

    for arg in _gen_cmd_argv():
        if arg.endswith((".js", ".mjs", ".cjs")) and Path(arg).is_file():
            return Path(arg)
    return None


def generator_cli_text() -> bytes:
    """The generator entry file's bytes (empty when absent), re-read only when its
    path or mtime moved: site lists ask once per row, so a hit is a stat."""
    path = _cli_js_path()
    try:
        key = (str(path), path.stat().st_mtime_ns) if path is not None else None
    except OSError:
        key = None
    if key != _cli_cache["key"]:
        try:
            text = path.read_bytes() if key is not None and path is not None else b""
        except OSError:
            text = b""
        _cli_cache["text"] = text
        _cli_cache["key"] = key
    return _cli_cache["text"]


def reset_cache() -> None:
    _cli_cache.update(key=None, text=b"")


def _switched_off() -> bool:
    return (os.environ.get(ENV_SWITCH) or "").strip().lower() in ("0", "false", "off", "no")


def _has_command(text: bytes, name: str) -> bool:
    return f'"{name}"'.encode() in text or f"'{name}'".encode() in text


def cli_has_source_tags(text: bytes) -> bool:
    """A generator whose ``project-build`` takes ``--source-tags`` (paw-sites#80)."""
    return b"--source-tags" in text


def visual_edit_supported() -> bool:
    """The generator has ``visual-edit`` (and the switch is not off)."""
    return not _switched_off() and _has_command(generator_cli_text(), "visual-edit")


def stamp_html_supported() -> bool:
    """The generator has ``stamp-html`` (and the switch is not off)."""
    return not _switched_off() and _has_command(generator_cli_text(), "stamp-html")


def source_tags_supported() -> bool:
    """The generator's ``project-build`` takes ``--source-tags`` (and the switch is
    not off)."""
    return not _switched_off() and cli_has_source_tags(generator_cli_text())


# ---------------------------------------------------------------------------
# Input checks
# ---------------------------------------------------------------------------


def _bad(code: str, message: str) -> ValidationError:
    return ValidationError(f"sites.visual_edit_{code}", message)


def parse_src(src: Any) -> tuple[str, int, int]:
    """``<file>:<line>:<col>`` as ``(normalized path, line, col)``. Raises 422 for a
    malformed stamp, a path that leaves the project, or a file type no patcher reads."""
    from pocketpaw_ee.sites.project_tools import normalize_path

    if not isinstance(src, str) or src.count(":") < 2:
        raise _bad("bad_src", "`src` must be <file>:<line>:<col>.")
    raw, line_s, col_s = src.rsplit(":", 2)
    if not (line_s.isdigit() and col_s.isdigit()) or int(line_s) < 1 or int(col_s) < 1:
        raise _bad("bad_src", "`src` line and column must be positive whole numbers.")
    rel = normalize_path(raw)
    if not rel.lower().endswith(EDITABLE_EXTENSIONS):
        raise _bad(
            "bad_src",
            f"{rel} cannot be edited visually; only {', '.join(EDITABLE_EXTENSIONS)} files.",
        )
    return rel, int(line_s), int(col_s)


def _check_url(value: str) -> None:
    if value.strip().lower().replace("\t", "").replace("\n", "").startswith(_BAD_URL_SCHEMES):
        raise _bad("bad_value", "That link scheme is not allowed.")


def check_value(kind: Any, value: Any) -> Any:
    """The ``value`` for ``kind``, checked against the contract: text and class a
    string, attr ``{name, value}`` with a known name, delete / duplicate null, move
    ``up`` / ``down``. Raises 422 otherwise."""
    if kind not in KINDS:
        raise _bad("bad_kind", f"`kind` must be one of {', '.join(KINDS)}.")
    if kind == "text":
        if not isinstance(value, str) or len(value) > MAX_TEXT_CHARS:
            raise _bad("bad_value", f"A text edit needs a string of at most {MAX_TEXT_CHARS}.")
        return value
    if kind == "class":
        if not isinstance(value, str) or len(value) > MAX_VALUE_CHARS:
            raise _bad("bad_value", "A class edit needs the full class string.")
        return value
    if kind == "attr":
        if (
            not isinstance(value, Mapping)
            or value.get("name") not in ATTR_NAMES
            or not isinstance(value.get("value"), str)
            or len(value["value"]) > MAX_VALUE_CHARS
        ):
            raise _bad(
                "bad_value",
                f"An attr edit needs {{name, value}} with name one of {', '.join(ATTR_NAMES)}.",
            )
        if value["name"] in ("src", "href"):
            _check_url(value["value"])
        return {"name": value["name"], "value": value["value"]}
    if kind == "move":
        if value not in MOVES:
            raise _bad("bad_value", "A move is `up` or `down`.")
        return value
    if value is not None:
        raise _bad("bad_value", f"A {kind} edit takes no value.")
    return None


def file_hash(text: str) -> str:
    """The ``base_hash`` / ``file_hash`` of one file: sha256 of its UTF-8 text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# The generator calls
# ---------------------------------------------------------------------------


async def _run_json(command: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Run one stdin-JSON generator subcommand and return its JSON answer. Raises
    ``ProjectCliError`` for a crash (non-zero exit), a timeout, or no JSON line."""
    from pocketpaw_ee.sites.project_tools import ProjectCliError, run_cli

    code, out, err = await run_cli(
        [command],
        timeout=CLI_TIMEOUT_SEC,
        stdin=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    if code != 0 or out is None:
        raise ProjectCliError("crashed", f"{command} exited {code}. {err[-500:]}".strip())
    return out


def _failed(message: str) -> CloudError:
    """A generator crash (non-zero exit, timeout, no JSON) or an answer off the
    contract: a server fault, logged where it happens, never a 501."""
    return CloudError(500, "sites.visual_edit_failed", message)


def _unavailable() -> CloudError:
    return CloudError(
        501,
        "sites.visual_edit_unavailable",
        "Visual editing needs a newer site generator on this server.",
    )


async def apply_visual_edit(
    *,
    workspace_id: str,
    user_id: str,
    pocket_id: str,
    src: Any,
    kind: Any,
    value: Any,
    base_hash: str | None = None,
) -> dict[str, Any]:
    """Apply one visual edit to a project site's source and queue its draft build.

    Returns ``{ok, pocket_id, file, file_hash, changed, build_status, job_id,
    verification}``. Raises 501 ``sites.visual_edit_unavailable`` on a generator
    without ``visual-edit``; 422 for a bad src / kind / value or a generator refusal
    (``not_editable`` / ``parse_error`` / ``unsupported``); 409 when the file moved
    since the client saw it (``base_hash``) or the generator says ``stale``; 404 for a
    file that is not in the project; 500 ``sites.visual_edit_failed`` when the generator
    crashes or answers off the contract."""
    from pocketpaw_ee.sites import project_tools

    if not visual_edit_supported():
        raise _unavailable()
    rel, line, col = parse_src(src)
    checked = check_value(kind, value)
    pocket = await project_tools.load_project(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
    )
    source = project_tools.source_of(pocket)
    current = source.get(rel)
    if not isinstance(current, str):
        raise NotFound("site_file", rel)
    if base_hash and base_hash != file_hash(current):
        raise ConflictError(
            "sites.visual_edit_stale",
            f"{rel} changed since this preview was built. Wait for the new preview, "
            "then edit again.",
        )
    try:
        answer = await _run_json(
            "visual-edit",
            {
                "file": rel,
                "content": current,
                "src": f"{rel}:{line}:{col}",
                "kind": kind,
                "value": checked,
            },
        )
    except project_tools.ProjectCliError as exc:
        logger.error("sites.visual_edit: %s on %s crashed: %s", kind, pocket_id, exc.message)
        raise _failed("The visual edit could not be applied.") from exc
    if answer.get("ok") is not True:
        code = answer.get("code")
        message = str(answer.get("message") or "This element cannot be edited here.")
        if code not in REFUSALS:
            logger.error("sites.visual_edit: unknown refusal %r for %s", code, pocket_id)
            raise _failed(message)
        raise CloudError(REFUSALS[code], f"sites.visual_edit_{code}", message)
    new_text = answer.get("content")
    if not isinstance(new_text, str):
        logger.error("sites.visual_edit: %s on %s answered no content", kind, pocket_id)
        raise _failed("The site generator returned no file contents.")
    changed = new_text != current
    if changed:
        await project_tools.save_changes(
            user_id=user_id,
            pocket_id=pocket_id,
            source=source,
            writes={rel: new_text},
            label=f"Visual edit ({kind}) in {rel}",
        )
    verification = await project_tools.build_verification(
        workspace_id=workspace_id, user_id=user_id, pocket_id=pocket_id
    )
    return {
        "ok": True,
        "pocket_id": pocket_id,
        "file": rel,
        "file_hash": file_hash(new_text),
        "changed": changed,
        "build_status": verification.get("status"),
        "job_id": verification.get("job_id"),
        "verification": verification,
    }


async def stamp_html(pages: Mapping[str, bytes]) -> dict[str, bytes] | None:
    """``pages`` (relpath -> bytes, the .html files only) stamped with ``data-paw-src``
    / ``data-paw-text`` by ``stamp-html``, or ``None`` when the generator lacks it or
    the stamp failed (logged; the caller serves the pages unstamped). A page that is
    not UTF-8 is passed through untouched."""
    if not pages or not stamp_html_supported():
        return None
    from pocketpaw_ee.sites.project_tools import ProjectCliError

    texts: dict[str, str] = {}
    for name, data in pages.items():
        try:
            texts[name] = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
    if not texts:
        return None
    try:
        answer = await _run_json("stamp-html", {"files": texts})
    except ProjectCliError as exc:
        logger.warning("sites.visual_edit: stamp-html failed: %s", exc.message)
        return None
    files = answer.get("files")
    if not isinstance(files, Mapping):
        logger.warning("sites.visual_edit: stamp-html answered without files")
        return None
    out = dict(pages)
    for name in texts:
        stamped = files.get(name)
        if isinstance(stamped, str):
            out[name] = stamped.encode("utf-8")
    return out if out != pages else None


__all__ = [
    "ATTR_NAMES",
    "EDITABLE_EXTENSIONS",
    "ENV_SWITCH",
    "KINDS",
    "REFUSALS",
    "apply_visual_edit",
    "check_value",
    "cli_has_source_tags",
    "file_hash",
    "generator_cli_text",
    "parse_src",
    "reset_cache",
    "source_tags_supported",
    "stamp_html",
    "stamp_html_supported",
    "visual_edit_supported",
]
