"""Measure what our prompt caps and cache markers actually cost (PA-9).

Created 2026-08-03 (PA-9, feat/prompt-budget-measurement).

Exists because every cap in the prompt path was inherited rather than measured:
``PREAMBLE_MAX_CHARS = 1500``, the 12-widget cut in ``handlers/pocket.py``, and
``_DEFAULT_BUDGET_CHARS = 32_000`` were all set by judgement and carried
forward. PA-9 is the task that replaces the judgement with numbers.

It answers four questions, one per arm:

  ``--arm threshold``  The headline. ``_ANTHROPIC_CACHE_MIN_CHARS = 4000`` in
      ``agents/deep_agents.py`` gates cache marking in CHARS, while
      ``llm/caching.CACHE_MIN_TOKENS`` states the provider floors in TOKENS.
      4000 chars is ~1000 tokens, well under Haiku 4.5's 4096-token floor. This
      arm sweeps prefix sizes across that floor and reports, per size, whether a
      warm turn actually read from cache — and whether marking a sub-floor
      prompt costs anything (the write premium question).

  ``--arm warm``  The filed ``RunUsage`` harness: >=6 warm turns over a real
      marked prefix (``POCKET_SPECIALIST_PROMPT``, ~67.5k chars), reporting
      ``cache_read`` vs the uncached remainder per turn.

  ``--arm caps``  The per-cap cost. Converts each cap from chars to MEASURED
      tokens (not the /4 rule of thumb) and prices it per turn.

  ``--arm summary-ab``  A/B of ``prompt_pocket_summary_only`` rendered through
      the REAL ``ChannelCurrentPocketLayer``, not a hand-built blob — the
      distinction is the finding. PA-8a's ``_WIDGET_SUMMARY_MAX_CHARS`` bounds
      the widget dump before serialisation, so the OFF arm plateaus near 3.2k
      chars instead of growing to the ~41k a synthetic dump would suggest, and
      the flag's real saving is ~50% rather than ~96%.

This is a script, not a test. It spends real money against a live API, so
nothing runs it implicitly and no test imports it.

    uv run python scripts/evals/prompt_cache_eval.py --arm threshold
    uv run python scripts/evals/prompt_cache_eval.py --arm warm --turns 6
    uv run python scripts/evals/prompt_cache_eval.py --arm caps
    uv run python scripts/evals/prompt_cache_eval.py --arm summary-ab
    uv run python scripts/evals/prompt_cache_eval.py --arm all

Route: OpenRouter (``settings.openrouter_api_key``). The LiteLLM gateway at
``settings.litellm_api_base`` 502s every chat completion behind a
``headroom-compression`` guardrail that 404s, and the direct DeepSeek route
returns 401 — both were probed on 2026-08-03 and neither is a fault in this
repo. OpenRouter is passed ``{"usage": {"include": true}}`` so every response
carries ``usage.cost`` and ``usage.prompt_tokens_details.cached_tokens``.

The key is read from settings at run time and never printed. Every arm skips
with a clear message (exit 0) when no key is configured, so this file is safe
to leave in a repo that CI checks out.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from pocketpaw.llm.caching import CACHE_MIN_TOKENS, build_cacheable, report_savings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Haiku 4.5 carries the HIGHEST documented Anthropic cache floor (4096 tokens),
# which makes it the strictest test of a chars-based threshold and also the
# cheapest model to run the test on. A threshold that is safe here is safe on
# every other Anthropic model.
MODEL = "anthropic/claude-haiku-4.5"

# Per-model cache floors, in TOKENS, for the models this harness can target.
# Haiku 4.5 has the highest floor and so needs the largest request to
# demonstrate a hit; a key with a small per-request cap may only be able to
# afford the warm arm on a lower-floor model.
MODEL_FLOOR_TOKENS = {
    "anthropic/claude-haiku-4.5": 4096,
    "anthropic/claude-sonnet-4.5": 1024,
    "anthropic/claude-opus-4.5": 4096,
}

# Keep every completion tiny — this harness measures INPUT accounting, and
# output tokens are pure cost with no signal.
MAX_TOKENS = 1


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class BudgetExhausted(RuntimeError):
    """The API refused on funding/size grounds, not on anything we control."""


@dataclass
class Call:
    """One completion's input accounting."""

    label: str
    prompt_tokens: int
    cached_tokens: int
    cost: float
    raw_usage: dict[str, Any] = field(default_factory=dict)

    @property
    def uncached(self) -> int:
        """The remainder we paid full price for."""
        return max(0, self.prompt_tokens - self.cached_tokens)


