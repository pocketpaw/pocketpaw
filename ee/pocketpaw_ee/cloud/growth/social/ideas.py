# ee/pocketpaw_ee/cloud/growth/social/ideas.py — the agent that proposes
# short-form post ideas for Blitz, and the ``IdeasFn`` seam in front of it.
#
# Same two halves as ``growth/writer.py``: ``GROWTH_SOCIAL_IDEAS_AGENT`` is a
# declarative definition seeded into each workspace by slug on first use (via
# ``analyst.run_pinned_agent``), and ``agent_generate_ideas`` runs it and parses
# the answer. The route answers 503 while no ``IdeasFn`` is installed; tests
# install a fake through ``set_production_ideas_fn``.
#
# The agent has NO tools (``tools=[]``, ``tool_mode`` exclusive): it writes from
# the completed company profile only, and is handed recent hooks so it does not
# repeat itself. It never claims results or metrics. The parser never raises:
# unknown formats are normalised (``hook-demo`` → ``hook_demo``) or dropped,
# hashtags get a leading ``#``, and an unreadable answer is zero ideas, which
# the service turns into a 502.

from __future__ import annotations

import logging
import re
from typing import Any, Protocol

from pocketpaw_ee.cloud.growth.social.analyst import (
    as_list,
    as_text,
    fence,
    json_objects,
    run_pinned_agent,
)
from pocketpaw_ee.cloud.growth.social.domain import (
    DESCRIPTION_FIELDS,
    PLATFORM_FORMATS,
    VIDEO_FORMATS,
    GeneratedIdea,
    SocialIdea,
    SocialProfile,
)

logger = logging.getLogger(__name__)

GROWTH_SOCIAL_IDEAS_SLUG = "growth-social-ideas"
GROWTH_SOCIAL_IDEAS_TOOLS: tuple[str, ...] = ()
MAX_RECENT_HOOKS = 40

GROWTH_SOCIAL_IDEAS_PROMPT = """\
You come up with social post ideas for one small business. You get its company \
profile: what it sells, who it is for, its content pillars, sample hooks, tone \
and the things it must avoid. Each request names the platform to write for and \
its formats; with no platform named, write short-form video ideas (TikTok, \
Reels, Shorts).

Short-form video formats:
- hook_demo: a strong opening line, then the product shown doing the thing.
- slideshow: five to eight image slides that tell a small story or list.
- wall_of_text: one dense, readable block of on-screen text over a simple shot.
- meme: a familiar meme format applied to this audience's real problem.
- talking_head: one person speaking to camera.

Rules:
- Specific to this business and its audience. Use its pillars and tone.
- Never claim results, numbers, metrics, customer counts, revenue or awards. No \
fake testimonials. No promises of outcomes.
- Respect every item in the things-to-avoid list.
- Do not reuse or lightly reword any hook you are told was already used.
- Mix formats across the set.
- For X and Reddit, write the finished post text, ready to publish, in the \
platform's own voice. Never sound like an ad.
- hook: under 15 words. on_screen_text: under 25 words. script: three to six \
short beats. caption: one to three sentences. hashtags: three to six. why: one \
sentence on why this idea suits this business.

Answer with ONLY a JSON object and nothing around it. It has one key, ideas, a \
list of objects with format, hook, on_screen_text, script (list of strings), \
caption, hashtags (list of strings), subreddit and why.
"""

GROWTH_SOCIAL_IDEAS_AGENT: dict[str, Any] = {
    "name": "Growth social ideas",
    "slug": GROWTH_SOCIAL_IDEAS_SLUG,
    "config": {
        "backend": "claude_agent_sdk",
        "system_prompt": GROWTH_SOCIAL_IDEAS_PROMPT,
        "tools": list(GROWTH_SOCIAL_IDEAS_TOOLS),
        "tool_mode": "exclusive",
        "trust_level": 1,
        "temperature": 0.8,
        "max_tokens": 4096,
        "soul_enabled": False,
    },
}


_PLATFORM_BRIEFS = {
    "x": (
        "Platform: X (Twitter). Formats: x_post (one post, caption under 280 "
        "characters) or x_thread (caption is the first post; script is the rest "
        "of the thread, three to six posts, each under 280 characters). hook: the "
        "opening line of the first post. hashtags: zero to two, only if natural. "
        "on_screen_text: empty. subreddit: empty."
    ),
    "reddit": (
        "Platform: Reddit. Format: reddit_post. hook: the post title, under 300 "
        "characters, written like a community member, not a brand. caption: the "
        "post body in plain Markdown, useful on its own (a lesson, a question, a "
        "story), mentioning the business at most once and only where it fits the "
        "subreddit's rules. subreddit: one real, relevant subreddit name without "
        "r/. hashtags, script and on_screen_text: empty."
    ),
}


