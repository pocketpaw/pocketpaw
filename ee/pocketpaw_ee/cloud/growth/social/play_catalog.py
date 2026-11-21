# ee/pocketpaw_ee/cloud/growth/social/play_catalog.py — the marketing tree as data,
# and the source of truth for Growth branch and play ids (the paw-enterprise
# ``tree-catalog.ts`` prototype was copied from here, not the other way round).
#
# A branch is one section of the Growth nav (Today is a fixed item, not a branch).
# A play is a platform plus a purpose (``x-authority``, ``linkedin-outreach``); a
# ``play:<id>`` Blueprint unit is only valid for an id listed here. ``acts`` is
# true only for the six plays an agent executes today (posting on X, Reddit and
# LinkedIn, outreach email, the LinkedIn queue); every other play is plan-only.
# ``channel`` maps an acting play onto the legacy Blueprint channel whose weekly
# cadence it feeds (``cadence`` says which number: posts or sends). ``peso`` is a
# hidden tag used only for a coverage check.
# ``DEFAULT_BRANCHES`` is what a brand sees when its ``growth_branches`` is empty.
# ``acting_post_plays`` groups a Blueprint's active acting post plays by platform.
# Pure Python, no Pydantic or Beanie, so dto, service and agents may import it.

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Peso = Literal["paid", "earned", "shared", "owned"]


@dataclass(frozen=True)
class Branch:
    id: str
    label: str
    purpose: str
    groups: tuple[str, ...]
    leaves: tuple[str, ...]
    plan_only: bool


@dataclass(frozen=True)
class Play:
    id: str
    label: str
    branch: str
    leaf: str
    platform: str
    peso: Peso
    acts: bool = False
    channel: str | None = None
    cadence: Literal["posts", "sends"] | None = None


BRANCHES: tuple[Branch, ...] = (
    Branch(
        "strategy",
        "Strategy",
        "The Blueprint and the research behind it.",
        ("Strategy & Positioning",),
        ("Blueprint", "Research"),
        False,
    ),
    Branch(
        "content",
        "Content",
        "Ideas, posts and the week calendar for every channel you publish on.",
        ("Content Marketing", "Personal Branding"),
        (
            "Social media",
            "Personal branding",
            "Community",
            "SEO",
            "Video",
            "Email marketing",
            "Calendar",
        ),
        False,
    ),
    Branch(
        "outbound",
        "Outbound",
        "People, prospects, hunts and sequences. Sales lives here.",
        ("Outbound & Direct", "B2B"),
        ("Prospects", "Cold email", "LinkedIn outreach", "Sequences"),
        False,
    ),
    Branch(
        "paid",
        "Paid",
        "Ads you pay for. Plan only: nothing spends from here.",
        ("Performance Marketing",),
        ("Search ads", "Paid social"),
        True,
    ),
    Branch(
        "pr",
        "PR",
        "Launches, press and podcasts that earn attention.",
        ("PR & Authority",),
        ("Launches", "Media", "Podcasts", "Directories"),
        True,
    ),
    Branch(
        "partnerships",
        "Partnerships",
        "Affiliates, co-marketing and creators who carry the word.",
        ("Partnerships & Distribution",),
        ("Affiliates", "Co-marketing", "Creators"),
        True,
    ),
    Branch(
        "growth-loops",
        "Growth loops",
        "Referrals, experiments and community that compound.",
        ("Growth Marketing",),
        ("Referrals", "Experiments", "Community"),
        True,
    ),
    Branch(
        "results",
        "Results",
        "What worked, what did not, and the rules we keep.",
        ("Analytics & Optimization", "Lifecycle", "CRO", "Attribution"),
        ("Dashboards", "Learnings", "Experiments log", "Attribution"),
        False,
    ),
    Branch(
        "settings",
        "Settings",
        "Accounts, automation and who can approve.",
        ("Marketing Operations",),
        ("Accounts", "Automation", "Team"),
        False,
    ),
)