class Ledger:
    """Running total of what this run spent, so the report can state it."""

    def __init__(self) -> None:
        self.calls: list[Call] = []

    def add(self, call: Call) -> Call:
        self.calls.append(call)
        return call

    @property
    def total_cost(self) -> float:
        return sum(c.cost for c in self.calls)


def _api_key() -> str | None:
    """Read the OpenRouter key from settings at run time. Never logged."""
    from pocketpaw.config import get_settings

    return get_settings().openrouter_api_key or None


def _post(client: httpx.Client, key: str, system: Any, user: str, label: str) -> Call:
    """One completion. ``system`` is a str or a content-block list."""
    body = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        # Without this OpenRouter omits cost and the cached-token breakdown,
        # which is the entire measurement.
        "usage": {"include": True},
    }
    resp = client.post(
        OPENROUTER_URL,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body,
        timeout=120.0,
    )
    if resp.status_code == 402:
        # A free-tier OpenRouter key caps the size of a SINGLE request, so a
        # large prefix 402s while small ones keep succeeding. Say so plainly:
        # a bare traceback here reads like a broken script rather than a
        # funding limit, and that misdiagnosis costs more than the run.
        raise BudgetExhausted(
            f"OpenRouter returned 402 for {label} "
            f"(system prompt ~{len(str(system)):,} chars). "
            "On a free-tier key this is a per-request size cap, not total "
            "exhaustion — smaller prefixes still succeed. Re-run with a "
            "smaller --prefix-chars, or fund the key."
        )
    resp.raise_for_status()
    usage = resp.json().get("usage", {}) or {}

    # Reuse the shipped reporter rather than re-deriving the hit rate here.
    # OpenRouter returns the OpenAI shape (prompt_tokens +
    # prompt_tokens_details.cached_tokens), which report_savings already reads.
    savings = report_savings(usage)
    return Call(
        label=label,
        prompt_tokens=savings.prompt_tokens or int(usage.get("prompt_tokens", 0)),
        cached_tokens=savings.cache_read_tokens,
        cost=float(usage.get("cost", 0.0)),
        raw_usage=usage,
    )


# ---------------------------------------------------------------------------
# Corpus
# ---------------------------------------------------------------------------


def _corpus() -> str:
    """Real prompt text, so the measured chars-per-token ratio is OURS.

    The whole char-vs-token question turns on this ratio, and prose, JSON and
    XML-ish markup tokenize differently. Using the actual specialist prompt
    means the answer applies to the prompts we actually send.
    """
    from pocketpaw.ripple._pockets import POCKET_SPECIALIST_PROMPT

    return POCKET_SPECIALIST_PROMPT


def _prefix(size_chars: int, run_id: str) -> str:
    """A byte-stable prefix of ``size_chars``, unique to this run.

    The run id goes FIRST so the prefix differs across runs (guaranteeing a
    genuinely cold turn 1) while staying byte-identical within a run
    (guaranteeing turns 2..N can hit). Any drift here silently destroys the
    measurement, which is the same failure mode the caching module warns about.
    """
    head = f"<eval-run id={run_id} size={size_chars}>\n"
    body = _corpus()
    while len(body) < size_chars:
        body += body
    return head + body[: max(0, size_chars - len(head))]


# ---------------------------------------------------------------------------
# Arm 1 — the threshold sweep
# ---------------------------------------------------------------------------

# Bracket Haiku 4.5's 4096-token floor. 4000 is the live
# ``_ANTHROPIC_CACHE_MIN_CHARS``; 16000 is roughly where 4096 tokens lands if a
# token averages ~4 chars. Measuring both sides of the floor is the point.
SWEEP_SIZES = (2_000, 4_000, 8_000, 12_000, 16_000, 20_000)


