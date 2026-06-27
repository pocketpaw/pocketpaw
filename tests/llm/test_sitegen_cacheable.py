# tests/llm/test_sitegen_cacheable.py
# Created 2026-06-26 (integration/model-catalog-v2, MCG-11): proves the site/
# pocket-generation system prompt is now structured as a BYTE-STABLE cached
# prefix ⊕ a varying suffix via ``ripple.build_specialist_cacheable`` (the
# concrete site-gen application of the universal ``build_cacheable`` helper).
#
# The win this guards: the generator's huge near-identical prefix (widget
# catalog + canonical shapes + workflow + design rules) is cached, so the Nth
# site pays ~10% on it. These tests assert (a) the cache_control breakpoint sits
# on the STABLE prefix only, (b) the per-brief variable suffix is a separate,
# UNMARKED trailing block, and (c) two different briefs yield a byte-identical
# prefix block — the property the provider cache keys on.

from __future__ import annotations

from pocketpaw.ripple import POCKET_SPECIALIST_PROMPT, build_specialist_cacheable


class TestSiteGenCacheablePrefix:
    def test_prefix_is_the_byte_stable_specialist_prompt(self):
        """The cached prefix block carries the full POCKET_SPECIALIST_PROMPT —
        the stable widget-catalog + design-rules text the generator reuses."""
        blocks = build_specialist_cacheable("brief: a dentist landing page")
        assert blocks[0]["text"] == POCKET_SPECIALIST_PROMPT
        assert blocks[0]["type"] == "text"

    def test_cache_control_on_prefix_only(self):
        """The stable prefix block is marked; the variable suffix is not."""
        blocks = build_specialist_cacheable("brief: a bakery site")
        assert blocks[0]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
        # The trailing variable suffix block is never marked.
        assert "cache_control" not in blocks[-1]

    def test_default_ttl_is_one_hour(self):
        """Site-gen defaults to a 1h TTL — the generator reuses this prefix
        across back-to-back briefs, so the 2x write cost amortises."""
        blocks = build_specialist_cacheable("brief")
        assert blocks[0]["cache_control"]["ttl"] == "1h"

    def test_variable_suffix_is_separate_trailing_block(self):
        """The per-brief content is appended as its own block AFTER the prefix
        — not concatenated into the cached text."""
        blocks = build_specialist_cacheable("brief: a SaaS pricing page")
        assert len(blocks) == 2
        assert blocks[1]["text"] == "brief: a SaaS pricing page"

    def test_empty_suffix_yields_prefix_only(self):
        """No variable suffix → just the marked prefix block (still cacheable)."""
        blocks = build_specialist_cacheable(None)
        assert len(blocks) == 1
        assert blocks[0]["text"] == POCKET_SPECIALIST_PROMPT
        assert blocks[0]["cache_control"]["type"] == "ephemeral"

    def test_list_suffix_parts_each_become_a_block(self):
        """Callers can pass multiple variable parts (hints + current-pocket
        block); each lands as its own unmarked trailing block."""
        blocks = build_specialist_cacheable(
            ["CALLER METADATA: name=Acme", "<current-pocket>id: p_123</current-pocket>"]
        )
        assert len(blocks) == 3
        assert blocks[1]["text"].startswith("CALLER METADATA")
        assert blocks[2]["text"].startswith("<current-pocket>")
        assert all("cache_control" not in b for b in blocks[1:])

    # --- the load-bearing property: byte-stable prefix across briefs ---

    def test_two_different_briefs_share_byte_identical_prefix(self):
        """THE margin property: customer A and customer B get DIFFERENT
        suffixes but a BYTE-IDENTICAL cached prefix block — so the provider
        cache hits on the 2nd (and Nth) site."""
        a = build_specialist_cacheable("brief: a dentist in Austin, blue theme")
        b = build_specialist_cacheable("brief: a law firm in NYC, dark theme, 4 services")
        # The cached prefix block (text + marker) is identical.
        assert a[0] == b[0]
        # ... while the variable suffix legitimately differs.
        assert a[-1] != b[-1]

    def test_prefix_stable_regardless_of_suffix_count(self):
        """Whether the caller passes one suffix part or several, the prefix
        block is unchanged."""
        one = build_specialist_cacheable("just a brief")
        many = build_specialist_cacheable(["a", "b", "c"])
        assert one[0] == many[0]

    def test_prefix_blocks_are_independent_objects(self):
        """Two builds don't share the prefix dict — a downstream mutation of
        one call's structure can't corrupt another's (or the module-level
        POCKET_SPECIALIST_PROMPT constant)."""
        a = build_specialist_cacheable("x")
        b = build_specialist_cacheable("y")
        a[0]["text"] = "MUTATED"
        assert b[0]["text"] == POCKET_SPECIALIST_PROMPT
        # The module constant itself is a str, never mutated.
        assert isinstance(POCKET_SPECIALIST_PROMPT, str)
        assert "MUTATED" not in POCKET_SPECIALIST_PROMPT