PLAYS: tuple[Play, ...] = (
    # Content: the four acting social plays plus owned channels.
    Play("x-organic", "X organic", "content", "Social media", "X", "shared", True, "x", "posts"),
    Play(
        "x-authority",
        "X founder authority",
        "content",
        "Personal branding",
        "X",
        "shared",
        True,
        "x",
        "posts",
    ),
    Play(
        "linkedin-content",
        "LinkedIn founder posts",
        "content",
        "Personal branding",
        "LinkedIn",
        "shared",
        True,
        "linkedin",
        "posts",
    ),
    Play(
        "reddit-community",
        "Reddit community",
        "content",
        "Community",
        "Reddit",
        "shared",
        True,
        "reddit",
        "posts",
    ),
    Play("seo-articles", "SEO articles", "content", "SEO", "Blog", "owned"),
    Play("youtube-videos", "YouTube videos", "content", "Video", "YouTube", "owned"),
    Play("newsletter", "Newsletter", "content", "Email marketing", "Email", "owned"),
    # Outbound: both acting.
    Play(
        "cold-email",
        "Cold email",
        "outbound",
        "Cold email",
        "Email",
        "owned",
        True,
        "email",
        "sends",
    ),
    Play(
        "linkedin-outreach",
        "LinkedIn outreach",
        "outbound",
        "LinkedIn outreach",
        "LinkedIn",
        "owned",
        True,
        "linkedin",
        "sends",
    ),
    # Paid (plan only).
    Play("google-ads", "Google search ads", "paid", "Search ads", "Google", "paid"),
    Play("linkedin-ads", "LinkedIn ads", "paid", "Paid social", "LinkedIn", "paid"),
    Play("meta-ads", "Meta ads", "paid", "Paid social", "Meta", "paid"),
    Play("x-ads", "X promoted posts", "paid", "Paid social", "X", "paid"),
    # PR (plan only).
    Play("launch-platforms", "Launch platforms", "pr", "Launches", "Hacker News", "earned"),
    Play("media-outreach", "Media outreach", "pr", "Media", "Press", "earned"),
    Play("podcast-guesting", "Podcast guesting", "pr", "Podcasts", "Podcasts", "earned"),
    Play("directories", "Directories", "pr", "Directories", "Directories", "earned"),
    # Partnerships (plan only).
    Play("affiliates", "Affiliates", "partnerships", "Affiliates", "Partners", "earned"),
    Play("co-marketing", "Co-marketing", "partnerships", "Co-marketing", "Partners", "earned"),
    Play("creator-collabs", "Creator collabs", "partnerships", "Creators", "Creators", "paid"),
    # Growth loops (plan only).
    Play("referral-program", "Referral program", "growth-loops", "Referrals", "Product", "owned"),
    Play(
        "growth-experiments",
        "Growth experiments",
        "growth-loops",
        "Experiments",
        "Product",
        "owned",
    ),
    Play(
        "community-building", "Community building", "growth-loops", "Community", "Discord", "owned"
    ),
)

BRANCH_IDS: frozenset[str] = frozenset(b.id for b in BRANCHES)
PLAY_IDS: frozenset[str] = frozenset(p.id for p in PLAYS)
DEFAULT_BRANCHES: tuple[str, ...] = ("strategy", "content", "outbound", "results", "settings")
_BY_ID = {p.id: p for p in PLAYS}


def play(play_id: str) -> Play | None:
    return _BY_ID.get(play_id)


def acting_plays() -> tuple[Play, ...]:
    return tuple(p for p in PLAYS if p.acts)


def acting_post_plays(plays: dict[str, dict]) -> dict[str, list[str]]:
    """Per platform, the ids among ``plays`` (play id -> stored body) of the active
    (core / testing) acting post plays."""
    out: dict[str, list[str]] = {}
    for play_id, body in plays.items():
        q = play(play_id)
        active = body.get("status") in ("core", "testing")
        if q and q.acts and q.cadence == "posts" and q.channel and active:
            out.setdefault(q.channel, []).append(play_id)
    return out


__all__ = [
    "BRANCHES",
    "BRANCH_IDS",
    "DEFAULT_BRANCHES",
    "PLAYS",
    "PLAY_IDS",
    "Branch",
    "Play",
    "acting_plays",
    "acting_post_plays",
    "play",
]
