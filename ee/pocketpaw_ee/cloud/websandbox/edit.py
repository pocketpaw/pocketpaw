# edit.py — Web Cursor AI edit agent (Cmd-K), BACKEND-SIDE (WC-5a).
# Created 2026-07-15 (feat/websandbox-edit-agent).
#
# Given a file + an instruction (+ optional selection), a backend-side frontier
# model returns a PROPOSED rewrite of the file. This module is GENERATE-ONLY: it
# reads the file over the network via ``DaytonaClient``, optionally greps the repo
# for a symbol from the selection, calls the model, and returns the proposal for
# the frontend to review per-hunk and save via the existing file-RPC. It does NOT
# write anything to the VM.
#
# ARCHITECTURE (hard rule): the edit agent runs BACKEND-SIDE. There is NO in-VM
# agent, NO UDS bridge, NO code_mode — the only VM interaction is ``download_file``
# (read the target) and best-effort ``execute_command("rg …")`` (optional context).
#
# SECURITY: tenancy is enforced first (owner-scoped ``get_sandbox`` +
# fail-closed ``authorize_sandbox``, same guards the tree/ws use) BEFORE any model
# call or VM read; the target path is run through the SAME lexical jail as the
# file-RPC (``files._jail``, rooted at ``WEBSANDBOX_WORKDIR``) so a crafted
# ``path`` can't escape the workspace and no download happens for a traversal
# attempt.
#
# LLM SEAM: mirrors ``decisions/explain/narrator.py`` — ``AsyncAnthropic`` with
# the key from ``get_settings().anthropic_api_key``. The model id is
# env-configurable (``POCKETPAW_WEBSANDBOX_EDIT_MODEL``, default the Sonnet-class
# id narrator uses). The edit fn takes ``client=None`` (default builds the real
# client) so unit tests inject a fake and never hit the real API. Any model /
# read failure surfaces as a clean ``websandbox.edit_failed`` CloudError — never a
# fake success.
from __future__ import annotations

import logging
import os
import re

from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError, with_cause
from pocketpaw_ee.cloud.daytona.client import DaytonaClient, get_daytona_client
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR
from pocketpaw_ee.cloud.websandbox.dto import EditRequest, EditResponse
from pocketpaw_ee.cloud.websandbox.files import FileRpcError, _jail

logger = logging.getLogger(__name__)

# Default model id — matches what narrator uses (Sonnet-class). Env-overridable.
_DEFAULT_EDIT_MODEL = "claude-sonnet-4-7"
# Output ceiling for the proposed file. Non-streaming-safe (well under the SDK's
# ~10-min timeout guard). Very large files may truncate — the read cap below
# keeps targets in a sane range.
_EDIT_MAX_TOKENS = 16000
# Model call timeout (seconds) and retry budget — mirror narrator's shape.
_EDIT_TIMEOUT_SECONDS = 60.0
# Ripgrep context is bounded so one huge match set can't blow up the prompt.
_MAX_CONTEXT_CHARS = 4000

_SYSTEM_PROMPT = (
    "You are a precise code-editing assistant embedded in an IDE. You are given a "
    "file and an instruction. Apply the instruction and return ONLY the FULL "
    "updated file content — the entire file from first line to last, with your "
    "edit applied. Do not add prose, explanations, or markdown code fences. Do not "
    "omit or elide any unchanged part of the file. Preserve the file's existing "
    "indentation, style, and trailing newline. If the instruction cannot be "
    "applied, return the file unchanged."
)

# An identifier-like token, used to pull a symbol out of the selection for a
# best-effort ripgrep of the rest of the repo.
_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


def _edit_max_file_bytes() -> int:
    """Editable-file size cap in bytes (``POCKETPAW_WEBSANDBOX_EDIT_MAX_KB``,
    default 256). Bounds both the read and, indirectly, the model's output."""
    raw = os.environ.get("POCKETPAW_WEBSANDBOX_EDIT_MAX_KB", "").strip()
    kb = 256
    if raw:
        try:
            kb = int(raw)
        except ValueError:
            logger.warning(
                "ignoring non-numeric POCKETPAW_WEBSANDBOX_EDIT_MAX_KB=%r; using %d", raw, kb
            )
    return max(kb, 1) * 1024


