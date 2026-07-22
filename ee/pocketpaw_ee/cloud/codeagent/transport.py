# transport.py — how the code agent reaches a model (CA-0).
#
# Created 2026-07-22. This module exists because `codeagent/service.py` was the
# one place in the codebase that ignored a rule the codebase states explicitly:
#
#     CRITICAL — agent-mode LLM transport. PocketPaw runs in agent mode with NO
#     ANTHROPIC_API_KEY, so the triager LLM call MUST shell the `claude` CLI
#     (`claude -p <prompt> --output-format json`) — the mandate-foreman
#     `ClaudeCliLlm` pattern — NOT `AsyncAnthropic` (the narrator's direct
#     client fails in agent mode).
#         — ee/pocketpaw_ee/instinct/auto_triage.py
#
# `foreman.py` and `auto_triage.py` both follow it. `codeagent` did not: it built
# an `AsyncAnthropic` directly, inherited from the deleted `websandbox/edit.py`,
# which had copied `decisions/explain/narrator.py` — the very client that note
# says fails. The symptom is a 503 on every Ask and every Cmd-K on a deployment
# that has no key, which is every deployment running in agent mode.
#
# ── WHY THIS WASN'T JUST A ONE-LINE SWAP ────────────────────────────────────
#
# `ClaudeCliLlm.plan()` is `prompt -> str`. It has no tool protocol, and the code
# agent's whole design is a TOOL LOOP whose tools run on the CLIENT — a browser
# holding a WebContainer, or a socket onto a Daytona VM. The server has no access
# to those files, which is exactly why the loop was inverted in CA-2.
#
# So the transport has to do something the CLI's own tool machinery cannot: EMIT
# a tool call without EXECUTING it. The `claude` CLI executes what it calls; an
# MCP server would run in the backend, where the files are not.
#
# The way through is to carry the protocol in the PROMPT rather than in the
# API's tool channel, and to parse a JSON reply back into the same block shapes
# the Messages API returns. Verified against the real CLI before this was
# written: asked for a tool decision as JSON, it answers with the JSON and calls
# nothing.
#
# ── THE SHAPE THIS EXPOSES IS DELIBERATELY ANTHROPIC'S ──────────────────────
#
# `ClaudeCliClient` exposes `.messages.create(...)` and returns objects with
# `.content` / `.stop_reason`, so it is a DROP-IN for `AsyncAnthropic` at the one
# call site. `service.py` therefore has no branch in it: `_text_of` and
# `_tool_calls_of` duck-type the blocks and cannot tell which transport ran.
#
# That is worth more than a neater abstraction would be. A native `tool_use`
# from the Messages API and a parsed JSON decision from the CLI have to be
# indistinguishable downstream, or every consumer grows a second code path and
# the two drift — and the CLI path, being the one without a key, is the one
# nobody would be testing.
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

logger = logging.getLogger(__name__)

#: How long a single CLI turn may take. Generous: the CLI does its own auth,
#: may refresh a token, and a tool-loop turn over a large file is not fast.
CLI_TIMEOUT_SECONDS = 180.0

#: Cap on what we hand the CLI as one argv element. Well under Windows' ~32 KB
#: command-line limit, with room for the rest of the line.
MAX_PROMPT_CHARS = 24_000


# ── The response shape, mirroring the Messages API ──────────────────────────


@dataclass(frozen=True)
class TextBlock:
    """An answer fragment. `type` is a field rather than a class check because
    that is how the consumers read it (`getattr(block, "type", None)`)."""

    text: str
    type: str = "text"


@dataclass(frozen=True)
class ToolUseBlock:
    """A request for the CLIENT to run a tool.

    `id` is minted here. The Messages API supplies its own; the CLI has no
    concept of one, and the id is load-bearing — the client echoes it back on
    the matching result and `_replay_tool_exchanges` pairs the two halves by it.
    An unpaired id makes the next turn invalid.
    """

    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass(frozen=True)
class ModelResponse:
    content: list[Any] = field(default_factory=list)
    stop_reason: str | None = None


# ── Prompt encoding ─────────────────────────────────────────────────────────

