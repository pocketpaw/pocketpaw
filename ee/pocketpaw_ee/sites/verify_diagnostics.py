# ee/pocketpaw_ee/sites/verify_diagnostics.py — turn a sandbox build's stderr tail and
# the browser harness report into the agent-only ``diagnostics`` list.
#
# Created 2026-09-24 (PP-2, feat/sites-verify-pipeline). The verification verdict
# (contract §5, docs/design/drafts/2026-09-24-sites-deps-verify-contract.md) carries
# file / line / col / message entries so the authoring agent can fix its own code. Until
# now a failed build reached anyone only as a ``<rung>:<cause>`` string: the stderr tail
# went to the operator log and nowhere else, which was the right call while the only
# reader was a UI (build_job.py's header explains why).
#
# THE RULE THIS MODULE ENFORCES, AND WHY EVERY STEP IS HERE RATHER THAN AT THE CALLER.
# The agent is a new reader of text that used to stay in the log, so the text has to be
# made safe for it before it leaves the worker:
#
#   1. ``redact_output`` (``pocketpaw.security.redact``) — tokens, keys, passwords.
#   2. Sandbox absolute paths become project-relative. ``/home/daytona/paw-build/src/x``
#      is ``src/x``; any other absolute home or tmp path is reduced to its basename, so
#      nothing about the build host's layout travels.
#   3. ``site_key_*`` tokens are scrubbed. A preview build is handed a decoy key and the
#      scrub in ``build_job.scrub_build_input`` blanks it anyway; this is the belt to that
#      brace, because a key that did reach a message would be a durable leak.
#   4. The whole ``errors`` + ``warnings`` payload is capped at ``DIAGNOSTICS_CAP_BYTES``
#      (2 KB) with a trailing ``{"code": "truncated"}`` entry when anything was cut.
#
# One function, :func:`finalize`, applies all four; every producer (build parse, harness,
# static check) goes through it. A second copy of any step is how one reader ends up
# seeing text another was protected from.
#
# WHERE IT MAY GO: agent tool results only. Never the Site row, never
# ``/sites/by-pocket/{id}/status``, never a UI payload. The verdict store keeps it next
# to the preview artifact (same backend, same tenancy key) so a re-verify of unchanged
# source can hand it back without a sandbox; ``verify.status_summary`` reads counts only.
from __future__ import annotations

import json
import re
from typing import Any

#: The contract's ceiling on ``errors`` + ``warnings`` together (§5).
DIAGNOSTICS_CAP_BYTES = 2048

#: One message never runs longer than this, so a single giant stack cannot spend the
#: whole budget and push every other entry out.
MESSAGE_MAX_CHARS = 400

#: The marker appended when the cap cut something.
TRUNCATED_ENTRY: dict[str, str] = {"code": "truncated"}

#: Sandbox roots made project-relative. The build project and the harness live under the
#: sandbox user's home (daytona_runner / browser_check), so a path under either is the
#: author's own file or ours, and the relative remainder is what the agent can act on.
SANDBOX_ROOTS: tuple[str, ...] = (
    "/home/daytona/paw-build/",
    "/home/daytona/paw-harness/",
)

_SITE_KEY_RE = re.compile(r"site_key_[A-Za-z0-9_\-]+")
# Any other absolute path under a home dir, /tmp, /root, /opt, /usr or /var: reduced to
# its last component. Deliberately broad — the agent needs the filename, not the host.
_ABS_PATH_RE = re.compile(r"(?<![\w.])/(?:home|tmp|root|opt|usr|var|private|Users)/[^\s'\"`:)]*")
_WIN_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s'\"`:)]*")
_LOCAL_ORIGIN_RE = re.compile(r"https?://(?:127\.0\.0\.1|localhost):\d+/?")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# ``src/lib/components/Hero.svelte:12:5`` — the shape vite, svelte, rollup, esbuild and
# tsc all print somewhere in their error text.
_LOC_RE = re.compile(
    r"(?P<file>(?:[\w.@$+\-]+/)*[\w.@$+\-]+\.(?:svelte|tsx|ts|jsx|js|mjs|cjs|css|html|json))"
    r"(?:[:(](?P<line>\d+)(?:[:,](?P<col>\d+))?\)?)?"
)
_UNRESOLVED_RE = re.compile(
    r"(?:failed to resolve import|Could not resolve|Cannot find module|"
    r"Failed to resolve entry for package)\s+[\"'](?P<spec>[^\"']+)[\"']"
    r"(?:\s+from\s+[\"'](?P<file>[^\"']+)[\"'])?",
    re.IGNORECASE,
)
_ERROR_LINE_RE = re.compile(
    r"(?:\berror\b|Error:|\bERR\b|✘|SyntaxError|TypeError|ReferenceError|"
    r"CompileError|ParseError|failed)",
    re.IGNORECASE,
)
# Lines that match ``_ERROR_LINE_RE`` but carry no information the agent can act on.
_NOISE_RE = re.compile(
    r"^(?:error: script \"build\" exited with code|npm ERR! (?:A complete log|code)|"
    r"error during build:\s*$|\s*at\s)",
    re.IGNORECASE,
)