def _edit_model() -> str:
    return os.environ.get("POCKETPAW_WEBSANDBOX_EDIT_MODEL", "").strip() or _DEFAULT_EDIT_MODEL


def _require_daytona(daytona: DaytonaClient | None) -> DaytonaClient:
    """Resolve the Daytona client, raising a clean CloudError when unconfigured
    (mirrors ``provision._require_client`` — a None client is a 503, not a crash)."""
    resolved = daytona if daytona is not None else get_daytona_client()
    if resolved is None:
        raise CloudError(
            503,
            "websandbox.daytona_unavailable",
            "The sandbox runtime is not configured",
        )
    return resolved


def _strip_code_fences(text: str) -> str:
    """Best-effort strip of a wrapping ```lang … ``` block.

    The system prompt forbids fences, but models occasionally wrap output anyway;
    stripping keeps the proposal a clean file body rather than diffing fence lines.
    Only strips when the WHOLE payload is a single fenced block.
    """
    stripped = text.strip()
    if not stripped.startswith("```"):
        return text
    lines = stripped.splitlines()
    if len(lines) < 2 or not lines[-1].strip().startswith("```"):
        return text
    # Drop the opening fence (with optional language) and the closing fence.
    return "\n".join(lines[1:-1])


def _selection_symbol(content: str, start_line: int, end_line: int) -> str | None:
    """Pull the first identifier-like token from the selected line range (1-indexed
    inclusive) for a best-effort ripgrep of the rest of the repo. None if the
    range is out of bounds or has no usable symbol."""
    lines = content.splitlines()
    lo = max(start_line, 1)
    hi = min(end_line, len(lines))
    if lo > hi:
        return None
    snippet = "\n".join(lines[lo - 1 : hi])
    match = _IDENTIFIER_RE.search(snippet)
    return match.group(0) if match else None


async def _gather_context(daytona: DaytonaClient, sandbox_id: str, symbol: str) -> str:
    """Best-effort: ripgrep ``symbol`` across the workspace for extra context.

    Bounded (``rg`` line cap + a hard char cap) and fully defensive — any failure
    returns an empty string, because context is a nice-to-have, never load-bearing.
    ``rg`` is on the Paw dev image.
    """
    try:
        # -n line numbers, --max-count caps per-file hits, -g excludes the VCS dir.
        cmd = f"rg -n --max-count 5 --no-heading -g '!.git' -- {symbol!r} ."
        resp = await daytona.execute_command(
            sandbox_id, cmd, cwd=WEBSANDBOX_WORKDIR, timeout=15
        )
    except Exception:  # noqa: BLE001 — context is best-effort; never fail the edit on it
        logger.debug("websandbox.edit: context ripgrep failed for %r", symbol, exc_info=True)
        return ""
    out = getattr(resp, "result", None)
    if not isinstance(out, str) or not out.strip():
        return ""
    return out[:_MAX_CONTEXT_CHARS]


def _build_user_message(body: EditRequest, file_content: str, context: str) -> str:
    """Assemble the user turn: instruction + the file (+ optional selection markers
    + optional grepped context)."""
    parts = [f"Instruction:\n{body.instruction}\n"]
    if body.selection is not None:
        parts.append(
            f"Focus your edit on lines {body.selection.startLine}–"
            f"{body.selection.endLine} of the file, but still return the FULL "
            "updated file.\n"
        )
    if context:
        parts.append(
            "Related occurrences elsewhere in the repository (for context only — "
            f"do not edit other files):\n{context}\n"
        )
    parts.append(f"File `{body.path}`:\n{file_content}")
    return "\n".join(parts)


