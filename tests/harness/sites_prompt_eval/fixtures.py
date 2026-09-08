"""The brief set SD-6 scores against, and the pairing that makes rotation checkable.

New file 2026-09-08 (feat/sites-design-skills), part of SD-6.

WHY THESE BRIEFS. Each pair is two businesses in the SAME industry, because that
is the condition under which the rotation rule is load-bearing: an agent that
defaults by topic will hand a cafe and a bakery the identical warm-earthy palette,
which is the exact failure `pocketpaw-design-taste` MODULE 2.G names ("do NOT
default to the warm / earthy family just because the business is a cafe, salon, or
shop"). Two briefs from different industries would pass rotation by accident.

The pairs also span the three dials so a cut cannot be judged on one register:
one consumer/warm-coded pair, one technical/neutral-coded pair, one
premium/luxury-coded pair whose LLM default (beige + brass) design-taste bans by
name.

WHAT A BRIEF DELIBERATELY DOES NOT SAY. No brief names a colour, a font, or a
layout. The prompt's claim is that it INFERS the direction rather than asking or
defaulting, so a brief that specifies the look would measure nothing.

REFINE BRIEFS are separate and small on purpose: SD-2 cut the craft block on
refine down to the floor, so the thing to measure there is whether the floor still
holds on markup an edit adds. A refine fixture is a create output plus an
instruction.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Brief:
    """One create request, and which scorers must hold on its output."""

    key: str
    prompt: str
    #  Which industry pair this belongs to. Two briefs sharing a pair are scored
    #  against each other by ``behaviors.rotation``.
    pair: str
    #  Scorers expected to apply. Empty means "every page scorer".
    expect: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class RefineBrief:
    """One edit request against an existing page.

    ``instruction`` is what the user says; ``adds_markup`` records whether the edit
    is expected to introduce new markup, because that is the case where SD-2's
    floor-only slice is doing the work and the case a regression would show up in.
    """

    key: str
    instruction: str
    adds_markup: bool


CREATE_BRIEFS: tuple[Brief, ...] = (
    Brief(
        key="cafe",
        pair="hospitality",
        prompt=(
            "Build a landing page for Fennel & Ash, a neighbourhood coffee shop "
            "that roasts its own beans and runs a cupping class on Saturdays. "
            "They want people to book the class."
        ),
    ),
    Brief(
        key="bakery",
        pair="hospitality",
        prompt=(
            "Build a landing page for Overproof, a sourdough bakery that sells a "
            "weekly subscription box. They want subscription sign-ups."
        ),
    ),
    Brief(
        key="observability",
        pair="devtool",
        prompt=(
            "Build a landing page for a developer tool that traces slow database "
            "queries in production and shows which deploy introduced them. The "
            "action is starting a free trial."
        ),
    ),
    Brief(
        key="ci-runner",
        pair="devtool",
        prompt=(
            "Build a landing page for a hosted CI runner that is faster than the "
            "default one and bills per second. The action is connecting a repo."
        ),
    ),
    Brief(
        key="cookware",
        pair="premium",
        prompt=(
            "Build a landing page for a small brand selling one carbon-steel pan, "
            "made in a single workshop. The action is buying the pan."
        ),
    ),
    Brief(
        key="skincare",
        pair="premium",
        prompt=(
            "Build a landing page for a three-product skincare line with a short "
            "ingredient list. The action is buying the starter set."
        ),
    ),
)

REFINE_BRIEFS: tuple[RefineBrief, ...] = (
    RefineBrief(
        key="copy-only",
        instruction="Shorten the hero headline to five words.",
        adds_markup=False,
    ),
    RefineBrief(
        key="restructure",
        instruction=(
            "Add a testimonials section between the features and the pricing, with three quotes."
        ),
        adds_markup=True,
    ),
    RefineBrief(
        key="restyle",
        instruction="Make the nav sticky and give it a subtle background on scroll.",
        adds_markup=True,
    ),
)


def pairs() -> dict[str, tuple[Brief, ...]]:
    """Group the create briefs by industry pair, for the rotation scorer."""
    out: dict[str, list[Brief]] = {}
    for b in CREATE_BRIEFS:
        out.setdefault(b.pair, []).append(b)
    return {k: tuple(v) for k, v in out.items()}
