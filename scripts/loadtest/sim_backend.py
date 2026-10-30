"""A fake AgentBackend that simulates a realistic agent turn without calling
any model — so the chat stack can be load-tested at zero token cost.

Created 2026-07-29 — companion to ``chat_loadtest.py``. It exists to separate
the two independent ceilings that "how many users can we handle" collapses:

  1. YOUR stack — web process, run executor, Redis stream transport, Mongo,
     SSE fan-out. Measurable with this backend. Costs nothing.
  2. The provider — Anthropic ITPM/OTPM and the per-run Claude Code CLI
     subprocess footprint. Needs a real API key and real spend.

This module answers (1) only. Numbers produced with it are an UPPER bound on
what the real path can do: a real run also holds a Node subprocess and waits on
the model. Set ``PAW_SIM_SUBPROC=1`` to reproduce the process/RSS dimension.

Timing knobs (env, all optional) — defaults model a site-creation turn:
    PAW_SIM_TTFT_MS          1200   delay before the first token
    PAW_SIM_DURATION_MS      25000  total turn length
    PAW_SIM_TOKEN_INTERVAL_MS  40   gap between text chunks
    PAW_SIM_TOOLS               6   tool_use/tool_result pairs per turn
    PAW_SIM_JITTER_PCT         25   +/- randomisation on every delay
    PAW_SIM_SUBPROC             0   1 = spawn a real sleeping subprocess per run
    PAW_SIM_RSS_MB              0   MB to allocate per run (emulates CLI memory)
    PAW_SIM_FAIL_PCT            0   % of runs that end in an error event
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time
from collections.abc import AsyncIterator
from typing import Any

from pocketpaw.agents.backend import BackendInfo, Capability
from pocketpaw.agents.protocol import AgentEvent
from pocketpaw.tools.policy import ToolPolicy

_LOREM = (
    "Setting up the project structure and installing dependencies. "
    "Drafting the hero section with a headline and call to action. "
    "Wiring the pricing table and the contact form. "
    "Running the build and checking the output. "
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _jitter(value: float, pct: int) -> float:
    if pct <= 0:
        return value
    span = value * (pct / 100.0)
    return max(0.0, value + random.uniform(-span, span))


class SimBackend:
    """Emits the same AgentEvent stream shape a real turn does, on a timer."""

    def __init__(self, settings: Any = None) -> None:
        self._settings = settings
        self._policy = ToolPolicy()
        # NOTE: ``AgentPool`` caches ONE backend instance per agent, so every
        # concurrent run shares this object. Cancellation state must therefore
        # be per-run — an instance-level ``_stopped`` flag makes run N's stop()
        # silently truncate every other in-flight run, which shows up as a
        # clean ``stream_end`` carrying no content at all. Real cancellation
        # still works: the executor cancels the asyncio task.
        self._stops = 0

    @staticmethod
    def info() -> BackendInfo:
        return BackendInfo(
            name="sim",
            display_name="Load-test simulator",
            capabilities=(
                Capability.STREAMING
                | Capability.TOOLS
                | Capability.MULTI_TURN
                | Capability.CUSTOM_SYSTEM_PROMPT
            ),
            builtin_tools=["Bash", "Read", "Write"],
            required_keys=[],
            supported_providers=["sim"],
            beta=True,
        )

    async def run(
        self,
        message: str,
        *,
        system_prompt: str | None = None,
        history: list[dict] | None = None,
        session_key: str | None = None,
        **_kwargs: Any,
    ) -> AsyncIterator[AgentEvent]:
        ttft = _env_int("PAW_SIM_TTFT_MS", 1200) / 1000
        duration = _env_int("PAW_SIM_DURATION_MS", 25_000) / 1000
        interval = _env_int("PAW_SIM_TOKEN_INTERVAL_MS", 40) / 1000
        n_tools = _env_int("PAW_SIM_TOOLS", 6)
        jitter_pct = _env_int("PAW_SIM_JITTER_PCT", 25)
        fail_pct = _env_int("PAW_SIM_FAIL_PCT", 0)
        rss_mb = _env_int("PAW_SIM_RSS_MB", 0)
        want_proc = _env_int("PAW_SIM_SUBPROC", 0) == 1

        ballast: bytearray | None = None
        proc: asyncio.subprocess.Process | None = None
        started = time.perf_counter()
        try:
            # Hold the resources a real run would, so the sampler sees them.
            if rss_mb > 0:
                ballast = bytearray(rss_mb * 1_048_576)
            if want_proc:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-c",
                    f"import time; time.sleep({duration + 5})",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )

            await asyncio.sleep(_jitter(ttft, jitter_pct))

            # Interleave text and tool calls across the remaining budget.
            body = duration - ttft
            tool_at = {
                int((i + 1) * (body / interval) / max(n_tools + 1, 1)) for i in range(n_tools)
            }
            chunks = max(1, int(body / interval))
            words = _LOREM.split()
            emitted = 0
            for i in range(chunks):
                if i in tool_at:
                    name = random.choice(["Write", "Bash", "Read", "Edit"])
                    yield AgentEvent(
                        type="tool_use", content=name, metadata={"tool": name, "input": {}}
                    )
                    await asyncio.sleep(_jitter(0.35, jitter_pct))
                    yield AgentEvent(type="tool_result", content="ok", metadata={"tool": name})
                    emitted += 40
                    continue
                word = words[i % len(words)]
                yield AgentEvent(type="message", content=word + " ")
                emitted += 1
                await asyncio.sleep(_jitter(interval, jitter_pct))

            if fail_pct and random.randint(1, 100) <= fail_pct:
                yield AgentEvent(type="error", content="simulated backend failure")
                return

            elapsed = time.perf_counter() - started
            yield AgentEvent(
                type="token_usage",
                content="",
                metadata={
                    "input_tokens": 8_000 + len(message),
                    "output_tokens": emitted * 4,
                    "cache_read_input_tokens": 40_000,
                    "sim_elapsed_s": round(elapsed, 2),
                },
            )
            yield AgentEvent(type="done", content="")
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await asyncio.gather(proc.wait(), return_exceptions=True)
            del ballast

    async def stop(self) -> None:
        # Per-run cancellation only; see __init__. Counted for observability.
        self._stops += 1

    async def get_status(self) -> dict[str, Any]:
        return {"backend": "sim", "healthy": True}

    def get_tool_policy(self) -> ToolPolicy:
        return self._policy

    def set_tool_policy(self, policy: ToolPolicy) -> None:
        self._policy = policy

    def attach_specialist_tools(self, tools: list[Any]) -> None:
        return None

    def attach_subprocess_env(self, env: dict[str, str]) -> None:
        return None
