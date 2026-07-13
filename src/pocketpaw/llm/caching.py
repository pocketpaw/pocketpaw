# src/pocketpaw/llm/caching.py — universal LLM prompt-caching helper (MCG-11).
#
# Created 2026-06-26 (integration/model-catalog-v2, MCG-11): generalizes the
# ad-hoc Anthropic cache-control monkey-patch in
# ``src/pocketpaw/agents/deep_agents.py`` into a reusable, provider-agnostic,
# OSS-importable module. Two pure functions, no I/O, no ``ee/`` imports:
#
#   * ``build_cacheable(prefix_parts, variable_parts, *, ttl, breakpoints)``
#       Assembles a content-block list (the shape LiteLLM/Anthropic accept for
#       the ``system`` parameter or a message ``content``) with
#       ``cache_control`` markers placed ONLY on the STABLE prefix. The stable
#       content goes first, the variable suffix last, so the cached
#       longest-common-prefix is maximised. Marker placement respects
#       Anthropic's 4-breakpoint hard cap. For automatic-caching providers
#       (OpenAI/DeepSeek cache >=1024 tokens with no markup) the structure is
#       still correct — the markers are simply ignored.
#
#   * ``report_savings(usage)``
#       Reads ``cache_creation_input_tokens`` / ``cache_read_input_tokens``
#       (Anthropic), ``cached_tokens`` / ``prompt_tokens_details.cached_tokens``
#       (OpenAI), and ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``
#       (DeepSeek) off a response ``usage`` object (dict or attr-style) and
#       returns a small ``CacheSavings`` struct: cached-read tokens, cache-write
#       tokens, total prompt tokens, hit-rate, and an estimated cost saved (a
#       cached read costs ~10% of a fresh input token, so each cached-read token
#       saves ~90% of one input token).
#
# Guardrails (see ``CACHE_MIN_TOKENS`` / docstrings):
#   * The prefix MUST be byte-stable. ANY per-call drift (a timestamp, a uuid, a
#     trailing-whitespace difference) busts the cache for every downstream call.
#     ``build_cacheable`` therefore never mutates the prefix text and keeps every
#     variable part strictly after the last cache breakpoint.
#   * Min-token thresholds: 1024 tokens (most models), 2048 (Anthropic Sonnet),
#     4096 (Anthropic Haiku / Opus 4.5+). Below the floor the provider declines
#     to cache and the write is wasted — callers should gate on prefix size.
#   * 1h TTL costs 2x a 5m write on the create call; only worth it when the same
#     prefix recurs within the hour (the site-gen / pocket-gen case). 5m is the
#     default.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Constants / guardrails
# ---------------------------------------------------------------------------

# Anthropic allows at most 4 cache breakpoints (``cache_control`` blocks) per
# request. Markers beyond this are rejected by the API, so ``build_cacheable``
# clamps to it.
MAX_CACHE_BREAKPOINTS = 4

# Per-model minimum cacheable prefix sizes (tokens). Below the floor the
# provider silently declines to cache and the write is wasted. These are the
# documented Anthropic floors; the 1024 default also covers OpenAI/DeepSeek
# automatic caching. Keyed by a coarse model family the caller can look up.
CACHE_MIN_TOKENS: dict[str, int] = {
    "default": 1024,
    "anthropic-sonnet": 2048,
    "anthropic-haiku": 4096,
    "anthropic-opus": 4096,  # Opus 4.5+ raised the floor to 4096
    "openai": 1024,
    "deepseek": 1024,
}

# Valid Anthropic ``cache_control`` TTL strings. "5m" is the free default;
# "1h" extends the window at 2x the write cost.
_VALID_TTLS = ("5m", "1h")

# The ephemeral cache_control marker LiteLLM translates per provider. For a 5m
# TTL Anthropic accepts the bare ``{"type": "ephemeral"}`` (no ttl key); a 1h
# window needs the explicit ``ttl`` field (extended-cache-ttl beta).
_EPHEMERAL_5M: dict[str, str] = {"type": "ephemeral"}


def _cache_control(ttl: str) -> dict[str, str]:
    """Return the ``cache_control`` marker dict for a TTL.

    5m → the bare ``{"type": "ephemeral"}`` (the universally-accepted form).
    1h → ``{"type": "ephemeral", "ttl": "1h"}`` (extended-cache-ttl). LiteLLM
    passes the ttl through to Anthropic and ignores it for providers that cache
    automatically, so the structure stays correct everywhere.
    """
    if ttl == "1h":
        return {"type": "ephemeral", "ttl": "1h"}
    return dict(_EPHEMERAL_5M)


# ---------------------------------------------------------------------------
# build_cacheable
# ---------------------------------------------------------------------------


