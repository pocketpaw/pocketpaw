"""Smoke test — agent dispatch in group chat with mentions.

Verifies _should_agent_respond logic directly (avoids spinning up the agent
pool / LLM) for the matrix:

| mode         | mention X | mention Y | none |
|--------------|-----------|-----------|------|
| silent       | False     | False     | False|
| auto (X)     | True      | False     | True |
| auto (Y)     | False     | True      | True |
| mention_only | True (X)  | True (Y)  | False|
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass


@dataclass
class FakeGroupAgent:
    agent: str
    respond_mode: str


def _mention(agent_id: str) -> dict:
    return {"type": "agent", "id": agent_id, "display_name": f"@{agent_id}"}


async def main() -> int:
    from pocketpaw_ee.cloud.shared.agent_bridge import _should_agent_respond

    AX = FakeGroupAgent("agent-x", "auto")
    AY = FakeGroupAgent("agent-y", "auto")
    MX = FakeGroupAgent("agent-x", "mention_only")
    SY = FakeGroupAgent("agent-y", "silent")

    cases: list[tuple[str, FakeGroupAgent, list[dict], bool]] = [
        # --- the bug we fixed: two auto agents, only the mentioned one responds ---
        ("two-auto-mention-X / X", AX, [_mention("agent-x")], True),
        ("two-auto-mention-X / Y", AY, [_mention("agent-x")], False),
        ("two-auto-mention-Y / X", AX, [_mention("agent-y")], False),
        ("two-auto-mention-Y / Y", AY, [_mention("agent-y")], True),
        # --- broadcast (no mentions): both auto agents respond ---
        ("two-auto-no-mention / X", AX, [], True),
        ("two-auto-no-mention / Y", AY, [], True),
        # --- mention_only ---
        ("mention_only-mentioned", MX, [_mention("agent-x")], True),
        ("mention_only-not-mentioned", MX, [_mention("agent-y")], False),
        ("mention_only-no-mention", MX, [], False),
        # --- silent always opts out, even when mentioned ---
        ("silent-mentioned", SY, [_mention("agent-y")], False),
        ("silent-no-mention", SY, [], False),
        # --- non-agent mentions (user mentions) don't gate agent dispatch ---
        ("auto-only-user-mentions / X", AX, [{"type": "user", "id": "u1"}], True),
    ]

    failures: list[str] = []
    for name, ga, mentions, expected in cases:
        actual = await _should_agent_respond(ga, "hello", mentions)
        status = "OK" if actual == expected else "FAIL"
        print(f"  [{status}] {name}: expected={expected} actual={actual}")
        if actual != expected:
            failures.append(name)

    if failures:
        print(f"\n{len(failures)} FAIL(s): {failures}")
        return 1
    print("\nSMOKE OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
