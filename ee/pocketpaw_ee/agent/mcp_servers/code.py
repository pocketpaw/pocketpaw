# code.py — in-process MCP server exposing the file tools the main chat agent
# uses to reach the user's code. Created: 2026-07-22 (feat/code-mode-tool, CD-2).
# Reshaped 2026-07-24 (feat/code-mode-file-tools).
#
# What changed and why. CD-2 gave the main agent ONE coarse tool, ``code_mode``,
# that handed a whole task to a browser SUB-AGENT which ran its own read/write
# loop. That sub-agent was removed (2026-07-23): the /code surface runs on the
# MAIN PocketPaw agent, and a second, weaker in-browser agent was redundant.
# With it gone, the reasoning that decomposed a task into individual file reads
# and writes no longer existed anywhere — so it moves INTO the main agent, and
# this server changes shape to match. Instead of one ``code_mode(task)`` tool,
# it now exposes the four file verbs the agent reasons WITH:
#
#   readFile(path)          — read a file
#   search(query)           — search the project
#   listDir(path)           — list a directory
#   writeFile(path, content)— PROPOSE a full-file rewrite (staged, not written)
#
# The main agent runs its own tool loop over these, exactly as a coding agent
# does over local file tools. The difference this whole module exists for: the
# files are not here.
#
# Why the tools do not touch a file. An MCP server runs in the BACKEND, where
# the user's project is not — a WebContainer project lives in the user's tab and
# has no server-side row a backend could open. That is fatal to a tool that
# OPENS a file. It is not fatal to these, because they never open anything — they
# ask the BROWSER to. Each handler is an ``async def``, so it can await a future:
# ``delegate_call_to_browser`` (CD-1) registers a correlation id, pushes one
# ``code_delegate`` frame carrying ``{tool, input}`` down the live SSE stream,
# and parks until the tab POSTs the result back. The same inversion MCP
# standardizes as elicitation.
#
# The write gate stays in the tab. ``writeFile`` NEVER writes. It delegates the
# proposed content to the browser, which STAGES it for the user's per-hunk
# review; the user accepting a hunk is the only thing that writes, and it happens
# long after this tool has returned. So the honest result of a ``writeFile`` call
# is "a change was proposed for review", never "the file was changed" — the
# description and the system prompt both say so, because the model must not tell
# the user a file was written when nothing has been.
#
# Why this lives in ee/ and not src/pocketpaw/agents/. The channel this wraps
# (``ee.cloud.codeagent.delegates``) is EE cloud code, and the workspace identity
# it needs comes from the per-stream ContextVars in ``ee.cloud.chat.agent_service``.
# An OSS module importing either would invert the dependency. Registration is the
# standard ``pocketpaw.mcp_servers`` entry point (``CloudCodeMcpProvider``), NOT
# ``claude_sdk._get_mcp_servers``, which only builds the OSS-side servers.
#
# On metering: these handlers spend nothing. They call no model — they park on a
# future. The model spend happens on the main agent's own chat turn, which meters
# itself; the browser side runs no model at all (it just executes a file verb).
# There is no second meter here to reconcile.
#
# The tool ids namespace as ``mcp__pocketpaw_code__<verb>``. The /code
# SurfaceProfile hardcodes these exact strings as literals (it shipped before this
# file could export a constant). Do NOT drift the server name or any tool name
# without changing ``surface_registry._CODE_FILE_TOOL_IDS`` in the same PR — they
# are matched by string, so a rename fails silently by scoping the surface to an
# id nothing provides. ``test_code_mcp_server`` pins both ends against each other.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_code"

# Claude Code namespaces in-process MCP tools as ``mcp__<server>__<tool>``.
READ_FILE_TOOL_ID = f"mcp__{SERVER_NAME}__readFile"
WRITE_FILE_TOOL_ID = f"mcp__{SERVER_NAME}__writeFile"
SEARCH_TOOL_ID = f"mcp__{SERVER_NAME}__search"
LIST_DIR_TOOL_ID = f"mcp__{SERVER_NAME}__listDir"

# Exported as a tuple for the provider's ``tool_ids()``. Order is read-first so a
# log or an allow-list dump reads in the order the agent typically calls them.
CODE_TOOL_IDS = (
    READ_FILE_TOOL_ID,
    SEARCH_TOOL_ID,
    LIST_DIR_TOOL_ID,
    WRITE_FILE_TOOL_ID,
)