_PROTOCOL = """
You are answering as a JSON API. Reply with ONE JSON object and nothing else —
no prose before or after it, no markdown code fence.

Either you can answer now:

  {"answer": "<your answer, in markdown>"}

or you need to look at something first:

  {"tool_calls": [{"name": "<tool>", "input": {<arguments>}}]}

Rules:
  - Choose exactly one of the two forms. Never both.
  - Only call a tool listed under TOOLS. Never invent one.
  - Prefer answering when you already have enough to answer.
  - Ask for several tools at once only when they are genuinely independent.
""".strip()


def _render_content(content: Any) -> str:
    """Flatten one message's content into readable text.

    Tool traffic is rendered EXPLICITLY rather than dropped: the conversation
    handed in already contains `tool_use` / `tool_result` pairs replayed from
    earlier rounds, and a model that cannot see what it already asked for will
    ask again, forever.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            parts.append(str(block))
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "tool_use":
            args = json.dumps(block.get("input", {}), ensure_ascii=False)
            parts.append(f"[called {block.get('name')} with {args}]")
        elif kind == "tool_result":
            status = "error" if block.get("is_error") else "result"
            parts.append(f"[{status}] {block.get('content', '')}")
    return "\n".join(p for p in parts if p)


def render_prompt(system: str, messages: list[dict], tools: list[dict] | None) -> str:
    """Fold system + tools + conversation into one prompt for `claude -p`.

    Ordering is deliberate: instructions, then capabilities, then the protocol,
    then the conversation LAST — the closest thing to the reply is the thing the
    reply is about.
    """
    sections = [f"<instructions>\n{system}\n</instructions>"]

    if tools:
        described = [
            {
                "name": t.get("name"),
                "description": t.get("description"),
                "input_schema": t.get("input_schema"),
            }
            for t in tools
        ]
        sections.append(
            "<tools>\n" + json.dumps(described, ensure_ascii=False, indent=2) + "\n</tools>"
        )
    else:
        # The final round of a loop passes no tools, and saying so beats leaving
        # the model to infer it from an absent section.
        sections.append("<tools>\nNone. Answer from what you already have.\n</tools>")

    sections.append(f"<protocol>\n{_PROTOCOL}\n</protocol>")

    rendered = [
        f"{m.get('role', 'user')}: {_render_content(m.get('content', ''))}" for m in messages
    ]
    sections.append("<conversation>\n" + "\n\n".join(rendered) + "\n</conversation>")

    prompt = "\n\n".join(sections)
    if len(prompt) > MAX_PROMPT_CHARS:
        # Trim from the FRONT of the conversation. The tail holds the current
        # question and the most recent tool results; the head is the oldest
        # context and the cheapest thing to lose.
        marker = "…(earlier turns trimmed)\n"
        # The marker counts against the budget. Computing the overflow without
        # it overshoots the cap by exactly its length — a test caught that;
        # untested it would have surfaced only as a Windows argv-limit failure,
        # a long way from here.
        overflow = len(prompt) - MAX_PROMPT_CHARS + len(marker)
        head, sep, convo = prompt.partition("<conversation>\n")
        if sep and len(convo) > overflow:
            prompt = head + sep + marker + convo[overflow:]
        else:
            prompt = prompt[-MAX_PROMPT_CHARS:]
    return prompt


# ── Reply decoding ──────────────────────────────────────────────────────────


def _extract_json(raw: str) -> dict | None:
    """Find the JSON object in a reply that may be fenced or padded with prose.

    Three attempts, cheapest first. Returns None when there is nothing usable —
    the caller treats that as a plain answer rather than an error, because a
    model that replied in prose still replied.
    """
    text = raw.strip()
    if text.startswith("```"):
        # ```json ... ``` — drop the fence lines.
        lines = [ln for ln in text.splitlines() if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def decode_reply(raw: str) -> ModelResponse:
    """Turn the CLI's text into the block shape the Messages API would return.

    DEGRADES TO AN ANSWER, never to an error. A reply we cannot parse as the
    protocol is still a reply, and surfacing the model's prose beats surfacing
    "the model call failed" for a call that succeeded. The only thing lost is
    the chance to call a tool this round.
    """
    parsed = _extract_json(raw)
    if parsed is None:
        return ModelResponse(content=[TextBlock(text=raw.strip())], stop_reason="end_turn")

    calls = parsed.get("tool_calls")
    if isinstance(calls, list) and calls:
        blocks: list[Any] = []
        for call in calls:
            if not isinstance(call, dict):
                continue
            name = call.get("name")
            if not isinstance(name, str) or not name:
                continue
            arguments = call.get("input")
            blocks.append(
                ToolUseBlock(
                    # Prefixed like the API's own ids so a log line reads the
                    # same whichever transport produced it.
                    id=f"toolu_cli_{uuid4().hex[:16]}",
                    name=name,
                    input=arguments if isinstance(arguments, dict) else {},
                )
            )
        if blocks:
            return ModelResponse(content=blocks, stop_reason="tool_use")

    answer = parsed.get("answer")
    if isinstance(answer, str):
        return ModelResponse(content=[TextBlock(text=answer)], stop_reason="end_turn")

    # Well-formed JSON that is neither shape. Hand back its text rather than
    # nothing, so the user sees something they can act on.
    return ModelResponse(
        content=[TextBlock(text=json.dumps(parsed, ensure_ascii=False))],
        stop_reason="end_turn",
    )


# ── The client ──────────────────────────────────────────────────────────────


def claude_executable() -> str | None:
    """Absolute path to the `claude` CLI, or None.

    Resolved with `shutil.which` rather than passed bare. Bare names DO spawn on
    this machine, so this is not a bug fix — it is the difference between a
    clean "the CLI is not installed" and a `FileNotFoundError` from deep inside
    asyncio when a deployment's PATH differs from a developer's shell, which is
    a failure mode this workspace has hit twice on Windows.
    """
    return shutil.which(os.environ.get("POCKETPAW_CLAUDE_CLI", "claude"))


class _Messages:
    """The `.messages` namespace, so this object is shaped like AsyncAnthropic."""

    def __init__(self, client: ClaudeCliClient) -> None:
        self._client = client

    async def create(self, **kwargs: Any) -> ModelResponse:
        return await self._client._create(**kwargs)


class ClaudeCliClient:
    """A drop-in for `AsyncAnthropic` that shells the `claude` CLI.

    Needs NO API key — the CLI authenticates itself from the machine's own
    login, which is the entire reason this class exists.
    """

    def __init__(self, executable: str, timeout: float = CLI_TIMEOUT_SECONDS) -> None:
        self._executable = executable
        self._timeout = timeout

    @property
    def messages(self) -> _Messages:
        return _Messages(self)

    async def _create(
        self,
        *,
        system: Any = "",
        messages: list[dict] | None = None,
        tools: list[dict] | None = None,
        **_ignored: Any,
    ) -> ModelResponse:
        """Run one turn.

        `**_ignored` swallows `model`, `max_tokens`, `thinking`, `output_config`
        — Messages-API parameters with no CLI equivalent. Accepted and dropped
        rather than rejected, so the call site stays identical for both
        transports and adding a parameter for one cannot break the other.
        """
        # `system` arrives as the API's block list; the CLI wants text.
        if isinstance(system, list):
            system_text = "\n".join(str(b.get("text", "")) for b in system if isinstance(b, dict))
        else:
            system_text = str(system or "")

        prompt = render_prompt(system_text, messages or [], tools)
        raw = await self._run(prompt)
        return decode_reply(raw)

    async def _run(self, prompt: str) -> str:
        """Shell the CLI and return the model's text.

        The prompt is ONE argv element and nothing is shell-interpolated —
        the same discipline `foreman.ClaudeCliLlm` uses, and the reason a
        prompt containing backticks or quotes is inert here.
        """
        proc = await asyncio.create_subprocess_exec(
            self._executable,
            "-p",
            prompt,
            "--output-format",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(f"the claude CLI timed out after {self._timeout:.0f}s") from None

        if proc.returncode != 0:
            err = err_b.decode("utf-8", "replace").strip()
            raise RuntimeError(f"the claude CLI failed (exit {proc.returncode}): {err[:300]}")

        out = out_b.decode("utf-8", "replace")
        try:
            envelope = json.loads(out)
        except json.JSONDecodeError:
            # An older CLI, or plain output. The text IS the reply.
            return out
        if isinstance(envelope, dict):
            if envelope.get("is_error"):
                raise RuntimeError(f"the claude CLI reported an error: {str(envelope)[:300]}")
            result = envelope.get("result")
            if isinstance(result, str):
                return result
        return out


__all__ = [
    "CLI_TIMEOUT_SECONDS",
    "ClaudeCliClient",
    "ModelResponse",
    "TextBlock",
    "ToolUseBlock",
    "claude_executable",
    "decode_reply",
    "render_prompt",
]