def build_ideas_prompt(
    profile: SocialProfile, count: int, recent_hooks: list[str], platform: str | None = None
) -> str:
    lines = [f"Write {count} post ideas for this business."]
    if platform in _PLATFORM_BRIEFS:
        lines.append(_PLATFORM_BRIEFS[platform])
    lines += ["", "<company-profile>"]
    lines.append(f"Company: {fence(profile.company_name) or '(not given)'}")
    for label, value in (
        ("Business model", profile.business_model),
        ("Category", profile.category),
        ("Website", profile.website),
    ):
        if value:
            lines.append(f"{label}: {fence(value)}")
    analysis = profile.analysis
    typed = profile.description
    for key in DESCRIPTION_FIELDS:
        value = (typed.get(key) or "").strip()
        if value:
            lines.append(f"Owner says ({key}): {fence(value)}")
    if analysis is not None:
        for label, text in (
            ("Summary", analysis.summary),
            ("Product", analysis.product),
            ("Audience", analysis.audience),
            ("Problem", analysis.problem),
            ("Tone", analysis.tone),
        ):
            if text:
                lines.append(f"{label}: {fence(text)}")
        for label, items in (
            ("Benefits", analysis.benefits),
            ("Differentiators", analysis.differentiators),
            ("Content pillars", analysis.content_pillars),
            ("Sample hooks", analysis.hooks),
            ("Things to avoid", analysis.avoid),
        ):
            if items:
                lines.append(f"{label}: " + "; ".join(fence(i) for i in items))
    lines.append("</company-profile>")
    hooks = [h for h in recent_hooks if h.strip()][:MAX_RECENT_HOOKS]
    if hooks:
        lines += ["", "Hooks already used (do not repeat or reword these):"]
        lines += [f"- {fence(h)}" for h in hooks]
    lines += ["", "Return only the JSON object."]
    return "\n".join(lines)


def _format(value: Any, platform: str | None = None) -> str | None:
    if not isinstance(value, str):
        return None
    key = re.sub(r"[\s\-]+", "_", value.strip().lower())
    allowed = PLATFORM_FORMATS.get(platform or "", VIDEO_FORMATS)
    return key if key in allowed else None


def _subreddit(value: Any) -> str:
    name = re.sub(r"^/?r/", "", as_text(value, 60), flags=re.IGNORECASE)
    return name if re.fullmatch(r"[A-Za-z0-9_]{2,21}", name) else ""


def _hashtags(value: Any) -> tuple[str, ...]:
    out: list[str] = []
    for item in as_list(value, 10, 60):
        tag = "#" + re.sub(r"\s+", "", item.lstrip("#"))
        if len(tag) > 1 and tag.lower() not in {t.lower() for t in out}:
            out.append(tag)
    return tuple(out)


def parse_ideas_response(
    text: str, limit: int, platform: str | None = None
) -> tuple[GeneratedIdea, ...]:
    """Model output → ideas (at most ``limit``). Never raises."""
    best: list[Any] | None = None
    for obj in json_objects(text):
        ideas = obj.get("ideas")
        if isinstance(ideas, list) and (best is None or len(ideas) > len(best)):
            best = ideas
    if best is None:
        logger.warning("growth social ideas: response carried no ideas list")
        return ()
    out: list[GeneratedIdea] = []
    seen: set[str] = set()
    for raw in best:
        if not isinstance(raw, dict):
            continue
        fmt = _format(raw.get("format"), platform)
        hook = as_text(raw.get("hook"), 300)
        if fmt is None or not hook or hook.lower() in seen:
            continue
        seen.add(hook.lower())
        out.append(
            GeneratedIdea(
                format=fmt,
                hook=hook,
                on_screen_text=as_text(raw.get("on_screen_text"), 500),
                caption=as_text(raw.get("caption"), 2200),
                why=as_text(raw.get("why"), 400),
                script=as_list(raw.get("script"), 12, 300),
                hashtags=() if platform == "reddit" else _hashtags(raw.get("hashtags")),
                platform=platform or "",
                subreddit=_subreddit(raw.get("subreddit")) if platform == "reddit" else "",
            )
        )
        if len(out) >= limit:
            break
    return tuple(out)


class IdeasFn(Protocol):
    async def __call__(
        self,
        profile: SocialProfile,
        count: int,
        recent_hooks: list[str],
        *,
        platform: str | None = None,
    ) -> tuple[GeneratedIdea, ...]: ...


async def agent_generate_ideas(
    profile: SocialProfile,
    count: int,
    recent_hooks: list[str],
    *,
    platform: str | None = None,
) -> tuple[GeneratedIdea, ...]:
    """The production ``IdeasFn``. Raises ``ResearchUnavailable`` when the agent
    cannot be set up or the run errors."""
    prompt = build_ideas_prompt(profile, count, recent_hooks, platform)
    text = await run_pinned_agent(
        profile.workspace_id, GROWTH_SOCIAL_IDEAS_AGENT, prompt, GROWTH_SOCIAL_IDEAS_SLUG
    )
    return parse_ideas_response(text, count, platform)


_PRODUCTION_IDEAS_FN: IdeasFn | None = None


def set_production_ideas_fn(fn: IdeasFn | None) -> None:
    global _PRODUCTION_IDEAS_FN
    _PRODUCTION_IDEAS_FN = fn