def _as_text_block(part: Any) -> dict[str, Any]:
    """Normalise one input part into a ``{"type": "text", "text": ...}`` block.

    A plain ``str`` is wrapped. A dict is taken as a pre-shaped content block
    and passed through (a SHALLOW COPY so we never mutate the caller's object —
    important for byte-stability: the same input must always yield an equal,
    independent structure). Any pre-existing ``cache_control`` on a passed-in
    block is stripped here; placement is decided centrally by ``build_cacheable``
    so the breakpoint count and position are deterministic.
    """
    if isinstance(part, str):
        return {"type": "text", "text": part}
    if isinstance(part, dict):
        block = dict(part)
        block.pop("cache_control", None)
        return block
    # Anything else (numbers, etc.) — stringify so the structure stays valid.
    return {"type": "text", "text": str(part)}


def build_cacheable(
    prefix_parts: list[Any],
    variable_parts: list[Any] | None = None,
    *,
    ttl: str = "5m",
    breakpoints: int = 1,
) -> list[dict[str, Any]]:
    """Build a cacheable content-block list: STABLE prefix first (marked with
    ``cache_control``), VARIABLE suffix last (never marked).

    Args:
        prefix_parts: The byte-stable prefix — strings and/or pre-shaped content
            blocks. This is what gets cached. It MUST be identical across calls
            for a cache hit; any drift (timestamp, uuid, whitespace) busts it.
        variable_parts: The per-call suffix (e.g. the customer brief, the pocket
            id). Always placed AFTER the last cache breakpoint, never marked, so
            it can vary freely without disturbing the cached prefix.
        ttl: ``"5m"`` (default, free) or ``"1h"`` (2x write cost; worth it only
            when the prefix recurs within the hour).
        breakpoints: How many ``cache_control`` markers to place on the prefix
            (1–4). With N>1 the markers are spread across the prefix blocks so a
            partially-changed prefix can still hit the earlier breakpoints.
            Clamped to ``[1, MAX_CACHE_BREAKPOINTS]`` and to the number of prefix
            blocks. Most callers want 1 (a single stable prefix → one marker on
            its last block).

    Returns:
        A list of content blocks. Block ordering is deterministic and the prefix
        blocks are byte-identical for identical ``prefix_parts`` regardless of
        ``variable_parts`` — this is the property the cache relies on.

    Raises:
        ValueError: on an unknown ``ttl`` or an empty ``prefix_parts``.

    Byte-stability contract: ``build_cacheable(P, V1) == build_cacheable(P, V2)``
    for the prefix slice ``[: len(P)]`` whenever the prefix ``P`` is the same.
    The test-suite asserts this directly.
    """
    if ttl not in _VALID_TTLS:
        raise ValueError(f"ttl must be one of {_VALID_TTLS}, got {ttl!r}")
    if not prefix_parts:
        raise ValueError("prefix_parts must be non-empty — there is nothing to cache")

    prefix_blocks = [_as_text_block(p) for p in prefix_parts]
    variable_blocks = [_as_text_block(v) for v in (variable_parts or [])]

    # Clamp the breakpoint count: at least 1, at most the Anthropic cap, and
    # never more than the number of prefix blocks (you can't mark a block that
    # isn't there).
    n_breakpoints = max(1, min(breakpoints, MAX_CACHE_BREAKPOINTS, len(prefix_blocks)))

    marker = _cache_control(ttl)

    # Choose which prefix-block indices carry a breakpoint. The LAST block
    # always gets one (it terminates the longest cacheable prefix). For N>1 we
    # also mark evenly-spaced earlier blocks so a prefix that diverges late can
    # still reuse the earlier cached segments. Indices are computed from block
    # COUNT only (not content), so placement is deterministic and byte-stable.
    last = len(prefix_blocks) - 1
    marked: set[int] = {last}
    if n_breakpoints > 1:
        # Spread the remaining (n_breakpoints - 1) markers across [0, last).
        step = len(prefix_blocks) / n_breakpoints
        for k in range(1, n_breakpoints):
            idx = min(int(round(k * step)) - 1, last - 1)
            if idx >= 0:
                marked.add(idx)

    for i, block in enumerate(prefix_blocks):
        if i in marked:
            # Fresh dict each time so two calls never share a marker object.
            block["cache_control"] = dict(marker)

    # Variable blocks are appended untouched — no cache_control ever lands here.
    return prefix_blocks + variable_blocks


# ---------------------------------------------------------------------------
# report_savings
# ---------------------------------------------------------------------------

# A cached input-token READ is billed at ~10% of a fresh input token across
# Anthropic / OpenAI / DeepSeek. So each cached-read token SAVES ~90% of one
# input token. A cache WRITE (first call) costs ~125% (Anthropic 5m) — captured
# separately so callers can see the amortisation, not folded into "saved".
_CACHE_READ_DISCOUNT = 0.90  # fraction of an input token saved per cached read