async def _run_model(model: str, system: str, user_message: str, client) -> str:  # noqa: ANN001
    """Call the frontier model and return the raw text. Builds the real
    ``AsyncAnthropic`` client when none is injected (the DI seam for tests).

    Any failure — missing key, SDK error, empty response — raises a clean
    ``websandbox.edit_failed`` CloudError, never a fake success.
    """
    if client is None:
        try:
            from anthropic import AsyncAnthropic

            from pocketpaw.config import get_settings

            api_key = get_settings().anthropic_api_key
        except Exception as exc:  # noqa: BLE001
            raise with_cause(
                CloudError(503, "websandbox.edit_unavailable", "The edit model is not configured"),
                exc,
            ) from exc
        if not api_key:
            raise CloudError(
                503, "websandbox.edit_unavailable", "The edit model is not configured"
            )
        client = AsyncAnthropic(api_key=api_key, timeout=_EDIT_TIMEOUT_SECONDS, max_retries=1)

    try:
        response = await client.messages.create(
            model=model,
            max_tokens=_EDIT_MAX_TOKENS,
            system=[{"type": "text", "text": system}],
            messages=[{"role": "user", "content": user_message}],
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("websandbox.edit: model call failed", exc_info=True)
        raise with_cause(
            CloudError(502, "websandbox.edit_failed", "The edit model call failed"),
            exc,
        ) from exc

    try:
        raw_text = response.content[0].text
    except (AttributeError, IndexError) as exc:
        raise with_cause(
            CloudError(502, "websandbox.edit_failed", "The edit model returned no content"),
            exc,
        ) from exc
    if not isinstance(raw_text, str) or not raw_text.strip():
        raise CloudError(502, "websandbox.edit_failed", "The edit model returned empty content")
    return raw_text


async def propose_edit(
    workspace_id: str,
    user_id: str,
    row_id: str,
    body: EditRequest | dict,
    *,
    client=None,  # noqa: ANN001 — an AsyncAnthropic-shaped object (DI seam for tests)
    daytona: DaytonaClient | None = None,
) -> EditResponse:
    """Propose a model-authored rewrite of a file in a ready sandbox (generate-only).

    Flow: validate → resolve the row owner-scoped (``get_sandbox`` → NotFound for a
    row the caller doesn't own) → 409 if it never bound a Daytona id → fail-closed
    ``authorize_sandbox`` BEFORE any VM touch → jail the path (``..``/absolute are
    refused, no download for a traversal attempt) → ``download_file`` the target →
    optional best-effort ripgrep context → call the model → return the proposal.

    NEVER writes to the VM: the frontend reviews the proposed content per-hunk and
    applies accepted changes via the existing file-RPC.
    """
    body = EditRequest.model_validate(body)
    dt = _require_daytona(daytona)

    # 1. Tenancy — owner-scoped resolve (NotFound cross-tenant), then the row must
    #    have a bound VM, then the fail-closed authorize before ANY VM touch.
    row = await websandbox_service.get_sandbox(workspace_id, user_id, row_id)
    if not row.sandbox_id:
        raise ConflictError("websandbox.not_ready", "Sandbox is not provisioned yet")
    await websandbox_service.authorize_sandbox(workspace_id, user_id, row.sandbox_id)

    # 2. Path safety — the SAME lexical jail the file-RPC uses, rooted at the
    #    pinned workspace dir. An escape raises before any download.
    try:
        abs_path = _jail(WEBSANDBOX_WORKDIR, body.path, "read")
    except FileRpcError as exc:
        raise CloudError(400, "websandbox.edit_invalid_path", exc.message) from exc

    # 3. Read the target file over the network (backend-side, no in-VM agent).
    try:
        data = await dt.download_file(row.sandbox_id, abs_path)
    except FileNotFoundError as exc:
        raise CloudError(404, "websandbox.edit_no_file", f"no such file: {body.path}") from exc
    if data is None:
        raise CloudError(404, "websandbox.edit_no_file", f"no such file: {body.path}")
    cap = _edit_max_file_bytes()
    if len(data) > cap:
        raise CloudError(
            413,
            "websandbox.edit_too_large",
            f"file is {len(data) // 1024} KB, over the {cap // 1024} KB edit limit",
        )
    try:
        original = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CloudError(
            400, "websandbox.edit_not_text", "file is not UTF-8 text (binary is not supported)"
        ) from exc

    # 4. Optional, bounded, best-effort context from a symbol in the selection.
    context = ""
    if body.selection is not None:
        symbol = _selection_symbol(original, body.selection.startLine, body.selection.endLine)
        if symbol:
            context = await _gather_context(dt, row.sandbox_id, symbol)

    # 5. Call the model and return the PROPOSAL (never written to the VM).
    user_message = _build_user_message(body, original, context)
    raw_text = await _run_model(_edit_model(), _SYSTEM_PROMPT, user_message, client)
    proposed = _strip_code_fences(raw_text)

    return EditResponse(
        path=body.path,
        originalContent=original,
        proposedContent=proposed,
        selection=body.selection,
    )


__all__ = ["propose_edit"]
