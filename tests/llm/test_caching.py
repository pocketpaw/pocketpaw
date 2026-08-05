# tests/llm/test_caching.py
# Created 2026-06-26 (integration/model-catalog-v2, MCG-11): unit tests for the
# universal prompt-caching helper ``pocketpaw.llm.caching``.
#
# Covers ``build_cacheable`` (stable-prefix-first ordering, cache_control on the
# prefix ONLY, the 4-breakpoint cap, 5m vs 1h TTL markup, and the byte-stability
# contract that a changed variable suffix never disturbs the cached prefix) and
# ``report_savings`` across the three provider usage shapes (Anthropic
# cache_creation/cache_read, OpenAI cached_tokens incl. the nested
# prompt_tokens_details form, DeepSeek prompt_cache_hit_tokens), for both dict
# and attribute-style usage objects.

from __future__ import annotations

import pytest

from pocketpaw.llm.caching import (
    CACHE_MIN_TOKENS,
    MAX_CACHE_BREAKPOINTS,
    CacheSavings,
    build_cacheable,
    report_savings,
)

# ---------------------------------------------------------------------------
# build_cacheable
# ---------------------------------------------------------------------------


class TestBuildCacheable:
    def test_stable_prefix_comes_first_variable_last(self):
        """Block ordering is prefix-then-variable so the cached LCP is the
        whole stable prefix."""
        out = build_cacheable(["SCHEMA", "CATALOG", "RULES"], ["customer brief"])
        texts = [b["text"] for b in out]
        assert texts == ["SCHEMA", "CATALOG", "RULES", "customer brief"]

    def test_cache_control_on_prefix_only_not_variable(self):
        """The single default breakpoint lands on the LAST prefix block; the
        variable suffix never carries cache_control."""
        out = build_cacheable(["SCHEMA", "CATALOG", "RULES"], ["brief"])
        # Last prefix block (index 2) is marked.
        assert out[2]["cache_control"] == {"type": "ephemeral"}
        # Earlier prefix blocks are NOT marked (single breakpoint).
        assert "cache_control" not in out[0]
        assert "cache_control" not in out[1]
        # Variable suffix (index 3) is NOT marked.
        assert "cache_control" not in out[3]

    def test_no_variable_parts_marks_last_prefix_block(self):
        """``variable_parts`` is optional — a prefix-only call still marks the
        tail of the prefix."""
        out = build_cacheable(["A", "B"])
        assert len(out) == 2
        assert "cache_control" not in out[0]
        assert out[1]["cache_control"] == {"type": "ephemeral"}

    def test_5m_ttl_is_bare_ephemeral(self):
        out = build_cacheable(["stable"], ["var"], ttl="5m")
        assert out[0]["cache_control"] == {"type": "ephemeral"}
        assert "ttl" not in out[0]["cache_control"]

    def test_1h_ttl_carries_ttl_field(self):
        """1h TTL emits the extended-cache-ttl markup LiteLLM forwards to
        Anthropic."""
        out = build_cacheable(["stable"], ["var"], ttl="1h")
        assert out[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}

    def test_invalid_ttl_raises(self):
        with pytest.raises(ValueError, match="ttl must be one of"):
            build_cacheable(["x"], ttl="2h")

    def test_empty_prefix_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            build_cacheable([], ["var"])

    def test_breakpoint_cap_respected(self):
        """Requesting more than 4 breakpoints clamps to MAX_CACHE_BREAKPOINTS —
        the Anthropic hard limit."""
        prefix = [f"part{i}" for i in range(10)]
        out = build_cacheable(prefix, ["var"], breakpoints=8)
        n_marked = sum(1 for b in out if "cache_control" in b)
        assert n_marked == MAX_CACHE_BREAKPOINTS == 4

    def test_multiple_breakpoints_include_last_block(self):
        """With N breakpoints the LAST prefix block is always one of them
        (terminates the longest cacheable prefix)."""
        prefix = [f"part{i}" for i in range(8)]
        out = build_cacheable(prefix, ["var"], breakpoints=3)
        prefix_blocks = out[:-1]  # drop the variable suffix
        n_marked = sum(1 for b in prefix_blocks if "cache_control" in b)
        assert n_marked == 3
        assert "cache_control" in prefix_blocks[-1]  # last prefix block marked

    def test_breakpoints_clamped_to_prefix_block_count(self):
        """Can't place more breakpoints than there are prefix blocks."""
        out = build_cacheable(["only-one"], ["var"], breakpoints=4)
        n_marked = sum(1 for b in out if "cache_control" in b)
        assert n_marked == 1

    def test_breakpoints_never_land_on_variable_block(self):
        """Even at the 4-breakpoint cap, markers stay on prefix blocks; the
        variable suffix is always clean."""
        prefix = [f"p{i}" for i in range(6)]
        variables = ["v0", "v1"]
        out = build_cacheable(prefix, variables, breakpoints=4)
        var_blocks = out[len(prefix) :]
        assert all("cache_control" not in b for b in var_blocks)

    def test_dict_prefix_part_passthrough_with_marker(self):
        """A pre-shaped content-block dict is preserved (its non-cache keys)
        and the marker is added by us."""
        block = {"type": "text", "text": "RULES", "extra": "kept"}
        out = build_cacheable([block], ["var"])
        assert out[0]["text"] == "RULES"
        assert out[0]["extra"] == "kept"
        assert out[0]["cache_control"] == {"type": "ephemeral"}

    def test_caller_block_not_mutated(self):
        """build_cacheable must not mutate the caller's input dicts (shared
        module-level prompt constants would otherwise accrete cache_control)."""
        block = {"type": "text", "text": "RULES"}
        build_cacheable([block], ["var"])
        assert "cache_control" not in block  # original untouched

    def test_preexisting_cache_control_on_input_is_normalised(self):
        """A caller block that already carries cache_control doesn't get
        double-counted — placement is decided centrally."""
        # This block is NOT the last prefix block, so after normalisation it
        # should end up WITHOUT a marker (only the last block gets one at
        # breakpoints=1).
        tagged = {"type": "text", "text": "early", "cache_control": {"type": "ephemeral"}}
        out = build_cacheable([tagged, "late"], ["var"])
        assert "cache_control" not in out[0]
        assert out[1]["cache_control"] == {"type": "ephemeral"}

    # --- byte-stability contract ---

    def test_byte_stability_same_prefix_different_suffix(self):
        """THE core cache property: identical prefix + DIFFERENT variable
        suffix → byte-identical prefix blocks. Any drift here busts the cache
        for every downstream call."""
        prefix = ["SCHEMA", "CATALOG", "RULES"]
        out_a = build_cacheable(prefix, ["customer A brief"], ttl="1h")
        out_b = build_cacheable(prefix, ["a totally different customer B brief"], ttl="1h")
        # The prefix slice (everything but the trailing variable block) is equal.
        assert out_a[: len(prefix)] == out_b[: len(prefix)]
        # ... while the variable suffix legitimately differs.
        assert out_a[-1] != out_b[-1]

    def test_byte_stability_repeated_identical_calls(self):
        """Same inputs → equal (and independent) structures every time."""
        prefix = ["A", "B", "C"]
        out_1 = build_cacheable(prefix, ["v"])
        out_2 = build_cacheable(prefix, ["v"])
        assert out_1 == out_2
        # Independent objects — mutating one must not touch the other.
        out_1[0]["text"] = "MUTATED"
        assert out_2[0]["text"] == "A"

    def test_marker_objects_are_independent(self):
        """Each marked block gets its own cache_control dict (no shared
        reference that a downstream mutation could corrupt across blocks)."""
        out = build_cacheable(["a", "b", "c", "d"], ["v"], breakpoints=4)
        markers = [b["cache_control"] for b in out if "cache_control" in b]
        ids = {id(m) for m in markers}
        assert len(ids) == len(markers)  # all distinct objects


