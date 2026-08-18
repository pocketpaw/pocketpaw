# src/pocketpaw/paw_bar/appearance.py — the owner's Paw Bar appearance.
#
# Created 2026-08-19. The widget has read ``window.__PAWBAR__.tokens`` and
# injected them as CSS custom properties since the glass bar shipped
# (app/src/main.ts), and the backend has answered ``"tokens": {}`` the whole
# time — a fully built white-label path with nothing on the other end. This is
# the missing half: a persisted appearance the owner edits in paw-enterprise,
# rendered into that token map.
#
# THE SECURITY POSTURE, because it is not obvious from the field list: every
# value here ends up as the right-hand side of a CSS custom property inside a
# document the widget serves. An unvalidated value is therefore a style
# injection, and a URL field is an exfiltration channel (``background-image:
# url(...)`` fires a request carrying the referrer). So nothing is passed
# through. Colors are re-emitted from parsed components rather than echoed,
# lengths are clamped integers formatted by us, fonts are chosen from a fixed
# roster rather than accepting a family string, and URLs must be https (or a
# data: image). A field that cannot be validated into a safe literal does not
# get to exist.
#
# Everything is optional with a working default, so a Site that has never been
# styled serializes exactly as it does today and needs no migration.

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------

# #rgb / #rrggbb only. Deliberately NOT a general CSS color: `red`, `rgb(...)`,
# `var(--x)` and `oklch(...)` all widen the grammar this has to defend, and the
# editor is a color picker that emits hex.
_HEX_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")

_RADIUS_RANGE = (0, 32)
_BLUR_RANGE = (0, 48)
_HERO_HEIGHT_RANGE = (120, 340)

# The widget ships no webfonts (an 80KB budget and a third-party origin per
# font), so a family is chosen from stacks the bundle already declares rather
# than typed. This is why `font` is an enum and not a string.
FONT_STACKS: dict[str, str] = {
    "system": ("system-ui, -apple-system, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif"),
    "geometric": "Avenir, 'Avenir Next', Montserrat, Corbel, 'URW Gothic', sans-serif",
    "humanist": (
        "Seravek, 'Gill Sans Nova', Ubuntu, Calibri, 'DejaVu Sans', source-sans-pro, sans-serif"
    ),
    "serif": "Charter, 'Bitstream Charter', 'Sitka Text', Cambria, Georgia, serif",
    "mono": "ui-monospace, 'Cascadia Code', 'Source Code Pro', Menlo, Consolas, monospace",
}

LAUNCHER_POSITIONS = frozenset({"bottom-right", "bottom-left"})
SURFACE_MODES = frozenset({"dark", "light"})
HERO_STYLES = frozenset({"gradient", "solid", "image"})
# Motion presets. "none" is not the same as the visitor's reduced-motion
# setting: this is the OWNER choosing a calmer bar for everyone, while the
# visitor's OS preference always wins on top of it (see tokens.css).
MOTION_PRESETS = frozenset({"none", "subtle", "spring", "expressive"})

# Per-preset (duration_ms, easing, travel_scale). Authored here rather than in
# CSS so the owner's choice is one stored word instead of five stored numbers,
# and so a preset can be retuned for every existing site at once.
_MOTION: dict[str, tuple[int, str, str]] = {
    "none": (0, "linear", "0"),
    "subtle": (160, "cubic-bezier(0.16, 1, 0.3, 1)", "1"),
    "spring": (240, "cubic-bezier(0.34, 1.56, 0.64, 1)", "1"),
    "expressive": (360, "cubic-bezier(0.34, 1.56, 0.64, 1)", "1.35"),
}


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


def _safe_image_url(value: str) -> str:
    """An https:// or data:image/ URL, or "".

    http:// is refused rather than upgraded: the bar renders on the owner's own
    https site, so a plain-http asset is a mixed-content block in every browser
    — accepting it would store a value that can only ever fail. Everything else
    (javascript:, vbscript:, file:, //host) is refused outright.
    """
    v = (value or "").strip()
    if not v:
        return ""
    lowered = v.lower()
    if lowered.startswith("https://"):
        # Nothing may terminate the url() token and start a new declaration.
        return "" if any(c in v for c in "()\"'\\ \n\r\t;") else v
    if lowered.startswith("data:image/"):
        return "" if any(c in v for c in "()\"'\\ \n\r\t;") else v
    return ""


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class LauncherAppearance(BaseModel):
    position: str = "bottom-right"
    label: str = ""
    icon_url: str = ""

    @field_validator("position")
    @classmethod
    def _known_position(cls, v: str) -> str:
        return v if v in LAUNCHER_POSITIONS else "bottom-right"

    @field_validator("label")
    @classmethod
    def _bounded_label(cls, v: str) -> str:
        return (v or "").strip()[:40]

    @field_validator("icon_url")
    @classmethod
    def _safe_icon(cls, v: str) -> str:
        return _safe_image_url(v)