def arm_threshold(client: httpx.Client, key: str, ledger: Ledger, run_id: str) -> None:
    """Find the prefix size at which a warm turn actually reads from cache."""
    print("\n=== ARM: threshold — where does caching actually start? ===")
    print(f"model={MODEL}  documented floor={CACHE_MIN_TOKENS['anthropic-haiku']} tokens\n")
    print(
        f"{'chars':>7} {'prompt_tok':>11} {'cold_cached':>12} "
        f"{'warm_cached':>12} {'cold_$':>9} {'warm_$':>9}  verdict"
    )

    rows = []
    for size in SWEEP_SIZES:
        prefix = _prefix(size, run_id)
        system = build_cacheable([prefix])
        cold = ledger.add(_post(client, key, system, "1", f"marked-cold-{size}"))
        # A warm turn must reuse the identical prefix; only the user turn moves.
        warm = ledger.add(_post(client, key, system, "2", f"marked-warm-{size}"))
        cached = warm.cached_tokens > 0
        verdict = "CACHES" if cached else "no cache"
        print(
            f"{size:>7} {warm.prompt_tokens:>11} {cold.cached_tokens:>12} "
            f"{warm.cached_tokens:>12} {cold.cost:>9.5f} {warm.cost:>9.5f}  {verdict}"
        )
        rows.append((size, warm.prompt_tokens, cold, warm, cached))

    # Chars-per-token, measured rather than assumed.
    biggest = rows[-1]
    cpt = biggest[0] / biggest[1] if biggest[1] else 0.0
    print(f"\nmeasured chars/token on our own prompt text: {cpt:.2f}")
    floor_tokens = CACHE_MIN_TOKENS["anthropic-haiku"]
    print(f"=> {floor_tokens}-token floor is about {floor_tokens * cpt:,.0f} chars")

    caching = [r for r in rows if r[4]]
    if caching:
        print(f"=> smallest prefix that cached: {caching[0][0]:,} chars")
    else:
        print("=> NOTHING in the sweep cached")

    # The write-premium question: does marking a SUB-FLOOR prompt cost extra?
    # If the provider silently declines to cache, marked and unmarked cold calls
    # cost the same and the marker is merely inert, not wasteful.
    print("\n--- write-premium check at 4000 chars (the live threshold) ---")
    prefix = _prefix(4_000, run_id + "-wp")
    marked = ledger.add(_post(client, key, build_cacheable([prefix]), "1", "wp-marked"))
    unmarked = ledger.add(_post(client, key, prefix, "1", "wp-unmarked"))
    print(f"  marked   cold: prompt={marked.prompt_tokens} cost=${marked.cost:.6f}")
    print(f"  unmarked cold: prompt={unmarked.prompt_tokens} cost=${unmarked.cost:.6f}")
    if unmarked.cost > 0:
        ratio = marked.cost / unmarked.cost
        print(f"  marked/unmarked cost ratio: {ratio:.3f}")
        if ratio > 1.10:
            print("  => a write premium IS charged below the floor (marker is wasteful)")
        else:
            print("  => NO write premium below the floor (marker is inert, not costly)")


# ---------------------------------------------------------------------------
# Arm 2 — the RunUsage warm-turn harness
# ---------------------------------------------------------------------------


def arm_warm(
    client: httpx.Client,
    key: str,
    ledger: Ledger,
    run_id: str,
    turns: int,
    prefix_chars: int = 0,
) -> None:
    """>=6 warm turns over a real marked prefix.

    ``prefix_chars`` truncates ``POCKET_SPECIALIST_PROMPT`` when the key cannot
    afford the whole 67.5k-char thing in one request. Any value comfortably
    above the measured cache floor (~14,300 chars) exercises the same code
    path and reports the same accounting.
    """
    print(f"\n=== ARM: warm — {turns} turns over the real POCKET_SPECIALIST_PROMPT ===")
    corpus = _corpus()
    prefix = f"<eval-run id={run_id}>\n" + corpus
    if prefix_chars:
        prefix = prefix[:prefix_chars]
    system = build_cacheable([prefix])
    print(f"prefix: {len(prefix):,} chars\n")
    print(f"{'turn':>5} {'prompt_tok':>11} {'cache_read':>11} {'uncached':>9} {'$':>10}")

    first_cost = None
    warm_costs = []
    for i in range(1, turns + 1):
        # Only the user turn varies; the marked prefix stays byte-identical.
        call = ledger.add(_post(client, key, system, f"turn {i}", f"warm-{i}"))
        print(
            f"{i:>5} {call.prompt_tokens:>11} {call.cached_tokens:>11} "
            f"{call.uncached:>9} {call.cost:>10.6f}"
        )
        if i == 1:
            first_cost = call.cost
        else:
            warm_costs.append(call.cost)
        time.sleep(0.4)

    if warm_costs and first_cost:
        avg_warm = sum(warm_costs) / len(warm_costs)
        print(f"\ncold turn:      ${first_cost:.6f}")
        print(f"avg warm turn:  ${avg_warm:.6f}")
        if avg_warm > 0:
            print(f"warm is {first_cost / avg_warm:.1f}x cheaper than cold")