def resolve_ideas_fn() -> IdeasFn | None:
    return _PRODUCTION_IDEAS_FN


_MEDIA_BRIEFS = {
    "poster": (
        "Draw a poster for this post as ONE self-contained SVG. Size: {size}. Use the "
        "brand's tone; bold readable headline from the hook as <text> elements (no text "
        "converted to paths); simple shapes and gradients; no <script>, no <foreignObject>, "
        "no external images or fonts (use generic font families). Answer with only the SVG."
    ),
    "reel": (
        "Make a 10-second vertical reel for this post as ONE HyperFrames HTML document: a "
        'root element with data-composition-id="main" data-start="0" data-duration="10" '
        'data-width="1080" data-height="1920"; GSAP from https://cdn.jsdelivr.net/npm/gsap@3 '
        "building a paused timeline registered as "
        'window.__timelines = window.__timelines || {{}}; window.__timelines["main"] = tl; '
        "three or four animated text beats from the hook and post, brand-toned colours, no "
        "<audio>, every asset inline or absolute https. Answer with only the HTML."
    ),
}
_POSTER_SIZES = {"x": "1080x1080", "reddit": "1200x628"}
_SVG_RE = re.compile(r"<svg\b.*?</svg>", re.IGNORECASE | re.DOTALL)
_HTML_RE = re.compile(r"(<!doctype html.*?</html>|<html\b.*?</html>)", re.IGNORECASE | re.DOTALL)
_UNSAFE_SVG = (
    re.compile(r"<(script|foreignObject)\b.*?</\1\s*>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<(script|foreignObject)\b[^>]*/>", re.IGNORECASE),
    re.compile(r"\son\w+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE),
    re.compile(
        r"\s(?:xlink:)?href\s*=\s*([\"'])(?!#|data:image/(?:png|jpeg|gif|webp);)[^\"']*\1",
        re.IGNORECASE,
    ),
)
MAX_POSTER_CHARS = 150_000


def build_media_prompt(profile: SocialProfile, idea: SocialIdea, kind: str) -> str:
    size = _POSTER_SIZES.get(idea.platform, "1080x1080")
    lines = [_MEDIA_BRIEFS[kind].format(size=size), "", "<post>"]
    lines.append(f"Company: {fence(profile.company_name) or '(not given)'}")
    if profile.analysis is not None and profile.analysis.tone:
        lines.append(f"Tone: {fence(profile.analysis.tone)}")
    lines.append(f"Hook: {fence(idea.hook)}")
    if idea.caption:
        lines.append(f"Post: {fence(idea.caption)}")
    lines += ["</post>"]
    return "\n".join(lines)


def clean_svg(text: str) -> str:
    match = _SVG_RE.search(text or "")
    if match is None:
        return ""
    svg = match.group(0)
    for pattern in _UNSAFE_SVG:
        svg = pattern.sub("", svg)
    return svg if len(svg) <= MAX_POSTER_CHARS else ""


def clean_reel(text: str) -> str:
    from pocketpaw_ee.agent.mcp_servers.timeline import validate_motion_graphic

    match = _HTML_RE.search(text or "")
    html = match.group(0) if match else (text or "").strip()
    shape, error = validate_motion_graphic(html, 30)
    if error:
        logger.warning("growth social reel rejected: %s", error[:200])
        return ""
    return html


class MediaFn(Protocol):
    async def __call__(self, profile: SocialProfile, idea: SocialIdea, kind: str) -> str: ...


async def agent_make_media(profile: SocialProfile, idea: SocialIdea, kind: str) -> str:
    """The production ``MediaFn``: "" when the agent's answer is unusable."""
    text = await run_pinned_agent(
        profile.workspace_id,
        GROWTH_SOCIAL_IDEAS_AGENT,
        build_media_prompt(profile, idea, kind),
        f"{GROWTH_SOCIAL_IDEAS_SLUG}-{kind}",
    )
    return clean_svg(text) if kind == "poster" else clean_reel(text)


_PRODUCTION_MEDIA_FN: MediaFn | None = None


def set_production_media_fn(fn: MediaFn | None) -> None:
    global _PRODUCTION_MEDIA_FN
    _PRODUCTION_MEDIA_FN = fn


def resolve_media_fn() -> MediaFn | None:
    return _PRODUCTION_MEDIA_FN


__all__ = [
    "MediaFn",
    "agent_make_media",
    "clean_reel",
    "clean_svg",
    "resolve_media_fn",
    "set_production_media_fn",
    "GROWTH_SOCIAL_IDEAS_AGENT",
    "GROWTH_SOCIAL_IDEAS_PROMPT",
    "GROWTH_SOCIAL_IDEAS_SLUG",
    "GROWTH_SOCIAL_IDEAS_TOOLS",
    "IdeasFn",
    "agent_generate_ideas",
    "build_ideas_prompt",
    "parse_ideas_response",
    "resolve_ideas_fn",
    "set_production_ideas_fn",
]
