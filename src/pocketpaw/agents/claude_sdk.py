"""
Claude Agent SDK backend for PocketPaw: runs a turn on the Claude Code CLI
through ``claude_agent_sdk``, keeping one warm CLI subprocess per backend.

Per-turn state. The SDK applies ``ClaudeAgentOptions`` (system prompt, MCP
servers, cwd, plugins) only at ``connect()``; a reused client is sent the query
text and nothing else. So the system prompt holds only session-stable layers,
and what changes per turn rides the user message in a ``<turn-context>`` block
(``compose_turn_message``): the caller's ``turn_context`` (KB hits, scope,
uploads, recall) and the stored history the client has not seen. Each client
carries a ``_HistoryWatermark``: a turn whose history extends it sends only the
delta; a diverged history (edit/delete) evicts the client and the replacement
gets the whole conversation. Stateless launches get the full history,
native-resume launches none. ``prewarm`` therefore takes no history.

Warm-client key (``_client_cache_key``): session key, cwd, model, allowed tools,
the prompt's ``stable_digest`` (else a hash of its behavioural prefix), the
plugin/skills digest and the tenant scope. Any change forces a fresh subprocess.
Per-run skill dirs are cached per digest and dropped on eviction or cleanup.

Lifecycle invariants:
  * A run holds the client via ``_client_in_use`` + ``_lease_token``; only that
    run releases the lease, and its teardown touches only the client it drove.
    ``cleanup()`` never releases a lease.
  * ``prewarm`` never evicts: it no-ops while the client is in use (re-checked
    under ``_client_lock``) or when a client that already served the session is
    live, and it takes the turn's ``model_override`` / ``tools_enabled``.
  * Every ``connect()`` goes through ``_connect_client``, bounded by
    ``claude_sdk_connect_timeout``; the stateless path bounds its first event
    by the same setting. A failed connect is disconnected and never cached.
  * A stream that ends without its ResultMessage tears down the backend's own
    client, or interrupts and retires a supervisor-leased one (``warm_client`` /
    ``on_client_built``, WH-1).
  * ``stop(session_key)`` stops only that session's runs (``_RunState``).
  * The Bun-crash retry replays every ``run`` parameter except ``warm_client``.
  * API-error text and error results become one error event (a too-old CLI names
    ``POCKETPAW_CLAUDE_SDK_CLI_PATH``); a max-turns stop names the limit.

Options: permissions are always bypassed (headless), ``cli_path`` comes from
``claude_sdk_cli_path`` (else the bundled CLI), ``max_buffer_size`` is 32 MiB for
image-returning tools, the subprocess env always carries ``MAX_MCP_OUTPUT_TOKENS``
(``claude_sdk_max_mcp_output_tokens`` unless already set in the environment), and
the in-process MCP servers (pocketpaw, planner, atlas, ...) are gated by the
ToolPolicy and the per-surface allow/deny sets.
Images ride every persistent send; the stateless ``query()`` cannot carry them.
Tracing: persistent turns are read through ``receive_response()``, the method
logfire's SDK instrumentation patches; the stateless path opens its own span.
"""

import asyncio
import base64
import hashlib
import logging
import os
import re
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any, NamedTuple

from pocketpaw.agents.backend import (
    BackendInfo,
    BaseAgentBackend,
    Capability,
    ImageAttachment,
    LeasedClient,
    SessionHandle,
)
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings
from pocketpaw.observability import detached_span
from pocketpaw.security.rails import is_substring_blocked
from pocketpaw.tools.policy import OPT_IN_MCP_SERVERS, ToolPolicy, ungranted_surface_servers

logger = logging.getLogger(__name__)


def _scrub_nul_chars(options_kwargs: dict[str, Any]) -> list[str]:
    """Strip NUL characters out of the assembled options, IN PLACE.

    Returns the dotted/indexed paths that carried one, most useful field first as
    encountered, so the caller can log WHERE it came from.

    Why this exists: Windows ``CreateProcess`` refuses a command line, an
    environment block, or a cwd containing a NUL, and the SDK passes all three
    through untouched. One NUL anywhere in the option set therefore kills the spawn
    before the agent exists, with an error that names nothing:

        CLIConnectionError: Failed to start Claude Code: embedded null character

    Every turn on that path then fails identically and there is no way to tell from
    the message whether the offender was the system prompt, a tool id, an env value,
    or the working directory. POSIX is not immune either — ``execve`` takes
    NUL-terminated strings, so a NUL truncates the argument silently instead of
    failing loudly, which is worse.

    Stripping is always safe: a NUL is not valid in a prompt, a tool id, a model
    name, a path, or an env value, so there is no case where preserving one is
    correct. Reporting the path is the part that earns this function's keep — it
    turns an un-debuggable spawn failure into a working run plus a breadcrumb
    naming the source.
    """
    offenders: list[str] = []

    def walk(value: Any, path: str) -> Any:
        if isinstance(value, str):
            if "\0" in value:
                offenders.append(path)
                # Log the TEXT AROUND the NUL, not just the field name. The field
                # alone is not actionable: ``system_prompt`` is tens of thousands of
                # characters assembled from a surface preamble, the soul, the
                # about-member block, KB context and history, so "it was in
                # system_prompt" narrows nothing. The surrounding text names the
                # actual producer — that is how the first instance of this was
                # traced to a `.tgz` workspace snapshot the KB had indexed as text
                # and was injecting as raw gzip.
                #
                # ``env`` values are REDACTED to a length + offset: that is where
                # API keys and tokens live, and a log line is the wrong place for
                # them. The variable NAME is already in ``path``, which is the part
                # that identifies the source.
                i = value.index("\0")
                if path.startswith("env["):
                    logger.error(
                        "SDK: NUL in %s — value length %d, first NUL at offset %d "
                        "(value redacted: env carries credentials)",
                        path,
                        len(value),
                        i,
                    )
                else:
                    logger.error(
                        "SDK: NUL in %s at offset %d of %d — context: %r",
                        path,
                        i,
                        len(value),
                        value[max(0, i - 120) : i + 120],
                    )
                return value.replace("\0", "")
            return value
        # dict / list are rebuilt in place so the caller's object is the cleaned one.
        # Anything else (numbers, bools, None, hooks, session_store, transports) is
        # returned untouched — the guard must never coerce an opaque collaborator.
        if isinstance(value, dict):
            for key, item in list(value.items()):
                value[key] = walk(item, f"{path}[{key!r}]")
            return value
        if isinstance(value, list):
            for i, item in enumerate(value):
                value[i] = walk(item, f"{path}[{i}]")
            return value
        return value

    for key, item in list(options_kwargs.items()):
        options_kwargs[key] = walk(item, key)
    return offenders


class _RunState:
    """Per-run stop state: which session the run serves and whether ``stop``
    reached it. Replaces a single backend-wide flag that any new run reset."""

    __slots__ = ("session_key", "stopped")

    def __init__(self, session_key: str | None) -> None:
        self.session_key = session_key
        self.stopped = False


class _BuiltOptions(NamedTuple):
    """The product of ``ClaudeSDKBackend._build_options`` (feat/claude-sdk-prewarm).

    Bundles the assembled ``ClaudeAgentOptions`` plus everything ``run``'s
    dispatch + finally still need after option assembly was extracted into a
    shared helper so ``prewarm`` can build the IDENTICAL options the first turn
    will (same cache key → the prewarmed warm client is reused, not evicted):

      * ``options`` — the ``ClaudeAgentOptions`` instance to connect / query with.
      * ``options_kwargs`` — the raw kwargs dict; the token-usage event reads
        ``model`` off it.
      * ``llm`` — the resolved LLM client; ``run`` uses it to format API errors.
      * ``run_skills_root`` / ``skills_dir_adopted`` / ``plugin_digest`` — the
        per-run materialized-skills-plugin lifecycle triple (entity-rooms A2 +
        the warm-reuse fix): the dir path (or None), whether a warm client
        adopted it (so the per-run finally must NOT rmtree it), and the
        plugin-identity hash threaded into the cache key + the dir cache.
    """

    options: Any
    options_kwargs: dict[str, Any]
    llm: Any
    run_skills_root: Path | None
    skills_dir_adopted: bool
    plugin_digest: str


# Per-message read buffer handed to the SDK (``ClaudeAgentOptions.max_buffer_size``).
# The SDK default is 1 MB, and a tool result carrying screenshot tiles (the site
# preview / reference image tools) can exceed that in one JSON message, which
# kills the turn. 32 MiB leaves headroom without being unbounded.
_SDK_MAX_BUFFER_BYTES = 32 * 1024 * 1024

# Fallback for ``claude_sdk_max_mcp_output_tokens`` when settings carry no usable
# int (they are sometimes mocks). Mirrors the config default.
_DEFAULT_MAX_MCP_OUTPUT_TOKENS = 200_000

# Default identity fallback (used when AgentContextBuilder prompt is not available)
_DEFAULT_IDENTITY = (
    "You are PocketPaw, a helpful AI assistant running locally on the user's computer."
)

_HTTP_TRANSPORTS: frozenset[str] = frozenset({"http", "sse", "streamable-http"})

# Fail-closed sentinel scope minted by ``_tenant_scope_key`` when tenancy is
# attached with a BLANK workspace id (AT-5). Matches no connector rows and
# must never unlock per-workspace features (e.g. the AT-7 Fabric
# introspector) — a half-attached tenancy degrades to "nothing available".
_SENTINEL_TENANT_SCOPE = "ws:__missing-workspace-id__"

# Universal pocket-creation grant. When a surface imposes a restrictive MCP
# allow-list (``SurfaceProfile.allow_mcp_tool_ids``), these ids are always kept
# so "create a pocket" works from every chat mode — the core capability. The
# create-pocket SKILL is plugin-loaded (not an MCP tool), so it stays reachable
# regardless. Plain ids (no EE import): allow/deny sets cross the OSS boundary
# as bare ``frozenset[str]``.
POCKET_CREATION_GRANT: frozenset[str] = frozenset(
    {
        "mcp__pocketpaw_pocket_specialist__create",
        "mcp__pocketpaw_pocket_planner__plan_pocket",
    }
)

# MCP servers whose tools survive ANY restrictive allow-list — the "general
# tools everywhere" set: connectors (composio) + the pocket lifecycle (read /
# widget edit / create / edit / plan). A mode's allow-list only names its
# SPECIALIZED tools; these servers stay available so every mode can still use
# connectors and build/edit pockets. Server is ``<server>`` in
# ``mcp__<server>__<tool>``.
ALWAYS_ALLOWED_MCP_SERVERS: frozenset[str] = frozenset(
    {
        "composio",
        "pocketpaw_pocket",
        "pocketpaw_pocket_specialist",
        "pocketpaw_pocket_planner",
    }
)


def _mcp_server_of(tool_id: str) -> str:
    """Extract ``<server>`` from an ``mcp__<server>__<tool>`` id (else "")."""
    parts = tool_id.split("__")
    return parts[1] if len(parts) >= 2 and parts[0] == "mcp" else ""


# ── the Windows prompt spill ────────────────────────────────────────────────
#
# Windows caps a whole command line at ~32,767 chars (CreateProcess), and the SDK
# passes a string ``system_prompt`` inline via ``--system-prompt``. A long
# identity/KB prompt blows that limit and surfaces as a misleading
# ``CLINotFoundError``, so since SDK 0.1.72 we hand it a
# ``{"type": "file", "path": ...}`` dict instead and the CLI reads
# ``--system-prompt-file``.
#
# THE FILENAME CARRIES A HASH OF THE CONTENT (PA-7b), where it used to be one
# fixed path, ``~/.pocketpaw/runtime/system_prompt.md``. That single path was two
# bugs wearing one coat:
#
# * A CROSS-RUN RACE. ``AgentPool`` holds one backend instance per agent, so two
#   concurrent large-prompt runs on one box both wrote that file and whichever
#   CLI subprocess started second read the other's prompt. Windows-only, so the
#   cloud never saw it — but the desktop app runs a pool.
# * A CACHE-KEY COLLAPSE. ``_behavior_prefix`` has no text to cut when the prompt
#   arrives as a dict, so it returns ``file:<path>`` — which was CONSTANT. Every
#   prompt over the threshold hashed identically, so the warm client stopped
#   rebuilding on prompt changes for exactly the prompts big enough to spill:
#   the staleness the behavioural-prefix key exists to prevent, restored at the
#   top of the size range.
#
# Content-addressing fixes both at once and adds no I/O to ``_client_cache_key``,
# which reads the path it is given and nothing else. What it costs: the name
# hashes the WHOLE prompt, volatile tail included, so a no-digest caller on
# Windows now rebuilds its warm client on every spilled turn instead of never.
# That is the right side of the trade — never rebuilding meant serving the wrong
# prompt — and it is close to free in practice, because the callers still without
# a digest (the pocket specialist, out-of-tree embedders) build an isolated
# backend per run and stop it in a ``finally``, so they have no warm client to
# lose. Hashing the STABLE PREFIX instead would keep the key still, and would be
# wrong: two prompts with the same prefix and different tails would share a
# filename, and the CLI would read one of them for both.
#
# NOT SCOPED PER AGENT, deliberately. A hash of the content already separates two
# tenants' prompts, because their prompts differ; two runs that land on one
# filename have byte-identical content, so there is nothing one could learn from
# the other. An agent/session directory would multiply the surface the pruner has
# to walk without changing what any process can read (same OS user, same home).
# Windows caps the WHOLE command line at ~32,767 chars (CreateProcess).
_WINDOWS_PROMPT_SPILL_BYTES = 24_000

# POSIX caps a SINGLE argv entry at MAX_ARG_STRLEN = PAGE_SIZE * 32 = 131,072
# bytes (linux/binfmts.h). This is NOT the familiar ARG_MAX (~2 MB, the whole
# vector): the per-argument ceiling is 16x smaller and much less known, which is
# how a prompt comfortably under ARG_MAX still fails to exec. Over it, execve
# returns E2BIG and the SDK surfaces it as
#
#   Failed to start Claude Code: [Errno 7] Argument list too long: .../claude
#
# 96,000 leaves ~35 KiB for the rest of the command line — the flag itself, the
# tool allow-list, the MCP server config. A threshold set AT the kernel limit
# would still fail to exec.
_POSIX_PROMPT_SPILL_BYTES = 96_000


def _prompt_spill_threshold(os_name: str = os.name) -> int:
    """Max BYTES this platform will carry as an inline ``--system-prompt``."""
    return _WINDOWS_PROMPT_SPILL_BYTES if os_name == "nt" else _POSIX_PROMPT_SPILL_BYTES


def _prompt_must_spill(prompt: str, os_name: str = os.name) -> bool:
    """Is this prompt too long to pass inline on this platform?

    EVERY platform has a ceiling, which this got wrong until 2026-09-15: the
    check read ``os.name == "nt" and ...``, so on POSIX it answered False for a
    prompt of any size and the spill was never armed where the product actually
    runs. A cloud turn carrying a large upload (``_ATTACHMENT_TOTAL_CHARS`` is
    100,000) cleared the per-argument limit and the CLI failed to exec at all.

    MEASURED IN BYTES, because the kernel counts bytes and ``len()`` counts code
    points. These prompts are full of em dashes and box drawing — 3 bytes each
    in UTF-8 — so a prompt that reads as "40,000 chars, half the limit" can be
    120,000 bytes and refuse to exec. ``errors="replace"`` keeps a lone
    surrogate from raising here: this is a size check, and the same replacement
    is what the spill write and the SDK would do with it anyway.

    ``os_name`` is a parameter rather than a read of the global so a test can
    exercise the OTHER platform's branch without patching ``os.name``. Patching
    it looks equivalent and is not: ``pathlib`` decides at IMPORT time whether
    ``WindowsPath.__new__`` is real or a stub that raises, so a POSIX process
    with ``os.name`` forced to ``"nt"`` sends every ``Path(...)`` to the raising
    stub. The old spill test did exactly that and only CI could see it.
    """
    return len(prompt.encode("utf-8", "replace")) > _prompt_spill_threshold(os_name)


# How many spilled prompts survive a prune. They are 24k+ chars each and they
# accumulate in the user's HOME, so an unbounded pile is not an acceptable price
# for a cache key. 32 is chosen to be larger than any plausible count of live
# concurrent sessions on one desktop (a file is only load-bearing between the
# spill and the CLI's read at process start) while still bounding the directory
# at a few MB.
_SPILLED_PROMPT_KEEP = 32
_SPILLED_PROMPT_STEM = "system_prompt-"


def _spilled_prompt_path(prompt: str) -> Path:
    """The content-addressed path a given prompt spills to.

    32 hex chars (128 bits), wider than the 16 used by ``_client_cache_key`` and
    ``_plugin_digest``, because a collision means something different here. A
    collision in a CACHE key costs a wrong reuse of an in-memory object; a
    collision in this name means the write is skipped and the CLI reads ANOTHER
    prompt off disk. Cheap insurance for 16 characters of filename.
    """
    digest = hashlib.sha256(prompt.encode("utf-8", "replace")).hexdigest()[:32]
    return Path.home() / ".pocketpaw" / "runtime" / "prompts" / f"{_SPILLED_PROMPT_STEM}{digest}.md"


def _spill_prompt_to_file(prompt: str) -> Path:
    """Write ``prompt`` to its content-addressed path and return it.

    Skips the write when the file is already there: the name IS the content, so
    an existing file has the bytes we were about to write. That also removes the
    common concurrent case — two turns on the same prompt — from the race
    entirely, rather than relying on the write being atomic.

    For the uncommon case (two processes spilling the same NEW prompt at once)
    the write goes to a pid-suffixed temp and is moved into place. On Windows the
    move fails if the destination exists or is open; both mean somebody else
    landed the identical bytes first, so the failure is swallowed and their file
    is used.
    """
    path = _spilled_prompt_path(prompt)
    if path.exists():
        # Refresh the mtime: the prune keeps the NEWEST files, so a reused file
        # left at its original mtime could be pruned by a sibling's spill before
        # the CLI launched for this turn reads it.
        try:
            os.utime(path)
        except OSError:  # pragma: no cover - best effort
            pass
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    # Bytes, not write_text: text mode on Windows turns every "\n" into "\r\n",
    # so the CLI would read a prompt that differs from the one we hashed.
    tmp.write_bytes(prompt.encode("utf-8", "replace"))
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink()
        except OSError:  # pragma: no cover - best effort
            pass
        return path
    _prune_spilled_prompts(path.parent, protect=path)
    return path


