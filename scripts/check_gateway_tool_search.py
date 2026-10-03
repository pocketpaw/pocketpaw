"""Check whether this box's LLM route carries Claude Code MCP tool search.

Claude Code turns MCP tool search off behind any ANTHROPIC_BASE_URL that is not
api.anthropic.com, so PocketPaw on a gateway provider (litellm, openrouter,
openai_compatible, ollama, gemini) sends every MCP tool schema upfront.
``POCKETPAW_CLAUDE_SDK_TOOL_SEARCH=true`` turns it back on, but then requests fail
unless the gateway forwards the ``anthropic-beta`` header, ``defer_loading`` tool
fields and ``tool_reference`` blocks, to a Claude 4.5 or later model. Run this on
the box, with the env and config the server uses, before setting it.

It runs one claude_agent_sdk turn the way the Claude SDK backend would: the
provider env from ``resolve_llm_client(...).to_sdk_env()`` for
``claude_sdk_provider``, the same CLI (``claude_sdk_cli_path``) and model choice,
plus ``ENABLE_TOOL_SEARCH=true`` and an in-process MCP server with two tools. The
model is asked to call one of them.

    PASS  the model loaded the deferred tool through ToolSearch and called it
    FAIL  the turn errored (often the gateway rejecting the tool-search request),
          the tool ran without ToolSearch (deferral not active), or never ran

Run with:

    uv run python scripts/check_gateway_tool_search.py

Exits 0 on PASS, 1 otherwise. It spends one short turn on whatever account the
route bills (an API key, a gateway key, or the CLI's own login).
"""

from __future__ import annotations

import asyncio
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


async def main() -> int:
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server, tool

    from pocketpaw.config import Settings
    from pocketpaw.llm.client import resolve_backend_env, resolve_llm_client

    settings = Settings.load()
    resolve_backend_env(settings)  # what the server does at startup
    provider = settings.claude_sdk_provider or "anthropic"
    llm = resolve_llm_client(settings, force_provider=provider)
    env = llm.to_sdk_env()
    env["ENABLE_TOOL_SEARCH"] = "true"
    env["ENABLE_CLAUDEAI_MCP_SERVERS"] = "false"  # keep a claude.ai login's connectors out
    non_anthropic = (
        llm.is_ollama
        or llm.is_openai_compatible
        or llm.is_gemini
        or llm.is_litellm
        or llm.is_openrouter
    )  # the backend's is_non_anthropic
    model = llm.model if non_anthropic else (settings.claude_sdk_model or None)
    cli_path = (settings.claude_sdk_cli_path or "").strip() or None
    base_url = env.get("ANTHROPIC_BASE_URL")
    host = urlsplit(base_url).hostname if base_url else "api.anthropic.com"
    print(
        f"provider={provider} host={host} model={model or '<CLI default>'} "
        f"cli={cli_path or '<bundled>'}"
    )

    called = {"target": False}

    @tool(
        "lookup_order_status", "Look up the shipping status of an order by id.", {"order_id": str}
    )
    async def lookup_order_status(args):
        called["target"] = True
        return {"content": [{"type": "text", "text": f"Order {args['order_id']} shipped."}]}

    @tool("get_weather", "Get today's weather for a city.", {"city": str})
    async def get_weather(args):
        return {"content": [{"type": "text", "text": "Sunny."}]}

    stderr: list[str] = []
    options = ClaudeAgentOptions(
        system_prompt="You are a test harness. Use tools when asked.",
        mcp_servers={
            _SERVER: create_sdk_mcp_server(_SERVER, tools=[lookup_order_status, get_weather])
        },
        tools=["ToolSearch"],
        allowed_tools=["ToolSearch", _TARGET, _DECOY],
        permission_mode="bypassPermissions",
        setting_sources=[],  # never load ~/.claude settings or hooks
        max_turns=6,
        env=env,
        model=model,
        cli_path=cli_path,
        stderr=stderr.append,
    )
    try:
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
