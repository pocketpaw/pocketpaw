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

TWO ROUTES, and they answer different questions — ``--route openrouter``
(default) or ``--route litellm``.

  openrouter  The only route that reaches ANTHROPIC models, so the only one that
      can answer anything about ``cache_control`` or the per-model floors.
      Passed ``{"usage": {"include": true}}`` so each response carries
      ``usage.cost`` and ``usage.prompt_tokens_details.cached_tokens``.

  litellm     The gateway at ``settings.litellm_api_base``. It serves DeepSeek
      only, and DeepSeek caches AUTOMATICALLY — ``cache_control`` is ignored
      entirely — so this route CANNOT speak to the Anthropic floor questions,
      and ``--arm threshold`` prints a warning saying so. What it is good for is
      the warm-turn harness, which needs no marker. Cost arrives in the
      ``x-litellm-response-cost`` header rather than the usage body.

Both usage shapes are read by the SAME ``report_savings`` (OpenAI-shaped
``cached_tokens`` on one, DeepSeek's ``prompt_cache_hit_tokens`` on the other),
which is why this script carries no savings reporter of its own.

A ``headroom-compression`` pre-call guardrail on the gateway 502s on large
prompts — 40,000 chars passes, 67,563 fails (measured 2026-08-03) — so the
litellm route cannot take the whole specialist prompt in one request. Use
``--prefix-chars 40000``.

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

# Two routes, because neither answers every question alone.
#
#   openrouter — the only route that reaches ANTHROPIC models, so it is the only
#       one that can answer the cache_control / per-model-floor questions. Marker
#       placement is meaningful here.
#   litellm — the captain's gateway (``settings.litellm_api_base``). Serves
#       DeepSeek only, which caches AUTOMATICALLY: ``cache_control`` markers are
#       ignored, and the usage shape is
#       ``prompt_cache_hit_tokens``/``prompt_cache_miss_tokens``. It therefore
#       cannot speak to Anthropic floors at all, but it CAN run the warm-turn
#       harness, and it is not metered against a personal balance.
#
# A ``headroom-compression`` pre-call guardrail on the gateway 502s on large
# prompts: 40,000 chars succeeds, 67,563 fails (measured 2026-08-03). Keep the
# litellm-route prefix under that ceiling.
ROUTES = ("openrouter", "litellm")
ROUTE = "openrouter"

# Haiku 4.5 carries the HIGHEST documented Anthropic cache floor (4096 tokens),
# which makes it the strictest test of a chars-based threshold and also the
# cheapest model to run the test on. A threshold that is safe here is safe on
# every other Anthropic model.
MODEL = "anthropic/claude-haiku-4.5"

DEFAULT_MODEL_BY_ROUTE = {
    "openrouter": "anthropic/claude-haiku-4.5",
    "litellm": "deepseek/deepseek-v4-flash",
}

# Per-model cache floors, in TOKENS, for the models this harness can target.
# Haiku 4.5 has the highest floor and so needs the largest request to
# demonstrate a hit; a key with a small per-request cap may only be able to
# afford the warm arm on a lower-floor model.
MODEL_FLOOR_TOKENS = {
    "anthropic/claude-haiku-4.5": 4096,
    "anthropic/claude-sonnet-4.5": 1024,
    "anthropic/claude-opus-4.5": 4096,
    "deepseek/deepseek-v4-flash": 1024,
    "deepseek/deepseek-v4-pro": 1024,
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
    """Read the active route's key from settings at run time. Never logged."""
    from pocketpaw.config import get_settings

    settings = get_settings()
    if ROUTE == "litellm":
        return settings.litellm_api_key or None
    return settings.openrouter_api_key or None


def _endpoint() -> str:
    """The chat-completions URL for the active route."""
    if ROUTE == "litellm":
        from pocketpaw.config import get_settings

        base = str(get_settings().litellm_api_base).rstrip("/")
        return f"{base}/v1/chat/completions"
    return OPENROUTER_URL


def _post(client: httpx.Client, key: str, system: Any, user: str, label: str) -> Call:
    """One completion. ``system`` is a str or a content-block list."""
    body: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if ROUTE == "openrouter":
        # Without this OpenRouter omits cost and the cached-token breakdown,
        # which is the entire measurement. LiteLLM rejects the field.
        body["usage"] = {"include": True}
    resp = client.post(
        _endpoint(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=body,
        timeout=180.0,
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
    if resp.status_code == 502 and "headroom-compression" in resp.text:
        # The gateway's pre-call guardrail, not the model and not this script.
        # It passes at 40,000 chars and fails at 67,563 (measured 2026-08-03),
        # so the fix is a smaller prefix rather than a retry.
        raise BudgetExhausted(
            f"LiteLLM gateway 502 on the headroom-compression guardrail for "
            f"{label} (system prompt ~{len(str(system)):,} chars). The guardrail "
            "rejects large prompts before the model sees them; 40,000 chars is "
            "known good. Re-run with a smaller --prefix-chars."
        )
    resp.raise_for_status()
    usage = resp.json().get("usage", {}) or {}

    # Reuse the shipped reporter rather than re-deriving the hit rate here, and
    # note it needs no branch per route: OpenRouter returns the OpenAI shape
    # (prompt_tokens + prompt_tokens_details.cached_tokens) and the gateway
    # returns DeepSeek's (prompt_cache_hit_tokens / prompt_cache_miss_tokens).
    # report_savings already discriminates both, which is the reason this script
    # does not carry a second savings reporter.
    savings = report_savings(usage)

    # Cost: OpenRouter puts it in the usage body; LiteLLM returns it in a
    # response header instead.
    cost = float(usage.get("cost") or 0.0)
    if not cost:
        cost = float(resp.headers.get("x-litellm-response-cost") or 0.0)

    return Call(
        label=label,
        prompt_tokens=savings.prompt_tokens or int(usage.get("prompt_tokens", 0)),
        cached_tokens=savings.cache_read_tokens,
        cost=cost,
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
    floor_tokens = MODEL_FLOOR_TOKENS.get(MODEL, CACHE_MIN_TOKENS["default"])
    print(f"=> {MODEL}'s documented {floor_tokens}-token floor is ~{floor_tokens * cpt:,.0f} chars")

    caching = [r for r in rows if r[4]]
    if caching:
        smallest = caching[0]
        print(f"=> smallest prefix that cached: {smallest[0]:,} chars ({smallest[1]:,} tokens)")
        if smallest[1] < floor_tokens:
            print(
                f"   NOTE: that is BELOW the documented {floor_tokens}-token floor — "
                "the documented figure is conservative for this model."
            )
    else:
        print("=> NOTHING in the sweep cached")

    # The write-premium question: does marking a SUB-FLOOR prompt cost extra?
    #
    # THE TWO ARMS MUST USE DIFFERENT PREFIXES. An earlier version reused one
    # prefix for both, which is only sound when nothing caches at that size: on
    # a provider that DOES cache there, the second call reads the cache the
    # first one just wrote and the comparison reports a ~13x "premium" that is
    # really just a warm turn. Distinct salts keep both arms genuinely cold.
    print("\n--- write-premium check at 4000 chars (the live threshold) ---")
    marked = ledger.add(
        _post(client, key, build_cacheable([_prefix(4_000, run_id + "-wpA")]), "1", "wp-marked")
    )
    unmarked = ledger.add(_post(client, key, _prefix(4_000, run_id + "-wpB"), "1", "wp-unmarked"))
    print(
        f"  marked:   prompt={marked.prompt_tokens} cached={marked.cached_tokens} "
        f"cost=${marked.cost:.6f}"
    )
    print(
        f"  unmarked: prompt={unmarked.prompt_tokens} cached={unmarked.cached_tokens} "
        f"cost=${unmarked.cost:.6f}"
    )

    if marked.cached_tokens or unmarked.cached_tokens:
        # Either arm reading from cache invalidates the comparison outright.
        print(
            "  => INVALID: one arm read from cache, so this is a warm-vs-cold\n"
            "     difference, not a marker difference. The premium question is\n"
            "     only answerable at a size where nothing caches."
        )
    elif unmarked.cost > 0:
        ratio = marked.cost / unmarked.cost
        print(f"  marked/unmarked cost ratio: {ratio:.3f}")
        # The conclusion depends on which side of the model's floor 4000 chars
        # lands, and the two readings are NOT interchangeable. Below the floor
        # this answers "does a marker that cannot cache still cost anything";
        # above it, on a provider that ignores cache_control, it only says the
        # marker is a no-op there. Printing one wording for both would let a
        # DeepSeek run be quoted as evidence about Anthropic floors.
        floor = MODEL_FLOOR_TOKENS.get(MODEL, CACHE_MIN_TOKENS["default"])
        below = marked.prompt_tokens < floor
        where = "BELOW" if below else "ABOVE"
        print(f"  ({marked.prompt_tokens} tokens is {where} {MODEL}'s {floor}-token floor)")
        if ratio > 1.10:
            print("  => marking costs extra here (the marker is not free)")
        elif below:
            print("  => NO write premium below the floor (marker is inert, not costly)")
        else:
            print(
                "  => marker is a no-op at a CACHEABLE size — expected on a\n"
                "     provider that caches automatically and ignores cache_control.\n"
                "     This says nothing about sub-floor behaviour; re-run on a\n"
                "     model whose floor is above this size for that."
            )


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
    global MODEL, ROUTE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--route",
        default="openrouter",
        choices=ROUTES,
        help=(
            "openrouter reaches Anthropic models (the only route that can answer "
            "the cache_control / floor questions); litellm is the gateway, which "
            "serves DeepSeek only and caches automatically."
        ),
    )
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
        default=None,
        help=(
            "Model id. Defaults per route (Haiku 4.5 on openrouter, "
            "deepseek-v4-flash on litellm). Lower-floor models need a smaller "
            "request to demonstrate a cache hit."
        ),
    )
    args = parser.parse_args()
    ROUTE = args.route
    MODEL = args.model or DEFAULT_MODEL_BY_ROUTE[ROUTE]

    key = _api_key()
    if not key:
        # Skip cleanly: this file must never fail a suite run just because the
        # machine has no key configured.
        env = "POCKETPAW_LITELLM_API_KEY" if ROUTE == "litellm" else "POCKETPAW_OPENROUTER_API_KEY"
        print(f"SKIP: no {ROUTE} key configured (set {env} to run this eval).")
        return 0

    if ROUTE == "litellm" and args.arm in ("all", "threshold"):
        # Say this rather than produce a table that looks like an answer. The
        # gateway serves DeepSeek, which caches automatically at a flat 1024
        # tokens and ignores cache_control entirely, so a threshold sweep here
        # measures DeepSeek's floor and says nothing about the Anthropic
        # per-model floors the threshold question is actually about.
        print(
            "NOTE: --arm threshold is meaningful only on --route openrouter. "
            "The gateway serves DeepSeek, which caches automatically and ignores "
            "cache_control, so this sweep cannot speak to the Anthropic floors."
        )

    run_id = f"pa9-{int(time.time())}"
    ledger = Ledger()
    print(f"run_id={run_id}  route={ROUTE}  model={MODEL}")

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