def _prune_spilled_prompts(
    directory: Path, keep: int = _SPILLED_PROMPT_KEEP, *, protect: Path | None = None
) -> None:
    """Keep the ``keep`` newest spilled prompts in ``directory``; drop the rest.

    Runs only after a NEW file was created, which is the only moment the
    directory grows.

    ``protect`` is the file the caller is about to hand to the CLI, and it is
    excluded from the candidates rather than trusted to sort first. Newest-by-
    mtime would USUALLY put it at the head, but "usually" is not a property to
    hang a turn on: filesystem timestamp resolution is coarse enough that a burst
    of spills can tie, and a tie resolves to glob order. One slot of the budget is
    reserved for it so the total stays at ``keep``.

    Best-effort throughout: a prompt file that cannot be deleted (a live CLI
    holding it open on Windows) is left for the next prune and never fails the
    turn. The glob is deliberately wider than ``*.md`` so an orphaned ``.tmp``
    from a process that died mid-write is bounded too.
    """
    try:
        files = sorted(
            (p for p in directory.glob(f"{_SPILLED_PROMPT_STEM}*") if p.is_file()),
            key=lambda p: p.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:  # pragma: no cover - unreadable dir
        return
    budget = keep - 1 if protect is not None else keep
    survivors = 0
    for candidate in files:
        if candidate == protect:
            continue
        if survivors < budget:
            survivors += 1
            continue
        try:
            candidate.unlink()
        except OSError:
            logger.debug("could not prune spilled system prompt %s", candidate)


def _bundled_cli_path() -> Path | None:
    """The CLI shipped inside the ``claude-agent-sdk`` wheel, if there is one.

    The SDK prefers this binary over anything on PATH, so a machine with no
    ``claude`` installed still has a working CLI when the wheel bundles one.
    """
    try:
        import claude_agent_sdk
    except ImportError:
        return None
    name = "claude.exe" if os.name == "nt" else "claude"
    path = Path(claude_agent_sdk.__file__).parent / "_bundled" / name
    return path if path.is_file() else None


def _claude_cli_available(cli_path: str | None) -> bool:
    """Whether a CLI the SDK will actually spawn exists.

    Mirrors the SDK's own order: an explicit ``cli_path`` (the operator's
    ``claude_sdk_cli_path``) wins and must exist; otherwise the bundled CLI;
    otherwise ``claude`` on PATH.
    """
    import shutil

    if isinstance(cli_path, str) and cli_path.strip():
        return Path(cli_path).is_file()
    if _bundled_cli_path() is not None:
        return True
    return shutil.which("claude") is not None


# Upper bound on one ``connect()`` of the CLI subprocess when the setting is
# absent or unusable. ``CLAUDE_CODE_STREAM_CLOSE_TIMEOUT`` (24 h, see ``run``)
# also sets the SDK's own initialize timeout, so without this a CLI that hangs
# during start-up would hold the warm-client lock for a day.
_DEFAULT_CONNECT_TIMEOUT_S = 90.0


class _StatelessStreamCut:
    """Yielded by ``_resilient_query`` when a parse error killed the stateless
    stream, so ``run`` can tell the user the reply was cut off."""


_STATELESS_STREAM_CUT = _StatelessStreamCut()

_CLI_TOO_OLD_RE = re.compile(
    r"Claude Code (?P<version>[\d.]+) does not support this model"
    r"|version (?P<required>[\d.]+) or newer is required",
    re.IGNORECASE,
)


def _result_error_text(event: Any, *, max_turns: int | None) -> str:
    """The user-facing text for a ``ResultMessage`` with ``is_error`` set.

    ``result`` is None on the ``error_*`` subtypes (max turns, execution errors),
    which used to reach the user as the string "None". A CLI too old for the
    requested model gets one message naming the setting that fixes it.
    """
    result = getattr(event, "result", None)
    subtype = getattr(event, "subtype", "") or ""
    terminal = getattr(event, "terminal_reason", None) or ""
    if isinstance(result, str) and result.strip():
        text = result.strip()
    elif subtype == "error_max_turns" or terminal == "max_turns":
        limit = max_turns or getattr(event, "num_turns", None)
        if limit:
            return (
                f"Stopped after reaching the tool-turn limit ({limit}). "
                "Ask me to continue, or raise the limit in settings."
            )
        return "Stopped after reaching the tool-turn limit."
    else:
        errors = getattr(event, "errors", None) or []
        text = "; ".join(str(e) for e in errors if e) or (
            f"The agent run failed ({subtype})." if subtype else "The agent run failed."
        )
    return _cli_too_old_message(text) or text


def _cli_too_old_message(text: str) -> str | None:
    """One clear message for "this CLI is too old for this model", else None."""
    match = _CLI_TOO_OLD_RE.search(text or "")
    if not match:
        return None
    found = match.group("version")
    which = f"The Claude Code CLI in use ({found})" if found else "The Claude Code CLI in use"
    return (
        f"{which} is too old for the selected model. Set "
        "POCKETPAW_CLAUDE_SDK_CLI_PATH to a newer `claude` binary, or pick another model."
    )


def _mcp_result_text(content: object) -> str:
    """Extract the plain-text payload from a tool-result content envelope.

    Handles EVERY shape the CLI/SDK can put on a tool result, so a payload is
    never dropped for an unexpected wrapper:

    * a plain ``str`` (Bash ``tool_result`` content) — passes through unchanged;
    * a ``dict`` MCP envelope (``{"content": [{"type": "text", "text":
      "..."}], "is_error": bool}`` — what ``ServerToolResultBlock.content``
      carries for in-process SDK MCP tools like ``build_studio_flow``);
    * a BARE ``list`` of text blocks (``[{"type": "text", "text": ...}]`` —
      what the CLI emits for an MCP ``tool_result`` inside a ``user`` message,
      parsed by the SDK as ``ToolResultBlock.content``).

    Unwraps the text blocks so the loop's detector sees the bare payload
    (e.g. ``{"studio_flow": {...}}``) rather than the wrapper — which would
    otherwise win the brace-match and hide the marker.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        inner = content.get("content")
        if isinstance(inner, str):
            return inner
        if isinstance(inner, list):
            parts: list[str] = []
            for item in inner:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif hasattr(item, "text"):
                    parts.append(getattr(item, "text"))
            return "\n".join(parts)
        # Unknown shape (e.g. a bare is_error marker) — surface the raw JSON so
        # nothing is silently swallowed.
        try:
            import json as _json

            return _json.dumps(content)
        except Exception:  # noqa: BLE001
            return ""
    if isinstance(content, list):
        # Bare list of text blocks — items can be dicts (``{"type": "text",
        # "text": ...}``) OR dataclass blocks with a ``.text`` attribute.
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                _t = item.get("text")
                if isinstance(_t, str) and _t:
                    parts.append(_t)
            elif hasattr(item, "text"):
                _t = getattr(item, "text")
                if isinstance(_t, str) and _t:
                    parts.append(_t)
        return "\n".join(parts)
    return ""


def build_streaming_user_message(message: str, images: "tuple[ImageAttachment, ...]") -> dict:
    """Build the SDK streaming-input message carrying ``images`` alongside the text.

    Shape is the one the Agent SDK documents for streaming input mode:

        {"type": "user", "message": {"role": "user", "content": [
            {"type": "text",  "text": ...},
            {"type": "image", "source": {"type": "base64",
                                         "media_type": ..., "data": ...}},
        ]}}

    STREAMING INPUT IS NOT OPTIONAL HERE. The SDK's single-message mode
    explicitly does not support image attachments, so a turn carrying images has
    to go down the persistent-client path; sending it as a plain string is how
    an image silently becomes no image at all.

    The text block comes FIRST. The API's vision guidance puts the image before
    the question when there is one image and a question about it, but here the
    text is the user's turn and the images are what they attached to it — and
    with several images the model needs the framing before the pile.
    """
    content: list[dict] = [{"type": "text", "text": message}]
    for img in images:
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.media_type,
                    "data": base64.b64encode(img.data).decode("ascii"),
                },
            }
        )
    return {"type": "user", "message": {"role": "user", "content": content}}


async def stream_one_message(payload: dict):
    """Yield ``payload`` as the async iterable ``ClaudeSDKClient.query`` wants.

    The SDK takes an async iterable for streaming input; a bare dict is not one,
    and passing it raises rather than degrading — which is the behaviour we want
    over silently dropping the images.
    """
    yield payload


# ── Per-turn state for a warm client ──────────────────────────────────────────
# The SDK applies ``ClaudeAgentOptions`` (the system prompt included) once, at
# ``connect()``. A reused client is sent the query text and nothing else. So the
# system prompt carries only what is stable across a session, and everything
# that changes per turn rides the user message in a ``<turn-context>`` block:
# the caller's ``turn_context`` (KB hits, scope/participants, uploaded-file text,
# soul recall) and whatever stored history the live client has not seen yet.
#
# What a client has seen is its ``_HistoryWatermark``, kept ON the client object
# (``_WATERMARK_ATTR``) so a leased client carries it between backend instances.
# ``seen`` fingerprints the history entries it holds; ``pending`` is the turn it
# is serving, which the store will show as a user entry and an assistant reply
# the client already has natively. A new history must extend ``seen`` (a window
# that dropped entries off the front is fine); if it does not, something was
# edited or deleted and the client is rebuilt with the full conversation.

_WATERMARK_ATTR = "_pocketpaw_history_watermark"
# Fingerprints kept per client. The cloud window is 50 entries; the slack lets a
# window slide without the alignment search ever losing its anchor.
_WATERMARK_MAX_SEEN = 200
_HISTORY_ENTRY_CHARS = 2000
# Pending-reply marker: any assistant entry directly after the matched user
# entry. The stored reply is post-processed (ripple specs stripped, labels
# prefixed), so matching its text would miss more than it catches.
_ANY_REPLY = None


def _normalize_turn_text(text: object) -> str:
    return " ".join(str(text or "").split())


def _history_fingerprint(entry: dict) -> str:
    raw = f"{entry.get('role', 'user')}\x00{_normalize_turn_text(entry.get('content', ''))}"
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:16]


def _parse_tolerant_client(base: type) -> type:
    """``base`` (the SDK's ``ClaudeSDKClient``) with a ``receive_messages()`` that
    survives ``MessageParseError``.

    The SDK parses each frame inside its ``receive_messages()`` generator, so one
    unreadable frame kills that generator. This override re-creates it over the
    same message channel and carries on. It subclasses rather than wraps so
    logfire's patches on the base class (``__init__``, ``query``,
    ``receive_response``) still apply.
    """

    class ParseTolerantClaudeSDKClient(base):
        async def receive_messages(self):
            consecutive = 0
            while consecutive < 50:  # safety valve
                try:
                    async for msg in super().receive_messages():
                        consecutive = 0
                        yield msg
                    return
                except Exception as exc:
                    if "MessageParseError" not in type(exc).__name__:
                        raise
                    consecutive += 1
                    logger.debug(
                        "Skipping unreadable SDK event (retry %d), re-creating iterator: %s",
                        consecutive,
                        exc,
                    )
            logger.error("Too many consecutive MessageParseErrors — aborting stream")

    return ParseTolerantClaudeSDKClient


class _HistoryWatermark:
    """The stored history a live client already holds (see the block above)."""

    __slots__ = ("seen", "pending")

    def __init__(self) -> None:
        self.seen: list[str] = []
        self.pending: list[tuple[str, str | None]] = []


def _pending_matches(entry: dict, want: tuple[str, str | None]) -> bool:
    role, text = want
    if entry.get("role", "user") != role:
        return False
    if text is _ANY_REPLY:
        return True
    got = _normalize_turn_text(entry.get("content", ""))
    # The sent and stored texts differ at the edges: the group bridge appends an
    # "Attached files" block to what it sends and prefixes the sender's name to
    # what it replays. So the opening of either, found inside the other, counts.
    if got == text:
        return True
    head_sent, head_got = text[:200], got[:200]
    return bool(head_sent and head_got) and (head_sent in got or head_got in text)


def _client_has_served(client: Any) -> bool:
    """Has ``client`` been sent a turn? A fresh connect installs an empty
    watermark; any sent turn fills it. A client whose watermark was dropped
    (``_forget_history``) or never set counts as served, so a prewarm leaves it
    alone rather than guess."""
    wm = getattr(client, _WATERMARK_ATTR, None)
    if not isinstance(wm, _HistoryWatermark):
        return True
    return bool(wm.seen or wm.pending)


def _unseen_history(wm: _HistoryWatermark, history: list[dict]) -> list[dict] | None:
    """The entries of ``history`` the client behind ``wm`` has not seen, or
    ``None`` when ``history`` no longer extends what it saw (edit / delete)."""
    fps = [_history_fingerprint(e) for e in history]
    start = 0
    if wm.seen:
        for i in range(len(wm.seen)):
            tail = wm.seen[i:]
            if fps[: len(tail)] == tail:
                start = len(tail)
                break
        else:
            return None
    unseen: list[dict] = []
    p = 0
    for entry in history[start:]:
        if p < len(wm.pending) and _pending_matches(entry, wm.pending[p]):
            p += 1
            continue
        unseen.append(entry)
    return unseen


def _render_history(entries: list[dict]) -> str:
    lines = []
    for msg in entries:
        role = str(msg.get("role", "user")).capitalize()
        content = str(msg.get("content", ""))
        if len(content) > _HISTORY_ENTRY_CHARS:
            content = content[:_HISTORY_ENTRY_CHARS] + "..."
        lines.append(f"**{role}**: {content}")
    return "\n".join(lines)


def compose_turn_message(
    message: str,
    *,
    turn_context: str = "",
    history: list[dict] | None = None,
    history_is_delta: bool = False,
) -> str:
    """The query text for one turn: a ``<turn-context>`` block, then the user's
    message. Returns ``message`` unchanged when there is nothing to add."""
    parts: list[str] = []
    if history:
        heading = (
            "# Messages since your last reply" if history_is_delta else "# Recent Conversation"
        )
        parts.append(f"{heading}\n{_render_history(history)}")
    if turn_context and turn_context.strip():
        parts.append(turn_context.strip())
    if not parts:
        return message
    body = "\n\n".join(parts)
    return (
        "<turn-context>\n"
        "Context for the message below, refreshed on every turn. It replaces any "
        "earlier turn-context and is not something the user typed.\n\n"
        f"{body}\n"
        "</turn-context>\n\n"
        f"{message}"
    )


class ClaudeSDKBackend(BaseAgentBackend):
    """Claude Agent SDK backend — the recommended default.

    Provides all built-in tools (Bash, Read, Write, Edit, Glob, Grep,
    WebSearch, WebFetch), streaming responses, PreToolUse hooks for
    security, and MCP server support.

    Requires: pip install claude-agent-sdk
    """

    _TOOL_POLICY_MAP: dict[str, str] = {
        # NOTE: is_tool_allowed() returns True for any key not explicitly
        # denied when the profile is 'full' (empty _allowed_set). For
        # restrictive profiles ('minimal', 'coding') it returns False for
        # any key absent from the resolved allow set. 'Agent' therefore
        # MUST have an explicit entry here; without it, any registered
        # subagent (general-purpose claude_agent_sdk capability) would
        # be silently blocked for every non-full profile. Mapped to
        # 'shell' because invoking a subagent has comparable privilege
        # scope to running a shell command — the gating is deliberately
        # conservative.
        "Agent": "shell",
        "Bash": "shell",
        "Read": "read_file",
        "Write": "write_file",
        "Edit": "edit_file",
        "Glob": "list_dir",
        "Grep": "shell",
        "WebSearch": "browser",
        "WebFetch": "browser",
        "Skill": "skill",
    }

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="claude_agent_sdk",
            display_name="Claude Agent SDK",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MCP
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            builtin_tools=[
                "Bash",
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
                "WebSearch",
                "WebFetch",
            ],
            tool_policy_map=ClaudeSDKBackend._TOOL_POLICY_MAP,
            required_keys=["anthropic_api_key"],
            supported_providers=[
                "anthropic",
                "ollama",
                "openrouter",
                "openai_compatible",
                "litellm",
            ],
        )

    def __init__(self, settings: Settings, policy: ToolPolicy | None = None):
        self.settings = settings
        self._stop_flag = False
        self._sdk_available = False
        self._cli_available = False  # Whether the `claude` CLI binary is installed
        self._cwd = settings.file_jail_path  # Default working directory
        # ``policy`` lets a caller (AgentPool) inject a per-agent
        # ToolPolicy — e.g. one that opts the agent into the planner MCP
        # server. When omitted, build the process-wide policy from
        # settings, which is the behaviour every other caller relies on.
        self._policy = policy or ToolPolicy(
            profile=settings.tool_profile,
            allow=settings.tools_allow,
            deny=settings.tools_deny,
        )

        # Persistent client — reuses subprocess across messages.
        # _client_in_use prevents concurrent queries on the same client
        # (cross-session messages fall back to stateless query()).
        self._client = None
        self._client_options_key: str | None = None
        self._client_in_use = False
        # The session the live client was connected for (prewarm leaves a
        # session's served client alone whatever its key) and the token of the
        # run holding ``_client_in_use`` (only that run may release it).
        self._client_session_key: str | None = None
        self._lease_token: object | None = None
        # One entry per in-flight ``run``: its session and whether stop() hit it.
        # Stop is per run, so a new run cannot clear a pending stop of another.
        self._active_runs: dict[object, _RunState] = {}
        # Serializes the connect-or-reuse critical section in
        # ``_get_or_create_client`` (feat/claude-sdk-prewarm). ``prewarm`` runs
        # CONCURRENTLY with the first ``run`` (fired as a background task before
        # the turn), and ``_get_or_create_client`` ``await``s ``disconnect()`` /
        # ``connect()`` — without this lock the run could enter the section while
        # prewarm is mid-connect and the two would race to create / evict the
        # subprocess (double connect, or the run throwing away the half-built
        # prewarmed client). With the lock, whichever arrives second sees the
        # other's finished client under a MATCHING key and reuses it — which is
        # exactly the win. Lazily created so a backend built off-loop is safe.
        self._client_lock: asyncio.Lock | None = None
        # Plugin-identity digest of the currently-live warm client, and a map of
        # plugin_digest -> materialized per-run skills dir (fix/claude-sdk-warm-
        # client-skills). A skill run now REUSES the warm subprocess instead of
        # re-spawning, so the materialized dir it was connected with must outlive
        # the per-run finally — the subprocess holds that path from its first
        # connect(). The dir is keyed on the stable plugin IDENTITY (sorted skill
        # names + bundled flag), never the throwaway mkdtemp PATH, so two turns
        # with the same skills find the same cached dir. Ownership: the dir is
        # removed only when its warm client is evicted (_get_or_create_client) or
        # on cleanup() — NOT by a normal per-run finally.
        self._client_plugin_digest: str = ""
        self._skills_dir_by_digest: dict[str, Path] = {}

        # Per-run subprocess env injected by the pocket-specialist
        # runtime via ``attach_subprocess_env`` (PR #1222 R1 Blocker 1).
        # Merged into ``options_kwargs["env"]`` at spawn time so the
        # Claude Code subprocess inherits per-request tenancy values
        # (``POCKETPAW_WORKSPACE_ID`` / ``POCKETPAW_USER_ID`` /
        # ``POCKETPAW_INTERNAL_TOKEN``) without the runtime mutating
        # the parent's ``os.environ`` — which would race across
        # concurrent requests.
        self._extra_subprocess_env: dict[str, str] = {}

        # SDK imports (set during initialization)
        self._query = None
        self._ClaudeSDKClient = None
        self._ClaudeAgentOptions = None
        self._HookMatcher = None
        self._AssistantMessage = None
        self._UserMessage = None
        self._SystemMessage = None
        self._ResultMessage = None
        self._TextBlock = None
        self._ToolUseBlock = None
        self._ToolResultBlock = None
        self._ServerToolUseBlock = None
        self._ServerToolResultBlock = None
        self._StreamEvent = None

        self._initialize()

    def get_tool_policy(self) -> ToolPolicy:
        return self._policy

    def set_tool_policy(self, policy: ToolPolicy) -> None:
        self._policy = policy

    def attach_subprocess_env(self, env: dict[str, str]) -> None:
        """Merge ``env`` into the Claude Code subprocess env at next spawn.

        The pocket-specialist runtime calls this once per isolated run
        (PR #1222 R1 Blocker 1) to ship per-request tenancy values
        (``POCKETPAW_WORKSPACE_ID`` / ``POCKETPAW_USER_ID`` /
        ``POCKETPAW_INTERNAL_TOKEN``) into the subprocess without
        mutating the parent process's ``os.environ`` — which would
        race across concurrent requests sharing the same parent.

        ``run()`` merges this dict into ``options_kwargs["env"]`` after
        the LLM-provider env (``ANTHROPIC_API_KEY`` /
        ``CLAUDE_CODE_OAUTH_TOKEN``) so an attached value cannot
        accidentally clobber the auth key. Each call REPLACES the
        previous dict — an isolated backend instance is per-run, so
        the new run wants a fresh tenancy, not a merge with stale.
        """
        # Defensive copy so the caller can mutate their dict without
        # corrupting the backend's stash.
        self._extra_subprocess_env = dict(env)

    def _initialize(self) -> None:
        """Initialize the Claude Agent SDK with all imports."""
        try:
            # Core SDK imports
            # Message type imports
            # Content block imports
            from claude_agent_sdk import (
                AssistantMessage,
                ClaudeAgentOptions,
                ClaudeSDKClient,
                HookMatcher,
                ResultMessage,
                ServerToolResultBlock,
                ServerToolUseBlock,
                SystemMessage,
                TextBlock,
                ToolResultBlock,
                ToolUseBlock,
                UserMessage,
                query,
            )

            # Store references
            self._query = query
            self._ClaudeSDKClient = _parse_tolerant_client(ClaudeSDKClient)
            self._ClaudeAgentOptions = ClaudeAgentOptions
            self._HookMatcher = HookMatcher
            self._AssistantMessage = AssistantMessage
            self._UserMessage = UserMessage
            self._SystemMessage = SystemMessage
            self._ResultMessage = ResultMessage
            self._TextBlock = TextBlock
            self._ToolUseBlock = ToolUseBlock
            self._ToolResultBlock = ToolResultBlock
            self._ServerToolUseBlock = ServerToolUseBlock
            self._ServerToolResultBlock = ServerToolResultBlock

            # StreamEvent for token-by-token streaming (optional)
            try:
                from claude_agent_sdk import StreamEvent

                self._StreamEvent = StreamEvent
            except ImportError:
                self._StreamEvent = None
                logger.info("StreamEvent not available - coarse-grained streaming only")

            self._sdk_available = True

            # Check that a CLI the SDK will spawn exists: the configured
            # cli_path, else the wheel's bundled CLI, else `claude` on PATH.
            if _claude_cli_available(getattr(self.settings, "claude_sdk_cli_path", None)):
                self._cli_available = True
                logger.info("✓ Claude Agent SDK ready ─ cwd: %s", self._cwd)
            else:
                logger.warning(
                    "⚠️ Claude Code CLI not found (no claude_sdk_cli_path, no bundled "
                    "CLI, nothing on PATH). "
                    "Install with: npm install -g @anthropic-ai/claude-code "
                    "and set ANTHROPIC_API_KEY, or switch to a different backend in Settings."
                )

        except ImportError as e:
            logger.warning("⚠️ Claude Agent SDK not installed ─ pip install claude-agent-sdk")
            logger.debug("Import error: %s", e)
            self._sdk_available = False
        except Exception as e:
            logger.error(f"❌ Failed to initialize Claude Agent SDK: {e}")
            self._sdk_available = False

    def _resolve_cwd(self) -> Path:
        """Resolve the agent's working directory for THIS run.

        NOTE: the per-tenant cwd jail + fail-closed live ONLY in this backend.
        Other backends (codex_cli, deep_agents, …) receive workspace tenancy via
        ``subprocess_env`` but NOT the cwd jail — a non-``claude_agent_sdk`` cloud
        agent would run in ``file_jail_path``. Cloud chat defaults to this
        backend; see ART-2's report for the residual non-claude gap.

        Defaults to ``settings.file_jail_path`` (the OSS / dedicated behavior,
        unchanged). When an EE ``pocketpaw.agent_extensions`` provider supplies
        an ``agent_cwd`` (the cloud product), its result wins — a
        per-workspace/session jail that keeps each tenant's file operations
        isolated instead of co-mingling in the shared home dir.

        A provider that RAISES (a multi-tenant cloud run with no resolvable
        workspace) is propagated, NOT swallowed: that fail-closed is the whole
        point — we must never silently fall back to ``~`` and let one tenant's
        files land on another's. Resolved per-run (not cached on the instance)
        so a single warm backend serving multiple sessions reads each session's
        own jail; the warm-client cache key folds in the resolved cwd (ART-2), so
        a changed cwd rebuilds the subprocess with its correct working directory.
        """
        from pocketpaw._registry import providers as _ext_providers

        for ext in _ext_providers("pocketpaw.agent_extensions"):
            resolver = getattr(ext, "agent_cwd", None)
            if resolver is None:
                continue
            resolved = resolver()  # may raise (fail-closed) — let it propagate
            if resolved:
                return Path(resolved)
        return self.settings.file_jail_path

    def _is_dangerous_command(self, command: str) -> str | None:
        """Check if a command matches dangerous patterns.

        Uses both regex patterns (for complex matching) and substring
        patterns (for literal matches).

        Args:
            command: Command string to check

        Returns:
            The matched pattern if dangerous, None otherwise
        """
        # Primary: regex matching (catches obfuscation, spacing tricks)
        from pocketpaw.security.rails import COMPILED_DANGEROUS_PATTERNS

        for pattern in COMPILED_DANGEROUS_PATTERNS:
            if pattern.search(command):
                return pattern.pattern

        # Secondary: substring matching (catches simple literal fragments).
        # is_substring_blocked() applies .lower() on both sides so that
        # uppercase variants like "SUDO RM" are caught (OWASP A01).
        return is_substring_blocked(command)

    # Patterns that indicate an OS-level "open file" command.
    _FILE_OPEN_PATTERNS = [
        re.compile(
            r"(?:^|&&|\|\||;)\s*start\s+(?:\"\"?\s*)?(.+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|&&|\|\||;)\s*explorer(?:\.exe)?\s+(.+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|&&|\|\||;)\s*xdg-open\s+(.+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|&&|\|\||;)\s*open\s+(?!-a)(.+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|&&|\|\||;)\s*(?:powershell(?:\.exe)?\s+(?:-[Cc]ommand\s+)?)?"
            r"Invoke-Item\s+(.+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|&&|\|\||;)\s*cmd\s+/[cC]\s+start\s+(?:\"\"?\s*)?(.+)",
            re.IGNORECASE,
        ),
    ]

    def _is_file_open_command(self, command: str) -> str | None:
        """Detect OS-level file-open commands and extract the file path.

        Returns the file path if the command is an OS open, or None.
        """
        stripped = command.strip()
        for pattern in self._FILE_OPEN_PATTERNS:
            m = pattern.search(stripped)
            if m:
                path = m.group(1).strip().strip("'\"")
                # Skip if it's opening a URL (http/https) — not a local file
                if path.startswith(("http://", "https://")):
                    return None
                return path
        return None

    async def _block_dangerous_hook(self, input_data, tool_use_id: str | None, context) -> dict:
        """PreToolUse hook to block dangerous commands.

        This hook is called before any Bash command is executed.
        Returns a deny decision for dangerous commands.

        The callback must be resilient — an unhandled exception here
        tears down the entire CLI stream.

        Args:
            input_data: PreToolUseHookInput (TypedDict with tool_name,
                tool_input, tool_use_id, etc.)
            tool_use_id: Match group or None
            context: HookContext from the SDK

        Returns:
            Empty dict to allow, or deny decision dict to block
        """
        try:
            tool_name = input_data.get("tool_name", "")
            tool_input = input_data.get("tool_input", {})

            # Only check Bash commands
            if tool_name != "Bash":
                return {}

            command = str(tool_input.get("command", ""))

            matched = self._is_dangerous_command(command)
            if matched:
                # Scrub before logging — dangerous commands routinely carry
                # Authorization headers or API keys inline (#893).
                from pocketpaw.security.scrub import scrub_command

                safe_command = scrub_command(command)
                logger.warning("🛑 BLOCKED dangerous command: %s", safe_command[:100])
                logger.warning("   └─ Matched pattern: %s", matched)
                try:
                    from pocketpaw.security.audit import (
                        AuditEvent,
                        AuditSeverity,
                        get_audit_logger,
                    )

                    get_audit_logger().log(
                        AuditEvent.create(
                            severity=AuditSeverity.ALERT,
                            actor="agent",
                            action="dangerous_command_blocked",
                            target="bash",
                            status="block",
                            command=safe_command[:500],
                            matched_pattern=matched,
                        )
                    )
                except Exception:
                    pass  # Don't let audit failure break the hook
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"PocketPaw security: '{matched}' pattern is blocked"
                        ),
                    }
                }

            # Redirect OS file-open commands to the in-app viewer.
            # Matches: start, explorer, xdg-open, open (macOS), Invoke-Item
            redirect = self._is_file_open_command(command)
            if redirect:
                logger.info("↩ Redirecting OS open command to open_in_explorer: %s", redirect)
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "Do not use OS commands to open files. "
                            "Instead, use the PocketPaw in-app viewer:\n"
                            "python -m pocketpaw.tools.cli open_in_explorer "
                            f'\'{{"path": "{redirect}", "action": "view"}}\''
                        ),
                    }
                }

            logger.debug(f"✅ Allowed command: {command[:50]}...")
            return {}
        except Exception as e:
            logger.error(f"Hook callback error (blocking command as precaution): {e}")
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "Safety hook encountered an internal error — "
                        "blocking command as a precaution"
                    ),
                }
            }

    def _extract_text_from_message(self, message: Any) -> str:
        """Extract text content from an AssistantMessage.

        Args:
            message: AssistantMessage with content blocks

        Returns:
            Concatenated text from all TextBlocks
        """
        if not hasattr(message, "content"):
            return ""

        content = message.content
        if content is None:
            return ""

        if isinstance(content, str):
            return content

        if isinstance(content, list):
            texts = []
            for block in content:
                # Check if it's a TextBlock
                if self._TextBlock and isinstance(block, self._TextBlock):
                    if hasattr(block, "text") and block.text:
                        texts.append(block.text)
                # Fallback: check for text attribute
                elif hasattr(block, "text") and isinstance(block.text, str):
                    texts.append(block.text)
            return "".join(texts)

        return ""

    def _extract_tool_info(self, message: Any) -> list[dict]:
        """Extract tool use information from an AssistantMessage.

        Args:
            message: AssistantMessage with content blocks

        Returns:
            List of tool use dicts with name and input
        """
        if not hasattr(message, "content") or message.content is None:
            return []

        tools = []
        for block in message.content:
            if self._ToolUseBlock and isinstance(block, self._ToolUseBlock):
                tools.append(
                    {
                        "name": getattr(block, "name", "unknown"),
                        "input": getattr(block, "input", {}),
                        "id": getattr(block, "id", ""),
                    }
                )
            elif hasattr(block, "name") and hasattr(block, "input"):
                # Fallback check (also catches ``ServerToolUseBlock`` — the
                # MCP-tool announcement on the SDK transcript).
                tools.append(
                    {
                        "name": block.name,
                        "input": block.input,
                        "id": getattr(block, "id", ""),
                    }
                )
        return tools

    # MCP servers whose functionality is already provided by Claude Code's
    # built-in WebSearch tool.  Passing these causes duplicate/conflicting
    # search behaviour and wastes context on redundant tool definitions.
    _BUILTIN_SEARCH_MCP_NAMES = frozenset(
        {
            "brave-search",
            "tavily-search",
            "exa-search",
            "Brave Search",
            "Tavily Search",
            "Exa Search",
        }
    )

    def _get_mcp_servers(self, surface_grants: frozenset[str] = frozenset()) -> dict[str, dict]:
        """Load enabled MCP server configs, filtered by tool policy.

        ``surface_grants`` is the run's ``allow_sdk_tools``: a server in
        ``SURFACE_SCOPED_MCP_SERVERS`` registers only when it names one of that
        server's tool ids.

        Returns a dict keyed by server name.  The SDK supports three
        transport types: stdio, sse, and http — each with its own
        TypedDict shape (McpStdioServerConfig, McpSSEServerConfig,
        McpHttpServerConfig).

        Web search MCP servers (Tavily, Brave, Exa) are excluded because
        Claude Code already provides a built-in WebSearch tool.
        """
        try:
            from pocketpaw.mcp.config import load_mcp_config
        except ImportError:
            return {}

        configs = load_mcp_config()
        servers: dict[str, dict] = {}
        for cfg in configs:
            if not cfg.enabled:
                continue
            if cfg.name in self._BUILTIN_SEARCH_MCP_NAMES:
                logger.info(
                    "MCP server '%s' skipped — Claude Code has built-in WebSearch", cfg.name
                )
                continue
            if not self._policy.is_mcp_server_allowed(cfg.name):
                logger.info("MCP server '%s' blocked by tool policy", cfg.name)
                continue

            if cfg.transport == "stdio":
                entry: dict = {"type": "stdio", "command": cfg.command}
                if cfg.args:
                    entry["args"] = cfg.args
                if cfg.env:
                    entry["env"] = cfg.env
            elif cfg.transport in _HTTP_TRANSPORTS:
                if not cfg.url:
                    logger.warning("MCP server '%s' (%s) has no url", cfg.name, cfg.transport)
                    continue
                # Claude SDK expects "http" for both SSE and streamable-http
                sdk_type = "http" if cfg.transport == "streamable-http" else cfg.transport
                entry = {"type": sdk_type, "url": cfg.url}
                if cfg.env:
                    entry["headers"] = cfg.env
            else:
                logger.debug("Skipping MCP '%s' (unknown transport=%s)", cfg.name, cfg.transport)
                continue

            servers[cfg.name] = entry

        # In-process MCP server: ripple widget-spec lookups (get_widget_spec,
        # get_inline_widget_help). Pure core — the ripple manifest / inline
        # catalog have no cloud dependency, so this server is always built
        # locally. Why in-process MCP at all: the rippleSpec.ui tree can be
        # tens of KB, which would blow the Windows CLI command-line limit if
        # embedded in the system prompt.
        try:
            from pocketpaw.agents.sdk_mcp_widgets import build_widgets_context_server

            widgets_server = build_widgets_context_server()
            if widgets_server is not None:
                name, cfg_entry = widgets_server
                if self._policy.is_mcp_server_allowed(name):
                    servers[name] = cfg_entry
                else:
                    logger.info("MCP server '%s' blocked by tool policy", name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pocketpaw_widgets MCP server not registered: %s", exc)

        # In-process MCP server: the atlas OS self-model (atlas_search,
        # atlas_describe). Pure core — the hand-authored seed ships as
        # packaged data (pocketpaw.atlas), no cloud dependency. Lets the
        # agent query what the OS is and can do (paw meanings of Pocket /
        # Instinct / Fabric / ...) before guessing from LLM priors.
        #
        # AT-5: the server carries a per-run entitlement/availability
        # provider so atlas answers reflect the CALLING workspace, not a
        # global view. Scope resolution lives in ``_tenant_scope_key``
        # (per-run env, fail-closed on a blank workspace id, never a
        # process-global mode flag — repo lesson #1570/#1574).
        #
        # Honesty note: under a ``ws:<id>`` scope the availability read is
        # plumbed but currently INERT — the cloud connector state store does
        # not enumerate tenant rows, so cloud runs conservatively report
        # every connector unavailable until the EE availability provider
        # lands. The OSS ``"default"`` scope reads live file-store state.
        #
        # AT-7: a real ``ws:<id>`` scope ALSO gets a live Fabric
        # introspector (EE workspace ontology — entity types, properties,
        # links) so atlas can answer "what entity types exist in THIS
        # workspace". Built per run with the run's workspace id, never a
        # process-global. The builder degrades to None (fail-closed, DEBUG
        # log) when pocketpaw_ee isn't importable or construction fails;
        # the "default" scope and the blank-id sentinel get NO introspector
        # (the OSS JSONFileFabricRegistry needs an explicit registry file —
        # there is no ambient OSS fabric registry to wire today).
        try:
            from pocketpaw.agents.sdk_mcp_atlas import build_atlas_context_server
            from pocketpaw.atlas.overlay import (
                DefaultEntitlementProvider,
                build_role_aware_provider,
            )

            tenant_scope = self._tenant_scope_key()
            fabric_introspector = None
            atlas_provider: Any = DefaultEntitlementProvider(scope_key=tenant_scope)
            if tenant_scope.startswith("ws:") and tenant_scope != _SENTINEL_TENANT_SCOPE:
                from pocketpaw.atlas.fabric import build_workspace_fabric_introspector

                fabric_introspector = build_workspace_fabric_introspector(
                    tenant_scope[len("ws:") :]
                )
                # WA-3: a real ws:<id> run gets the role-aware provider so
                # non-admins don't see admin capabilities in atlas. It resolves
                # the caller's role at query time and grants ``role:*`` entries
                # by role tier; a None return (OSS install / EE provider not
                # importable / construction failure) leaves the fail-closed
                # default in place — which HIDES every role-gated entry, so admin
                # capabilities never leak without the role-aware provider.
                role_aware = build_role_aware_provider(tenant_scope)
                if role_aware is not None:
                    atlas_provider = role_aware
            atlas_server = build_atlas_context_server(
                provider=atlas_provider,
                introspector=fabric_introspector,
            )
            if atlas_server is not None:
                name, cfg_entry = atlas_server
                if self._policy.is_mcp_server_allowed(name):
                    servers[name] = cfg_entry
                else:
                    logger.info("MCP server '%s' blocked by tool policy", name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pocketpaw_atlas MCP server not registered: %s", exc)

        # In-process MCP server: /studio flow building (build_studio_flow).
        # The agent scaffolds a node graph (model → text → [picture] →
        # image/video → [toolcall] → output) from a natural-language goal;
        # the loop fans a dedicated ``studio_flow`` SSE event so paw-enterprise
        # materialises the canvas. Pure core — no cloud dependency. Mirrors the
        # atlas/widgets wiring below it.
        try:
            from pocketpaw.agents.sdk_mcp_studio import build_studio_context_server

            studio_server = build_studio_context_server()
            if studio_server is not None:
                name, cfg_entry = studio_server
                if self._policy.is_mcp_server_allowed(name):
                    servers[name] = cfg_entry
                else:
                    logger.info("MCP server '%s' blocked by tool policy", name)
        except Exception as exc:  # noqa: BLE001
            logger.debug("pocketpaw_studio MCP server not registered: %s", exc)

        # EE-provided in-process MCP servers — cloud pocket context, Mission
        # Control tasks, the planner, and the pocket specialist. Discovered
        # via the ``pocketpaw.mcp_servers`` entry-point (see
        # pocketpaw_ee.extensions); an OSS install registers none and this
        # loop is a no-op.
        #
        # Most of these servers are ambient: allow-by-default policy lets
        # them register on every agent run. The planner is the exception —
        # it is *opt-in, not ambient*. Most agent runs never plan a
        # project, and carrying the ``plan_project`` schema in every
        # context is dead weight. For a server in ``OPT_IN_MCP_SERVERS``
        # the loop uses ``is_mcp_server_explicitly_allowed``, which
        # registers it only when the policy's ``mcp_servers_allow`` set
        # names it. AgentPool builds that set from the cloud agent's
        # ``tools`` field — an agent enables the planner by listing the
        # bare token ``pocketpaw_planner`` there. Deny still wins.
        from pocketpaw._registry import providers as _ext_providers

        scoped_off = ungranted_surface_servers(surface_grants)
        for provider in _ext_providers("pocketpaw.mcp_servers"):
            provider_name = type(provider).__name__
            try:
                built = provider.build_server()
            except Exception as exc:  # noqa: BLE001
                # 2026-05-28 (#FU-F): a stale editable install + dashboard
                # restart left CloudForesightMcpProvider unable to import its
                # SDK server, which silently swallowed the failure at DEBUG.
                # The diagnostic took 30+ minutes because the failure mode was
                # invisible. Promote to WARNING + include exception type +
                # module path so the operator sees it on startup.
                logger.warning(
                    "MCP server provider %s failed to build: %s: %s",
                    provider_name,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                continue
            if built is None:
                continue
            name, cfg_entry = built
            if name in scoped_off:
                continue
            if name in OPT_IN_MCP_SERVERS:
                if not self._policy.is_mcp_server_explicitly_allowed(name):
                    logger.debug(
                        "MCP server '%s' not registered — agent has not opted "
                        "in (add '%s' to the agent's tools)",
                        name,
                        name,
                    )
                    continue
            elif not self._policy.is_mcp_server_allowed(name):
                logger.info("MCP server '%s' blocked by tool policy", name)
                continue
            servers[name] = cfg_entry

        # Startup summary — operators check this on dashboard restart to confirm
        # their install picked up the expected entry-point set.
        if servers:
            logger.info("MCP servers registered: %s", ", ".join(sorted(servers)))
        else:
            logger.info("No MCP servers registered.")

        return servers

    def _collect_mcp_tool_ids(self) -> list[str]:
        """Collect the in-process MCP tool ids to add to the SDK allowlist.

        An MCP tool is only callable if its id is on the allowlist. This
        gathers the core ripple widget-spec + atlas self-model ids plus every cloud
        ``pocketpaw.mcp_servers`` provider's ``tool_ids()`` (which includes
        the ``pocketpaw_pocket`` server's writable ``add_widget`` tool).

        Opt-in servers (the planner) are skipped unless the policy opts
        them in, mirroring the registration gate in ``_get_mcp_servers``.
        Tool ids follow the ``mcp__<server>__<tool>`` convention, so the
        server name is the segment between the first and second ``__``.
        """
        from pocketpaw._registry import providers as _ext_providers
        from pocketpaw.agents.sdk_mcp_atlas import ATLAS_TOOL_IDS
        from pocketpaw.agents.sdk_mcp_studio import STUDIO_TOOL_IDS
        from pocketpaw.agents.sdk_mcp_widgets import WIDGET_TOOL_IDS

        ids: list[str] = list(WIDGET_TOOL_IDS) + list(ATLAS_TOOL_IDS) + list(STUDIO_TOOL_IDS)
        for provider in _ext_providers("pocketpaw.mcp_servers"):
            try:
                tool_ids = list(provider.tool_ids())
            except Exception as exc:  # noqa: BLE001
                logger.debug("MCP provider tool ids not added to allowlist: %s", exc)
                continue
            for tool_id in tool_ids:
                parts = tool_id.split("__")
                server = parts[1] if len(parts) >= 3 and parts[0] == "mcp" else ""
                if server in OPT_IN_MCP_SERVERS and not (
                    self._policy.is_mcp_server_explicitly_allowed(server)
                ):
                    continue
                ids.append(tool_id)

        # External stdio/http MCP servers (``~/.pocketpaw/mcp_servers.json`` via
        # ``load_mcp_config``) are registered with the SDK in ``_get_mcp_servers``
        # but have no in-process ``tool_ids()`` provider — their tool names are
        # only known after the SDK connects to the server. Without an allowlist
        # entry the SDK refuses every call (e.g. a deployment's ``fabric`` server
        # exposing ``fabric_query`` / ``fabric_stats`` was registered yet
        # uncallable). Allow each enabled external server wholesale with a bare
        # ``mcp__<server>`` entry — the Claude Code permission convention that
        # admits all of a server's tools — gated by the same tool policy that
        # gates registration.
        try:
            from pocketpaw.mcp.config import load_mcp_config

            for cfg in load_mcp_config():
                if not cfg.enabled:
                    continue
                if cfg.name in self._BUILTIN_SEARCH_MCP_NAMES:
                    continue
                if not self._policy.is_mcp_server_allowed(cfg.name):
                    continue
                ids.append(f"mcp__{cfg.name}")
        except Exception as exc:  # noqa: BLE001
            logger.debug("External MCP server allowlist not added: %s", exc)

        return ids

    # Section markers that ``AgentPool.run`` appends to the system prompt
    # AFTER the authoritative behavioral instructions. Everything from the
    # first marker onward is per-turn and volatile (query-specific KB hits,
    # soul-memory recall, injected conversation history on a cold subprocess),
    # so it MUST NOT participate in the persistent-client cache key — otherwise
    # every turn would needlessly tear down and rebuild the subprocess. The
    # behavioral prefix BEFORE these markers carries the home-pocket backend
    # summary, which is exactly the mutable state we want the key to track.
    # Stored WITH the leading blank line, and matched at a block BOUNDARY: the
    # separator is present when something precedes the block and absent when the
    # block opens the prompt, and both are the same block. Matching the literal
    # alone was correct only while the legacy string assembly appended the
    # knowledge wrapper unconditionally — it always emitted the "\n\n" even with
    # nothing before it. The prompt assembler (``pocketpaw.prompt``) joins layers
    # instead, so a block that is FIRST starts at index 0 with no separator at
    # all; a marker-blind ``find`` misses it and the whole volatile block lands
    # in the key, rebuilding the warm subprocess every turn. That is not a
    # transitional quirk: PA-3/PA-4/PA-8 give these blocks their own layers,
    # where the join means their text NEVER carries a leading "\n\n".
    _VOLATILE_PROMPT_MARKERS = (
        "\n\n## Your Knowledge Base",
        "\n\n## Relevant Past Memories",
        "\n\n# Recent Conversation",
    )

    # The soul "# Key Knowledge" block is a MID-prompt volatile section, unlike
    # the tail markers above. ``AgentPool._assemble_system_prompt`` splices it in
    # RIGHT AFTER the stable soul identity via
    # ``f"{system_prompt}\n\n# Key Knowledge\n{knowledge_lines}"`` (pool.py:243-245),
    # where ``knowledge_lines = "\n".join(f"- {k}" for k in ctx.knowledge)`` — so
    # every item is a ``- ``-prefixed line and the block sits EARLY (char ~1.4k of
    # a ~44k prefix), sandwiched between the stable ``ctx.identity`` before it and
    # the stable authoritative ``instructions`` (ripple LAW) + ``<runtime-identity>``
    # + tool docs after it. Its ``ctx.knowledge`` items are ALL volatile soul state
    # (self-image confidences, an incrementing bond level, a growing memory count,
    # recalled semantic/procedural memories — see ``soul/_bridge.py``), NONE of
    # them behavioral instructions. Live instrumentation proved two consecutive
    # turns' prefixes differed by exactly the incrementing "Bond level" / "Memories"
    # digits inside this block, so the prefix digest changed every turn and the
    # warm subprocess was rebuilt every turn (warm reuse NEVER fired).
    #
    # Because it is mid-prompt, the tail cut above cannot touch it, and adding it
    # to ``_VOLATILE_PROMPT_MARKERS`` (a tail cut) would strip ~97% of the real
    # behavioral prefix (over-strip — a real ``instructions``/identity change would
    # then wrongly reuse a stale client). So we EXCISE it IN PLACE, keeping every
    # stable byte before and after it.
    _SOUL_KNOWLEDGE_BLOCK_HEADER = "\n\n# Key Knowledge\n"

    @classmethod
    def _strip_soul_knowledge_block(cls, text: str) -> str:
        """Surgically remove the mid-prompt soul "# Key Knowledge" block.

        Structure-anchored, not blank-line-anchored: pool.py builds the block as
        the header followed by a run of ``- ``-prefixed item lines. A recalled
        memory's ``content`` can itself contain newlines (soul/_bridge.py:106),
        so an item may WRAP onto continuation lines — those are absorbed into the
        block as long as they are not separated from the items by a blank line.
        The block ENDS at the first blank line whose following line does NOT
        resume ``- `` items: that blank line is the ``\\n\\n`` join before the
        next stable section (the authoritative ``instructions`` / runtime docs).
        This deliberately terminates at a blank line rather than swallowing text
        up to a heuristic anchor, so a stable section that follows in plain prose
        (e.g. the ripple LAW ``instructions``) is NEVER over-stripped.

        Robustness:
        * ``rfind`` + a ``- `` item guard select the MACHINE-built block, so a
          user-authored "# Key Knowledge" heading in persona/identity prose (no
          ``- `` items under it) is left untouched and still keys the prefix.
        * Block as the LAST section (no trailing blank line) → removed to EOS.
        * Header absent (empty ``ctx.knowledge`` / legacy path) → byte-identical.
        """
        header = cls._SOUL_KNOWLEDGE_BLOCK_HEADER
        # Select the machine-built block: the LAST header immediately followed by
        # a ``- `` item line. Walk backwards past any prose heading collisions.
        search_from = len(text)
        while True:
            start = text.rfind(header, 0, search_from)
            if start == -1:
                return text
            body_start = start + len(header)
            if text[body_start : body_start + 2] == "- ":
                break
            search_from = start
        n = len(text)
        i = body_start
        block_end = body_start
        while i < n:
            nl = text.find("\n", i)
            line_end = n if nl == -1 else nl
            line = text[i:line_end]
            if line.startswith("- "):
                # An item line — extend the block through it.
                block_end = line_end
                i = line_end + 1
                continue
            if line == "":
                # A blank line: it is internal to the block only if a later line
                # resumes ``- `` items; otherwise it is the ``\n\n`` join before
                # the next stable section, so the block ends here.
                j = line_end + 1
                while j < n and text[j] == "\n":
                    j += 1
                nl2 = text.find("\n", j)
                nxt_end = n if nl2 == -1 else nl2
                if text[j:nxt_end].startswith("- "):
                    i = line_end + 1
                    continue
                break
            # A non-blank, non-item line with NO preceding blank line is a wrapped
            # continuation of the current item's content — absorb it.
            block_end = line_end
            i = line_end + 1
        return text[:start] + text[block_end:]

    @classmethod
    def _behavior_prefix(cls, system_prompt: Any) -> str:
        """Return the stable behavioral prefix of ``system_prompt``.

        AS OF PA-6 THIS IS THE FALLBACK, NOT THE MECHANISM. A caller that
        assembles its prompt through :mod:`pocketpaw.prompt` passes
        ``system_prompt_digest`` and this function is never consulted for its
        key — the digest is a claim the LAYERS made about themselves, where this
        is a guess made by pattern-matching two modules away. PA-6 was filed as
        "delete it" and PA-7 was expected to finish the job when the channel path
        gained a digest.

        IT IS NOT DELETABLE, and PA-7b is where that gets said in the right
        place. The channel path DOES have a digest now (``AgentLoop`` forwards
        ``AgentContextBuilder``'s through ``AgentRouter``), which was the whole
        premise of the deletion — and two callers reach ``run`` without one
        anyway, so this function still keys real traffic:

        * the pocket specialist, which builds an isolated backend via
          ``AgentRouter.create_isolated_backend`` and calls ``backend.run(
          user_message, system_prompt=...)`` DIRECTLY, bypassing the router that
          does the forwarding (``ee/pocketpaw_ee/agent/pocket_specialist/
          runtime.py``, the create path and the edit path). Its prompt is built
          by its own ``_build_system_prompt``, not by the assembler, so there is
          no digest to forward even in principle;
        * any out-of-tree embedder holding a backend and calling ``run`` itself,
          which is a supported thing to do — ``system_prompt`` is a public
          parameter of the ``AgentBackend`` protocol.

        Deleting this would hand both of them a constant key: every prompt would
        look identical to the warm client and a changed persona would be served
        the previous one. That is #1842, restored for the callers least able to
        notice.

        Measured 2026-08-03 over 8 turns of a realistic channel prompt: keying on
        the whole prompt instead held 0/7 boundaries, because ``run`` then spliced
        a GROWING ``# Recent Conversation`` block into ``options.system_prompt``
        (history now rides the query text instead) — so the warm subprocess was
        torn down and respawned every turn. With this prefix it held 7/7. So PA-7b claims no
        cache-rate win on the channel path — 7/7 was already the baseline that
        measurement recorded, and what the prefix genuinely cannot do is see a
        REAL behaviour change sitting below the marker it cuts at.

        ONE CAVEAT ON THAT 7/7, found while threading PA-7b and worth stating
        where it will be read. ``_VOLATILE_PROMPT_MARKERS`` are the CLOUD path's
        block headers. The channel path emits ``# Memory Context (already
        loaded…)`` and ``# Knowledge Base (relevant articles…)``, and NEITHER is
        in the tuple — so the per-message recall stays inside the prefix, and a
        two-turn probe (``tests/test_channel_prompt_digest.py::
        test_a_changed_recall_moves_the_prefix_and_not_the_digest``) shows the
        prefix moving when only that recall changes, while the digest holds. The
        7/7 is presumably a run whose recall did not vary between turns. That is
        a mechanism, not a rate: nobody has measured how often a real channel
        turn changes its recall, and this note is not a licence to assume.

        What it is worse at than the digest, measured on the cloud path over the
        same 8 turns: it retains ``## Self-Understanding``, which
        ``to_system_prompt()`` renders ABOVE the ``# Key Knowledge`` block this
        excises, so the strip never reaches it. It moved on 6 of 7 boundaries,
        and the warm client rebuilt with it — 1/7 held, against 7/7 for the
        digest. The prefix was never the high baseline PA-6's success metric
        assumed.

        Two independent volatile strips run here so two turns that differ only
        in per-turn soul/retrieval state hash to the same value:

        1. The MID-prompt soul "# Key Knowledge" block is excised in place
           (``_strip_soul_knowledge_block``) — its bond level / memory count /
           recalled memories increment every turn but carry no behavioral
           instructions.
        2. The volatile per-turn TAIL (KB block, soul-memory recall, injected
           history) is cut at the earliest ``_VOLATILE_PROMPT_MARKERS`` marker,
           matched at a block boundary — after the separator, or at index 0 when
           the block opens the prompt and there is no separator to find.

        A REAL behavioral change (different persona/identity, override, or
        ``instructions``) still lands in the retained text, so it changes the
        digest and forces a warm-client rebuild. The boundary rule keeps that
        true: a marker mid-prompt without its blank line is content, not a
        section header, and is left alone.

        On Windows the SDK may pass ``system_prompt`` as a
        ``{type: "file", path: ...}`` dict — there is no inline text to key on,
        so fall back to the path. That path is CONTENT-ADDRESSED as of PA-7b
        (``_spill_prompt_to_file``), which is what makes the fallback a real key:
        it used to be one fixed filename, so every spilled prompt hashed alike
        and the warm client never rebuilt for them. It keys on the whole prompt
        rather than on the stable prefix — no cut is possible without reading the
        file, and a key function that does disk I/O per turn is not a trade worth
        making. See the module-level block above ``_spill_prompt_to_file``.
        """
        if isinstance(system_prompt, dict):
            return f"file:{system_prompt.get('path', '')}"
        if not isinstance(system_prompt, str):
            return ""
        system_prompt = cls._strip_soul_knowledge_block(system_prompt)
        cut = len(system_prompt)
        for marker in cls._VOLATILE_PROMPT_MARKERS:
            # The block opens the prompt: nothing precedes it, so the separator
            # the marker carries was never emitted. Everything is volatile.
            if system_prompt.startswith(marker.lstrip("\n")):
                return ""
            idx = system_prompt.find(marker)
            if idx != -1:
                cut = min(cut, idx)
        return system_prompt[:cut]

    @staticmethod
    def _should_load_bundled_plugin(*, enabled: bool, skill_names: frozenset[str]) -> bool:
        """Whether to load the WHOLE bundled-skills plugin this connect.

        The bundled skills ship as a local plugin passed via SDK ``plugins=``,
        which loads independently of ``skill_names``. That made a surface's
        skill allowlist advisory rather than binding: naming a narrow set could
        not withhold ``pocketpaw-create-pocket``, whose description matches
        "build an app with components and nice design" almost word for word.
        /code had to deny the ``Skill`` built-in outright to stop it.

        Gating the wholesale load on an EMPTY ``skill_names`` makes the
        allowlist binding — name skills and the run gets exactly those (each
        materialized by ``materialize_run_skills``, bundled ones included);
        name none and the full bundled set loads as before, so general chat is
        unchanged. ``sdk_load_bundled_skills=False`` still disables it outright.
        """
        return enabled and not skill_names

    @staticmethod
    def _plugin_digest(skill_names: frozenset[str], *, bundled: bool) -> str:
        """Stable digest of the agent's plugin IDENTITY for the cache key.

        Folds the per-entity skill subset (sorted ``skill_names``) and whether
        the bundled-skills plugin is loaded into one short hash. Empty when no
        skills and no bundled plugin participate, so a plain run's key is
        unchanged.

        CRITICAL: this digests the IDENTITY of the skills, never the
        materialized ``plugins=`` PATH. ``materialize_run_skills`` creates a
        fresh ``tempfile.mkdtemp`` dir on every run, so hashing the path would
        change the cache key every turn and defeat warm-client reuse entirely —
        the exact latency bug this fix removes. Two turns that request the same
        skills (and the same bundled state) MUST hash identically so the warm
        subprocess is reused; a changed skill set MUST hash differently so it
        rebuilds.
        """
        if not skill_names and not bundled:
            return ""
        payload = ("b1:" if bundled else "b0:") + ",".join(sorted(skill_names))
        return hashlib.sha256(payload.encode("utf-8", "replace")).hexdigest()[:16]

    def _tenant_scope_key(self) -> str:
        """Connector/entitlement scope for THIS backend instance.

        Resolved from the per-run ``_extra_subprocess_env`` (never a
        process-global mode flag — repo lesson #1570/#1574):

        - no ``POCKETPAW_WORKSPACE_ID`` attached → the OSS single-user
          ``"default"`` scope the builtin connector tools use;
        - a non-blank id → ``ws:<id>`` (the EE cloud connector row keying);
        - tenancy attached but BLANK id → fail CLOSED to a sentinel scope
          that matches no rows. Never the shared ``"default"`` bucket:
          a half-attached tenancy must degrade to "nothing available",
          not to another scope's connector availability.
        """
        from pocketpaw.atlas.overlay import DEFAULT_SCOPE_KEY

        raw = self._extra_subprocess_env.get("POCKETPAW_WORKSPACE_ID")
        if raw is None:
            return DEFAULT_SCOPE_KEY
        ws = raw.strip()
        return f"ws:{ws}" if ws else _SENTINEL_TENANT_SCOPE

    @classmethod
    def _client_cache_key(
        cls,
        options: Any,
        *,
        session_key: str | None = None,
        plugin_digest: str = "",
        tenant_scope: str = "",
        system_prompt_digest: str = "",
    ) -> str:
        """Persistent-client cache key: session + cwd + model + tools + the
        prompt's identity + the plugin-identity digest.

        THE PROMPT SLOT HAS TWO SOURCES AND THEY ARE DIFFERENT CLAIMS (PA-6).
        ``d:`` is the assembler's ``stable_digest`` — a hash over the prompt
        LAYERS that declared themselves cacheable. ``t:`` is a hash of
        ``_behavior_prefix``, which infers the same thing by cutting the rendered
        text at known markers. They are prefixed rather than sharing a namespace
        because a caller that gains a digest mid-deploy must rebuild once rather
        than silently match a key minted under the other rule.

        The digest WINS where it exists, and it is strictly better there. It sees
        the drift the prefix cannot: ``## Self-Understanding`` renders above the
        ``# Key Knowledge`` block the prefix excises, so the prefix keeps it and
        rebuilt the warm subprocess on 6 of 7 measured turn boundaries. Over the
        same 8 turns the digest held one value. History and per-turn context are
        outside both, and outside the system prompt entirely: they ride the
        query text (``compose_turn_message``).

        ``t:`` is not a transitional wart to be deleted on sight, and PA-7b did
        NOT retire it. The channel path gained a digest there, but the pocket
        specialist calls ``backend.run`` directly (bypassing the router that
        forwards it) and so does any out-of-tree embedder; both would key on a
        constant without this branch. See ``_behavior_prefix`` for the full
        argument and for the measurement — on a channel-shaped prompt, keying on
        the whole prompt held 0 of 7 boundaries against the prefix's 7 of 7.

        The prefix digest is what makes a mid-session backend config change
        (configured:false -> configured:true, baked into the static home
        prompt) evict and rebuild the warm subprocess on the next turn, instead
        of staying frozen until a cold restart. The ``plugin_digest`` does the
        same for the agent's skill set (per-entity ``skill_names`` + bundled
        flag): folding it in lets a warm client tell a skill run apart from a
        non-skill one, so a skill run can REUSE the subprocess instead of
        re-spawning every turn. Empty ``plugin_digest`` (the default) leaves the
        key byte-for-byte identical to the pre-fix behavior for non-skill
        callers. Hashing keeps the key bounded regardless of prompt length.

        ``cwd`` (ART-2) is folded in so warm-client tenant isolation is
        STRUCTURAL, not an implicit consequence of the session_key<->cwd
        coupling: if cwd derivation ever changes to depend on something not in
        session_key, a stale warm subprocess can never be reused across two
        different working directories (i.e. two tenants). The SDK fixes cwd at
        connect() time, so a changed cwd MUST force a fresh subprocess.

        ``tenant_scope`` (AT-5) extends the same argument to the atlas
        entitlement scope: the per-run provider is baked into the MCP server
        set at connect() time, so a changed scope must also force a fresh
        subprocess rather than reusing one warmed for another tenant.
        """
        if system_prompt_digest:
            prompt_key = f"d:{system_prompt_digest}"
        else:
            prefix = cls._behavior_prefix(getattr(options, "system_prompt", None))
            prompt_key = "t:" + hashlib.sha256(prefix.encode("utf-8", "replace")).hexdigest()[:16]
        return (
            f"{session_key or ''}:"
            f"{getattr(options, 'cwd', '')}:"
            f"{getattr(options, 'model', '')}:"
            f"{sorted(getattr(options, 'allowed_tools', []) or [])}:"
            f"{prompt_key}:"
            f"{plugin_digest}:"
            f"{tenant_scope}"
        )

    async def _get_or_create_client(
        self,
        options: Any,
        *,
        session_key: str | None = None,
        plugin_digest: str = "",
        system_prompt_digest: str = "",
        history: list[dict] | None = None,
        prewarm: bool = False,
    ) -> Any:
        """Get or create a persistent ClaudeSDKClient.

        ``prewarm=True`` makes the call a no-op (returns None) when a run holds
        the lease or a client that already served ``session_key`` is live, so a
        prewarm can never evict a client someone is using.

        Reuses the existing subprocess if model, tools, session, the system
        prompt's behavioral prefix, **and the plugin-identity digest** haven't
        changed. Different sessions get a fresh subprocess so the CLI's internal
        conversation context doesn't leak between chats; a changed behavioral
        prefix (e.g. the home pocket's backend summary flipping to "configured"
        mid-session) or a changed skill set (``plugin_digest``) also forces a
        fresh subprocess so the new prompt / plugins actually take effect — the
        SDK applies both only at connect() time.

        When a stale client is evicted, its materialized per-run skills dir
        (tracked by the old ``plugin_digest``) is removed here: the subprocess
        that held that path is being torn down, so nothing references it
        anymore. Re-materializing the dir per turn does NOT work — the warm
        subprocess keeps the original path from its first connect — so the dir
        is cached per digest and only dropped on eviction or cleanup().

        ``history`` is the stored conversation this turn carries. History is
        volatile and stays out of the key, and it reaches the model in the query
        text rather than the prompt, so a matching client is reused as long as
        ``history`` still extends what it has seen (``_client_knows_history``). A
        history that was edited or had entries deleted evicts it, and the new
        client is sent the full conversation. ``None`` skips the check.
        """
        import time

        # Serialize the whole reuse-or-connect section so a concurrent prewarm +
        # first run (feat/claude-sdk-prewarm) cannot both create / evict the
        # client across the ``connect()`` await. Whichever wins the lock first
        # connects; the other then re-reads ``_client`` / ``_client_options_key``
        # under the SAME key and reuses it. Lazily created on the running loop.
        if self._client_lock is None:
            self._client_lock = asyncio.Lock()

        key = self._client_cache_key(
            options,
            session_key=session_key,
            plugin_digest=plugin_digest,
            tenant_scope=self._tenant_scope_key(),
            system_prompt_digest=system_prompt_digest,
        )

        async with self._client_lock:
            # A prewarm never takes a client away from anyone. Checked INSIDE the
            # lock because a run can take the lease between prewarm's entry check
            # and here. A client that already served this session is left alone
            # whatever its key (a model-picker or tools-off turn keys differently
            # from the default prewarm): the turn itself decides whether to reuse.
            if prewarm:
                if self._client_in_use:
                    logger.debug("prewarm skipped: the warm client is in use")
                    return None
                if (
                    self._client is not None
                    and self._client_session_key == session_key
                    and _client_has_served(self._client)
                ):
                    logger.debug("prewarm skipped: session %s is already warm", session_key)
                    return None
            # Re-check INSIDE the lock: a prewarm (or sibling) may have connected
            # a matching client while we awaited the lock — reuse it, don't churn.
            if self._client is not None and self._client_options_key == key:
                if self._client_knows_history(self._client, history):
                    logger.debug("Reusing persistent client (key=%s)", key)
                    return self._client
                logger.info(
                    "Rebuilding warm client: the stored conversation no longer "
                    "extends what it has seen (key=%s)",
                    key,
                )

            # Disconnect stale client and drop the skills dir it was connected with.
            if self._client is not None:
                try:
                    await self._client.disconnect()
                except Exception as e:
                    logger.debug("Failed to disconnect Claude client: %s", e)
                self._client = None
                self._client_options_key = None
                self._client_session_key = None
                self._drop_skills_dir(self._client_plugin_digest)

            # Create and connect the new client; it becomes the warm client only
            # once connected, so a failed connect never leaves a broken client
            # cached under a key a later turn could match.
            t0 = time.monotonic()
            client = self._ClaudeSDKClient(options=options)
            try:
                await self._connect_client(client)
            except BaseException:
                await self._discard_client(client)
                raise
            self._client = client
            self._client_session_key = session_key
            self._client_options_key = key
            self._client_plugin_digest = plugin_digest
            setattr(self._client, _WATERMARK_ATTR, _HistoryWatermark())
            t1 = time.monotonic()
            logger.info("Persistent client connected in %.0fms (key=%s)", (t1 - t0) * 1000, key)
            return self._client

    @staticmethod
    def _client_knows_history(client: Any, history: list[dict] | None) -> bool:
        """Can ``client`` serve a turn carrying ``history`` by being sent only
        what it has not seen? False when the history diverged from its
        watermark, or when the client has none (built somewhere that did not
        track one), in which case what it holds is unknown."""
        if history is None:
            return True
        wm = getattr(client, _WATERMARK_ATTR, None)
        if not isinstance(wm, _HistoryWatermark):
            return False
        return _unseen_history(wm, history) is not None

    @staticmethod
    def _turn_query_text(
        client: Any,
        message: str,
        *,
        history: list[dict] | None,
        turn_context: str,
        history_is_native: bool = False,
    ) -> str:
        """Compose this turn's query text for ``client`` and advance its watermark.

        ``client=None`` is a stateless launch: it holds nothing, so it gets the
        full history. ``history_is_native`` is a native-resume launch, whose CLI
        session already carries the conversation: no history is rendered, but it
        counts as seen. Otherwise the client is sent only the entries it has not
        seen. The watermark then records all of ``history`` plus this turn, which
        the store will return as a user entry and a reply the client already has.
        """
        entries = list(history or ())
        if client is None:
            return compose_turn_message(message, turn_context=turn_context, history=entries)
        wm = getattr(client, _WATERMARK_ATTR, None)
        if not isinstance(wm, _HistoryWatermark):
            wm = _HistoryWatermark()
            setattr(client, _WATERMARK_ATTR, wm)
        is_delta = bool(wm.seen or wm.pending)
        unseen: list[dict] = []
        if history is not None and not history_is_native:
            planned = _unseen_history(wm, entries)
            if planned is None:
                unseen, is_delta = entries, False
            else:
                unseen = planned
        if history is not None:
            wm.seen = [_history_fingerprint(e) for e in entries][-_WATERMARK_MAX_SEEN:]
        wm.pending = [("user", _normalize_turn_text(message)), ("assistant", _ANY_REPLY)]
        return compose_turn_message(
            message, turn_context=turn_context, history=unseen, history_is_delta=is_delta
        )

    @staticmethod
    def _forget_history(client: Any) -> None:
        """Drop ``client``'s watermark after a send that may not have landed, so
        its next turn rebuilds instead of trusting what it may not hold."""
        try:
            setattr(client, _WATERMARK_ATTR, None)
        except Exception:  # noqa: BLE001 - best effort on an exotic client
            pass

    def _drop_skills_dir(self, plugin_digest: str) -> None:
        """Remove the materialized per-run skills dir cached under
        ``plugin_digest`` (if any). Best-effort; never raises. Called when the
        warm client that referenced the dir is evicted or on cleanup()."""
        if not plugin_digest:
            return
        root = self._skills_dir_by_digest.pop(plugin_digest, None)
        if root is not None:
            from pocketpaw.skills import cleanup_run_skills

            cleanup_run_skills(root)

    def _connect_timeout(self) -> float:
        raw = getattr(self.settings, "claude_sdk_connect_timeout", None)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool) and raw > 0:
            return float(raw)
        return _DEFAULT_CONNECT_TIMEOUT_S

    async def _connect_client(self, client: Any) -> None:
        """``client.connect()`` bounded by ``claude_sdk_connect_timeout``.

        On timeout the half-started client is disconnected and a RuntimeError
        raised, which every caller already treats as a failed connect.
        """
        timeout = self._connect_timeout()
        try:
            await asyncio.wait_for(client.connect(), timeout)
        except TimeoutError:
            try:
                await client.disconnect()
            except Exception as exc:  # noqa: BLE001
                logger.debug("disconnect after connect timeout failed: %s", exc)
            raise RuntimeError(f"Claude CLI did not finish starting within {timeout:g}s") from None

    async def _discard_client(self, client: Any) -> None:
        """Best-effort disconnect of a client that is being dropped."""
        if client is None:
            return
        try:
            await client.disconnect()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Failed to disconnect Claude client: %s", exc)

    async def cleanup(self) -> None:
        """Disconnect the persistent client and release resources.

        Does NOT release ``_client_in_use``: only the run holding the lease may,
        from its own ``finally``. Releasing it here let a second run take the
        lease while the first was still streaming.
        """
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception as e:
                logger.debug("Failed to disconnect Claude client: %s", e)
            self._client = None
            self._client_options_key = None
            self._client_session_key = None
            logger.info("Persistent client disconnected")
        # Sweep every materialized per-run skills dir adopted by a warm client.
        # Safe even when no client existed (the map is just empty).
        if self._skills_dir_by_digest:
            from pocketpaw.skills import cleanup_run_skills

            for root in self._skills_dir_by_digest.values():
                cleanup_run_skills(root)
            self._skills_dir_by_digest.clear()
        self._client_plugin_digest = ""

    async def _resilient_query(self, prompt: str, options):
        """Wrap stateless _query with MessageParseError handling.

        Unlike the persistent client, a stateless ``query()`` generator cannot be
        re-created mid-turn, so a parse error ends the stream. It yields
        ``_STATELESS_STREAM_CUT`` so ``run`` tells the user the reply was cut off
        instead of ending it silently.

        Only the FIRST event is time-bounded, by ``claude_sdk_connect_timeout``:
        the CLI emits its init message as soon as it has started, so a launch
        that produces nothing in that window is hung (the SDK's own initialize
        wait is 24 h here). Every later event is unbounded, so a long reply or a
        slow tool is never cut.
        """
        stream = self._query(prompt=prompt, options=options)
        # Same span name and gen_ai attributes as logfire's instrumentation of the
        # persistent ClaudeSDKClient path, so a fallback turn is traced like one.
        # Detached: this span is held across ``yield``, and an attached one would
        # leak into the consumer and fail to detach when finalized elsewhere.
        with detached_span(
            "invoke_agent",
            **{
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.provider.name": "anthropic",
                "gen_ai.system": "anthropic",
                "pocketpaw.claude_sdk.mode": "stateless",
            },
        ) as otel_span:
            try:
                timeout = self._connect_timeout()
                try:
                    first = await asyncio.wait_for(anext(stream), timeout)
                except StopAsyncIteration:
                    return
                except TimeoutError:
                    raise RuntimeError(
                        f"Claude CLI did not finish starting within {timeout:g}s"
                    ) from None
                yield first
                async for event in stream:
                    yield event
            except Exception as exc:
                if "MessageParseError" in type(exc).__name__:
                    logger.warning("Unreadable SDK event ended the stateless query: %s", exc)
                    if otel_span is not None:
                        otel_span.record_exception(exc)
                    yield _STATELESS_STREAM_CUT
                else:
                    raise
            finally:
                # Closing the SDK's generator is what ends its CLI subprocess.
                aclose = getattr(stream, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception as close_exc:  # noqa: BLE001
                        logger.debug("closing the stateless query failed: %s", close_exc)

    async def _resilient_receive(self, client):
        """One turn from a persistent client: its messages through the ResultMessage.

        Reads ``client.receive_response()``, the method logfire's Claude Agent SDK
        instrumentation patches: the turn's ``invoke_agent`` span, its ``chat``
        spans and the state its injected tool hooks need all live inside it.
        Parse errors are recovered beneath it, in the client class's own
        ``receive_messages`` (``_parse_tolerant_client``), so a recovered error
        neither ends the turn early (leaking its tail into the next turn) nor
        splits it into two traces. Test doubles that only define
        ``receive_messages`` are read through that, up to the ResultMessage.

        The inner stream is run to its own end, and closed here if the consumer
        abandons the turn: logfire's span is attached across its ``yield``, and a
        generator left suspended is finalized later from another context, where
        the span fails to detach and is never exported.
        """
        response = getattr(client, "receive_response", None)
        stream = response() if response is not None else client.receive_messages()
        try:
            async for msg in stream:
                yield msg
                if response is None and isinstance(msg, self._ResultMessage or ()):
                    return
        finally:
            await stream.aclose()

    async def _build_options(
        self,
        message: str,
        *,
        system_prompt: str | None,
        session_key: str | None,
        deny_mcp_tool_ids: frozenset[str],
        allow_sdk_tools: frozenset[str],
        allow_mcp_tool_ids: frozenset[str] | None,
        skill_names: frozenset[str],
        stderr_sink: list[str],
        session_handle: SessionHandle | None = None,
        model_override: str | None = None,
        exclusive_mcp_tools: bool = False,
        tools_enabled: bool = True,
    ) -> _BuiltOptions:
        """Assemble the ``ClaudeAgentOptions`` a turn (or a prewarm) will run on.

        Extracted from ``run`` (feat/claude-sdk-prewarm) so ``prewarm`` can build
        the EXACT same options the first real turn will, and therefore compute
        the same ``_client_cache_key`` — model + tools + system-prompt behavioral
        prefix + ``plugin_digest``. If the keys diverged, the prewarmed warm
        client would be EVICTED on the first turn (a net loss: prewarm paid a
        connect the run then threw away), which is the whole hazard this
        extraction removes.

        Returns a ``_BuiltOptions`` carrying everything ``run``'s dispatch +
        finally still need: the ``options`` object, the raw ``options_kwargs``
        (the token-usage event reads ``model`` off it), the resolved ``llm`` (for
        error formatting), and the per-run skills-plugin lifecycle triple
        (``run_skills_root`` / ``skills_dir_adopted`` / ``plugin_digest``).

        Pure assembly — no ``yield``, no streaming, no client creation. Reads the
        warm-client state (``self._client_in_use`` / ``self._skills_dir_by_digest``)
        only to decide whether to reuse a cached materialized skills dir, exactly
        as the inline block did. ``stderr_sink`` is the caller's list that the
        ``stderr`` callback appends to (so ``run`` keeps capturing CLI stderr for
        diagnostics; ``prewarm`` passes a throwaway list).
        """
        run_skills_root: Path | None = None
        skills_dir_adopted = False
        plugin_digest = ""

        # Per-run working directory. OSS / dedicated → ``file_jail_path``; cloud
        # → a per-workspace/session jail (or a fail-closed raise when a cloud run
        # has no resolvable workspace). Resolved here so BOTH ``run`` and
        # ``prewarm`` warm the SAME cwd for a session — the warm-client cache key
        # already keys on ``session_key``, so a session change rebuilds the
        # subprocess with its own jail.
        resolved_cwd = self._resolve_cwd()

        # Resolve LLM provider early -- needed for routing + env.
        # Use per-backend provider setting (defaults to "anthropic").
        # An API key is REQUIRED for Anthropic provider -- OAuth tokens from
        # Claude Free/Pro/Max plans are not permitted for third-party use.
        # See: https://code.claude.com/docs/en/legal-and-compliance
        from pocketpaw.llm.client import resolve_llm_client

        provider = self.settings.claude_sdk_provider or "anthropic"
        llm = resolve_llm_client(self.settings, force_provider=provider)

        # ── API key check for Anthropic provider ──────────────
        # Skip if using a non-Anthropic provider, or if the active
        # provider is claude_code (it handles OAuth auth via its CLI).
        is_non_anthropic = (
            llm.is_ollama
            or llm.is_openai_compatible
            or llm.is_gemini
            or llm.is_litellm
            or llm.is_openrouter
        )

        # Smart model routing — classify complexity to pick the model tier.
        # All messages go through the Claude Code CLI subprocess, which
        # handles conversation compaction automatically (PreCompact hook).
        if self.settings.smart_routing_enabled and not is_non_anthropic:
            from pocketpaw.agents.model_router import ModelRouter

            model_router = ModelRouter(self.settings)
            selection = model_router.classify(message)
            logger.info(
                "Smart routing: %s -> %s (%s)",
                selection.complexity.value,
                selection.model,
                selection.reason,
            )

        # System prompt — instructions are now part of identity
        # (injected by BootstrapContext.to_system_prompt() via INSTRUCTIONS.md)
        identity = system_prompt or _DEFAULT_IDENTITY

        # Inject connector instructions so the agent can use data sources
        try:
            from pocketpaw.connectors.registry import ConnectorRegistry

            reg = ConnectorRegistry()
            if reg.available:
                names = ", ".join(c["name"] for c in reg.available)
                identity += (
                    "\n\n# Data Connectors\n"
                    f"Available connectors: {names}\n"
                    "To manage connectors, use Bash to call the local API:\n"
                    "- List: curl -s http://localhost:8888/api/v1/connectors\n"
                    "- Detail: curl -s http://localhost:8888/api/v1/connectors/<name>\n"
                    "- Connect: curl -s -X POST "
                    "http://localhost:8888/api/v1/connectors/connect "
                    "-H 'Content-Type: application/json' "
                    '-d \'{"connector_name":"<name>","config":{...}}\'\n'
                    "- Execute: curl -s -X POST "
                    "http://localhost:8888/api/v1/connectors/execute "
                    "-H 'Content-Type: application/json' "
                    '-d \'{"connector_name":"<name>","action":"<action>"'
                    ',"params":{...}}\'\n'
                    "- Disconnect: curl -s -X POST "
                    "http://localhost:8888/api/v1/connectors/disconnect "
                    "-H 'Content-Type: application/json' "
                    '-d \'{"connector_name":"<name>"}\'\n'
                )
        except Exception:
            pass  # Don't break agent if connector registry fails

        # History is NOT in the system prompt. The SDK applies the prompt only
        # at ``connect()``, so a reused warm client would keep whatever history
        # the connecting turn had; ``run`` sends it in the query text instead
        # (``_turn_query_text``), which keeps this prompt identical across turns.
        final_prompt = identity

        # Native-resume session id (feat/session-supervisor SS-1): the CLI is
        # launched with ``resume=<id>`` and reloads that session's transcript.
        resume_session_id = session_handle.cli_session_id if session_handle is not None else None

        # Pocket sessions don't need shell or filesystem access — the
        # MCP pocket tools (get_pocket / list_pockets / set_state /
        # set_node_prop / add_node / etc.) are the complete interface.
        # Detect via the <pocket-scope> marker every pocket prompt
        # carries; lock tools down to delegation + web + pocket MCP.
        #
        # Without this gate, the agent has been observed reaching for
        # shell introspection (e.g. `env | grep pocket; curl localhost`)
        # to "figure out" pocket state, which trips the security rails
        # AND is the wrong path — the MCP tools already expose
        # everything the agent needs.
        is_pocket_session = "<pocket-scope>" in (final_prompt or "")

        if is_pocket_session:
            all_sdk_tools = ["Agent", "WebSearch", "WebFetch"]
            logger.info(
                "Pocket session detected — tool surface locked to %s",
                all_sdk_tools,
            )
        else:
            all_sdk_tools = [
                "Agent",
                "Bash",
                "Read",
                "Write",
                "Edit",
                "Glob",
                "Grep",
                "WebSearch",
                "WebFetch",
                "Skill",
            ]
        allowed_tools = [
            t
            for t in all_sdk_tools
            if self._policy.is_tool_allowed(self._TOOL_POLICY_MAP.get(t, t))
        ]
        if len(allowed_tools) < len(all_sdk_tools):
            blocked = set(all_sdk_tools) - set(allowed_tools)
            logger.info("Tool policy blocked SDK tools: %s", blocked)

        # In-process MCP tool ids must be on the allowlist to be
        # callable. The ripple widget-spec tools are core; the cloud
        # pocket / Mission Control tasks / planner / pocket-specialist
        # ids come from the ``pocketpaw.mcp_servers`` providers (none on
        # an OSS install). The cloud ``pocketpaw_pocket`` server carries
        # both read tools (get_pocket / list_pockets) and the writable
        # ``add_widget`` tool — they all flow through the loop below.
        allowed_tools.extend(self._collect_mcp_tool_ids())

        # Per-entity ADDITIVE allowlist (entity-rooms chunk ①). UNION the
        # entity's ``allowed_sdk_tools`` into the allowlist BEFORE the deny
        # subtraction below, so the precedence is
        # ``effective = (agent_tools ∪ allow) − deny``. Dedup-preserve order:
        # only append ids not already present. Empty for legacy / non-entity
        # runs, so this is a no-op there. The deny set (subtracted next) is
        # the hard cap — an id in BOTH allow and deny stays denied.
        if allow_sdk_tools:
            existing = set(allowed_tools)
            for tool_id in allow_sdk_tools:
                if tool_id not in existing:
                    allowed_tools.append(tool_id)
                    existing.add(tool_id)
            logger.info("Surface tool-allow: unioned %s into allowlist", sorted(allow_sdk_tools))

        # Surface-scoped servers (``SURFACE_SCOPED_MCP_SERVERS``) exist only on
        # a surface that granted them through ``allow_sdk_tools``. Anywhere else
        # their tool ids leave the allowlist here and ``_get_mcp_servers`` does not
        # register the server, so a one-page toolset costs other chats nothing.
        scoped_off = ungranted_surface_servers(allow_sdk_tools)
        if scoped_off:
            allowed_tools = [t for t in allowed_tools if _mcp_server_of(t) not in scoped_off]

        # Per-surface MCP-tool deny set (threaded from the chat loop's
        # resolved ``SurfaceProfile``). Any denied id is subtracted from the
        # allowlist BEFORE the SDK launches, so the agent is physically
        # unable to call it. On the /sites svelte-create surface this forbids
        # the two ripple-create tools (``create_landing_site`` +
        # ``pocket_specialist__create``) so the agent CANNOT fall back to
        # building a rippleSpec landing page — prose-only "do not call the
        # ripple tool" routing was proven to fail. Empty for every other
        # surface (a no-op), so ``create_svelte_site`` / ``publish`` /
        # ``pocket_specialist__edit`` and the ripple-engine / refine /
        # non-sites flows are untouched. This is the typed replacement for
        # the old prompt-sniffing ``engine="svelte"`` marker gate.
        if deny_mcp_tool_ids:
            before_count = len(allowed_tools)
            allowed_tools = [t for t in allowed_tools if t not in deny_mcp_tool_ids]
            if len(allowed_tools) < before_count:
                logger.info(
                    "Surface tool-deny: excluded %s from allowlist",
                    sorted(deny_mcp_tool_ids),
                )

        # Per-MODE restrictive MCP allow-list (distinct from the additive
        # ``allow_sdk_tools`` above). ``None`` keeps every MCP tool (broad
        # surfaces like /chat). When set, keep only MCP tools that are in the
        # mode's set, in the pocket-creation grant, a ripple widget / atlas
        # tool, OR from an always-allowed server (connectors + pocket
        # lifecycle).
        # Built-in SDK tools (Read/Write/Bash/...) are NEVER filtered here —
        # only ``mcp__*`` ids — so scoping a mode can't strip core tools.
        # Applied AFTER deny so a denied id can't sneak back via the grant.
        #
        # ``exclusive_mcp_tools`` (CX-1) is the exact-toolset signal: an
        # EXCLUSIVE turn CAPS the MCP surface to ``allow_mcp_tool_ids`` alone —
        # no POCKET_CREATION_GRANT, no widget/atlas ids, and NOT the
        # ALWAYS_ALLOWED_MCP_SERVERS escape hatch — so a dedicated agent (e.g.
        # /code) gets EXACTLY the ids it declared and nothing the universal
        # grant would otherwise union back in. Precedence rule: an exclusive
        # turn with ``allow_mcp_tool_ids=None`` strips ALL ``mcp__`` ids (an
        # empty permitted set), which is how an exclusive agent wins even over a
        # broad surface. The default (signal off) path below is byte-for-byte
        # the legacy grant-union scoping.
        if exclusive_mcp_tools:
            permitted = allow_mcp_tool_ids or frozenset()
            before_count = len(allowed_tools)
            allowed_tools = [
                t for t in allowed_tools if not t.startswith("mcp__") or t in permitted
            ]
            if len(allowed_tools) < before_count:
                logger.info(
                    "Exclusive MCP-allow: capped to exactly %s (no general grant)",
                    sorted(permitted),
                )
        elif allow_mcp_tool_ids is not None:
            from pocketpaw.agents.sdk_mcp_atlas import ATLAS_TOOL_IDS
            from pocketpaw.agents.sdk_mcp_studio import STUDIO_TOOL_IDS
            from pocketpaw.agents.sdk_mcp_widgets import WIDGET_TOOL_IDS

            grant = (
                allow_mcp_tool_ids
                | POCKET_CREATION_GRANT
                | frozenset(WIDGET_TOOL_IDS)
                | frozenset(ATLAS_TOOL_IDS)
                | frozenset(STUDIO_TOOL_IDS)
            )
            before_count = len(allowed_tools)
            allowed_tools = [
                t
                for t in allowed_tools
                if not t.startswith("mcp__")
                or t in grant
                or _mcp_server_of(t) in ALWAYS_ALLOWED_MCP_SERVERS
            ]
            if len(allowed_tools) < before_count:
                logger.info(
                    "Mode MCP-allow: scoped to %s (+ general grant)",
                    sorted(allow_mcp_tool_ids),
                )

        # Build hooks for security
        hooks = {
            "PreToolUse": [
                self._HookMatcher(
                    matcher="Bash",  # Only hook Bash commands
                    hooks=[self._block_dangerous_hook],
                )
            ]
        }

        # Build options
        #
        # Windows note: an oversized prompt is spilled to a file and passed as a
        # ``SystemPromptFile`` dict. The path is content-addressed and pruned —
        # see ``_spill_prompt_to_file`` and the block above it for why one fixed
        # path was both a cross-run race and a cache-key collapse.
        system_prompt_arg: Any = final_prompt
        if _prompt_must_spill(final_prompt):
            prompt_path = _spill_prompt_to_file(final_prompt)
            system_prompt_arg = {"type": "file", "path": str(prompt_path)}
            logger.info(
                "System prompt %d chars exceeds Windows CLI safe limit; "
                "passing via --system-prompt-file %s",
                len(final_prompt),
                prompt_path,
            )

        # ``setting_sources=[]`` keeps the agent on its OWN persona.
        # PocketPaw is not Claude Code: we pass a custom ``system_prompt``
        # string (never the ``claude_code`` preset), and an empty
        # setting-source list stops the SDK from injecting CLAUDE.md,
        # output styles, or filesystem settings as context. The repo
        # CLAUDE.md literally opens with "guidance to Claude Code
        # (claude.ai/code)" — loading it bled that identity into the
        # agent. Hooks, MCP servers, allowed_tools and permissions are
        # all passed explicitly below, so none of them depend on
        # setting sources. See
        # https://code.claude.com/docs/en/agent-sdk/modifying-system-prompts
        # Per-send tool switch (2026-09-11). ``tools`` is the BASE SET and
        # ``allowed_tools`` only filters it, so emptying the allowlist is NOT
        # how you turn tools off here — measured in the SDK's own CLI
        # transport, which extends the command with ``--allowed-tools`` only
        # ``if effective_allowed_tools:``. An empty list is therefore not
        # "allow nothing", it is "say nothing", and the CLI falls back to its
        # DEFAULT tool set. A switch built that way would read Off and change
        # nothing, which is the exact defect this switch already shipped once.
        #
        # ``tools=[]`` is the lever: the transport turns it into ``--tools ""``.
        # The allowlist is emptied too, because it is what the warm-client cache
        # key is built from — without that a tools-off turn would be served the
        # client built WITH tools, the one-slot problem again.
        if not tools_enabled:
            allowed_tools = []
        options_kwargs = {
            "system_prompt": system_prompt_arg,
            "allowed_tools": allowed_tools,
            "setting_sources": [],
            "hooks": hooks,
            "cwd": str(resolved_cwd),
            "max_turns": self.settings.claude_sdk_max_turns or None,
            "max_buffer_size": _SDK_MAX_BUFFER_BYTES,
        }
        if not tools_enabled:
            options_kwargs["tools"] = []

        # Load PocketPaw's bundled skills as a Claude Code *local plugin*.
        # ``setting_sources=[]`` above disables the SDK's ~/.claude/skills
        # discovery, so the boot-time ~/.claude/skills mirror is invisible
        # to this backend and a local plugin is the ONLY way the bundled
        # skills reach it. Persona isolation is preserved — a plugin loads
        # only its own ``skills/`` directory, never the rest of ~/.claude
        # (CLAUDE.md, output styles). Empirically verified 2026-06-03: the
        # ``skills=`` option is also gated by setting_sources, but
        # ``plugins=`` is not. Toggle via ``sdk_load_bundled_skills``.
        bundled_loaded = False
        if self._should_load_bundled_plugin(
            enabled=self.settings.sdk_load_bundled_skills, skill_names=skill_names
        ):
            from pocketpaw.bundled_skills import bundled_skills_plugin_dir

            plugin_dir = bundled_skills_plugin_dir()
            if plugin_dir is not None:
                options_kwargs["plugins"] = [{"type": "local", "path": str(plugin_dir)}]
                bundled_loaded = True
                # Enumerate the actual skill dirs so operators can confirm which
                # bundled skills reached the agent this connect (e.g. that a newly
                # added skill is picked up after a restart).
                try:
                    _skills_root = plugin_dir / "skills"
                    _skill_names = sorted(
                        p.name for p in _skills_root.iterdir() if (p / "SKILL.md").is_file()
                    )
                except Exception:  # noqa: BLE001 — logging must never break the run
                    _skill_names = []
                logger.info(
                    "SDK: loading bundled-skills plugin from %s — %d skills: %s",
                    plugin_dir,
                    len(_skill_names),
                    ", ".join(_skill_names) or "(none found)",
                )

        # Plugin-identity digest (fix/claude-sdk-warm-client-skills): folds
        # the requested skill set + the bundled flag into the cache key so a
        # skill run can REUSE the warm subprocess instead of re-spawning.
        # Keyed on identity, never the mkdtemp path (see _plugin_digest).
        plugin_digest = self._plugin_digest(skill_names, bundled=bundled_loaded)

        # Per-entity skill subset (entity-rooms A2). Materialize ONLY the
        # named skills into a local plugin and append it to the ``plugins=``
        # list (creating the list if the bundled plugin above was off). It
        # coexists with the bundled entry. Empty ``skill_names`` is a no-op.
        #
        # The warm client keeps whatever ``plugins=`` path it was connected
        # with from its first connect(), so the materialized dir must be
        # STABLE per plugin_digest across turns. When this run will reuse the
        # warm client and a dir for this digest already exists, reuse it
        # rather than re-materializing — the live subprocess already points
        # at it. Otherwise materialize fresh; cache + adopt it on the warm
        # path (cleaned on eviction/cleanup), or leave it owned by this run
        # on the stateless path (the per-run finally removes it).
        if skill_names:
            from pocketpaw.skills import materialize_run_skills

            will_reuse_warm = not self._client_in_use
            cached_dir = self._skills_dir_by_digest.get(plugin_digest)
            if will_reuse_warm and cached_dir is not None and cached_dir.exists():
                run_skills_root = cached_dir
                skills_dir_adopted = True
                logger.info(
                    "SDK: reusing cached per-run skill plugin (%d requested) at %s",
                    len(skill_names),
                    run_skills_root,
                )
            else:
                run_skills_root = materialize_run_skills(skill_names, run_id=session_key)
                if run_skills_root is not None and will_reuse_warm:
                    self._skills_dir_by_digest[plugin_digest] = run_skills_root
                    skills_dir_adopted = True

            if run_skills_root is not None:
                options_kwargs.setdefault("plugins", [])
                options_kwargs["plugins"].append({"type": "local", "path": str(run_skills_root)})
                if not skills_dir_adopted:
                    logger.info(
                        "SDK: loading per-run skill plugin (%d requested) from %s "
                        "(stateless — dir owned by this run)",
                        len(skill_names),
                        run_skills_root,
                    )

        # Configure LLM provider for the Claude CLI subprocess.
        # Ollama/OpenAI-compat providers set their own env vars via to_sdk_env().
        sdk_env = llm.to_sdk_env()
        if not sdk_env:
            env_key = os.environ.get("ANTHROPIC_API_KEY")
            if env_key:
                sdk_env = {"ANTHROPIC_API_KEY": env_key}

        # Pass Claude Code OAuth token (Max/Pro subscription in Docker/headless)
        oauth_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
        if oauth_token:
            sdk_env = sdk_env or {}
            sdk_env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token

        # Strip nesting-detection env vars (set when launched from
        # a Claude Code terminal) so the subprocess starts cleanly.
        # These should already be removed by main(), but do it here
        # too as a safety net.
        for _strip_key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
            os.environ.pop(_strip_key, None)
        # Merge per-run subprocess env (PR #1222 R1 Blocker 1) —
        # e.g. the pocket-specialist runtime attaches
        # ``POCKETPAW_WORKSPACE_ID`` / ``POCKETPAW_USER_ID`` /
        # ``POCKETPAW_INTERNAL_TOKEN`` here instead of writing to
        # the parent process's ``os.environ``. LLM-auth env wins:
        # we lay extras DOWN FIRST so anything the caller attaches
        # cannot accidentally clobber the auth key. An isolated
        # backend has its own ``_extra_subprocess_env`` so the
        # tenancy of one request cannot leak into another.
        if self._extra_subprocess_env:
            merged_env = dict(self._extra_subprocess_env)
            if sdk_env:
                merged_env.update(sdk_env)
            sdk_env = merged_env
        # Claude Code truncates any MCP tool result above MAX_MCP_OUTPUT_TOKENS
        # (its default: 25k) or diverts it to a file surfaces like /sites cannot
        # read, so a large ``read_site_source`` came back partial and the agent
        # re-read in a loop. Always raise it, unless the parent env (which the
        # SDK layers ``env`` over) or the per-run extras already set one.
        sdk_env = sdk_env or {}
        if "MAX_MCP_OUTPUT_TOKENS" not in os.environ:
            cap = getattr(self.settings, "claude_sdk_max_mcp_output_tokens", None)
            if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
                cap = _DEFAULT_MAX_MCP_OUTPUT_TOKENS
            sdk_env.setdefault("MAX_MCP_OUTPUT_TOKENS", str(cap))
        if sdk_env:
            options_kwargs["env"] = sdk_env
        if is_non_anthropic:
            options_kwargs["model"] = llm.model

        # An explicit Claude Code CLI. The SDK otherwise runs the CLI bundled in
        # its wheel and only falls back to PATH when none is bundled, so an
        # installed newer `claude` is never picked up on its own. That is how
        # "Claude Code 2.1.276 does not support this model" persisted with
        # 2.1.283 on PATH. ``isinstance`` because settings are sometimes mocks.
        cli_path = getattr(self.settings, "claude_sdk_cli_path", None)
        if isinstance(cli_path, str) and cli_path.strip():
            options_kwargs["cli_path"] = cli_path.strip()

        # ── Debug logging for troubleshooting SDK startup ──
        logger.info(
            "SDK launch: provider=%s, has_api_key=%s, "
            "CLAUDECODE=%s, CLAUDE_CODE_ENTRYPOINT=%s, "
            "ANTHROPIC_API_KEY=%s, sdk_env_keys=%s, "
            "cli_path=%s, cwd=%s",
            provider,
            bool(llm.api_key),
            os.environ.get("CLAUDECODE", "<unset>"),
            os.environ.get("CLAUDE_CODE_ENTRYPOINT", "<unset>"),
            "set" if os.environ.get("ANTHROPIC_API_KEY") else "<unset>",
            list(sdk_env.keys()) if sdk_env else "none",
            options_kwargs.get("cli_path") or "<bundled>",
            resolved_cwd,
        )

        # Wire in MCP servers (policy-filtered). Skipped entirely on a
        # tools-off turn: an MCP server is a tool source, and registering one
        # whose ids are not on the allowlist still pays its startup.
        mcp_servers = {} if not tools_enabled else self._get_mcp_servers(allow_sdk_tools)
        if mcp_servers:
            options_kwargs["mcp_servers"] = mcp_servers
            logger.info("MCP: passing %d servers to Claude SDK", len(mcp_servers))

        # Enable token-by-token streaming if StreamEvent is available
        if self._StreamEvent is not None:
            options_kwargs["include_partial_messages"] = True

        # Permission handling — PocketPaw always runs headless (web dashboard,
        # Telegram, Discord, Slack, etc.) with no terminal for interactive
        # permission prompts. Without bypassPermissions, tool calls that need
        # approval (like Bash — used by memory save, web search, etc.) hang
        # indefinitely on messaging channels.
        # Dangerous Bash commands are still caught by the PreToolUse hook.
        options_kwargs["permission_mode"] = "bypassPermissions"

        # Model selection for Anthropic providers:
        # 1. Smart routing (opt-in) — overrides with complexity-based model
        # 2. Explicit claude_sdk_model — user-chosen fixed model
        # 3. Neither set — let Claude Code CLI auto-select (recommended)
        if not is_non_anthropic:
            if self.settings.smart_routing_enabled:
                from pocketpaw.agents.model_router import ModelRouter

                model_router = ModelRouter(self.settings)
                selection = model_router.classify(message)
                options_kwargs["model"] = selection.model
            elif self.settings.claude_sdk_model:
                options_kwargs["model"] = self.settings.claude_sdk_model

        # CS-13 — per-send model override. Applied LAST so it wins over ALL of the
        # above: the non-anthropic ``llm.model``, smart-routing's complexity pick,
        # and the configured ``claude_sdk_model``. It is the user's explicit choice
        # for THIS one turn (the composer's model picker), so nothing overrides it.
        # Already validated at the HTTP edge (``CloudAgentChatRequest.model`` —
        # ``max_length`` + a strict character pattern) before it ever reaches this
        # subprocess launch arg. Because ``_client_cache_key`` folds ``model`` in, a
        # turn carrying a different model naturally MISSES the warm client and gets a
        # fresh subprocess — no stale-model reuse. ``None`` (the default, and the
        # only value ``prewarm`` ever passes) leaves the selection above untouched.
        if model_override:
            options_kwargs["model"] = model_override

        # Capture stderr for better error diagnostics
        def _on_stderr(line: str) -> None:
            stderr_sink.append(line)
            logger.debug("Claude CLI stderr: %s", line)

        options_kwargs["stderr"] = _on_stderr

        # Native-resume (feat/session-supervisor SS-1). When the caller threads a
        # ``session_handle`` carrying a ``cli_session_id``, set the SDK's
        # ``resume`` field so the freshly-launched CLI subprocess loads that
        # session's transcript natively (the ``ClaudeAgentOptions.resume: str |
        # None`` field). ``run`` routes a resume-bearing turn down the stateless
        # ``query()`` path precisely so this fresh-launch option is honored (the
        # warm client applies options only at first ``connect()``). Absent /
        # ``None`` leaves ``resume`` unset — the unchanged legacy path.
        if resume_session_id:
            options_kwargs["resume"] = resume_session_id

        # Tenancy-keyed session store (feat/session-supervisor SS-2). When the
        # caller threads a ``session_handle`` carrying a ``session_store``, hand
        # it to the SDK opaquely (the ``ClaudeAgentOptions.session_store: SessionStore
        # | None`` field). On a resume turn the SDK materializes the conversation
        # from THIS store — tenancy-keyed by ``(workspace_id, project_key,
        # session_id)`` — instead of local disk, and mirrors new transcript
        # lines back via ``append``. The object satisfies the SDK's ``SessionStore``
        # protocol by duck typing; OSS never imports the concrete (possibly ee)
        # class, so it flows through as-is and the EE→OSS boundary stays clean.
        # Absent / ``None`` leaves ``session_store`` unset — the unchanged SS-1
        # (and legacy) behavior.
        if session_handle is not None and session_handle.session_store is not None:
            options_kwargs["session_store"] = session_handle.session_store

        # LAST gate before the options become a subprocess. A NUL anywhere in here
        # — an arg, an env value, the cwd — makes the CLI spawn fail with
        # "embedded null character" and names nothing, so every turn on the path
        # dies identically and undiagnosably. Strip it and say where it was; see
        # ``_scrub_nul_chars``. Must stay after EVERY kwarg is set (env and the
        # per-send model override are assigned above) and before
        # ``_ClaudeAgentOptions`` is constructed.
        nul_paths = _scrub_nul_chars(options_kwargs)
        if nul_paths:
            logger.error(
                "SDK: stripped NUL character(s) from options before spawn — "
                "fields: %s. The run proceeds with them removed; this is a BUG at "
                "whatever produced these values, please report the field list.",
                ", ".join(nul_paths),
            )

        # Create options (after all kwargs are set, including model)
        options = self._ClaudeAgentOptions(**options_kwargs)

        return _BuiltOptions(
            options=options,
            options_kwargs=options_kwargs,
            llm=llm,
            run_skills_root=run_skills_root,
            skills_dir_adopted=skills_dir_adopted,
            plugin_digest=plugin_digest,
        )

    async def prewarm(
        self,
        *,
        session_key: str,
        system_prompt: str | None = None,
        deny_mcp_tool_ids: frozenset[str] = frozenset(),
        allow_sdk_tools: frozenset[str] = frozenset(),
        allow_mcp_tool_ids: frozenset[str] | None = None,
        skill_names: frozenset[str] = frozenset(),
        exclusive_mcp_tools: bool = False,
        system_prompt_digest: str = "",
        model_override: str | None = None,
        tools_enabled: bool = True,
    ) -> None:
        """Eagerly ``connect()`` the warm CLI subprocess for a session before its
        first turn, so the first real ``run`` reuses it instead of paying the
        ~12s cold ``connect()`` (feat/claude-sdk-prewarm).

        ``model_override`` / ``tools_enabled`` are the turn's per-send model pick
        and tool switch. They change the model / ``allowed_tools`` and so the
        cache key; passing the turn's values is what lets a model-picker or
        tools-off turn reuse the prewarmed client instead of rebuilding it.

        Prewarm never evicts: it is a no-op while a run holds the lease (checked
        again under the client lock) and when a client that already served this
        session is live, whatever that client's key.

        Builds the SAME options the first turn will (via ``_build_options``) and
        hands them to ``_get_or_create_client`` with the SAME ``session_key`` +
        ``plugin_digest`` — so the cache key matches and the first turn finds the
        client already live. If the keys diverged the first turn would EVICT the
        prewarmed client (a net loss), which is why the caller must prewarm with
        the same model/tools/prefix/skills the first turn will use.

        ``system_prompt_digest`` (PA-6) joins that list, and it is the reason the
        prewarm still matches after the cutover. ``AgentPool.prewarm`` assembles
        with ``message=""`` and ``knowledge_context=""``, which changes the
        rendered TEXT — the old prefix survived that only because those two
        blocks sit below its cut. The digest survives it for a stronger reason:
        ``legacy_tail`` and ``retrieval`` are the layers those two fields feed and
        both declare ``cache_key=None``, so neither can reach the digest at all.

        It takes no history. History rides the first turn's query text, and a
        prewarmed client has seen none, so turn 1 sends it the whole
        conversation (``_turn_query_text``) and the prewarm is never rebuilt.

        FIRE-AND-FORGET, never-break-a-turn semantics:
          * ALL exceptions are logged and SWALLOWED — a failed prewarm must never
            propagate to (or poison) a later turn. On failure the half-built
            client is torn down and the lease released, so the next ``run`` starts
            clean and simply pays the cold connect it would have paid anyway (no
            regression).
          * If a run is already active (``_client_in_use``) prewarm is a NO-OP —
            it must not contend for the lease or disturb an in-flight stream.
          * If the SDK / CLI is unavailable it is a no-op (nothing to warm).

        The skills-dir lifecycle is identical to ``run``'s warm path: when
        ``skill_names`` is non-empty ``_build_options`` materializes + adopts the
        plugin dir into ``self._skills_dir_by_digest`` keyed on ``plugin_digest``,
        and the warm client created here owns it until eviction / ``cleanup()``.
        On a prewarm failure that adopted dir is dropped so it doesn't leak.
        """
        if not self._sdk_available or not self._cli_available:
            return
        # A run holds the lease — never contend. The in-flight turn will create
        # (or already created) the warm client itself.
        if self._client_in_use:
            logger.debug("prewarm skipped: a run is active (_client_in_use)")
            return

        adopted_digest = ""
        try:
            built = await self._build_options(
                "",  # no real message yet — safe only when the model is
                # message-independent (smart routing OFF). The trigger gates on
                # this; see prewarm_session in run_core.
                system_prompt=system_prompt,
                session_key=session_key,
                deny_mcp_tool_ids=deny_mcp_tool_ids,
                allow_sdk_tools=allow_sdk_tools,
                allow_mcp_tool_ids=allow_mcp_tool_ids,
                skill_names=skill_names,
                exclusive_mcp_tools=exclusive_mcp_tools,
                model_override=model_override,
                tools_enabled=tools_enabled,
                stderr_sink=[],
            )
            # Remember the digest we may have adopted a dir under, so a failed
            # connect can release it instead of leaking.
            if built.skills_dir_adopted:
                adopted_digest = built.plugin_digest
            warmed = await self._get_or_create_client(
                built.options,
                session_key=session_key,
                plugin_digest=built.plugin_digest,
                system_prompt_digest=system_prompt_digest,
                prewarm=True,
            )
            if warmed is None:
                if adopted_digest and self._client_plugin_digest != adopted_digest:
                    self._drop_skills_dir(adopted_digest)
                return
            logger.info(
                "Prewarmed Claude client for session_key=%s (skills=%d)",
                session_key,
                len(skill_names),
            )
        except Exception as exc:  # noqa: BLE001 — prewarm must NEVER raise
            logger.warning("Prewarm failed (swallowed, turn unaffected): %s", exc)
            # Tear down any half-built client so the next run starts on a clean
            # slate (and just pays the cold connect). CRITICAL: prewarm never
            # acquired the ``_client_in_use`` lease, so it must NEVER clear it —
            # a run firing concurrently may legitimately own it, and stealing it
            # would corrupt that run. Do the teardown UNDER the client lock and
            # only when no run holds the lease, so we can't disconnect a client a
            # sibling run is actively using.
            # ``_get_or_create_client`` already disconnected the client whose
            # connect failed and never cached it, so the only thing left to
            # release is a skills dir adopted for a client that does not exist.
            if self._client_lock is None:
                self._client_lock = asyncio.Lock()
            async with self._client_lock:
                if adopted_digest and not (
                    self._client is not None and self._client_plugin_digest == adopted_digest
                ):
                    self._drop_skills_dir(adopted_digest)

    async def _leased_dispatch(
        self,
        *,
        message: str,
        options: Any,
        this_turn_key: str,
        resume_active: bool,
        warm_client: LeasedClient | None,
        on_client_built: Callable[[Any, str, Callable], None] | None,
        image_attachments: tuple[ImageAttachment, ...] = (),
        history: list[dict] | None = None,
        turn_context: str = "",
    ) -> tuple[Any, LeasedClient | None]:
        """feat/warm-reuse WH-1 — route a turn against a caller-LEASED warm client.

        Called by ``run`` only when ``warm_client`` or ``on_client_built`` is set.
        The backend's own ``self._client`` is NOT involved here — the supervisor
        owns the leased client's lifecycle, so this never touches ``self._client``,
        ``acquired_lease``, or ``self._client_in_use``.

        Returns ``(event_stream, warm_lease)``:
          * ``event_stream`` is the message iterator to stream, or ``None`` to make
            the caller fall through to a fresh stateless ``query()`` for this turn.
          * ``warm_lease`` is the ``LeasedClient`` whose ``busy`` flag THIS turn set
            (the caller clears it in its finally), or ``None`` when no lease is held.

        Three outcomes:
          1. WARM REUSE — ``warm_client`` key matches, not a resume turn, lease not
             ``busy``, and ``history`` still extends what the client has seen →
             drive ``warm_client.client.query`` directly (no connect, no resume)
             with this turn's context and only the history it has not seen, and
             return its receive iterator. The lease stays ``busy`` for the
             stream's duration and is NEVER disconnected. A diverged history
             (an edit or delete) skips reuse and takes step 2.
          2. SUPERVISED FRESH BUILD — ``on_client_built`` set and warm reuse did not
             apply → build + ``connect()`` a fresh client (``options`` already carry
             ``resume`` iff ``session_handle.cli_session_id`` was set), hand it to
             ``on_client_built(client, this_turn_key, teardown)`` for the supervisor
             to own, then query it.
          3. STATELESS FALLBACK — a busy matching lease, any connect/query failure,
             or a ``warm_client`` key mismatch with no ``on_client_built`` to rebind
             → return ``(None, None)`` so the caller runs a fresh stateless query.
        """
        # ── 1. Warm reuse ────────────────────────────────────────────────
        # A resume turn must take a fresh launch (the live client carries its OWN
        # conversation, not the requested on-disk session), so warm reuse is gated
        # on ``not resume_active`` as well as an exact key match.
        if (
            warm_client is not None
            and not resume_active
            and warm_client.options_key == this_turn_key
            and self._client_knows_history(warm_client.client, history)
        ):
            # Busy detection: the lease's own ``busy`` flag. Single-threaded
            # asyncio means the check-then-set below has no ``await`` between it, so
            # it is atomic — a second concurrent turn can never both see "free" and
            # then both drive a query. A busy lease falls back to a fresh stateless
            # client for THIS turn (never blocks, never corrupts the shared client)
            # and does NOT rebind the slot.
            if warm_client.busy:
                logger.info(
                    "WH-1: leased warm client is busy (concurrent turn) — "
                    "fresh stateless fallback for this turn"
                )
                return None, None
            warm_client.busy = True
            try:
                logger.info("WH-1: reusing leased warm client (key match) — no connect, no resume")
                text = self._turn_query_text(
                    warm_client.client, message, history=history, turn_context=turn_context
                )
                await warm_client.client.query(
                    stream_one_message(build_streaming_user_message(text, image_attachments))
                    if image_attachments
                    else text
                )
                return self._resilient_receive(warm_client.client), warm_client
            except Exception as exc:  # noqa: BLE001
                # The leased client failed mid-send. Release its busy flag (we no
                # longer drive it) but do NOT disconnect — the supervisor owns it.
                # What it holds is now unknown, so its next turn rebuilds.
                self._forget_history(warm_client.client)
                warm_client.busy = False
                logger.warning("WH-1: leased warm client query failed, stateless fallback: %s", exc)
                return None, None

        # ── 2. Supervised fresh build ────────────────────────────────────
        if on_client_built is not None:
            try:
                fresh = self._ClaudeSDKClient(options=options)
                try:
                    await self._connect_client(fresh)
                except Exception:
                    await self._discard_client(fresh)
                    raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "WH-1: supervised fresh client connect failed, stateless fallback: %s",
                    exc,
                )
                return None, None

            async def _teardown() -> None:
                """Disconnect the client THIS run built. The supervisor calls this
                when it drops / replaces the slot — the backend never caches the
                client on ``self._client``."""
                try:
                    await fresh.disconnect()
                except Exception as disc_exc:  # noqa: BLE001
                    logger.debug(
                        "WH-1: leased fresh client teardown disconnect error (ignored): %s",
                        disc_exc,
                    )

            try:
                on_client_built(fresh, this_turn_key, _teardown)
            except Exception as exc:  # noqa: BLE001
                # The supervisor refused the slot — tear the client down so it
                # doesn't leak, then fall back to stateless so the turn completes.
                logger.warning(
                    "WH-1: on_client_built raised, tearing down + stateless fallback: %s",
                    exc,
                )
                await _teardown()
                return None, None

            try:
                logger.info(
                    "WH-1: supervised fresh client built + bound (resume=%s) — driving query",
                    bool(getattr(options, "resume", None)),
                )
                text = self._turn_query_text(
                    fresh,
                    message,
                    history=history,
                    turn_context=turn_context,
                    history_is_native=resume_active,
                )
                await fresh.query(
                    stream_one_message(build_streaming_user_message(text, image_attachments))
                    if image_attachments
                    else text
                )
                # The supervisor now OWNS the client; the backend does not tear it
                # down here even if the stream later aborts.
                return self._resilient_receive(fresh), None
            except Exception as exc:  # noqa: BLE001
                self._forget_history(fresh)
                logger.warning(
                    "WH-1: supervised fresh client query failed, stateless fallback: %s",
                    exc,
                )
                return None, None

        # ── 3. Stateless fallback ────────────────────────────────────────
        # ``warm_client`` provided but key-mismatched / resume / busy, and no
        # ``on_client_built`` to build + rebind → run a fresh stateless query.
        return None, None

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
        deny_mcp_tool_ids: frozenset[str] = frozenset(),
        allow_sdk_tools: frozenset[str] = frozenset(),
        allow_mcp_tool_ids: frozenset[str] | None = None,
        skill_names: frozenset[str] = frozenset(),
        session_handle: SessionHandle | None = None,
        warm_client: LeasedClient | None = None,
        on_client_built: Callable[[Any, str, Callable], None] | None = None,
        model_override: str | None = None,
        exclusive_mcp_tools: bool = False,
        system_prompt_digest: str = "",
        tools_enabled: bool = True,
        # Images the user attached to this turn. They ride the PERSISTENT-client
        # paths only: the SDK's single-message mode explicitly does not support
        # image attachments, so a stateless fresh-launch turn carrying images
        # would drop them silently. See where this is consumed below.
        image_attachments: tuple[ImageAttachment, ...] = (),
        # Per-turn volatile context (KB hits, scope/participants, uploaded-file
        # text, soul recall). Sent in the query text, never the system prompt.
        turn_context: str = "",
    ) -> AsyncIterator[AgentEvent]:
        """Process a message through Claude Agent SDK with streaming.

        Yields AgentEvent objects as the agent responds.

        ``turn_context`` and ``history`` reach the model in the QUERY TEXT
        (``compose_turn_message``), never in ``options.system_prompt``: the SDK
        applies the prompt only at ``connect()``, so on a reused warm client
        anything per-turn in it would stay whatever the connecting turn carried.
        A warm client is sent only the history it has not seen; a fresh or
        stateless one gets all of it; a native-resume launch gets none.

        ``system_prompt_digest`` (PA-6) is the assembler's ``stable_digest``, and
        it replaces ``_behavior_prefix`` in the warm-client cache key for every
        caller that has one. Declared rather than swallowed by ``**kwargs``:
        ``AgentPool._accepts_prompt_digest`` reads this signature to decide
        whether to pass it, and a backend that accepted it silently would look
        ported while keying on nothing. Empty = a caller outside the assembler,
        which keeps the prefix. As of PA-7b that is no longer the channel path
        (``AgentLoop`` forwards a digest through ``AgentRouter``); it is the
        pocket specialist calling ``backend.run`` directly and any out-of-tree
        embedder doing the same. See ``_client_cache_key``.

        ``session_handle`` (feat/session-supervisor SS-1) carries native-resume
        identity. When it holds a non-None ``cli_session_id``, the SDK options
        get ``resume=<cli_session_id>`` (so the CLI subprocess resumes that
        on-disk session natively instead of replaying Mongo history) and THIS
        run is routed down the FRESH stateless ``query()`` launch path — never
        the warm persistent client, whose options freeze at first ``connect()``
        and whose cache key omits ``resume`` (so a reused warm client would
        silently ignore a fresh ``resume``). The per-turn ``system_prompt`` is
        still passed on every turn, so a resumed session honors a rebuilt
        prompt. When a handle is present, the SDK's turn-1 init/system message
        ``session_id`` is extracted and surfaced once as a ``session_id``
        AgentEvent (for the controller to persist — SS-3). ``cli_session_id is
        None`` / no handle = the UNCHANGED legacy warm-client path. The
        ``session_store`` field is opaque here (SS-2 owns it).

        ``deny_mcp_tool_ids`` is a per-surface MCP-tool deny set threaded down
        from the chat loop (resolved from the request's ``SurfaceProfile``).
        Any id in it is subtracted from ``allowed_tools`` before the SDK
        launches, so the agent is physically unable to call those tools. Empty
        by default (a no-op for legacy / non-/sites runs); non-empty only on the
        /sites svelte-create surface, where it forbids the two ripple-create
        tools so the agent cannot fall back to a rippleSpec landing page. This
        is the typed replacement for the deleted prompt-sniffing gate.

        ``allow_sdk_tools`` is the per-entity ADDITIVE SDK-tool allowlist
        (entity-rooms chunk ①), resolved from the entity pocket's
        ``surface_profile.allowed_sdk_tools``. It is UNIONed into
        ``allowed_tools`` BEFORE the deny subtraction — precedence
        ``effective = (agent_tools ∪ allow) − deny`` (the deny is the hard cap,
        so an allow can never re-enable a denied id). Empty by default (a no-op
        for legacy / non-entity runs).

        ``skill_names`` is the per-entity skill subset (entity-rooms A2), resolved
        from the entity pocket's ``surface_profile.skill_names``. When non-empty,
        the named skills are MATERIALIZED into a throwaway local-plugin directory
        and appended to the SDK ``plugins=`` list, so the agent sees ONLY those
        skills (coexisting with the bundled-skills plugin when that is enabled).
        ``setting_sources=[]`` disables both filesystem discovery and the SDK
        ``skills=`` option, so a local plugin is the only working channel — the
        same mechanism the bundled skills already use. CRITICAL: the persistent
        ("warm") client applies its options only at first ``connect()`` and the
        cache key does NOT include ``plugins=``, so a warm client connected
        WITHOUT these skills would silently ignore them. So when ``skill_names``
        is non-empty we BYPASS the warm client and run on a fresh stateless query
        whose options carry the materialized plugin. The temp dir is removed in a
        ``finally`` after the stream drains. Empty by default (a no-op).

        ``warm_client`` / ``on_client_built`` (feat/warm-reuse WH-1) let the
        SessionSupervisor drive the turn against a caller-LEASED warm client
        instead of the backend's own ``self._client``. When either is set, this
        turn's ``_client_cache_key`` is computed once and ``_leased_dispatch``
        routes the turn (warm reuse on a key match, else a supervised fresh build
        handed to ``on_client_built`` for the supervisor to own, with a busy lease
        falling back to a fresh stateless query). Neither set → the unchanged
        legacy ``self._client`` path. See the module docstring for the full table.
        """
        # Every parameter of this call, captured before anything else is bound,
        # so the crash retry below re-runs the SAME turn (tool caps, model, skills,
        # images…) and a parameter added later is forwarded without anyone
        # remembering to list it. MUST stay the first statement.
        _call_kwargs = {k: v for k, v in locals().items() if k not in ("self", "message")}
        if not self._sdk_available:
            yield AgentEvent(
                type="error",
                content=(
                    "❌ Claude Agent SDK Python package not found.\n\n"
                    "Install with: pip install claude-agent-sdk\n\n"
                    "Or switch to **PocketPaw Native** backend in **Settings → General**."
                ),
            )
            return

        if not self._cli_available:
            yield AgentEvent(
                type="error",
                content=(
                    "❌ Claude Code CLI not found on this machine.\n\n"
                    "The Claude Agent SDK backend requires the CLI. To fix this:\n\n"
                    "**Install Claude Code CLI:**\n"
                    "- Windows: `irm https://claude.ai/install.ps1 | iex`\n"
                    "- macOS/Linux: `curl -fsSL https://claude.ai/install.sh | bash`\n"
                    "- Or: `npm install -g @anthropic-ai/claude-code`\n\n"
                    "Then set your `ANTHROPIC_API_KEY` in **Settings → General**.\n\n"
                    "Or switch to a different backend in **Settings → General** "
                    "(OpenAI Agents, Google ADK, Codex, etc.) that doesn't need the CLI."
                ),
            )
            return

        # Status only (``get_status``). Whether THIS run was stopped lives in
        # ``_run_state``, which a later run cannot reset.
        self._stop_flag = False
        _run_token = object()
        _run_state = _RunState(session_key)
        self._active_runs[_run_token] = _run_state

        # ── Prevent the SDK from closing stdin too early ──────────
        # When hooks are present the SDK's stream_input() waits for
        # the first ResultMessage before closing stdin.  The default
        # timeout is 60 s which is far too short for long-running
        # tool use (file search, code analysis, etc.).  Set to 24 h
        # so the agent can work as long as it needs.
        os.environ.setdefault(
            "CLAUDE_CODE_STREAM_CLOSE_TIMEOUT",
            str(24 * 60 * 60 * 1000),  # 24 hours in ms
        )

        _stderr_lines: list[str] = []

        # Per-run materialized-skills plugin dir (entity-rooms A2). Declared
        # above the try so the finally can always clean it up, even if an
        # exception fires before/after materialization. None on every run that
        # doesn't pass a non-empty ``skill_names``.
        run_skills_root: Path | None = None

        # fix/claude-sdk-warm-client-skills: ``skills_dir_adopted`` is True when
        # ``run_skills_root`` was handed to (or reused by) a WARM client — its
        # lifetime then belongs to ``self._skills_dir_by_digest`` and the
        # per-run finally must NOT rmtree it (the live subprocess still holds the
        # path). It stays False on the genuine stateless-fallback path, where the
        # dir is this run's alone and the finally is the only thing that removes
        # it. ``plugin_digest`` is the plugin-identity hash threaded into the
        # cache key + the dir cache.
        skills_dir_adopted = False
        plugin_digest = ""

        # Ownership flag — True only if THIS run acquired the shared
        # _client_in_use lease. Declared above the try/except so it is always
        # in scope in the except handler (an exception can fire before the
        # dispatch block runs). Both the finally block and the except handler
        # gate the lease clear and the persistent-client teardown on this so a
        # non-owning run (stateless fallback, or a failure before acquisition)
        # can never release a sibling's lease or destroy its subprocess.
        acquired_lease = False
        # The warm client THIS run drives under the lease. Teardown is always of
        # this object, and the backend's slot is cleared only while it still
        # holds it: after a cleanup() the slot may belong to the next run.
        _persistent_client: Any = None
        # feat/warm-reuse WH-1: the ``LeasedClient`` whose ``busy`` flag THIS run
        # set on the warm-reuse path. Declared above the try so the finally /
        # except can always release it (set ``busy=False``) without ever
        # disconnecting it — the supervisor owns the leased client's lifecycle.
        # None on every legacy / supervised-fresh / stateless run.
        _warm_lease: LeasedClient | None = None
        # Resolved LLM client — bound by ``_build_options`` below. Declared above
        # the try (feat/claude-sdk-prewarm) so the ``except`` handler's
        # ``llm.format_api_error`` call is safe even when ``_build_options`` itself
        # raises before returning (e.g. the provider resolves but options
        # construction fails); the handler guards ``llm is None`` for that case.
        llm: Any = None
        try:
            # Assemble the SDK options (feat/claude-sdk-prewarm). The whole
            # block that built ``options_kwargs`` -> ``options`` (LLM resolve,
            # model routing, system-prompt assembly, tool allow/deny, bundled +
            # per-run skills materialization, subprocess env, MCP wiring) now
            # lives in the shared ``_build_options`` helper so ``prewarm`` can
            # build the IDENTICAL options the first turn will — same cache key,
            # so a prewarmed warm client is REUSED here, not evicted. The
            # returned triple (run_skills_root / skills_dir_adopted /
            # plugin_digest) drives the dispatch + finally exactly as before; the
            # stderr sink is this run's ``_stderr_lines`` so CLI diagnostics still
            # flow to the error handlers below.
            _built = await self._build_options(
                message,
                system_prompt=system_prompt,
                session_key=session_key,
                deny_mcp_tool_ids=deny_mcp_tool_ids,
                allow_sdk_tools=allow_sdk_tools,
                allow_mcp_tool_ids=allow_mcp_tool_ids,
                skill_names=skill_names,
                session_handle=session_handle,
                model_override=model_override,
                exclusive_mcp_tools=exclusive_mcp_tools,
                tools_enabled=tools_enabled,
                stderr_sink=_stderr_lines,
            )
            options = _built.options
            options_kwargs = _built.options_kwargs
            llm = _built.llm
            run_skills_root = _built.run_skills_root
            skills_dir_adopted = _built.skills_dir_adopted
            plugin_digest = _built.plugin_digest

            logger.debug(f"🚀 Starting Claude Agent SDK query: {message[:100]}...")

            # Try persistent client first, fall back to stateless query.
            # _client_in_use guard prevents concurrent queries on the same
            # subprocess — cross-session messages fall back to stateless query.
            event_stream = None
            logger.info(
                "SDK dispatch: _client_in_use=%s, session_key=%s",
                self._client_in_use,
                session_key,
            )
            _persistent_client = None
            # Native-resume runs (feat/session-supervisor SS-1) MUST take the
            # fresh stateless ``query()`` launch path, never the warm persistent
            # client: the warm client applies its options (incl. ``resume``) only
            # at first ``connect()``, and ``_client_cache_key`` does NOT fold in
            # ``resume`` — so a reused warm client would silently ignore the fresh
            # ``resume`` and continue its OWN in-memory conversation instead of
            # the requested on-disk session. The stateless ``query()`` spawns a
            # fresh subprocess per call that honors ``options.resume`` directly.
            _resume_active = (
                session_handle is not None and session_handle.cli_session_id is not None
            )
            # feat/warm-reuse WH-1: when the caller LEASES a warm client
            # (``warm_client``) or wants to OWN the freshly-built one
            # (``on_client_built``), compute THIS turn's cache key ONCE and route
            # through ``_leased_dispatch`` instead of the backend's own
            # ``self._client`` path. The key is recomputed via the pure
            # ``_client_cache_key`` classmethod with the SAME
            # ``options``/``session_key``/``plugin_digest`` that
            # ``_get_or_create_client`` would hash internally — byte-identical, so
            # the legacy ``self._client`` branch below stays untouched. The
            # supervised paths never set ``acquired_lease`` / ``self._client_in_use``
            # (they don't own ``self._client``), so the finally / except teardown of
            # the per-agent warm client cannot misfire on a leased client.
            if warm_client is not None or on_client_built is not None:
                this_turn_key = self._client_cache_key(
                    options,
                    session_key=session_key,
                    plugin_digest=plugin_digest,
                    tenant_scope=self._tenant_scope_key(),
                    system_prompt_digest=system_prompt_digest,
                )
                event_stream, _warm_lease = await self._leased_dispatch(
                    message=message,
                    options=options,
                    this_turn_key=this_turn_key,
                    resume_active=_resume_active,
                    warm_client=warm_client,
                    on_client_built=on_client_built,
                    image_attachments=image_attachments,
                    history=history,
                    turn_context=turn_context,
                )
            # fix/claude-sdk-warm-client-skills: the warm-client bypass for skill
            # runs is REMOVED. ``_client_cache_key`` now folds in
            # ``plugin_digest`` (the skill-identity hash), so a warm client can
            # distinguish a skill run from a non-skill one — a same-skill turn
            # reuses the subprocess and a changed skill set rebuilds it. The
            # materialized plugin dir was cached + adopted above so the warm
            # subprocess keeps a valid path across turns. Fallbacks to the
            # stateless path: the original concurrency guard (a sibling run holds
            # the lease, ``_client_in_use``) OR a native-resume turn.
            elif not self._client_in_use and not _resume_active:
                try:
                    self._client_in_use = True
                    self._lease_token = _run_token
                    acquired_lease = True
                    _persistent_client = await self._get_or_create_client(
                        options,
                        session_key=session_key,
                        plugin_digest=plugin_digest,
                        system_prompt_digest=system_prompt_digest,
                        history=history,
                    )
                    turn_text = self._turn_query_text(
                        _persistent_client,
                        message,
                        history=history,
                        turn_context=turn_context,
                    )
                    logger.info(
                        "Persistent client: sending query (%d chars, %d image(s))",
                        len(message),
                        len(image_attachments),
                    )
                    # The images ride HERE too, not only on the two leased sends
                    # in ``_leased_dispatch``. This is the "unchanged legacy
                    # ``self._client``" route the module docstring names, and it
                    # is what runs whenever no SessionSupervisor is driving the
                    # turn — which is most turns. #2171 wired the payload into
                    # the leased sends only, so on this path an attached image
                    # was dropped and the model got the attachments NOTE with no
                    # pixels: "I can see screenshot-compare.jpg is attached, but
                    # the image itself isn't coming through to me on this turn."
                    #
                    # It is not a capability limit. ``_get_or_create_client``
                    # returns a persistent ``ClaudeSDKClient``, and the SDK docs
                    # list "Image uploads: attach images directly to messages"
                    # as a streaming-input capability. Only the stateless
                    # ``query()`` fallback below genuinely cannot carry one —
                    # the same docs say single-message input does NOT support
                    # direct image attachments.
                    #
                    # Withhold-when-empty, as on both leased sends: a turn with
                    # no attachment keeps sending the bare string every existing
                    # run sends, rather than a one-element parts list.
                    await _persistent_client.query(
                        stream_one_message(
                            build_streaming_user_message(turn_text, image_attachments)
                        )
                        if image_attachments
                        else turn_text
                    )
                    # _resilient_receive reads one turn through receive_response()
                    # (traced by logfire) and survives MessageParseError, so
                    # stale events cannot leak into the next turn.
                    event_stream = self._resilient_receive(_persistent_client)
                    logger.info("Persistent client: _resilient_receive() ready")
                except Exception as client_err:
                    logger.warning(
                        "Persistent client failed, falling back to stateless query: %s",
                        client_err,
                    )
                    # Log stderr lines captured so far
                    if _stderr_lines:
                        logger.warning(
                            "CLI stderr during persistent client failure:\n%s",
                            "\n".join(_stderr_lines),
                        )
                    # Disconnect the broken client (dropping it without that
                    # leaked its subprocess) so the next call creates a fresh
                    # one. A failed connect never reached the slot, so there is
                    # nothing more to drop then. This run is falling back to
                    # stateless: release the lease and the ownership flag so the
                    # finally/except teardown below cannot misfire.
                    if _persistent_client is not None and self._client is _persistent_client:
                        self._client = None
                        self._client_options_key = None
                        self._client_session_key = None
                        await self._discard_client(_persistent_client)
                    if self._lease_token is _run_token:
                        self._client_in_use = False
                        self._lease_token = None
                    acquired_lease = False
                    _persistent_client = None
                    # No warm client adopted the skills dir we cached for this
                    # digest (connect failed before/at adoption). The stateless
                    # query below uses ``options`` (which already point at the
                    # dir) for THIS run only, so transfer ownership back to the
                    # per-run finally: drop it from the digest cache and clear the
                    # adopted flag so the finally cleans it up exactly once.
                    if skills_dir_adopted:
                        self._skills_dir_by_digest.pop(plugin_digest, None)
                        self._client_plugin_digest = ""
                        skills_dir_adopted = False

            if event_stream is None:
                logger.info("Starting stateless query (reason: _client_in_use was True)")
                if image_attachments:
                    # The one place an attached image genuinely CANNOT ride. The
                    # SDK docs are explicit that single-message input "does NOT
                    # support: Direct image attachments in messages", so there is
                    # no payload shape that would work here — unlike the
                    # persistent send above, which was a wiring gap and is fixed.
                    #
                    # Logged rather than silent because the user-visible result is
                    # a model that says it can see the filename and not the
                    # pixels, and every other cause of that looks identical from
                    # the outside. The turn still runs: the attachments block
                    # names the file, so the model knows one arrived.
                    #
                    # Reachable on three routes, all of which force this path: a
                    # native-resume turn, a sibling run holding the client lease,
                    # and a turn carrying per-entity skills. A resume turn could
                    # in principle keep its images by driving a FRESH persistent
                    # client with ``resume`` in its options (what the supervised
                    # build does) instead of a stateless query — that is a
                    # dispatch change, not a payload one, and is left to its own
                    # PR.
                    logger.warning(
                        "stateless query cannot carry image attachments; "
                        "%d image(s) dropped for this turn "
                        "(resume_active=%s, client_in_use=%s, skills=%s)",
                        len(image_attachments),
                        _resume_active,
                        self._client_in_use,
                        bool(skill_names),
                    )
                # A stateless launch holds nothing, so it gets the full history
                # in the prompt text; a native-resume launch gets none (its CLI
                # session carries the conversation).
                event_stream = self._resilient_query(
                    prompt=self._turn_query_text(
                        None,
                        message,
                        history=None if _resume_active else history,
                        turn_context=turn_context,
                    ),
                    options=options,
                )

            # State tracking for StreamEvent deduplication
            _streamed_via_events = False
            _event_count = 0
            _saw_result = False  # Track if ResultMessage was consumed
            # The model that actually answered (from AssistantMessage.model) —
            # feeds token_usage's ``model`` when the CLI auto-selected.
            _last_seen_model: str | None = None
            # feat/session-supervisor SS-1: emit the native session id at most
            # once per run (from the SDK's turn-1 init/system message). Gated on
            # an opted-in ``session_handle`` so the legacy stream is byte-identical.
            _session_id_emitted = False
            # tool_use_id → MCP tool id. The SDK's ServerToolResultBlock carries
            # only ``tool_use_id`` (no name); the AssistantMessage's resolved
            # tool_use carries the id+name. Correlate so the loop's tool_result
            # name-match pops the right pending entry (and the UI shows the real
            # tool, not a fallback).
            _server_tool_names: dict[str, str] = {}
            # Text of an assistant message the CLI synthesized for an API error
            # (``AssistantMessage.error`` set). It is not a reply: it is held back
            # and reported once, as an error, with the result.
            _api_error_text: str | None = None
            _error_emitted = False

            # Stream responses — release the persistent client guard when done
            try:
                async for event in event_stream:
                    _event_count += 1
                    if _event_count <= 3:
                        logger.info(
                            "SDK event #%d: type=%s",
                            _event_count,
                            type(event).__name__,
                        )
                    if _run_state.stopped:
                        logger.info("🛑 Stop requested for this run, breaking stream")
                        break

                    if event is _STATELESS_STREAM_CUT:
                        yield AgentEvent(
                            type="error",
                            content=(
                                "The reply was cut off: the Claude CLI sent a message "
                                "this SDK could not read. Please try again."
                            ),
                        )
                        _error_emitted = True
                        continue

                    # Handle different message types using isinstance checks

                    # ========== StreamEvent - token-by-token streaming ==========
                    if self._StreamEvent and isinstance(event, self._StreamEvent):
                        raw = getattr(event, "event", None) or {}
                        event_type = raw.get("type", "")
                        delta = raw.get("delta", {})

                        if event_type == "content_block_delta":
                            if "text" in delta:
                                yield AgentEvent(type="message", content=delta["text"])
                                _streamed_via_events = True
                            elif "thinking" in delta:
                                yield AgentEvent(type="thinking", content=delta["thinking"])
                        elif event_type == "content_block_start":
                            cb = raw.get("content_block", {})
                            if cb.get("type") == "tool_use":
                                tool_name = cb.get("name", "unknown")
                                # PROVISIONAL announcement. ``content_block_start``
                                # opens the block before a single argument fragment
                                # has streamed, so the name is known here and the
                                # input is not. Emit anyway — a prompt "tool
                                # started" indicator is the whole reason this
                                # branch exists — and flag ``input_pending`` so a
                                # consumer can tell the empty ``input`` is a
                                # placeholder that the AssistantMessage branch
                                # below supersedes with the real arguments.
                                yield AgentEvent(
                                    type="tool_use",
                                    content=f"Using {tool_name}...",
                                    metadata={
                                        "name": tool_name,
                                        "input": {},
                                        "input_pending": True,
                                    },
                                )
                        elif event_type == "content_block_stop":
                            if getattr(event, "_block_type", None) == "thinking":
                                yield AgentEvent(type="thinking_done", content="")
                        continue

                    # ========== SystemMessage - metadata ==========
                    if self._SystemMessage and isinstance(event, self._SystemMessage):
                        subtype = getattr(event, "subtype", "")
                        # feat/session-supervisor SS-1: the SDK's init/system
                        # message carries the native ``session_id`` in its
                        # ``data`` dict. When the caller opted into a
                        # ``session_handle``, capture it on turn 1 and surface it
                        # ONCE as a ``session_id`` AgentEvent (mirroring the
                        # ``token_usage`` metadata event) so the controller can
                        # persist it for a later ``resume`` turn (SS-3). Gated on
                        # the handle so the legacy stream stays byte-identical.
                        if session_handle is not None and not _session_id_emitted:
                            _data = getattr(event, "data", None)
                            _sid = _data.get("session_id") if isinstance(_data, dict) else None
                            if _sid:
                                _session_id_emitted = True
                                logger.info(
                                    "session_id captured from init SystemMessage (id=%s)",
                                    _sid,
                                )
                                yield AgentEvent(
                                    type="session_id",
                                    content="",
                                    metadata={
                                        "session_id": _sid,
                                        "backend": "claude_agent_sdk",
                                    },
                                )
                        logger.debug(f"SystemMessage: {subtype}")
                        continue

                    # ========== UserMessage - extract media from tool results ==========
                    if self._UserMessage and isinstance(event, self._UserMessage):
                        # UserMessages in multi-turn SDK flow contain ToolResultBlocks
                        # with the raw output of tool calls. Bash results are a plain
                        # str; MCP-server tool results ride as a LIST OF DICTS
                        # (``[{"type": "text", "text": ...}]``) — the exact shape the
                        # CLI emits for ``mcp__pocketpaw_studio__build_studio_flow``.
                        # ``_mcp_result_text`` unwraps str/list/dict so a tool_result
                        # is NEVER dropped for an unexpected wrapper.
                        if hasattr(event, "content") and isinstance(event.content, list):
                            for block in event.content:
                                if self._ToolResultBlock and isinstance(
                                    block, self._ToolResultBlock
                                ):
                                    _t_result = _mcp_result_text(getattr(block, "content", None))
                                    _t_name = _server_tool_names.get(
                                        getattr(block, "tool_use_id", ""), "bash"
                                    )
                                    if _t_result:
                                        yield AgentEvent(
                                            type="tool_result",
                                            content=_t_result,
                                            metadata={"name": _t_name},
                                        )
                                    continue
                                # MCP-server tool results (SDK >= 0.1.5x surface
                                # these as ServerToolResultBlock, which is NOT a
                                # ToolResultBlock subclass and was previously
                                # dropped — so MCP tool calls never emitted a
                                # tool_result event and the loop could not fan
                                # the result to a dedicated SystemEvent. This is
                                # the pipeline that powers agent-built /studio
                                # flows (build_studio_flow) and pocket creation
                                # on the default claude_agent_sdk backend.)
                                if self._ServerToolResultBlock and isinstance(
                                    block, self._ServerToolResultBlock
                                ):
                                    _mcp_text = _mcp_result_text(getattr(block, "content", None))
                                    _tool_name = _server_tool_names.get(
                                        getattr(block, "tool_use_id", ""),
                                        "mcp_server",
                                    )
                                    if _mcp_text:
                                        yield AgentEvent(
                                            type="tool_result",
                                            content=_mcp_text,
                                            metadata={"name": _tool_name},
                                        )
                        logger.debug("UserMessage processed")
                        continue

                    # ========== AssistantMessage - main content ==========
                    if self._AssistantMessage and isinstance(event, self._AssistantMessage):
                        # Remember the model that actually answered — when the
                        # CLI auto-selects (no explicit model option), this is
                        # the only truthful source for token_usage's ``model``.
                        _msg_model = getattr(event, "model", None)
                        if isinstance(_msg_model, str) and _msg_model:
                            _last_seen_model = _msg_model
                        if getattr(event, "error", None):
                            # The CLI's stand-in reply for a failed API call
                            # ("API Error: 400 …"). Never the assistant's answer:
                            # emitting it put the raw error in the reply AND
                            # persisted it as the message.
                            _api_error_text = self._extract_text_from_message(event) or str(
                                event.error
                            )
                            logger.warning("SDK API error message: %s", _api_error_text)
                            _streamed_via_events = False
                            continue
                        if not _streamed_via_events:
                            text = self._extract_text_from_message(event)
                            if text:
                                yield AgentEvent(type="message", content=text)

                        # RESOLVED emission. The completed message carries the
                        # SDK's fully assembled ``input``, and this is the only
                        # place the real arguments exist — the streamed
                        # announcement above never has them. It is emitted
                        # UNCONDITIONALLY: it used to be skipped for any tool the
                        # streaming branch had already named, which on a streamed
                        # turn is every tool, so consumers only ever saw
                        # ``input={}``. A streamed call therefore surfaces twice
                        # (provisional, then resolved) — correct, because consumers
                        # REPLACE a tool's status line rather than append to it. On
                        # the non-streaming path (no ``_StreamEvent``, so no
                        # ``include_partial_messages``) this branch is the only
                        # emitter and one call still yields exactly one event.
                        tools = self._extract_tool_info(event)
                        for tool in tools:
                            logger.info(f"🔧 Tool: {tool['name']}")
                            # Correlate the MCP tool_use_id → tool id so the
                            # later ServerToolResultBlock (which carries only
                            # the id) resolves to its real name.
                            _tid = tool.get("id")
                            if _tid:
                                _server_tool_names[_tid] = tool["name"]
                            yield AgentEvent(
                                type="tool_use",
                                content=f"Using {tool['name']}...",
                                metadata={
                                    "name": tool["name"],
                                    "input": tool["input"],
                                    "input_pending": False,
                                },
                            )

                        # MCP (advisor) tool results ride INSIDE AssistantMessage
                        # content as ``ServerToolResultBlock`` (message_parser.py
                        # maps ``advisor_tool_result`` there). Emit a tool_result
                        # event so the loop's detector (``_publish_studio_flow_event``
                        # / ``_publish_pocket_event``) can fan the payload to a
                        # dedicated SystemEvent. Without this, an MCP tool call on
                        # the default SDK backend silently produced no result event
                        # — the canvas never materialised the graph.
                        _am_content = getattr(event, "content", None)
                        if _am_content and isinstance(_am_content, list):
                            for _srv_block in _am_content:
                                if not (
                                    self._ServerToolResultBlock
                                    and isinstance(_srv_block, self._ServerToolResultBlock)
                                ):
                                    continue
                                _mcp_text = _mcp_result_text(getattr(_srv_block, "content", None))
                                _tool_name = _server_tool_names.get(
                                    getattr(_srv_block, "tool_use_id", ""),
                                    "mcp_server",
                                )
                                if _mcp_text:
                                    yield AgentEvent(
                                        type="tool_result",
                                        content=_mcp_text,
                                        metadata={"name": _tool_name},
                                    )

                        _streamed_via_events = False
                        continue

                    # ========== ResultMessage - final result ==========
                    if self._ResultMessage and isinstance(event, self._ResultMessage):
                        _saw_result = True
                        # feat/warm-reuse fix: capture the native session_id from the
                        # ResultMessage as a ROBUST FALLBACK. SS-1 captured it from the
                        # init SystemMessage's ``data["session_id"]`` — that fired for
                        # the persistent v1 path but NOT for the leased supervised-fresh
                        # path (code-identical connect->query->receive, but the init's
                        # session_id doesn't surface at runtime on the fresh client), so
                        # ``owns_capture`` stayed True forever and WARM reuse / native
                        # resume never engaged. The ``ResultMessage`` ALWAYS carries the
                        # native ``session_id`` (types.py: a direct str field) and is the
                        # terminal message every completed run processes, so capturing it
                        # here guarantees turn-1 capture. Still gated on the handle +
                        # emit-once, so the legacy stream stays byte-identical and no
                        # double-emit if the SystemMessage already surfaced it.
                        if session_handle is not None and not _session_id_emitted:
                            _rsid = getattr(event, "session_id", None)
                            if _rsid:
                                _session_id_emitted = True
                                logger.info(
                                    "session_id captured from ResultMessage (fallback) (id=%s)",
                                    _rsid,
                                )
                                yield AgentEvent(
                                    type="session_id",
                                    content="",
                                    metadata={
                                        "session_id": _rsid,
                                        "backend": "claude_agent_sdk",
                                    },
                                )
                        is_error = getattr(event, "is_error", False)
                        result = getattr(event, "result", "")

                        # Extract token usage from ResultMessage
                        # Per SDK docs: ResultMessage has total_cost_usd and usage dict
                        total_cost = getattr(event, "total_cost_usd", None)
                        usage = getattr(event, "usage", None) or {}
                        if isinstance(usage, dict) and (usage or total_cost):
                            # Report the model that ACTUALLY ran, not the
                            # request option: prefer the ResultMessage's
                            # modelUsage keys (the CLI's own accounting),
                            # then the last AssistantMessage's model, then
                            # the explicit option. The old hard fallback
                            # "claude" hid the auto-selected model from the
                            # UI's response-meta line.
                            _model_usage = getattr(event, "modelUsage", None) or getattr(
                                event, "model_usage", None
                            )
                            if isinstance(_model_usage, dict) and _model_usage:
                                _model_name = next(iter(_model_usage.keys()))
                            else:
                                _model_name = (
                                    _last_seen_model or options_kwargs.get("model") or "claude"
                                )
                            # MCG-11 — read prompt-cache effectiveness off the
                            # SDK usage via the universal helper so the margin
                            # from the byte-stable cached prefix (site/pocket-gen)
                            # is MEASURABLE: hit-rate + est. input-token-equivalents
                            # saved, surfaced to metering alongside the raw counts.
                            from pocketpaw.llm.caching import report_savings

                            savings = report_savings(usage)
                            if savings.cache_read_tokens or savings.cache_write_tokens:
                                logger.info(
                                    "[claude_sdk] prompt-cache: read=%d write=%d "
                                    "hit_rate=%.1f%% est_saved=%.0f input-tok-equiv",
                                    savings.cache_read_tokens,
                                    savings.cache_write_tokens,
                                    savings.hit_rate * 100,
                                    savings.est_tokens_saved,
                                )
                            yield AgentEvent(
                                type="token_usage",
                                content="",
                                metadata={
                                    "input_tokens": usage.get("input_tokens", 0),
                                    "output_tokens": usage.get("output_tokens", 0),
                                    "cached_input_tokens": usage.get("cache_read_input_tokens", 0)
                                    + usage.get("cache_creation_input_tokens", 0),
                                    # Structured cache telemetry (MCG-11) — metering
                                    # can attribute the margin without re-parsing.
                                    "cache_read_tokens": savings.cache_read_tokens,
                                    "cache_write_tokens": savings.cache_write_tokens,
                                    "cache_hit_rate": savings.hit_rate,
                                    "cache_est_tokens_saved": savings.est_tokens_saved,
                                    "total_cost_usd": total_cost,
                                    "model": _model_name
                                    if isinstance(_model_name, str)
                                    else "claude",
                                    "backend": "claude_agent_sdk",
                                },
                            )

                        if is_error:
                            logger.error(
                                "ResultMessage error (subtype=%s): %s",
                                getattr(event, "subtype", ""),
                                result,
                            )
                            if not _error_emitted:
                                _error_emitted = True
                                yield AgentEvent(
                                    type="error",
                                    content=_result_error_text(
                                        event, max_turns=options_kwargs.get("max_turns")
                                    ),
                                )
                        elif _api_error_text and not _error_emitted:
                            _error_emitted = True
                            yield AgentEvent(
                                type="error",
                                content=_cli_too_old_message(_api_error_text) or _api_error_text,
                            )
                        else:
                            logger.debug(f"ResultMessage: {str(result)[:100]}...")
                        continue

                    # ========== Unknown event type - log it ==========
                    event_class = event.__class__.__name__
                    logger.debug(f"Unknown event type: {event_class}")
            finally:
                # ── Drain remaining events if the main loop exited
                # before consuming the ResultMessage.  For the persistent
                # client, _resilient_receive handles this.  For the
                # stateless path or early-break scenarios (stop flag),
                # we still need to ensure the pipe is clean. ──
                # Only a run that actually acquired the lease may tear down
                # the shared persistent client — a stateless-fallback run does
                # not own it and must leave a sibling's subprocess alone. It
                # tears down ITS client, and clears the backend's slot only if
                # the slot still holds it (after a cleanup() the slot may
                # already belong to the next run).
                if acquired_lease and _persistent_client is not None and not _saw_result:
                    logger.warning(
                        "Main loop exited without ResultMessage — "
                        "destroying persistent client to avoid stale data"
                    )
                    if self._client is _persistent_client:
                        self._client = None
                        self._client_options_key = None
                        self._client_session_key = None
                    await self._discard_client(_persistent_client)

                # A LEASED warm client (supervisor-owned) abandoned before its
                # ResultMessage still has the rest of this reply in its pipe, and
                # the next turn would read that tail as its own answer. The
                # backend may not disconnect it, so it interrupts it and poisons
                # the lease's key: no turn can match it again, and the next turn
                # builds fresh and rebinds the slot (which tears this one down).
                if _warm_lease is not None and not _saw_result:
                    logger.warning(
                        "Leased warm client abandoned mid-reply — interrupting "
                        "and retiring it so the next turn cannot read its tail"
                    )
                    _warm_lease.options_key = "retired:" + _warm_lease.options_key
                    try:
                        await asyncio.wait_for(_warm_lease.client.interrupt(), 5)
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("leased client interrupt failed: %s", exc)

                # Only release the lease if this run acquired it. Clearing it
                # unconditionally would steal a sibling persistent run's lease.
                if acquired_lease and self._lease_token is _run_token:
                    self._client_in_use = False
                    self._lease_token = None
                logger.info(
                    "SDK stream finished: %d events, _client_in_use=%s",
                    _event_count,
                    self._client_in_use,
                )

                # ── Close the inner async generator LAST. ──
                # ``_resilient_receive`` / ``_resilient_query`` spawn
                # background ``asend`` tasks under the hood; without
                # ``aclose()`` those tasks linger in the loop's pending
                # set until GC, surfacing as
                # ``Task exception was never retrieved`` +
                # ``StopAsyncIteration`` log noise on every turn (most
                # visible right after the soul-mutation hook fires).
                #
                # Order matters: aclose runs AFTER the drain decision
                # has read ``_saw_result`` so closing the generator
                # cannot influence that branch. Idempotent + safe on a
                # generator that already exited cleanly.
                close = getattr(event_stream, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("event_stream aclose error (non-fatal): %s", exc)

            yield AgentEvent(type="done", content="")

        except Exception as e:
            error_msg = str(e)

            # ── Detect Bun/subprocess crash and auto-retry once ──
            # The bundled claude.exe uses Bun, which can crash on Windows
            # with "switch on corrupt value" (exit code 3).
            stderr_text = "\n".join(_stderr_lines) if _stderr_lines else ""
            _is_bun_crash = "exit code" in error_msg.lower() and any(
                hint in stderr_text.lower()
                for hint in ["bun has crashed", "panic", "switch on corrupt value"]
            )

            # Clear client on any error — but only if THIS run owned it.
            # A non-owning run (stateless fallback, or a failure before
            # lease acquisition) must not destroy a sibling persistent run's
            # subprocess or release its lease on the error path.
            if acquired_lease:
                if _persistent_client is not None and self._client is _persistent_client:
                    self._client = None
                    self._client_options_key = None
                    self._client_session_key = None
                await self._discard_client(_persistent_client)
                if self._lease_token is _run_token:
                    self._client_in_use = False
                    self._lease_token = None

            if _is_bun_crash and not getattr(self, "_bun_retry_done", False):
                self._bun_retry_done = True
                logger.warning(
                    "Bun runtime crashed — retrying with fresh client (stderr: %s)",
                    stderr_text[:200],
                )
                yield AgentEvent(
                    type="status",
                    content="Runtime crashed, retrying with a fresh process...",
                )
                await asyncio.sleep(1)
                # Lease state is consistent before the recursive retry, on
                # both branches of the ownership gate above:
                #  - acquired_lease True  → this run owned the persistent
                #    client; the gate already cleared _client and set
                #    _client_in_use=False, so the retry starts on a clean
                #    lease and may take the persistent path itself.
                #  - acquired_lease False → this run never owned the lease
                #    (stateless fallback, or a failure before acquisition);
                #    the gate left _client_in_use untouched, so a sibling
                #    persistent run still holds it. The recursive run() will
                #    correctly see _client_in_use=True and fall back to
                #    stateless again — it cannot steal or double-release the
                #    sibling's lease.
                # The retry is the SAME turn: every parameter is forwarded
                # (``_call_kwargs``), so a turn with a narrowed tool surface,
                # skills, a picked model or images cannot retry wider or
                # different. The one exception is ``warm_client``: the crashed
                # lease is not driven again; with ``on_client_built`` the retry
                # builds fresh and rebinds the slot, otherwise it runs stateless.
                try:
                    async for retry_event in self.run(
                        message, **{**_call_kwargs, "warm_client": None}
                    ):
                        yield retry_event
                finally:
                    self._bun_retry_done = False
                return

            logger.error(f"Claude Agent SDK error: {error_msg}", exc_info=True)

            # Log any stderr captured from the CLI subprocess
            if _stderr_lines:
                logger.error("CLI stderr output:\n%s", "\n".join(_stderr_lines))

            # Provide helpful error messages
            if "CLINotFoundError" in error_msg:
                yield AgentEvent(
                    type="error",
                    content=(
                        "❌ Claude Code CLI not found.\n\n"
                        "**Install Claude Code CLI:**\n"
                        "- Windows: `irm https://claude.ai/install.ps1 | iex`\n"
                        "- macOS/Linux: `curl -fsSL https://claude.ai/install.sh | bash`\n"
                        "- Or: `npm install -g @anthropic-ai/claude-code`\n\n"
                        "Then set your `ANTHROPIC_API_KEY` in **Settings → General**.\n\n"
                        "Or switch to a different backend in **Settings → General** "
                        "(OpenAI Agents, Google ADK, Codex, etc.)."
                    ),
                )
            elif llm is not None:
                yield AgentEvent(
                    type="error",
                    content=llm.format_api_error(e, stderr=stderr_text),
                )
            else:
                # ``_build_options`` raised before binding ``llm`` — fall back to
                # a plain message so the error still surfaces (no UnboundLocalError).
                yield AgentEvent(
                    type="error",
                    content=f"❌ Claude Agent SDK error: {error_msg}",
                )
        finally:
            # feat/warm-reuse WH-1: release the leased warm client's ``busy`` flag
            # (set only on the warm-reuse path) so the supervisor's NEXT turn can
            # drive it again. Done in the OUTER finally so it runs on every exit
            # path — normal completion, error, and the Bun-crash retry ``return``
            # (the recursive retry never carries the lease, so it can't double-set
            # busy). NEVER disconnect the leased client here — the supervisor owns
            # its lifecycle and keeps it warm.
            if _warm_lease is not None:
                _warm_lease.busy = False
            self._active_runs.pop(_run_token, None)
            # Remove the per-run materialized-skills plugin dir (entity-rooms
            # A2) ONLY when this run owns it — i.e. the genuine stateless-
            # fallback case where no warm client adopted the dir
            # (fix/claude-sdk-warm-client-skills). When ``skills_dir_adopted`` is
            # True the dir belongs to ``self._skills_dir_by_digest`` and a LIVE
            # warm subprocess still references its path; deleting it here would
            # leave the next reuse pointing at a missing dir. Adopted dirs are
            # instead removed on client eviction (_get_or_create_client) or
            # cleanup(). Best-effort; never raises.
            if run_skills_root is not None and not skills_dir_adopted:
                from pocketpaw.skills import cleanup_run_skills

                cleanup_run_skills(run_skills_root)

    async def stop(self, session_key: str | None = None) -> None:
        """Stop in-flight runs and disconnect the persistent client.

        With ``session_key`` only that session's runs are stopped, and the warm
        client is interrupted and disconnected only if it belongs to that
        session; other sessions' streams on this backend are left alone. Without
        it (pool teardown, shutdown) every run is stopped and the client released.
        Each run carries its own stop flag, so a run started after this call
        cannot clear the stop for one already unwinding.
        """
        self._stop_flag = True
        for state in self._active_runs.values():
            if session_key is None or state.session_key == session_key:
                state.stopped = True
        if session_key is not None and self._client_session_key != session_key:
            logger.info("🛑 Claude Agent SDK stop requested for session %s", session_key)
            return
        if self._client is not None:
            try:
                await self._client.interrupt()
            except Exception as e:
                logger.debug("Failed to interrupt Claude client: %s", e)
        await self.cleanup()
        logger.info("🛑 Claude Agent SDK stop requested")

    async def get_status(self) -> dict:
        """Get current agent status."""
        ready = self._sdk_available and self._cli_available
        return {
            "backend": "claude_agent_sdk",
            "available": ready,
            "sdk_installed": self._sdk_available,
            "cli_installed": self._cli_available,
            "running": not self._stop_flag,
            # Base (OSS/default) working dir only. The ACTUAL per-run cwd is
            # resolved each turn by ``_resolve_cwd`` — in cloud it's a per-tenant
            # jail, not this base — so labelling it ``base_cwd`` keeps status
            # honest (and avoids resolving here, which would fail closed off-run).
            "base_cwd": str(self._cwd),
            "features": ["Bash", "Read", "Write", "Edit", "Glob", "Grep", "WebSearch", "WebFetch"]
            if ready
            else [],
        }


# Backward-compat aliases
ClaudeAgentSDK = ClaudeSDKBackend
ClaudeAgentSDKWrapper = ClaudeSDKBackend
