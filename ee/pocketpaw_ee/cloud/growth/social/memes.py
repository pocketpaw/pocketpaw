# ee/pocketpaw_ee/cloud/growth/social/memes.py — Growth › Social › Create: original vector
# meme characters and the memes made with them.
#
# Everything is drawn by the social ideas agent as SVG; no image model and no third-party
# media, so there is nothing to license. A character is a flat mascot the agent invents from a
# description (``draw_character``); a meme redraws that same character with a new expression
# inside one of ``MEME_FORMATS``' layouts and writes the X / Reddit post that carries it
# (``make_meme``). The prompts forbid real people, celebrities and existing franchise
# characters. Output goes through ``ideas.clean_svg`` (no scripts, handlers or external links).
#
# Both are seams like the ideas / media fns: ``set_production_character_fn`` and
# ``set_production_meme_fn`` install the agent-backed versions; tests install fakes.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pocketpaw_ee.cloud.growth.social.analyst import as_text, fence, json_objects, run_pinned_agent
from pocketpaw_ee.cloud.growth.social.domain import SocialProfile
from pocketpaw_ee.cloud.growth.social.ideas import (
    GROWTH_SOCIAL_IDEAS_AGENT,
    GROWTH_SOCIAL_IDEAS_SLUG,
    clean_svg,
)

MAX_CHARACTERS = 6

MEME_FORMATS: tuple[tuple[str, str, str], ...] = (
    (
        "nobody_me",
        "Nobody: / Me:",
        (
            "Top: 'Nobody:' then 'Me:' with the setup line; the character below doing the "
            "over-the-top thing."
        ),
    ),
    (
        "pov",
        "POV:",
        (
            "One 'POV: …' line across the top; the character full-frame reacting as if "
            "the viewer is in the scene."
        ),
    ),
    (
        "expectation_reality",
        "Expectation vs Reality",
        (
            "Two panels side by side labelled Expectation and Reality; the character confident in "
            "one, wrecked in the other."
        ),
    ),
    (
        "two_buttons",
        "Two buttons",
        "Two big buttons with labels at the top; the character below sweating, unable to choose.",
    ),
    (
        "starter_pack",
        "Starter pack",
        (
            "Title '<X> starter pack'; a 2x3 grid of simple icon tiles with short labels; the "
            "character in one tile."
        ),
    ),
    (
        "tell_me_without",
        "Tell me … without telling me",
        (
            "Top line 'Tell me you're <X> without telling me you're <X>'; the character below with "
            "the giveaway detail."
        ),
    ),
    (
        "reaction",
        "Reaction face",
        (
            "One short setup line on top; the character large with a strong reaction expression "
            "(shocked, side-eye or smug)."
        ),
    ),
    (
        "this_is_fine",
        "Everything is fine",
        (
            "The character calmly sitting with a mug while small chaos icons surround it; caption "
            "'everything is fine'."
        ),
    ),
    (
        "before_after",
        "Before / After",
        "Two stacked panels labelled Before and After; the character transformed by the product.",
    ),
    (
        "wall_of_text",
        "Wall of text",
        (
            "A dense, readable block of first-person text filling most of the frame; the character "
            "small in a corner."
        ),
    ),
    (
        "brain_levels",
        "Galaxy brain",
        (
            "Four stacked rows of increasingly 'enlightened' takes; the character's head glowing "
            "brighter each row."
        ),
    ),
    (
        "plot_twist",
        "Plot twist",
        (
            "Setup line, then 'plot twist:' in bold, then the punchline; the character with a "
            "surprised face."
        ),
    ),
)
MEME_FORMAT_IDS: frozenset[str] = frozenset(f[0] for f in MEME_FORMATS)
_FORMAT_BY_ID = {f[0]: f for f in MEME_FORMATS}

_ORIGINALITY = (
    "The character must be ORIGINAL: never a real person, celebrity, politician, influencer, "
    "or any existing cartoon, game, film or brand character, even if asked. If asked for one, "
    "invent an original character with a similar vibe instead."
)

_CHARACTER_BRIEF = (
    "Draw one original meme mascot as a single self-contained SVG, viewBox '0 0 512 512', flat "
    "vector shapes, bold outlines, transparent background, no text. Put the face parts in groups "
    "with ids 'eyes', 'mouth' and 'brows' so later drawings can change the expression. No "
    "<script>, no <foreignObject>, no external images or fonts. " + _ORIGINALITY + " Answer "
    "with only the SVG."
)

_MEME_BRIEF = (
    "Make one meme for this business. First a JSON object with keys hook (the post's opening "
    "line), caption (the full post text for {platform}), why (one sentence). Then, after the "
    "JSON, one self-contained SVG meme, viewBox '{viewbox}': follow the format layout, use the "
    "character drawing below as the base (same shapes and colours, change only the expression "
    "and pose to fit the joke), meme-style bold text as <text> elements in generic font "
    "families, white background, no <script>, no <foreignObject>, no external images or fonts. "
    "Never claim results, numbers or awards. " + _ORIGINALITY
)
_VIEWBOX = {"x": "0 0 1080 1080", "reddit": "0 0 1200 1200"}


