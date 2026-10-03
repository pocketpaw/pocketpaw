# AI visibility — fixes: one plain-language next step from a check's signals.
#
# Created 2026-10-03 (feat/ai-visibility-core, AV-3). ``RULES`` is ordered and the
# first match wins; ``none`` is the fallthrough. Order, and why:
#   1. ai_access          the site blocks AI search bots: nothing else helps until
#                         the engines can read it (input flag, AV-1's checker).
#   2. negative_mentions  an engine already warns people off: fix that first.
#   3. site_content       the business has a site but no engine consulted it.
#   4-6. gbp / yelp / tripadvisor
#                         engines read that kind of listing for this question
#                         (it shows up among consulted sources) but never the
#                         business's own page there. ponytail: "competitors have
#                         it" is approximated by "the engines consult that listing
#                         type"; per-competitor attribution needs URL->competitor
#                         matching, add when a fix picks wrong in the eval set.
#   7. reviews            named, but only listed (never recommended) or lukewarm.
#   8. site_content       not named anywhere and nothing more specific applies.
# ``we_can_apply`` marks the fixes Paw Sites can make itself (ai_access,
# site_content); the rest are steps the owner takes on another site.

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

FIX_IDS = (
    "ai_access",
    "site_content",
    "gbp",
    "yelp",
    "tripadvisor",
    "reviews",
    "negative_mentions",
    "none",
)

# id -> (plain-language text, we_can_apply)
FIXES: dict[str, tuple[str, bool]] = {
    "ai_access": (
        "Your website tells AI search tools to stay out, so they can't read it. "
        "Let them in and they can start recommending you.",
        True,
    ),
    "site_content": (
        "AI assistants aren't using your website when they answer. Say clearly on it "
        "what you do, where you are and who you serve, so they can quote you.",
        True,
    ),
    "gbp": (
        "AI assistants read Google Business Profiles for this kind of question, and "
        "yours didn't come up. Claim or complete your Google Business Profile.",
        False,
    ),
    "yelp": (
        "AI assistants read Yelp for this kind of question, and your Yelp page didn't "
        "come up. Claim your Yelp page and fill it in.",
        False,
    ),
    "tripadvisor": (
        "AI assistants read TripAdvisor for this kind of question, and you're not "
        "there. Claim or create your TripAdvisor listing.",
        False,
    ),
    "reviews": (
        "AI assistants know you but don't recommend you yet. More recent, detailed "
        "customer reviews are the usual way to change that.",
        False,
    ),
    "negative_mentions": (
        "At least one AI assistant mentions you with a complaint. Find where that "
        "complaint comes from and reply to it or fix it.",
        False,
    ),
    "none": ("AI assistants already name and recommend you. Keep checking monthly.", False),
}

LISTING_TYPES = ("gbp", "yelp", "tripadvisor")


@dataclass(frozen=True)
class Signals:
    mentioned_any: bool
    recommended_any: bool
    negative_mentions: int
    avg_sentiment: float | None
    has_site: bool
    own_site_consulted: bool
    #: Listing types (gbp/yelp/tripadvisor) seen among consulted sources.
    listing_types_seen: frozenset[str] = frozenset()
    #: Listing types where the business's own page was among consulted sources.
    business_listing_types: frozenset[str] = frozenset()
    site_blocks_ai_bots: bool = False


def _listing_gap(kind: str) -> Callable[[Signals], bool]:
    return lambda s: kind in s.listing_types_seen and kind not in s.business_listing_types


RULES: list[tuple[str, Callable[[Signals], bool]]] = [
    ("ai_access", lambda s: s.site_blocks_ai_bots),
    ("negative_mentions", lambda s: s.negative_mentions > 0),
    ("site_content", lambda s: s.has_site and not s.own_site_consulted),
    *[(kind, _listing_gap(kind)) for kind in LISTING_TYPES],
    (
        "reviews",
        lambda s: s.mentioned_any
        and (not s.recommended_any or (s.avg_sentiment is not None and s.avg_sentiment < 2.5)),
    ),
    ("site_content", lambda s: not s.mentioned_any),
]


def pick_fix(signals: Signals) -> dict:
    """The first matching rule's fix as ``{id, text, we_can_apply}``."""
    fix_id = next((fid for fid, rule in RULES if rule(signals)), "none")
    text, we_can_apply = FIXES[fix_id]
    return {"id": fix_id, "text": text, "we_can_apply": we_can_apply}


__all__ = ["FIXES", "FIX_IDS", "RULES", "Signals", "pick_fix"]