# ---------------------------------------------------------------------------
# report_savings
# ---------------------------------------------------------------------------


class _AttrUsage:
    """Attribute-style usage stand-in (mimics the Anthropic/OpenAI SDK usage
    objects, which expose fields as attributes, not dict keys)."""

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class TestReportSavingsAnthropic:
    def test_anthropic_dict_shape(self):
        usage = {
            "input_tokens": 100,  # uncached remainder
            "cache_read_input_tokens": 2000,
            "cache_creation_input_tokens": 500,
        }
        s = report_savings(usage)
        assert s.provider == "anthropic"
        assert s.cache_read_tokens == 2000
        assert s.cache_write_tokens == 500
        assert s.prompt_tokens == 2600  # 100 + 2000 + 500
        assert s.hit_rate == pytest.approx(2000 / 2600, abs=1e-4)
        assert s.est_tokens_saved == pytest.approx(2000 * 0.90)

    def test_anthropic_attr_shape(self):
        usage = _AttrUsage(
            input_tokens=0,
            cache_read_input_tokens=3000,
            cache_creation_input_tokens=0,
        )
        s = report_savings(usage)
        assert s.provider == "anthropic"
        assert s.cache_read_tokens == 3000
        assert s.prompt_tokens == 3000
        assert s.hit_rate == 1.0

    def test_anthropic_cost_saved_helper(self):
        usage = {"input_tokens": 0, "cache_read_input_tokens": 1000}
        s = report_savings(usage)
        # 1000 cached reads * 0.90 discount = 900 input-token-equivalents.
        # At $3/Mtok input → $0.0027 saved.
        assert s.est_cost_saved(3e-6) == pytest.approx(900 * 3e-6)


