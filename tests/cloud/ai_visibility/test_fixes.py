# tests/cloud/ai_visibility/test_fixes.py — the fix rules table.
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3). One case per rule, the
# first-match-wins order, the ``none`` fallthrough and ``we_can_apply``.
from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.ai_visibility.fixes import FIX_IDS, FIXES, Signals, pick_fix

GOOD = Signals(
    mentioned_any=True,
    recommended_any=True,
    negative_mentions=0,
    avg_sentiment=3.5,
    has_site=True,
    own_site_consulted=True,
    listing_types_seen=frozenset({"gbp", "yelp"}),
    business_listing_types=frozenset({"gbp", "yelp"}),
)


def _with(**kw) -> Signals:
    return Signals(**{**GOOD.__dict__, **kw})


@pytest.mark.parametrize(
    ("signals", "fix_id"),
    [
        (GOOD, "none"),
        (_with(site_blocks_ai_bots=True, negative_mentions=2), "ai_access"),
        (_with(negative_mentions=1, own_site_consulted=False), "negative_mentions"),
        (_with(own_site_consulted=False, business_listing_types=frozenset()), "site_content"),
        (_with(business_listing_types=frozenset({"yelp"})), "gbp"),
        (_with(business_listing_types=frozenset({"gbp"})), "yelp"),
        (_with(listing_types_seen=frozenset({"tripadvisor"})), "tripadvisor"),
        (_with(recommended_any=False), "reviews"),
        (_with(avg_sentiment=2.0), "reviews"),
        # no site and never named, listings fine -> site_content (generic)
        (
            _with(
                mentioned_any=False,
                recommended_any=False,
                avg_sentiment=None,
                has_site=False,
                own_site_consulted=False,
            ),
            "site_content",
        ),
        # a listing type nobody consults is not a gap
        (_with(listing_types_seen=frozenset(), business_listing_types=frozenset()), "none"),
    ],
)
def test_pick_fix(signals: Signals, fix_id: str) -> None:
    assert pick_fix(signals)["id"] == fix_id


def test_we_can_apply_only_site_fixes() -> None:
    assert {k for k, (_, can) in FIXES.items() if can} == {"ai_access"}
    assert set(FIXES) == set(FIX_IDS)
    fix = pick_fix(_with(site_blocks_ai_bots=True))
    assert fix["we_can_apply"] is True and fix["text"]