@dataclass(frozen=True)
class MadeMeme:
    hook: str
    caption: str
    why: str
    svg: str


def build_character_prompt(profile: SocialProfile, name: str, description: str) -> str:
    return "\n".join(
        [
            _CHARACTER_BRIEF,
            "",
            f"Brand: {fence(profile.company_name) or '(not given)'}",
            f"Character name: {fence(name) or '(unnamed)'}",
            f"Character idea: {fence(description)}",
        ]
    )


def build_meme_prompt(
    profile: SocialProfile,
    character: dict[str, Any] | None,
    fmt: str | None,
    mention_business: bool,
    prompt: str,
    platform: str,
) -> str:
    lines = [
        _MEME_BRIEF.format(
            platform="X" if platform == "x" else "Reddit", viewbox=_VIEWBOX[platform]
        )
    ]
    if fmt in _FORMAT_BY_ID:
        _, name, layout = _FORMAT_BY_ID[fmt]
        lines.append(f"Format: {name}. Layout: {layout}")
    else:
        lines.append(
            "Format: pick the best of: " + "; ".join(f"{f[1]} ({f[2]})" for f in MEME_FORMATS)
        )
    lines.append(
        "Mention the business by name in the meme or caption."
        if mention_business
        else "Do not name the business; make it a relatable joke for its audience."
    )
    lines += ["", "<company>", f"Company: {fence(profile.company_name)}"]
    if profile.analysis is not None:
        for label, text in (
            ("Product", profile.analysis.product),
            ("Audience", profile.analysis.audience),
            ("Tone", profile.analysis.tone),
        ):
            if text:
                lines.append(f"{label}: {fence(text)}")
        if profile.analysis.avoid:
            lines.append("Avoid: " + "; ".join(fence(a) for a in profile.analysis.avoid))
    lines.append("</company>")
    if prompt.strip():
        lines.append(f"The user wants it to be about: {fence(prompt.strip())}")
    if character:
        lines += ["", f"Character '{fence(character.get('name', ''))}':", character.get("svg", "")]
    return "\n".join(lines)


def parse_meme(text: str) -> MadeMeme | None:
    svg = clean_svg(text)
    if not svg:
        return None
    meta = next((o for o in json_objects(text.split("<svg", 1)[0]) if "hook" in o), {})
    hook = as_text(meta.get("hook"), 300)
    caption = as_text(meta.get("caption"), 2200)
    return MadeMeme(
        hook=hook or caption[:120] or "Meme",
        caption=caption,
        why=as_text(meta.get("why"), 400),
        svg=svg,
    )


class CharacterFn(Protocol):
    async def __call__(self, profile: SocialProfile, name: str, description: str) -> str: ...


class MemeFn(Protocol):
    async def __call__(
        self,
        profile: SocialProfile,
        character: dict[str, Any] | None,
        fmt: str | None,
        mention_business: bool,
        prompt: str,
        platform: str,
    ) -> MadeMeme | None: ...


async def agent_draw_character(profile: SocialProfile, name: str, description: str) -> str:
    text = await run_pinned_agent(
        profile.workspace_id,
        GROWTH_SOCIAL_IDEAS_AGENT,
        build_character_prompt(profile, name, description),
        f"{GROWTH_SOCIAL_IDEAS_SLUG}-character",
    )
    return clean_svg(text)


async def agent_make_meme(
    profile: SocialProfile,
    character: dict[str, Any] | None,
    fmt: str | None,
    mention_business: bool,
    prompt: str,
    platform: str,
) -> MadeMeme | None:
    text = await run_pinned_agent(
        profile.workspace_id,
        GROWTH_SOCIAL_IDEAS_AGENT,
        build_meme_prompt(profile, character, fmt, mention_business, prompt, platform),
        f"{GROWTH_SOCIAL_IDEAS_SLUG}-meme",
    )
    return parse_meme(text)


_CHARACTER_FN: CharacterFn | None = None
_MEME_FN: MemeFn | None = None


def set_production_character_fn(fn: CharacterFn | None) -> None:
    global _CHARACTER_FN
    _CHARACTER_FN = fn


def set_production_meme_fn(fn: MemeFn | None) -> None:
    global _MEME_FN
    _MEME_FN = fn


def resolve_character_fn() -> CharacterFn | None:
    return _CHARACTER_FN


def resolve_meme_fn() -> MemeFn | None:
    return _MEME_FN


__all__ = [
    "MAX_CHARACTERS",
    "MEME_FORMATS",
    "MEME_FORMAT_IDS",
    "MadeMeme",
    "agent_draw_character",
    "agent_make_meme",
    "parse_meme",
    "resolve_character_fn",
    "resolve_meme_fn",
    "set_production_character_fn",
    "set_production_meme_fn",
]