# ---------------------------------------------------------------------------
# Arm 3 — what each cap costs
# ---------------------------------------------------------------------------


def _measure_tokens(client: httpx.Client, key: str, ledger: Ledger, text: str, label: str) -> Call:
    """Real token count for ``text`` (no cache marker, so nothing is reused)."""
    return ledger.add(_post(client, key, text, "1", label))


def arm_caps(client: httpx.Client, key: str, ledger: Ledger) -> None:
    """Price each inherited cap in measured tokens and dollars-per-turn."""
    print("\n=== ARM: caps — what each inherited cap actually costs ===")

    from pocketpaw_ee.cloud.surface.handlers._helpers import PREAMBLE_MAX_CHARS

    from pocketpaw.bootstrap.context_builder import _DEFAULT_BUDGET_CHARS

    corpus = _corpus()
    # An empty system prompt still bills a few tokens of chat scaffolding;
    # subtract that baseline so each cap's cost is the cap's own.
    base = _measure_tokens(client, key, ledger, "x", "caps-baseline")

    caps = [
        ("PREAMBLE_MAX_CHARS", PREAMBLE_MAX_CHARS),
        ("_DEFAULT_BUDGET_CHARS", _DEFAULT_BUDGET_CHARS),
    ]
    print(f"\n{'cap':>24} {'chars':>8} {'tokens':>8} {'chars/tok':>10} {'$/turn':>10}")
    for name, chars in caps:
        call = _measure_tokens(client, key, ledger, corpus[:chars], f"cap-{name}")
        tokens = max(0, call.prompt_tokens - base.prompt_tokens)
        cpt = chars / tokens if tokens else 0.0
        cost = max(0.0, call.cost - base.cost)
        print(f"{name:>24} {chars:>8,} {tokens:>8,} {cpt:>10.2f} {cost:>10.6f}")

    print(f"\nbaseline (empty-ish prompt): {base.prompt_tokens} tokens, ${base.cost:.6f}")


# ---------------------------------------------------------------------------
# Arm 4 — the pocket-summary A/B
# ---------------------------------------------------------------------------


def _render_pocket_block(widget_count: int, summary_only: bool) -> str:
    """Render ``<current-pocket>`` through the REAL layer, both flag states.

    Hand-rolling the block here would measure a blob the runtime never sends —
    and would miss the whole point, because PA-8a's
    ``_WIDGET_SUMMARY_MAX_CHARS`` bounds the widget dump BEFORE serialisation.
    A synthetic ``json.dumps`` of 300 widgets looks like ~40k chars; what the
    layer actually emits is ~3.2k. Only the layer knows that.
    """
    from unittest.mock import MagicMock

    import pocketpaw.config as cfg
    from pocketpaw.prompt.channel import ChannelInputs
    from pocketpaw.prompt.channel.request import ChannelCurrentPocketLayer
    from pocketpaw.prompt.layer import PromptContext

    pocket_context = {
        "id": "pk-123",
        "name": "Launch Tracker",
        "widgets": [
            {
                "id": f"w-{i:03d}",
                "name": f"Widget {i}",
                "type": "chart",
                "title": f"Some widget title {i}",
                "props": {"source": "api", "refresh": 30},
            }
            for i in range(widget_count)
        ],
    }
    ctx = PromptContext(
        instance=None,
        agent_id="",
        message="",
        instructions="",
        knowledge_context="",
        system_message_override=None,
        channel_inputs=ChannelInputs(metadata={"pocket_context": pocket_context}),
    )
    settings = MagicMock()
    settings.prompt_pocket_summary_only = summary_only
    original = cfg.get_settings
    cfg.get_settings = lambda *a, **k: settings
    try:
        import asyncio

        return asyncio.run(ChannelCurrentPocketLayer().render(ctx)).text
    finally:
        cfg.get_settings = original


