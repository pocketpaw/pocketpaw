"""Check whether this box's LLM route carries Claude Code MCP tool search.

Claude Code turns MCP tool search off behind any ANTHROPIC_BASE_URL that is not
api.anthropic.com, so PocketPaw on a gateway provider (litellm, openrouter,
openai_compatible) sends every MCP tool schema upfront.
``POCKETPAW_CLAUDE_SDK_TOOL_SEARCH=true`` turns it back on, but then requests fail
unless the gateway forwards the ``anthropic-beta`` header, ``defer_loading`` tool
fields and ``tool_reference`` blocks, to a Claude 4.5 or later model. Run this on
the box, with the env and config the server uses, before setting it.

It builds the turn's options through the real backend,
``ClaudeSDKBackend(settings)._build_options(...)``, so the provider env, model
(smart routing included), CLI and tool gate are the ones a real turn gets. Only
the probe's pieces replace the backend's: an in-process MCP server with two tools,
``tools=["ToolSearch"]``, an allowlist of those three, and ``ENABLE_TOOL_SEARCH=true``
in the env. The model is asked to call one of the tools. With smart routing on,
the probe's prompt picks one tier; the others need checking the same way.

    PASS  the model loaded the deferred tool through ToolSearch and called it
    FAIL  the options could not be built, the turn errored (often the gateway
          rejecting the tool-search request), the tool ran without ToolSearch
          (deferral not active), or never ran

Run with:

    uv run python scripts/check_gateway_tool_search.py

Exits 0 on PASS, 1 otherwise. It spends one short turn on whatever account the
route bills (an API key, a gateway key, or the CLI's own login).
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import sys
from urllib.parse import urlsplit

_SERVER = "gwcheck"
_TARGET = f"mcp__{_SERVER}__lookup_order_status"
_DECOY = f"mcp__{_SERVER}__get_weather"
_TIMEOUT_S = 240
_PROMPT = (
    "Look up the status of order 4417 with the lookup_order_status tool and reply with "
    "the status it returns. You must call that tool; do not answer from memory."
)


async def _turn(options, called: dict[str, bool]) -> str:
    """Run the turn; return "" on PASS, else why it failed."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, ToolUseBlock, query

    tool_calls: list[str] = []
    result = None
    async for message in query(prompt=_PROMPT, options=options):
        if isinstance(message, AssistantMessage):
            tool_calls += [b.name for b in message.content if isinstance(b, ToolUseBlock)]
        elif isinstance(message, ResultMessage):
            result = message
    print(f"tool calls: {tool_calls or 'none'}")
    if result is None:
        return "the turn ended without a result message"
    if result.is_error:
        return f"the turn errored ({result.subtype}): {result.result}"
    if not called["target"]:
        return f"the model never called {_TARGET}; reply: {result.result!r}"
    first = tool_calls.index(_TARGET) if _TARGET in tool_calls else len(tool_calls)
    if "ToolSearch" not in tool_calls[:first]:
        return (
            f"{_TARGET} ran without a ToolSearch call first, so tools were loaded upfront: "
            "check that CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS is unset and the model is "
            "Claude 4.5 or later"
        )
    return ""


async def _probe_options(settings, called: dict[str, bool], stderr: list[str]):
    """The backend's options for this turn, with the probe's MCP server and tools."""
    from claude_agent_sdk import create_sdk_mcp_server, tool

    from pocketpaw.agents.claude_sdk import ClaudeSDKBackend

    @tool(
        "lookup_order_status", "Look up the shipping status of an order by id.", {"order_id": str}
    )
    async def lookup_order_status(args):
        called["target"] = True
        return {"content": [{"type": "text", "text": f"Order {args['order_id']} shipped."}]}

    @tool("get_weather", "Get today's weather for a city.", {"city": str})
    async def get_weather(args):
        return {"content": [{"type": "text", "text": "Sunny."}]}

    backend = ClaudeSDKBackend(settings)
    if not backend._sdk_available:
        raise RuntimeError("the Claude SDK backend could not load claude_agent_sdk")
    built = await backend._build_options(
        _PROMPT,
        system_prompt="You are a test harness. Use tools when asked.",
        session_key="check-gateway-tool-search",
        deny_mcp_tool_ids=frozenset(),
        # The tool gate is built from the allowlist inside _build_options, so the
        # probe's tools must be on it there, not only in the override below.
        allow_sdk_tools=frozenset({_TARGET, _DECOY}),
        allow_mcp_tool_ids=None,
        skill_names=frozenset(),
        stderr_sink=stderr,
    )
    return dataclasses.replace(
        built.options,
        mcp_servers={
            _SERVER: create_sdk_mcp_server(_SERVER, tools=[lookup_order_status, get_weather])
        },
        tools=["ToolSearch"],
        allowed_tools=["ToolSearch", _TARGET, _DECOY],
        env={**built.options.env, "ENABLE_TOOL_SEARCH": "true"},
    )


async def main() -> int:
    from pocketpaw.config import Settings
    from pocketpaw.llm.client import resolve_backend_env

    settings = Settings.load()
    resolve_backend_env(settings)  # what the server does at startup
    called = {"target": False}
    stderr: list[str] = []
    try:
        options = await _probe_options(settings, called, stderr)
        # The CLI inherits the process env under options.env, as the backend assumes.
        base_url = options.env.get("ANTHROPIC_BASE_URL", os.environ.get("ANTHROPIC_BASE_URL"))
        host = urlsplit(base_url).hostname if base_url else "api.anthropic.com"
        print(
            f"provider={settings.claude_sdk_provider or 'anthropic'} host={host} "
            f"model={options.model or '<CLI default>'} cli={options.cli_path or '<bundled>'}"
        )
        why = await asyncio.wait_for(_turn(options, called), _TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 - any failure is a FAIL with its reason
        why = f"{type(exc).__name__}: {exc}"
    if why:
        print(f"FAIL: {why}")
        if stderr:
            print("CLI stderr (last lines):\n  " + "\n  ".join(stderr[-15:]))
        return 1
    print(f"PASS: {_TARGET} was found with ToolSearch and called")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