@dataclass(frozen=True)
class CacheSavings:
    """Outcome of reading a response ``usage`` object for cache effectiveness.

    All token counts are ints; ``hit_rate`` and ``est_tokens_saved`` are floats.
    ``est_tokens_saved`` is in *input-token-equivalents* (cached reads * the
    ~90% discount) — multiply by the model's input $/token to get dollars.
    Pure data; no I/O.
    """

    cache_read_tokens: int  # tokens served from cache (the win)
    cache_write_tokens: int  # tokens written to cache this call (the create cost)
    prompt_tokens: int  # total prompt/input tokens for the call
    hit_rate: float  # cache_read / prompt_tokens, in [0, 1]
    est_tokens_saved: float  # input-token-equivalents saved by the cached reads
    provider: str  # which usage shape matched: anthropic|openai|deepseek|none

    def est_cost_saved(self, input_cost_per_token: float) -> float:
        """Dollars saved, given the model's input $/token. Convenience over
        ``est_tokens_saved`` so metering can attach a real figure."""
        return self.est_tokens_saved * input_cost_per_token


def _get(usage: Any, key: str) -> Any:
    """Read ``key`` off a usage object that may be a dict OR an attr-style
    object (Anthropic SDK ``Usage``, OpenAI ``CompletionUsage``). Returns None
    when absent."""
    if usage is None:
        return None
    if isinstance(usage, dict):
        return usage.get(key)
    return getattr(usage, key, None)


def _int(value: Any) -> int:
    """Coerce a usage field to a non-negative int; None/garbage → 0."""
    if value is None:
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def report_savings(usage: Any) -> CacheSavings:
    """Read cache effectiveness off a response ``usage`` object.

    Handles all three provider shapes (dict or attr-style):

      * Anthropic — ``cache_read_input_tokens`` (the win) +
        ``cache_creation_input_tokens`` (the write); ``input_tokens`` is the
        UNCACHED remainder, so total prompt = input + read + write.
      * OpenAI — ``prompt_tokens`` total with ``cached_tokens`` (a subset)
        either top-level or under ``prompt_tokens_details``. No separate write
        line (writes are free / implicit).
      * DeepSeek — ``prompt_cache_hit_tokens`` + ``prompt_cache_miss_tokens``;
        their sum is the prompt total. (This is the shape the live proxy probe
        confirmed: hit 2944 / 3056.)

    Unknown / empty usage → an all-zero ``CacheSavings`` with provider="none".
    Pure + deterministic.
    """
    # --- DeepSeek: most specific keys, check first ---
    ds_hit = _get(usage, "prompt_cache_hit_tokens")
    ds_miss = _get(usage, "prompt_cache_miss_tokens")
    if ds_hit is not None or ds_miss is not None:
        read = _int(ds_hit)
        miss = _int(ds_miss)
        total = read + miss
        return _build(read, 0, total, "deepseek")

    # --- Anthropic: cache_creation / cache_read present ---
    a_read = _get(usage, "cache_read_input_tokens")
    a_write = _get(usage, "cache_creation_input_tokens")
    if a_read is not None or a_write is not None:
        read = _int(a_read)
        write = _int(a_write)
        # Anthropic's input_tokens is the UNCACHED portion; total prompt is the
        # sum of uncached + cached-read + cache-write.
        uncached = _int(_get(usage, "input_tokens"))
        total = uncached + read + write
        return _build(read, write, total, "anthropic")

    # --- OpenAI: cached_tokens (subset of prompt_tokens) ---
    o_cached = _get(usage, "cached_tokens")
    if o_cached is None:
        details = _get(usage, "prompt_tokens_details")
        o_cached = _get(details, "cached_tokens") if details is not None else None
    o_prompt = _get(usage, "prompt_tokens")
    if o_cached is not None or o_prompt is not None:
        read = _int(o_cached)
        total = _int(o_prompt)
        # cached_tokens is a SUBSET of prompt_tokens; no extra write line.
        return _build(read, 0, total, "openai")

    return _build(0, 0, 0, "none")


def _build(read: int, write: int, total: int, provider: str) -> CacheSavings:
    """Assemble a CacheSavings, computing hit-rate + estimated saved tokens.

    hit_rate divides cached reads by the total prompt tokens (0 when there are
    none). est_tokens_saved discounts the cached reads by ~90% (a cached read
    costs ~10% of a fresh input token, so it saves ~90%).
    """
    hit_rate = (read / total) if total > 0 else 0.0
    est_saved = read * _CACHE_READ_DISCOUNT
    return CacheSavings(
        cache_read_tokens=read,
        cache_write_tokens=write,
        prompt_tokens=total,
        hit_rate=round(hit_rate, 4),
        est_tokens_saved=round(est_saved, 2),
        provider=provider,
    )


__all__ = [
    "MAX_CACHE_BREAKPOINTS",
    "CACHE_MIN_TOKENS",
    "CacheSavings",
    "build_cacheable",
    "report_savings",
]
