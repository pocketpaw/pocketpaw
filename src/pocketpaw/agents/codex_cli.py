"""Codex CLI backend for PocketPaw.

Uses OpenAI's official ``openai-codex-sdk`` Python package (which embeds
the ``codex`` binary as a subprocess and parses its NDJSON stream into
typed Pydantic events). This replaces an earlier hand-rolled subprocess
wrapper that parsed ``codex exec --json`` output by hand — the schema
drifted across Codex CLI 0.14+ releases (``output`` → ``aggregated_output``,
``name`` → ``tool``, ``filename`` → ``changes[].path``) and our parser
returned empty strings for tool results, so the agent thrashed.

Cloud-mode pocket edits flow through ``pocketpaw.tools.cli cloud_*``
which Codex invokes via its built-in ``shell`` tool. Codex doesn't see
the in-process Claude SDK MCP server (different process), and we don't
build a separate stdio MCP server because the existing CLI dispatcher
already exposes the same operations as a JSON-in/JSON-out shell entry
point — it just needed cloud-write variants
(``cloud_add_widget`` / ``cloud_get_pocket`` / etc.) that talk to Mongo
instead of the local mutation-instruction shape. We export per-turn
identity (workspace_id, user_id, session_id, pocket_id) into the Codex
subprocess env so those CLI commands can boot Beanie + scope mutations
correctly.

Built-in tools: shell (``CommandExecutionItem``), file editing
(``FileChangeItem``), MCP tool calls (``McpToolCallItem``), web search.

Requires:
  - ``OPENAI_API_KEY`` (or a ChatGPT login via ``codex login``) for the
    backing OpenAI account.
  - The native ``codex`` binary, supplied either by ``npm install -g
    @openai/codex`` (auto-discovered on Windows) or by
    ``Codex.install(version=...)`` which downloads it from the public
    GitHub release artifacts.

System prompt is delivered by writing it to ``AGENTS.md`` inside an
ephemeral working directory and pointing Codex at that directory via
``ThreadOptions.working_directory``. The Codex binary auto-loads
``AGENTS.md`` from cwd on every turn — same mechanism the CLI uses for
project-level context. We avoided the ``-c experimental_instructions_file``
config flag because it's not a documented kwarg on ``ThreadOptions`` and
the path-quoting on Windows TOML is fiddly. ``AGENTS.md`` is the
documented, stable extension point.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from pocketpaw.agents.backend import (
    _DEFAULT_IDENTITY,
    BackendInfo,
    BaseAgentBackend,
    Capability,
)
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.config import Settings
from pocketpaw.tools.policy import ToolPolicy

logger = logging.getLogger(__name__)


_CURRENT_POCKET_ID_RE = re.compile(r'<current-pocket\s+id="([^"]+)"')


# Codex CLI 0.125+ emits a leading deprecation warning that the SDK packages
# into the first ``AgentMessageItem.text`` field. The warning has no separator
# from the actual model reply, so users see it concatenated to their answer:
#
#   ``[features].web_search_request is deprecated because web search is
#   enabled by default. (Set web_search to "live", "cached", or "disabled"
#   at the top level (or under a profile) in config.toml if you want to
#   override it.)Hello there, Test.``
#
# The expected response is just ``Hello there, Test.``. Strip the known
# warning when it appears at the start of a message. If codex CLI fixes the
# leak in a future release the regex simply won't match. Add new patterns
# here as they surface — keep each one specific so we don't accidentally eat
# real model output.
_CODEX_STDERR_NOISE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"^\s*\[features\]\.web_search_request is deprecated"
        r".*?override it\.\)\s*",
        re.DOTALL,
    ),
)


def _strip_codex_stderr_noise(text: str) -> str:
    """Remove leading codex CLI stderr noise that leaks into agent message text.

    Defensive cleanup for the codex 0.125 deprecation-prefix bug. Returns
    the text unchanged when no known noise pattern matches.
    """
    cleaned = text
    for pattern in _CODEX_STDERR_NOISE_RES:
        cleaned = pattern.sub("", cleaned, count=1)
    return cleaned


_CODEX_PARSER_PATCHED = False


def _patch_codex_parser() -> None:
    """One-shot monkey-patch: tolerate unknown literal values in known item types.

    ``openai_codex_sdk.parsing.parse_thread_item`` has an ``UnknownThreadItem``
    fallback for unknown item ``type`` values, but no fallback when a known
    type fails ``model_validate`` on a NEW literal value the SDK's enum hasn't
    been updated for yet. Concrete instance: ``CommandExecutionItem.status``
    is typed ``Literal['in_progress', 'completed', 'failed']`` but Codex emits
    ``'declined'`` when the user denies a command, which crashes the entire
    stream.

    Patch ``parse_thread_item`` to fall back to ``UnknownThreadItem`` on
    ``ValidationError`` instead of propagating, matching the SDK's own
    unknown-type fallback semantics.
    """
    global _CODEX_PARSER_PATCHED
    if _CODEX_PARSER_PATCHED:
        return
    try:
        from openai_codex_sdk import parsing as _codex_parsing
        from pydantic import ValidationError
    except ImportError:
        return

    original_parse_item = _codex_parsing.parse_thread_item

    def patched(data: Any) -> Any:
        try:
            return original_parse_item(data)
        except ValidationError as exc:
            # Pull the command + status out of the raw dict so the operator
            # can see exactly what Codex auto-declined. Common cause:
            # sandbox_mode="workspace-write" rejecting a network call or
            # an out-of-workspace path. ``status='declined'`` is Codex's
            # own decision, NOT a user denial.
            cmd = None
            status = None
            if isinstance(data, dict):
                cmd = data.get("command") or data.get("changes") or data.get("tool")
                status = data.get("status")
            logger.warning(
                "codex SDK rejected known item type "
                "(type=%s status=%r command=%r): falling back to UnknownThreadItem; errors=%s",
                (data.get("type") if isinstance(data, dict) else type(data).__name__),
                status,
                cmd,
                exc.errors()[:2],
            )
            return _codex_parsing.UnknownThreadItem.model_validate(data)

    _codex_parsing.parse_thread_item = patched
    _CODEX_PARSER_PATCHED = True
    logger.info("Patched openai_codex_sdk.parsing.parse_thread_item for forward-compat literals")


def _build_subprocess_env(system_prompt: str) -> dict[str, str]:
    """Compose the env passed to the Codex subprocess.

    Inherits the parent's env (so ``CLOUD_MONGODB_URI`` and
    ``OPENAI_API_KEY`` survive) and overlays the per-turn identity
    variables that ``pocketpaw.tools.cli cloud_*`` reads to scope
    Mongo writes:

      - ``POCKETPAW_WORKSPACE_ID`` / ``POCKETPAW_USER_ID`` /
        ``POCKETPAW_SESSION_ID`` from the cloud chat ContextVars.
      - ``POCKETPAW_POCKET_ID`` extracted from the
        ``<current-pocket id="...">`` tag the cloud prompt builder
        injects (so the agent can omit it from JSON args).
      - ``POCKETPAW_MONGO_URI`` mirroring ``CLOUD_MONGODB_URI`` so the
        CLI's lookup falls back cleanly even when the parent only sets
        the cloud-flavoured name.

    All identity reads are best-effort: in OSS-only deployments
    (no ``ee.cloud.chat``) the import fails and the variables stay
    unset, which is fine — the cloud_* CLI commands surface a clear
    error in that case rather than silently writing to the wrong
    workspace.
    """
    env: dict[str, str] = dict(os.environ)

    # Cloud identity for the subprocess — the workspace/user/session ids the
    # cloud_* CLI commands read — comes from EE agent extensions, which pull
    # it from the per-stream ContextVars. Empty on an OSS install or when
    # called outside a cloud chat stream, which is fine: the cloud_* CLI
    # commands then surface a clear error rather than mis-tenanting.
    from pocketpaw._registry import providers as _ext_providers

    for ext in _ext_providers("pocketpaw.agent_extensions"):
        try:
            env.update(ext.subprocess_env())
        except Exception as exc:  # noqa: BLE001
            logger.debug("agent extension %r subprocess_env failed: %s", ext, exc)

    # Pocket id rides in on the system prompt (the cloud prompt builder
    # appends ``<current-pocket id="...">`` for in-pocket chats).
    match = _CURRENT_POCKET_ID_RE.search(system_prompt or "")
    if match:
        env["POCKETPAW_POCKET_ID"] = match.group(1)

    # Mirror the cloud Mongo URI under the CLI's preferred name.
    cloud_uri = env.get("CLOUD_MONGODB_URI")
    if cloud_uri and "POCKETPAW_MONGO_URI" not in env:
        env["POCKETPAW_MONGO_URI"] = cloud_uri

    return env


def _resolve_codex_binary() -> str | None:
    """Locate the native codex executable.

    The SDK's bundled ``find_codex_path`` only checks the wheel's vendor
    directory, which is empty unless the user ran ``Codex.install(...)``.
    We extend the lookup to:

    1. The SDK's vendor directory (preferred, no hop).
    2. The npm global install ("``npm install -g @openai/codex``"). On
       Windows the PATH entry is a ``.cmd`` shim that
       ``create_subprocess_exec`` can't run; the real ``codex.exe`` lives
       under the package's ``node_modules/@openai/codex-<triple>/vendor``.
    3. ``shutil.which("codex")`` on POSIX where the PATH entry is a real
       binary.

    Returns ``None`` if no native binary is reachable; the backend then
    surfaces a clear "not installed" error to the user instead of
    spawning failures inside the SDK.
    """
    # 1. SDK vendor (only present if user ran Codex.install).
    try:
        from openai_codex_sdk.exec import find_codex_path

        return find_codex_path()
    except Exception:
        pass

    # 2. NPM global install — walk shim → native binary.
    if sys.platform == "win32":
        npm_shim = shutil.which("codex")
        if npm_shim:
            shim = Path(npm_shim)
            search_root = shim.parent / "node_modules" / "@openai" / "codex"
            if search_root.is_dir():
                hits = list(
                    search_root.glob("node_modules/@openai/codex-*/vendor/*/codex/codex.exe")
                )
                if hits:
                    return str(hits[0])
    else:
        path = shutil.which("codex")
        if path:
            # Filter out shell shims even on POSIX (rare but possible).
            real = Path(path).resolve()
            if real.is_file() and os.access(real, os.X_OK):
                return str(real)

    return None


class CodexCLIBackend(BaseAgentBackend):
    """Codex CLI backend — SDK-driven, typed events, abort-signal stop."""

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="codex_cli",
            display_name="Codex CLI",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MCP
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            builtin_tools=["shell", "file_edit", "web_search", "mcp"],
            tool_policy_map={
                "shell": "shell",
                "file_edit": "write_file",
                "web_search": "browser",
                "mcp": "mcp",
            },
            required_keys=["openai_api_key"],
            supported_providers=["openai"],
            install_hint={
                "external_cmd": "npm install -g @openai/codex",
            },
            beta=True,
        )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._stop_flag = False
        self._codex_path = _resolve_codex_binary()
        self._cli_available = self._codex_path is not None
        # Active abort controller (one per ``run`` invocation). Stop hits this.
        self._abort_controller: Any | None = None
        # Tolerate unknown literal values in the codex SDK's pydantic models
        # (e.g. CommandExecutionItem.status='declined' which the SDK schema
        # doesn't list). One-shot patch; safe to call repeatedly.
        _patch_codex_parser()
        self._policy = ToolPolicy(
            profile=settings.tool_profile,
            allow=settings.tools_allow,
            deny=settings.tools_deny,
        )
        if self._cli_available:
            logger.info("Codex CLI binary: %s", self._codex_path)
        else:
            logger.warning(
                "Codex CLI binary not found — install with: "
                "npm install -g @openai/codex (or call Codex.install(version=...))"
            )

    def get_tool_policy(self) -> ToolPolicy:
        return self._policy

    def set_tool_policy(self, policy: ToolPolicy) -> None:
        # Policy is stored but never enforced — Codex runs tools inside an
        # external CLI process that has no awareness of this policy.
        logger.debug("set_tool_policy on CodexCLIBackend: stored but not enforced (external CLI)")
        self._policy = policy

    @staticmethod
    def _inject_history(instruction: str, history: list[dict]) -> str:
        """Append conversation history to instruction as text."""
        lines = ["# Recent Conversation"]
        for msg in history:
            role = msg.get("role", "user").capitalize()
            content = msg.get("content", "")
            if len(content) > 500:
                content = content[:500] + "..."
            lines.append(f"**{role}**: {content}")
        return instruction + "\n\n" + "\n".join(lines)

    async def run(  # noqa: C901 — single dispatch over many SDK item types.
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        if not self._cli_available:
            yield AgentEvent(
                type="error",
                content=(
                    "Codex CLI binary not found.\n\nInstall with: npm install -g @openai/codex"
                ),
            )
            return

        # Lazy-import the SDK so callers without ``pocketpaw[codex-sdk]``
        # installed get a clean ImportError surface instead of a hard
        # module load at agent-registry time.
        try:
            from openai_codex_sdk import (
                AbortController,
                AgentMessageItem,
                Codex,
                CommandExecutionItem,
                ErrorItem,
                FileChangeItem,
                ItemCompletedEvent,
                ItemStartedEvent,
                McpToolCallItem,
                ReasoningItem,
                ThreadOptions,
                TurnCompletedEvent,
                TurnFailedEvent,
                TurnOptions,
                WebSearchItem,
            )
            from openai_codex_sdk.errors import CodexExecError
            from openai_codex_sdk.types import CodexOptions
        except ImportError as exc:
            yield AgentEvent(
                type="error",
                content=(
                    f"openai-codex-sdk is not installed: {exc}\n\n"
                    "Install with: pip install pocketpaw[codex-sdk]"
                ),
            )
            return

        self._stop_flag = False
        # Pass through whatever the user configured. When unset, leave it
        # ``None`` so the SDK omits ``--model`` and codex falls back to
        # the user's ``~/.codex/config.toml`` ``model = "..."`` entry.
        # Hardcoding a default here breaks ChatGPT-plan accounts whose
        # available model set doesn't match ours (e.g. plans that only
        # authorize gpt-5.5 when we default to gpt-5.4 → exit 1 with
        # nothing useful on stderr).
        model = self.settings.codex_cli_model or None

        # AGENTS.md sandbox: a temp directory whose only content is the
        # PocketPaw system prompt. Codex auto-loads ``AGENTS.md`` from cwd
        # so this is the documented way to inject identity. The temp dir
        # also doubles as the working directory we pass to Codex — keeping
        # it empty stops the agent from poking around the user's repo.
        instructions = system_prompt or _DEFAULT_IDENTITY
        if history:
            instructions_with_history = self._inject_history(instructions, history)
        else:
            instructions_with_history = instructions

        work_dir = Path(tempfile.mkdtemp(prefix="paw_codex_"))
        try:
            (work_dir / "AGENTS.md").write_text(instructions_with_history, encoding="utf-8")

            subprocess_env = _build_subprocess_env(instructions)
            # Per-backend API key + base URL overrides — let the user
            # point Codex at a separate account or an OpenAI-compatible
            # proxy without flipping the global ``openai_api_key`` /
            # ``openai_compatible_base_url`` settings.
            codex_api_key = (
                getattr(self.settings, "codex_cli_api_key", None)
                or self.settings.openai_api_key
                or None
            )
            codex_base_url = getattr(self.settings, "codex_cli_base_url", None) or None
            codex = Codex(
                CodexOptions(
                    codex_path_override=self._codex_path,
                    env=subprocess_env,
                    api_key=codex_api_key,
                    base_url=codex_base_url,
                )
            )

            # Codex 0.125 on Windows sometimes leaks non-JSON lines into
            # its --experimental-json stdout — most reliably reproduced
            # is the ``codex-command-runner.exe`` shutdown printing
            # ``SUCCESS: The process with PID <n> ... has been
            # terminated.`` from its internal ``taskkill`` invocation.
            # The SDK's parser then raises ``EventParseError`` and the
            # whole stream dies. Wrap ``CodexExec.run`` so non-JSON
            # lines are dropped at the source — codex's actual JSONL
            # frames always start with ``{``. Guarded with hasattr
            # because tests can replace ``Codex`` with a stub that
            # doesn't carry an ``_exec`` attribute.
            _exec = getattr(codex, "_exec", None)
            if _exec is not None and hasattr(_exec, "run"):
                _orig_exec_run = _exec.run

                async def _filtered_run(args):  # type: ignore[no-redef]
                    async for line in _orig_exec_run(args):
                        stripped = line.lstrip() if line else ""
                        if stripped.startswith("{"):
                            yield line
                        elif stripped:
                            logger.debug(
                                "Dropped non-JSON line from codex stdout: %r",
                                line[:120],
                            )

                _exec.run = _filtered_run

            sandbox_mode = getattr(self.settings, "codex_cli_sandbox_mode", "danger-full-access")
            approval_policy = getattr(self.settings, "codex_cli_approval_policy", "never")
            thread = codex.start_thread(
                ThreadOptions(
                    model=model,
                    working_directory=str(work_dir),
                    sandbox_mode=sandbox_mode,
                    skip_git_repo_check=True,
                    approval_policy=approval_policy,
                    web_search_enabled=True,
                )
            )

            self._abort_controller = AbortController()
            streamed = await thread.run_streamed(
                message,
                TurnOptions(signal=self._abort_controller.signal),
            )

            async for event in streamed.events:
                if self._stop_flag:
                    break

                if isinstance(event, ItemStartedEvent):
                    item = event.item
                    if isinstance(item, CommandExecutionItem):
                        # Surface every shell invocation in the parent log so
                        # operators can see what Codex is doing — especially
                        # subprocess calls like ``python -m pocketpaw.tools.cli
                        # cloud_pocket_specialist_create -`` whose own logs
                        # are trapped inside the subprocess and never reach
                        # the main terminal.
                        cmd_str = (item.command or "")[:500]
                        logger.info("codex shell: %s", cmd_str)
                        yield AgentEvent(
                            type="tool_use",
                            content=f"Running: {item.command}",
                            metadata={
                                "name": "shell",
                                "input": {"command": item.command},
                            },
                        )
                    elif isinstance(item, FileChangeItem):
                        first = item.changes[0] if item.changes else None
                        path = first.path if first else "unknown"
                        yield AgentEvent(
                            type="tool_use",
                            content=f"Editing: {path}",
                            metadata={
                                "name": "file_edit",
                                "input": {
                                    "path": path,
                                    "changes": [
                                        {"path": c.path, "kind": c.kind} for c in item.changes
                                    ],
                                },
                            },
                        )
                    elif isinstance(item, McpToolCallItem):
                        yield AgentEvent(
                            type="tool_use",
                            content=f"MCP: {item.tool}",
                            metadata={
                                "name": item.tool,
                                "input": item.arguments or {},
                                "server": item.server,
                            },
                        )
                    elif isinstance(item, WebSearchItem):
                        yield AgentEvent(
                            type="tool_use",
                            content=f"Searching: {item.query}",
                            metadata={
                                "name": "web_search",
                                "input": {"query": item.query},
                            },
                        )

                elif isinstance(event, ItemCompletedEvent):
                    item = event.item
                    if isinstance(item, AgentMessageItem):
                        if item.text:
                            cleaned = _strip_codex_stderr_noise(item.text)
                            if cleaned:
                                yield AgentEvent(type="message", content=cleaned)
                    elif isinstance(item, CommandExecutionItem):
                        # ``aggregated_output`` is the canonical field on the
                        # typed item — no schema drift, no fallbacks needed.
                        # Pass the FULL output through (capped at 64 KiB for
                        # memory safety). The agent loop truncates this to
                        # 200 chars for the SSE wire payload separately, but
                        # ``_publish_pocket_event`` needs the full body to
                        # parse cloud_* CLI responses out of stdout.
                        out = item.aggregated_output
                        if item.exit_code not in (None, 0):
                            out = f"[exit {item.exit_code}] {out}"
                        # Mirror the result into the parent log (truncated)
                        # so operators can confirm subprocess completion and
                        # see the response body — particularly the
                        # specialist's ``{ok, action, pocket, ...}`` JSON.
                        preview = str(out)[:1000].replace("\n", " ")
                        logger.info(
                            "codex shell result (exit=%s): %s",
                            item.exit_code,
                            preview,
                        )
                        yield AgentEvent(
                            type="tool_result",
                            content=str(out)[:65536],
                            metadata={"name": "shell"},
                        )
                    elif isinstance(item, FileChangeItem):
                        if item.changes:
                            summary = ", ".join(f"{c.kind} {c.path}" for c in item.changes)
                        else:
                            summary = "updated"
                        yield AgentEvent(
                            type="tool_result",
                            content=summary[:200],
                            metadata={"name": "file_edit"},
                        )
                    elif isinstance(item, McpToolCallItem):
                        # MCP results carry a list of MCP content blocks.
                        # ``result`` is None on failure; ``error`` carries
                        # the failure message instead.
                        if item.error is not None:
                            output_text = f"[error] {item.error.message}"
                        elif item.result is not None:
                            text_parts = [
                                str(b.get("text", ""))
                                for b in (item.result.content or [])
                                if isinstance(b, dict) and b.get("type") == "text"
                            ]
                            output_text = "\n".join(p for p in text_parts if p)
                            if not output_text and item.result.structured_content:
                                import json as _json

                                output_text = _json.dumps(
                                    item.result.structured_content, default=str
                                )
                        else:
                            output_text = ""
                        yield AgentEvent(
                            type="tool_result",
                            content=output_text[:65536],
                            metadata={"name": item.tool, "server": item.server},
                        )
                    elif isinstance(item, WebSearchItem):
                        # Codex doesn't carry hits on web_search completion;
                        # echo the query so the activity feed has something.
                        yield AgentEvent(
                            type="tool_result",
                            content=f"Searched: {item.query}"[:200],
                            metadata={"name": "web_search"},
                        )
                    elif isinstance(item, ReasoningItem):
                        if item.text:
                            yield AgentEvent(type="thinking", content=item.text)
                    elif isinstance(item, ErrorItem):
                        if item.message:
                            yield AgentEvent(type="error", content=item.message)

                elif isinstance(event, TurnCompletedEvent):
                    usage = event.usage
                    yield AgentEvent(
                        type="token_usage",
                        content="",
                        metadata={
                            "input_tokens": getattr(usage, "input_tokens", 0),
                            "output_tokens": getattr(usage, "output_tokens", 0),
                            "cached_input_tokens": getattr(usage, "cached_input_tokens", 0),
                            "model": model or "(codex-config)",
                            "backend": "codex_cli",
                        },
                    )

                elif isinstance(event, TurnFailedEvent):
                    yield AgentEvent(
                        type="error",
                        content=getattr(event, "message", "Codex turn failed"),
                    )

            yield AgentEvent(type="done", content="")

        except CodexExecError as exc:
            logger.error("Codex SDK exec failure: %s", exc)
            msg = str(exc)
            # Codex's rollout-flush error after a hard failure is the only
            # thing on stderr most of the time — it tells the user nothing
            # about the actual cause. Add a hint pointing at the usual
            # culprit (model not authorised on this account / plan, or the
            # 0.124.0+ TTY-detached regression — openai/codex#19945).
            if (
                "failed to record rollout items" in msg or "Reading prompt from stdin" in msg
            ) and "invalid" not in msg.lower():
                msg += (
                    "\n\nHint: this stderr usually means codex bailed before "
                    "running the prompt. Two likely causes:\n"
                    f"  1. Model ({model or 'config-default'}) not authorised "
                    "on your account — check ``~/.codex/config.toml`` "
                    '(``model = "..."``).\n'
                    "  2. codex-cli 0.124.0/0.125.0 regression (openai/codex"
                    "#19945) — long prompts crash on Windows with stdio "
                    "piped. Workaround: ``npm install -g "
                    "@openai/codex@0.123.0``."
                )
            yield AgentEvent(type="error", content=f"Codex CLI error: {msg}")
            yield AgentEvent(type="done", content="")
        except Exception as exc:  # noqa: BLE001 — surface to the agent loop.
            # Specifically catch the SDK's JSONL parser failure when the
            # codex binary leaks non-JSON garbage into stdout (e.g. the
            # Windows elevated sandbox shutdown leaking ``taskkill``
            # output: ``SUCCESS: The process with PID ... terminated``).
            exc_class = type(exc).__name__
            exc_msg = str(exc)
            if exc_class == "EventParseError" or (
                "Failed to parse JSONL" in exc_msg and "SUCCESS: The process with PID" in exc_msg
            ):
                logger.error("Codex stdout-leak detected: %s", exc_msg)
                yield AgentEvent(
                    type="error",
                    content=(
                        "Codex CLI error: non-JSON line leaked into codex "
                        "stdout, breaking the SDK's event parser:\n\n"
                        f"  {exc_msg}\n\n"
                        "On Windows this is almost always the elevated "
                        "sandbox (``codex-windows-sandbox-setup.exe``) "
                        "leaking ``taskkill`` output during shutdown. "
                        "Workaround: edit ``~/.codex/config.toml`` and set\n"
                        "    [windows]\n"
                        '    sandbox = "none"\n'
                        "(or downgrade codex-cli to 0.123.0). The codex-CLI "
                        "sandbox we pass via ``--sandbox workspace-write`` "
                        "is independent of this OS-level setting."
                    ),
                )
                yield AgentEvent(type="done", content="")
                return
            logger.exception("Unexpected Codex SDK failure")
            yield AgentEvent(type="error", content=f"Codex CLI error: {exc}")
            yield AgentEvent(type="done", content="")
        finally:
            self._abort_controller = None
            try:
                shutil.rmtree(work_dir, ignore_errors=True)
            except Exception:
                pass

    async def stop(self) -> None:
        self._stop_flag = True
        if self._abort_controller is not None:
            try:
                self._abort_controller.abort()
            except Exception:
                pass

    async def get_status(self) -> dict[str, Any]:
        return {
            "backend": "codex_cli",
            "cli_available": self._cli_available,
            "running": self._abort_controller is not None,
            "model": self.settings.codex_cli_model or "(codex-config)",
        }