#: Cap on entries parsed out of one stderr tail, before the byte cap applies.
_MAX_PARSED = 12


def scrub_text(text: str) -> str:
    """Steps 1-3 of the header on one string: redact, relativize, scrub keys."""
    from pocketpaw.security.redact import redact_output

    if not text:
        return ""
    out = _ANSI_RE.sub("", str(text))
    for root in SANDBOX_ROOTS:
        out = out.replace(root, "")
    out = _LOCAL_ORIGIN_RE.sub("/", out)
    out = _SITE_KEY_RE.sub("site_key_[redacted]", out)
    out = _ABS_PATH_RE.sub(lambda m: m.group(0).rstrip("/").rsplit("/", 1)[-1] or "[path]", out)
    out = _WIN_PATH_RE.sub(lambda m: m.group(0).rstrip("\\").rsplit("\\", 1)[-1] or "[path]", out)
    out = redact_output(out)
    out = " ".join(out.split())
    if len(out) > MESSAGE_MAX_CHARS:
        out = out[: MESSAGE_MAX_CHARS - 1] + "…"
    return out


def _entry(
    layer: str, *, code: str, message: str, file: str = "", line: Any = None, col: Any = None
) -> dict[str, Any]:
    entry: dict[str, Any] = {"layer": layer, "code": code, "message": message}
    if file:
        entry["file"] = file
    if isinstance(line, int) or (isinstance(line, str) and line.isdigit()):
        entry["line"] = int(line)
    if isinstance(col, int) or (isinstance(col, str) and col.isdigit()):
        entry["col"] = int(col)
    return entry


def _clean_entry(raw: dict[str, Any], layer: str) -> dict[str, Any]:
    """Normalize one producer's entry and scrub every string field on it."""
    return _entry(
        str(raw.get("layer") or layer),
        code=scrub_text(str(raw.get("code") or "error"))[:64],
        message=scrub_text(str(raw.get("message") or "")),
        file=scrub_text(str(raw.get("file") or "")),
        line=raw.get("line"),
        col=raw.get("col"),
    )


def parse_build_stderr(stderr_tail: str | None) -> list[dict[str, Any]]:
    """Pull file / line / col / message entries out of a sandbox build's stderr tail.

    Tolerant by design: vite, the svelte compiler, rollup, esbuild and bun all print
    differently, and a parse that misses leaves a real failure unexplained. So it takes
    every line that looks like an error, attaches the nearest ``file:line:col`` it can
    find (on the line or the one before), and when nothing matched at all it still
    returns ONE entry carrying the last meaningful lines — a failed build must never
    reach the agent with an empty ``errors`` list.

    Returns UNSCRUBBED entries; :func:`finalize` is what makes them safe.
    """
    text = _ANSI_RE.sub("", stderr_tail or "")
    lines = [ln.rstrip() for ln in text.splitlines()]
    entries: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(entry: dict[str, Any]) -> None:
        key = (entry.get("file", ""), entry["message"][:120])
        if key in seen or len(entries) >= _MAX_PARSED:
            return
        seen.add(key)
        entries.append(entry)

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or _NOISE_RE.search(stripped):
            continue
        unresolved = _UNRESOLVED_RE.search(stripped)
        if unresolved:
            add(
                _entry(
                    "build",
                    code="unresolved_import",
                    message=stripped,
                    file=unresolved.group("file") or "",
                )
            )
            continue
        if not _ERROR_LINE_RE.search(stripped):
            continue
        loc = _LOC_RE.search(stripped)
        if loc is None and i > 0:
            loc = _LOC_RE.search(lines[i - 1])
        if loc is None and i + 1 < len(lines):
            loc = _LOC_RE.search(lines[i + 1])
        add(
            _entry(
                "build",
                code="build_error",
                message=stripped,
                file=loc.group("file") if loc else "",
                line=loc.group("line") if loc else None,
                col=loc.group("col") if loc else None,
            )
        )

    if not entries:
        meaningful = [ln.strip() for ln in lines if ln.strip() and not _NOISE_RE.search(ln)]
        if meaningful:
            entries.append(
                _entry("build", code="build_failed", message=" | ".join(meaningful[-3:]))
            )
    return entries