class TestReportSavingsOpenAI:
    def test_openai_top_level_cached_tokens(self):
        usage = {"prompt_tokens": 5000, "cached_tokens": 4096}
        s = report_savings(usage)
        assert s.provider == "openai"
        assert s.cache_read_tokens == 4096
        assert s.cache_write_tokens == 0
        assert s.prompt_tokens == 5000
        assert s.hit_rate == pytest.approx(4096 / 5000, abs=1e-4)

    def test_openai_nested_prompt_tokens_details(self):
        """OpenAI's real shape nests cached_tokens under
        prompt_tokens_details."""
        usage = {
            "prompt_tokens": 5000,
            "prompt_tokens_details": {"cached_tokens": 2048},
        }
        s = report_savings(usage)
        assert s.provider == "openai"
        assert s.cache_read_tokens == 2048
        assert s.prompt_tokens == 5000

    def test_openai_attr_shape_nested(self):
        usage = _AttrUsage(
            prompt_tokens=4000,
            prompt_tokens_details=_AttrUsage(cached_tokens=1024),
        )
        s = report_savings(usage)
        assert s.provider == "openai"
        assert s.cache_read_tokens == 1024


class TestReportSavingsDeepSeek:
    def test_deepseek_dict_shape_matches_live_probe(self):
        """The shape the live proxy probe confirmed: hit 2944 / miss 112,
        total 3056."""
        usage = {"prompt_cache_hit_tokens": 2944, "prompt_cache_miss_tokens": 112}
        s = report_savings(usage)
        assert s.provider == "deepseek"
        assert s.cache_read_tokens == 2944
        assert s.cache_write_tokens == 0
        assert s.prompt_tokens == 3056  # hit + miss
        assert s.hit_rate == pytest.approx(2944 / 3056, abs=1e-4)
        assert s.est_tokens_saved == pytest.approx(2944 * 0.90)

    def test_deepseek_attr_shape(self):
        usage = _AttrUsage(prompt_cache_hit_tokens=1000, prompt_cache_miss_tokens=0)
        s = report_savings(usage)
        assert s.provider == "deepseek"
        assert s.hit_rate == 1.0


class TestReportSavingsEdgeCases:
    def test_none_usage_is_all_zero(self):
        s = report_savings(None)
        assert s == CacheSavings(0, 0, 0, 0.0, 0.0, "none")

    def test_empty_dict_is_none_provider(self):
        s = report_savings({})
        assert s.provider == "none"
        assert s.cache_read_tokens == 0
        assert s.hit_rate == 0.0

    def test_unrecognised_keys_are_none_provider(self):
        s = report_savings({"total_tokens": 99, "completion_tokens": 10})
        assert s.provider == "none"

    def test_garbage_values_coerce_to_zero(self):
        usage = {"input_tokens": "oops", "cache_read_input_tokens": None}
        s = report_savings(usage)
        assert s.cache_read_tokens == 0
        assert s.prompt_tokens == 0

    def test_zero_prompt_tokens_no_div_by_zero(self):
        usage = {"prompt_cache_hit_tokens": 0, "prompt_cache_miss_tokens": 0}
        s = report_savings(usage)
        assert s.hit_rate == 0.0

    def test_deepseek_precedence_over_others(self):
        """If a payload somehow carried both DeepSeek and OpenAI keys, the
        DeepSeek shape (most specific) wins deterministically."""
        usage = {
            "prompt_cache_hit_tokens": 500,
            "prompt_cache_miss_tokens": 500,
            "cached_tokens": 999,
            "prompt_tokens": 9999,
        }
        s = report_savings(usage)
        assert s.provider == "deepseek"
        assert s.cache_read_tokens == 500