# Bounds on what crosses the wire into the browser. Paths and queries are small
# by nature; a rewrite can be a whole file. ``content`` is capped well under the
# channel's ``MAX_DELEGATE_RESULT_CHARS`` return ceiling so a proposal and its
# staged-summary answer both fit comfortably.
_MAX_PATH_CHARS = 1024
_MAX_QUERY_CHARS = 2048
_MAX_CONTENT_CHARS = 180_000


def _error_response(message: str) -> dict[str, Any]:
    """Build an MCP error response in the shape Claude's SDK expects. The agent
    reads ``text`` and surfaces the reason."""
    return {
        "content": [{"type": "text", "text": f"Error: {message}"}],
        "is_error": True,
    }


def _text_response(text: str, *, is_error: bool = False) -> dict[str, Any]:
    """Build an MCP response carrying ``text`` verbatim.

    The browser's executor already returns human/model-readable output (file
    contents, a search listing, a staged-change sentence), so this relays it
    rather than re-wrapping it in JSON. ``is_error`` rides the SDK's own flag so
    the model treats a failed lookup differently from an empty one.
    """
    response: dict[str, Any] = {"content": [{"type": "text", "text": text}]}
    if is_error:
        response["is_error"] = True
    return response


def _workspace_id() -> str | None:
    """Resolve the active workspace from the per-stream ContextVars set by the
    cloud chat agent runtime.

    Only the workspace is read. The delegate is addressed to a workspace's live
    stream, and ``delegates.resolve_pending`` scopes the return leg the same
    way; the user id is not an authorization input there, so asking for one here
    would imply a check that is not made.
    """
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_workspace_id

        return current_workspace_id()
    except Exception:  # noqa: BLE001 — outside a cloud stream this is simply absent
        return None


def _require_str(args: dict, key: str, *, max_chars: int, allow_empty: bool = False) -> str | None:
    """Pull a bounded string argument, or return ``None`` if it is unusable.

    ``None`` means "reject" — the caller turns that into an MCP error. A separate
    sentinel rather than an exception because a raise inside an in-process tool
    handler reaches the model as a BROKEN tool, whereas an error response gives it
    a sentence it can act on.
    """
    value = args.get(key)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text and not allow_empty:
        return None
    if len(value) > max_chars:
        return None
    return text if not allow_empty else value