def harness_entries(report: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Flatten a browser harness report (contract §4) into diagnostics entries.

    ``path`` becomes ``file`` and ``kind`` becomes ``code``, so the agent reads one
    shape for every layer. Unscrubbed; :func:`finalize` does that.
    """
    entries: list[dict[str, Any]] = []
    if not isinstance(report, dict):
        return entries
    for page in report.get("pages") or []:
        if not isinstance(page, dict):
            continue
        for err in page.get("errors") or []:
            if not isinstance(err, dict):
                continue
            message = str(err.get("message") or "")
            source = err.get("source")
            if source:
                message = f"{message} (at {source})"
            entries.append(
                _entry(
                    "browser",
                    code=str(err.get("kind") or "browser_error"),
                    message=message,
                    file=str(page.get("path") or ""),
                )
            )
    return entries


def _size(errors: list[Any], warnings: list[Any]) -> int:
    return len(json.dumps({"errors": errors, "warnings": warnings}, separators=(",", ":")).encode())


def finalize(
    errors: list[dict[str, Any]] | None,
    warnings: list[dict[str, Any]] | None = None,
    *,
    layer: str = "build",
    cap_bytes: int = DIAGNOSTICS_CAP_BYTES,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Scrub every entry and cap ``errors`` + ``warnings`` at ``cap_bytes`` together.

    Errors are kept before warnings (an error is what the agent must fix). When anything
    is dropped a ``{"code": "truncated"}`` entry is appended — to ``errors`` if an error
    was dropped, else to ``warnings`` — and the budget is measured WITH the marker, so the
    capped payload never exceeds ``cap_bytes``.
    """

    # A marker from an EARLIER finalize (a job's report being merged with the static
    # layer) is not an entry: drop it and remember that something was already cut.
    def _is_marker(entry: Any) -> bool:
        return (
            isinstance(entry, dict)
            and entry.get("code") == "truncated"
            and not entry.get("message")
        )

    raw_errors = [e for e in errors or [] if isinstance(e, dict)]
    raw_warnings = [w for w in warnings or [] if isinstance(w, dict)]
    errors_already_cut = any(_is_marker(e) for e in raw_errors)
    already_cut = errors_already_cut or any(_is_marker(w) for w in raw_warnings)
    clean_errors = [_clean_entry(e, layer) for e in raw_errors if not _is_marker(e)]
    clean_warnings = [_clean_entry(w, layer) for w in raw_warnings if not _is_marker(w)]
    if not already_cut and _size(clean_errors, clean_warnings) <= cap_bytes:
        return clean_errors, clean_warnings

    marker_bytes = len(json.dumps(TRUNCATED_ENTRY, separators=(",", ":")).encode()) + 1
    budget = cap_bytes - marker_bytes
    kept_errors: list[dict[str, Any]] = []
    kept_warnings: list[dict[str, Any]] = []
    errors_cut = errors_already_cut
    for entry in clean_errors:
        if _size([*kept_errors, entry], kept_warnings) <= budget:
            kept_errors.append(entry)
        else:
            errors_cut = True
    for entry in clean_warnings:
        if _size(kept_errors, [*kept_warnings, entry]) <= budget:
            kept_warnings.append(entry)
    if errors_cut:
        kept_errors.append(dict(TRUNCATED_ENTRY))
    else:
        kept_warnings.append(dict(TRUNCATED_ENTRY))
    return kept_errors, kept_warnings


__all__ = [
    "DIAGNOSTICS_CAP_BYTES",
    "MESSAGE_MAX_CHARS",
    "SANDBOX_ROOTS",
    "TRUNCATED_ENTRY",
    "finalize",
    "harness_entries",
    "parse_build_stderr",
    "scrub_text",
]