class TestConstants:
    def test_min_token_floors_present(self):
        assert CACHE_MIN_TOKENS["default"] == 1024
        assert CACHE_MIN_TOKENS["anthropic-haiku"] == 4096
        assert CACHE_MIN_TOKENS["anthropic-opus"] == 4096

    def test_deepseek_floor_is_the_measured_512_not_the_documented_1024(self):
        """DeepSeek cached a 595-token prefix (PA-9, 2026-08-03).

        Measured on deepseek-v4-flash through the LiteLLM gateway: a 595-token
        prompt reported 512 cached tokens on the warm turn, so the real floor is
        at or below 512 and reads arrive in 512-token blocks. The documented
        1024 was conservative in the direction that costs money — it would skip
        prompts between 512 and 1024 that do in fact cache.

        MUTATION: set ``deepseek`` back to 1024 in
        ``src/pocketpaw/llm/caching.py``; this fails.
        """
        assert CACHE_MIN_TOKENS["deepseek"] == 512
        # Below the generic default, which is the whole point of the row.
        assert CACHE_MIN_TOKENS["deepseek"] < CACHE_MIN_TOKENS["default"]

    def test_no_sonnet_floor_is_above_the_default(self):
        """Every shipping Sonnet caches from 1024 tokens (PA-9, 2026-08-03).

        This row read 2048 until PA-9, which is not the floor of any Sonnet that
        has ever shipped. Being too STRICT is the expensive direction: it skips
        marking prompts that would have cached, forfeiting the saving outright.
        (Being too permissive is free — a sub-floor marker is declined at no
        charge, measured the same day.)

        MUTATION: set ``anthropic-sonnet`` back to 2048 in
        ``src/pocketpaw/llm/caching.py``; this fails.
        """
        assert CACHE_MIN_TOKENS["anthropic-sonnet"] == 1024

    def test_opus_floors_are_not_monotonic_across_generations(self):
        """Opus cannot be one number, which is why the per-generation rows exist.

        The floor FALLS as the generation rises — 4096 on Opus 4.5/4.6 down to
        512 on Opus 5 — so any single "the Opus floor" value is wrong for most
        Opus models. The family key deliberately keeps the most conservative
        value for callers that cannot resolve a generation.

        MUTATION: delete the ``anthropic-opus-5`` row (or set it to 4096) in
        ``src/pocketpaw/llm/caching.py``; the ordering assertion fails.
        """
        assert CACHE_MIN_TOKENS["anthropic-opus-5"] == 512
        assert CACHE_MIN_TOKENS["anthropic-opus-4.8"] == 1024
        assert CACHE_MIN_TOKENS["anthropic-opus-4.5"] == 4096
        # Newer generation, LOWER floor — the property a single key destroys.
        assert CACHE_MIN_TOKENS["anthropic-opus-5"] < CACHE_MIN_TOKENS["anthropic-opus-4.5"]
        # The family key must never promise more than the strictest generation.
        assert CACHE_MIN_TOKENS["anthropic-opus"] == max(
            CACHE_MIN_TOKENS[k] for k in CACHE_MIN_TOKENS if k.startswith("anthropic-opus-")
        )

    def test_deep_agents_char_threshold_is_deliberately_below_the_haiku_floor(self):
        """The PA-9 headline, in executable form.

        ``_ANTHROPIC_CACHE_MIN_CHARS`` gates cache marking in CHARS while the
        floors are in TOKENS. Measured 2026-08-03 at 3.48 chars/token on our own
        prompt text, 4000 chars is ~1,149 tokens — a quarter of Haiku 4.5's
        4096-token floor, which sits near 14,250 chars.

        This is asserted rather than fixed BECAUSE the low value is correct. A
        sub-floor marker is declined free of charge (marked and unmarked calls
        billed identically), so an over-permissive gate costs nothing, while
        raising this to ~14,250 would stop marking on Opus 4.8 (floor 1024) and
        Opus 5 (floor 512) where 4k-14k prompts cache today — turning a harmless
        inaccuracy into a real regression. This test exists so that anyone
        "correcting" the threshold trips a failure and reads the reasoning first.

        MUTATION: set ``_ANTHROPIC_CACHE_MIN_CHARS = 20000`` in
        ``src/pocketpaw/agents/deep_agents.py``; this fails.
        """
        from pocketpaw.agents.deep_agents import _ANTHROPIC_CACHE_MIN_CHARS

        measured_chars_per_token = 3.48
        threshold_tokens = _ANTHROPIC_CACHE_MIN_CHARS / measured_chars_per_token

        assert threshold_tokens < CACHE_MIN_TOKENS["anthropic-haiku"], (
            "The chars threshold now clears the Haiku floor. That is not a fix — "
            "it stops marking on Opus 4.8/5, whose floors are 1024/512. See the "
            "constant's comment in deep_agents.py before changing this."
        )
        # ...but it must still clear the lowest floor we ship against, or the
        # gate would be excluding prompts that cache on every modern Opus.
        assert threshold_tokens > CACHE_MIN_TOKENS["anthropic-opus-5"]
