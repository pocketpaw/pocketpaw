"""A/B the Pydantic AI backend's tool surface against real user phrasings.

Created 2026-08-01.

Exists to answer the one question the deferred-loading work could not answer by
measurement: hiding 97 tools behind ``search_tools`` saves ~24,600 tokens per
request, but only if the model still finds the tool it needs. Token counts are
arithmetic; whether a given model searches rather than answering without looking
is behaviour, and behaviour needs cases.

This is a script, not a test. It spends real tokens against whatever
``POCKETPAW_PYDANTIC_AI_MODEL`` points at, so nothing runs it implicitly.

    uv run python scripts/evals/tool_search_eval.py                  # A/B deferral
    uv run python scripts/evals/tool_search_eval.py --thinking low   # add an effort level
    uv run python scripts/evals/tool_search_eval.py --cases 3        # a cheap smoke run

What it reports per arm: how often the expected tool was called, how many model
requests it took, and the input tokens spent. A saving that costs you the task
is not a saving, and that trade is what the table is for.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass, field

os.environ.setdefault("POCKETPAW_AGENT_BACKEND", "pydantic_ai")


@dataclass
class Case:
    """One user turn, and the tool a correct run reaches for."""

    name: str
    prompt: str
    expect_any_of: tuple[str, ...]


# Phrasings a user actually types, not the tool's own vocabulary. The gap
# between the two is the whole point: "make me a webpage" shares no token with
# ``pocketpaw_sites_manager_create_html_site``.
CASES: list[Case] = [
    Case(
        "build-site",
        "Build me a one-page site for a bakery called Flour & Ash.",
        ("create_html_site", "create_svelte_site", "create_landing_site"),
    ),
    Case(
        "webpage-word",
        "Make me a webpage for my dentist practice.",
        ("create_html_site", "create_svelte_site", "create_landing_site"),
    ),
    Case("publish", "Publish it.", ("publish",)),
    Case("deploy-word", "Ship the draft live please.", ("publish",)),
    Case(
        "edit-section",
        "Change the hero section copy to something warmer.",
        ("edit_svelte_component", "create_html_site"),
    ),
    Case("widget", "Add a revenue chart to the dashboard.", ("add_widget",)),
    Case(
        "image",
        "Generate a picture of a croissant for the header.",
        ("image_generate", "search_stock_images"),
    ),
    Case(
        "connector",
        "Connect my gmail so it can read the enquiries.",
        ("connector_connect", "connector_execute", "sense_execute"),
    ),
]


@dataclass
class ArmResult:
    label: str
    hits: int = 0
    misses: list[str] = field(default_factory=list)
    requests: int = 0
    input_tokens: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.hits + len(self.misses)

    def row(self) -> str:
        pct = (100 * self.hits // self.total) if self.total else 0
        return (
            f"{self.label:<28} {self.hits}/{self.total} ({pct:3d}%)  "
            f"requests={self.requests:<4} input_tokens={self.input_tokens:,}"
        )


async def run_case(settings_overrides: dict, case: Case) -> tuple[bool, int, int, str | None]:
    """One case, one configuration. Returns (hit, requests, input_tokens, error)."""
    from pocketpaw.agents.pydantic_ai import PydanticAIBackend
    from pocketpaw.config import Settings

    backend = PydanticAIBackend(Settings(**settings_overrides))
    called: list[str] = []
    requests = 0
    tokens = 0
    error: str | None = None
    try:
        async for ev in backend.run(case.prompt, session_key=f"eval-{case.name}"):
            if ev.type == "tool_use":
                called.append((ev.metadata or {}).get("name", ""))
            elif ev.type == "token_usage":
                tokens = int((ev.metadata or {}).get("input_tokens", 0) or 0)
            elif ev.type == "error":
                error = ev.content[:200]
    except Exception as exc:  # noqa: BLE001
        error = f"{type(exc).__name__}: {exc}"[:200]
    finally:
        await backend.stop()

    requests = len(called)
    hit = any(any(want in name for want in case.expect_any_of) for name in called)
    return hit, requests, tokens, error


async def run_arm(label: str, overrides: dict, cases: list[Case]) -> ArmResult:
    result = ArmResult(label=label)
    for case in cases:
        hit, requests, tokens, error = await run_case(overrides, case)
        result.requests += requests
        result.input_tokens += tokens
        if error:
            result.errors.append(f"{case.name}: {error}")
        if hit:
            result.hits += 1
        else:
            result.misses.append(case.name)
        print(f"  {'HIT ' if hit else 'MISS'} {case.name}", flush=True)
    return result


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cases", type=int, default=0, help="run only the first N cases")
    ap.add_argument("--thinking", default="", help="also A/B this reasoning effort level")
    args = ap.parse_args()

    cases = CASES[: args.cases] if args.cases else CASES

    base: dict = {}
    arms = [
        ("deferral off (today's default)", {**base, "pydantic_ai_defer_mcp_tools": False}),
        ("deferral on", {**base, "pydantic_ai_defer_mcp_tools": True}),
    ]
    if args.thinking:
        arms.append(
            (
                f"deferral on + thinking={args.thinking}",
                {
                    **base,
                    "pydantic_ai_defer_mcp_tools": True,
                    "pydantic_ai_thinking": args.thinking,
                },
            )
        )

    results: list[ArmResult] = []
    for label, overrides in arms:
        print(f"\n=== {label} ===", flush=True)
        results.append(await run_arm(label, overrides, cases))

    print("\n" + "=" * 78)
    if any(r.total and len(r.errors) == r.total for r in results):
        print("NO RESULT — every case in an arm errored, so these are not")
        print("scores. The model was unreachable. Check the proxy's /health")
        print("before reading anything into the table below.")
    for r in results:
        print(r.row())
    print("=" * 78)
    for r in results:
        if r.misses:
            print(f"{r.label}: missed {', '.join(r.misses)}")
        for err in r.errors[:3]:
            print(f"{r.label}: ERROR {err}")

    # The trade, stated rather than implied: tokens saved against tasks lost.
    if len(results) >= 2:
        off, on = results[0], results[1]
        saved = off.input_tokens - on.input_tokens
        lost = off.hits - on.hits
        print(
            f"\ndeferral: {saved:+,} input tokens, {-lost:+d} tasks. "
            + ("Worth it." if lost <= 0 and saved > 0 else "Read the misses before shipping it.")
        )


if __name__ == "__main__":
    asyncio.run(main())