class HeroAppearance(BaseModel):
    style: str = "gradient"
    from_color: str = "#2b4a9e"
    to_color: str = "#14161f"
    image_url: str = ""
    height: int = 200

    @field_validator("style")
    @classmethod
    def _known_style(cls, v: str) -> str:
        return v if v in HERO_STYLES else "gradient"

    @field_validator("from_color", "to_color")
    @classmethod
    def _hex_only(cls, v: str) -> str:
        v = (v or "").strip()
        return v if _HEX_RE.match(v) else ""

    @field_validator("image_url")
    @classmethod
    def _safe_image(cls, v: str) -> str:
        return _safe_image_url(v)

    @field_validator("height")
    @classmethod
    def _bounded_height(cls, v: int) -> int:
        return _clamp(v, *_HERO_HEIGHT_RANGE)


class MotionAppearance(BaseModel):
    preset: str = "spring"
    # The visitor's OS setting always wins regardless; this lets an owner ALSO
    # calm the bar for everyone. Off means the owner has opted out of honouring
    # it, which we do not offer — the field exists so the editor can show the
    # guarantee, and it is pinned True.
    honor_reduced_motion: bool = True

    @field_validator("preset")
    @classmethod
    def _known_preset(cls, v: str) -> str:
        return v if v in MOTION_PRESETS else "spring"

    @field_validator("honor_reduced_motion")
    @classmethod
    def _always_honored(cls, v: bool) -> bool:
        # Not a settable choice. A widget that ignores prefers-reduced-motion is
        # an accessibility defect on somebody else's website, and the owner does
        # not get to make that trade on their visitors' behalf.
        return True


class ConciergeAppearance(BaseModel):
    """The owner's full Paw Bar appearance. Every field defaults to today's look."""

    accent: str = "#3b6fe0"
    surface_mode: str = "dark"
    radius: int = 20
    blur: int = 28
    font: str = "system"
    show_branding: bool = True
    # Who the visitor is talking to. Rendered in the conversation header and the
    # Messages list; "" falls back to the widget's own generic copy.
    agent_name: str = ""
    agent_subtitle: str = ""
    agent_avatar_url: str = ""
    # Team faces on the Home card. Capped at 3 — the card shows three.
    team_avatar_urls: list[str] = Field(default_factory=list)

    launcher: LauncherAppearance = Field(default_factory=LauncherAppearance)
    hero: HeroAppearance = Field(default_factory=HeroAppearance)
    motion: MotionAppearance = Field(default_factory=MotionAppearance)

    @field_validator("accent")
    @classmethod
    def _hex_accent(cls, v: str) -> str:
        v = (v or "").strip()
        return v if _HEX_RE.match(v) else ""

    @field_validator("surface_mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        return v if v in SURFACE_MODES else "dark"

    @field_validator("font")
    @classmethod
    def _known_font(cls, v: str) -> str:
        return v if v in FONT_STACKS else "system"

    @field_validator("radius")
    @classmethod
    def _bounded_radius(cls, v: int) -> int:
        return _clamp(v, *_RADIUS_RANGE)

    @field_validator("blur")
    @classmethod
    def _bounded_blur(cls, v: int) -> int:
        return _clamp(v, *_BLUR_RANGE)

    @field_validator("agent_name", "agent_subtitle")
    @classmethod
    def _bounded_text(cls, v: str) -> str:
        return (v or "").strip()[:60]

    @field_validator("agent_avatar_url")
    @classmethod
    def _safe_avatar(cls, v: str) -> str:
        return _safe_image_url(v)

    @field_validator("team_avatar_urls")
    @classmethod
    def _safe_team(cls, v: list[str]) -> list[str]:
        return [u for u in (_safe_image_url(x) for x in (v or [])) if u][:3]

    # -- rendering ---------------------------------------------------------

    def tokens(self) -> dict[str, str]:
        """Render to the ``--pawbar-*`` map the widget injects.

        Only keys the owner actually set are emitted. A token the appearance
        does not carry is ABSENT rather than restated at its default, so the
        widget's own stylesheet stays the single source of the base look and a
        retune there reaches every site that never overrode it.

        Nothing here interpolates a stored string into a value. Colors are
        re-emitted from the validated hex, lengths are formatted from clamped
        ints, and the font is looked up in a fixed table by key.
        """
        out: dict[str, str] = {}
        if self.accent:
            out["--pawbar-accent"] = self.accent
        out["--pawbar-radius"] = f"{self.radius}px"
        out["--pawbar-blur"] = f"{self.blur}px"
        out["--pawbar-font"] = FONT_STACKS.get(self.font, FONT_STACKS["system"])

        hero = self.hero
        out["--pawbar-hero-height"] = f"{hero.height}px"
        if hero.style == "image" and hero.image_url:
            # The quotes are ours and the url cannot contain one (validated), so
            # this literal cannot be escaped out of.
            out["--pawbar-hero-image"] = f'url("{hero.image_url}")'
        if hero.from_color:
            out["--pawbar-hero-from"] = hero.from_color
        if hero.to_color:
            # A solid hero is a gradient whose stops match — one code path in the
            # widget rather than a second background rule to keep in sync.
            out["--pawbar-hero-to"] = (
                hero.from_color if hero.style == "solid" and hero.from_color else hero.to_color
            )

        duration, easing, travel = _MOTION.get(self.motion.preset, _MOTION["spring"])
        out["--pawbar-duration"] = f"{duration}ms"
        out["--pawbar-duration-fast"] = f"{max(0, duration // 2)}ms"
        out["--pawbar-duration-slow"] = f"{int(duration * 1.75)}ms"
        out["--pawbar-ease-spring"] = easing
        out["--pawbar-motion-scale"] = travel
        return out