async def _delegate(tool: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Resolve the workspace, delegate ONE call to the browser, relay the result.

    Every failure path returns an MCP response rather than raising, for the same
    reason the handlers do: the caller is an agent tool.
    """
    workspace_id = _workspace_id()
    if not workspace_id:
        # No cloud stream in scope: a CLI run, a background job, a test. The
        # delegate has nowhere to go and no tenant to scope the return leg to.
        return _error_response(
            "There is no active workspace session, so the code tool cannot reach "
            "the user's project from here."
        )

    from pocketpaw_ee.cloud.codeagent.delegates import delegate_call_to_browser

    outcome = await delegate_call_to_browser(workspace_id, tool, tool_input)

    if not outcome.ok:
        logger.info("code.%s delegate failed ws=%s error=%s", tool, workspace_id, outcome.error)
        # Relay the channel's own message. It already distinguishes "no browser
        # attached" from "the browser was slow", and the model needs that
        # difference to say something true about what happened.
        return _error_response(outcome.message or "The file operation could not be completed.")

    result = outcome.result or {}
    output = result.get("output")
    if not isinstance(output, str):
        output = ""
    is_error = bool(result.get("isError"))
    if is_error and not output:
        output = "The file operation failed in the browser."
    return _text_response(output, is_error=is_error)


async def _read_file_handler(args: dict) -> dict[str, Any]:
    path = _require_str(args, "path", max_chars=_MAX_PATH_CHARS)
    if path is None:
        return _error_response(
            "`path` is required — the file to read, relative to the project root."
        )
    return await _delegate("readFile", {"path": path})


async def _search_handler(args: dict) -> dict[str, Any]:
    query = _require_str(args, "query", max_chars=_MAX_QUERY_CHARS)
    if query is None:
        return _error_response(
            "`query` is required — the text or symbol to search the project for."
        )
    return await _delegate("search", {"query": query})


async def _list_dir_handler(args: dict) -> dict[str, Any]:
    # An empty path is legitimate here — it lists the project root — so this one
    # verb allows the empty string rather than rejecting it.
    path = _require_str(args, "path", max_chars=_MAX_PATH_CHARS, allow_empty=True)
    if path is None:
        return _error_response("`path` must be a string — the directory to list ('' for the root).")
    return await _delegate("listDir", {"path": path})


async def _write_file_handler(args: dict) -> dict[str, Any]:
    """Propose a full-file rewrite. This does NOT write the file.

    The proposed ``content`` is delegated to the browser, which stages it for the
    user's per-hunk review. Nothing is written until the user accepts a hunk,
    which happens after this returns — so a success here means "a change was
    proposed for review", not "the file was changed".
    """
    path = _require_str(args, "path", max_chars=_MAX_PATH_CHARS)
    if path is None:
        return _error_response(
            "`path` is required — the file to change, relative to the project root."
        )
    # ``content`` may legitimately be the empty string (blanking a file), so it is
    # required-present but allowed-empty; only a non-string or an over-long body
    # is rejected.
    content = args.get("content")
    if not isinstance(content, str):
        return _error_response("`content` is required — the full new contents of the file.")
    if len(content) > _MAX_CONTENT_CHARS:
        return _error_response(
            f"`content` is too long ({len(content)} chars, limit {_MAX_CONTENT_CHARS}). "
            "Propose a change to a smaller file, or split the work across files."
        )
    return await _delegate("writeFile", {"path": path, "content": content})


def build_code_server() -> tuple[str, Any] | None:
    """Build the in-process SDK MCP server for the /code file tools, or return
    ``None`` if the Claude Agent SDK isn't installed.

    Matches the ``(name, server) | None`` shape of ``build_belt_server`` so the
    backend's MCP registration loop treats it identically.
    """
    try:
        from claude_agent_sdk import create_sdk_mcp_server, tool
    except ImportError:
        logger.debug("claude_agent_sdk not installed; pocketpaw_code MCP disabled")
        return None

    @tool(
        "readFile",
        (
            "Read a file from the user's project and return its contents. This is "
            "how you look at their code — the project runs in the user's browser, "
            "not on your machine, so the built-in file tools do not address it. "
            "Args: `path` (the file, relative to the project root)."
        ),
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "File path relative to the project root, e.g. 'src/App.tsx'.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    async def read_file(args):  # type: ignore[no-untyped-def]
        return await _read_file_handler(args)

    @tool(
        "search",
        (
            "Search the user's project for a string or symbol and return the "
            "matching lines with their file and line number. Use this to locate "
            "code before reading or changing it. Args: `query` (the text to find)."
        ),
        {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Text or symbol to search for, e.g. 'useState' or 'Sidebar'.",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    )
    async def search(args):  # type: ignore[no-untyped-def]
        return await _search_handler(args)

    @tool(
        "listDir",
        (
            "List the entries of a directory in the user's project, to see how it "
            "is laid out. Args: `path` (the directory relative to the project "
            "root; use '' or '.' for the root)."
        ),
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory relative to the project root; '' for the root.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    async def list_dir(args):  # type: ignore[no-untyped-def]
        return await _list_dir_handler(args)

    @tool(
        "writeFile",
        (
            "Propose the full new contents of a file in the user's project. This "
            "does NOT save the file — it stages the change for the user to review "
            "and accept hunk by hunk. So report it as a change PROPOSED for "
            "review, never as a file that was written or a feature that is done; "
            "the user has to accept it first. Pass the ENTIRE new file in "
            "`content`, not a diff or a snippet. Args: `path` (the file), "
            "`content` (its full new contents)."
        ),
        {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "minLength": 1,
                    "description": "File path relative to the project root.",
                },
                "content": {
                    "type": "string",
                    "description": "The COMPLETE new contents of the file, not a diff.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    )
    async def write_file(args):  # type: ignore[no-untyped-def]
        return await _write_file_handler(args)

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[read_file, search, list_dir, write_file],
    )
    return SERVER_NAME, server


__all__ = [
    "CODE_TOOL_IDS",
    "LIST_DIR_TOOL_ID",
    "READ_FILE_TOOL_ID",
    "SEARCH_TOOL_ID",
    "SERVER_NAME",
    "WRITE_FILE_TOOL_ID",
    "build_code_server",
]