def arm_summary_ab(client: httpx.Client, key: str, ledger: Ledger) -> None:
    """A/B ``prompt_pocket_summary_only`` through the real layer."""
    print("\n=== ARM: summary-ab — prompt_pocket_summary_only, via the real layer ===")

    # Char counts are free and exact, so take them across a range of pocket
    # sizes. The interesting result is that the OFF arm PLATEAUS: the block
    # stops growing with the pocket, because the widget summary is bounded
    # before it is serialised.
    print(f"\n{'widgets':>8} {'OFF chars':>10} {'ON chars':>9} {'saved':>8} {'saved %':>8}")
    for n in (12, 50, 300, 1000):
        off = _render_pocket_block(n, False)
        on = _render_pocket_block(n, True)
        pct = 100 * (1 - len(on) / len(off)) if off else 0.0
        print(f"{n:>8} {len(off):>10,} {len(on):>9,} {len(off) - len(on):>8,} {pct:>7.1f}%")

    # Then price the 300-widget case in real tokens.
    off = _render_pocket_block(300, False)
    on = _render_pocket_block(300, True)
    base = _measure_tokens(client, key, ledger, "x", "ab-baseline")
    a = _measure_tokens(client, key, ledger, off, "ab-full")
    b = _measure_tokens(client, key, ledger, on, "ab-summary-only")

    a_tok = max(0, a.prompt_tokens - base.prompt_tokens)
    b_tok = max(0, b.prompt_tokens - base.prompt_tokens)
    a_cost = max(0.0, a.cost - base.cost)
    b_cost = max(0.0, b.cost - base.cost)

    print(f"\n{'arm':>26} {'chars':>9} {'tokens':>8} {'$/turn':>10}")
    print(f"{'summary_only=False (today)':>26} {len(off):>9,} {a_tok:>8,} {a_cost:>10.6f}")
    print(f"{'summary_only=True':>26} {len(on):>9,} {b_tok:>8,} {b_cost:>10.6f}")
    if a_tok:
        print(f"\nflag saves {a_tok - b_tok:,} tokens/turn ({100 * (1 - b_tok / a_tok):.1f}%)")
    # The block VARIES per pocket and per edit, so it lands after the cache
    # breakpoint and is re-paid every turn — this saving is never amortised by
    # caching, unlike the specialist prefix measured in --arm warm.
    print("(per-turn: this block varies, so it never sits inside a cached prefix)")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    global MODEL
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--arm",
        default="all",
        choices=("all", "threshold", "warm", "caps", "summary-ab"),
    )
    parser.add_argument("--turns", type=int, default=6, help="warm turns (min 6 per PA-9)")
    parser.add_argument(
        "--prefix-chars",
        type=int,
        default=0,
        help=(
            "truncate the warm-arm prefix to N chars (0 = the whole 67.5k "
            "specialist prompt). Use ~16000 on a key whose per-request cap "
            "rejects the full prompt; it is still above the measured floor."
        ),
    )
    parser.add_argument(
        "--model",
        default=MODEL,
        help="OpenRouter model id. Lower-floor models need a smaller request to cache.",
    )
    args = parser.parse_args()
    MODEL = args.model

    key = _api_key()
    if not key:
        # Skip cleanly: this file must never fail a suite run just because the
        # machine has no key configured.
        print(
            "SKIP: no OpenRouter key configured "
            "(set POCKETPAW_OPENROUTER_API_KEY to run this eval)."
        )
        return 0

    run_id = f"pa9-{int(time.time())}"
    ledger = Ledger()
    print(f"run_id={run_id}  model={MODEL}")

    rc = 0
    try:
        with httpx.Client() as client:
            if args.arm in ("all", "threshold"):
                arm_threshold(client, key, ledger, run_id)
            if args.arm in ("all", "warm"):
                arm_warm(client, key, ledger, run_id, max(6, args.turns), args.prefix_chars)
            if args.arm in ("all", "caps"):
                arm_caps(client, key, ledger)
            if args.arm in ("all", "summary-ab"):
                arm_summary_ab(client, key, ledger)
    except BudgetExhausted as exc:
        # Report the partial run rather than losing the arms that did land.
        print(f"\nSTOPPED: {exc}")
        rc = 1
    finally:
        print(f"\n=== SPEND: ${ledger.total_cost:.6f} over {len(ledger.calls)} calls ===")

    return rc


if __name__ == "__main__":
    sys.exit(main())
