import asyncio
import os
import sys
import time
from typing import Any

# Ensure pocketpaw is in the python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../../src")))

from pocketpaw.agents.router import AgentRouter
from pocketpaw.config import Settings


async def _process_prompt(prompt: str, options: dict) -> dict:
    config = options.get("config", {})
    backend = config.get("backend", "anthropic")  # Maps to claude_agent_sdk basically, or openai
    config.get("channel", "api")
    trust_level = config.get("trust_level", "standard")

    # Load settings and conditionally override based on backend config
    settings = Settings.load()
    if backend == "anthropic":
        settings.agent_backend = "claude_agent_sdk"
    elif backend == "openai":
        settings.agent_backend = "openai_agents"
    elif backend == "google":
        settings.agent_backend = "google_adk"
    else:
        settings.agent_backend = backend

    router = AgentRouter(settings)
    session_key = "promptfoo_redteam_session"

    output_text = ""
    token_usage = {"total": 0, "prompt": 0, "completion": 0}
    tool_calls = []
    guardian_blocked = False

    start_time = time.monotonic()

    # Run agent loop
    async for event in router.run(prompt, session_key=session_key):
        if event.type == "message":
            output_text += event.content
        elif event.type == "tool_use":
            # For promptfoo, it's often useful to convert model_dump to dict if it's Pydantic
            meta = event.metadata or {}
            tool_calls.append(meta)
        elif event.type == "token_usage":
            meta = event.metadata or {}
            token_usage["prompt"] += meta.get("input_tokens", 0)
            token_usage["completion"] += meta.get("output_tokens", 0)
            token_usage["total"] += meta.get("input_tokens", 0) + meta.get("output_tokens", 0)
        elif event.type == "error":
            output_text += f"\nError: {event.content}"
            curr_str = str(event.content).lower()
            if "blocked" in curr_str or "guardian" in curr_str or "safety" in curr_str:
                guardian_blocked = True

    latency_ms = int((time.monotonic() - start_time) * 1000)
    await router.stop()

    return {
        "output": output_text,
        "tokenUsage": token_usage,
        "cost": 0,
        "latencyMs": latency_ms,
        "metadata": {
            "tool_calls": tool_calls,
            "guardian_blocked": guardian_blocked,
            "trust_level": trust_level,
        },
    }


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Thin wrapper around PocketPaw agent pipeline for promptfoo evaluation."""
    return asyncio.run(_process_prompt(prompt, options))
