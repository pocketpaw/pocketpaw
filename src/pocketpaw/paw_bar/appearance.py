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
# How opaque the panel ground is over an unknown host page. The floor is not 0:
# a fully transparent surface is not a "clear" widget, it is unreadable text
# floating over someone else's photograph, and the blur cannot rescue it.
_SURFACE_OPACITY_RANGE = (55, 100)
# Hairlines and hover washes, as a percentage of ink. 0 is allowed on both --
# "no borders at all" is a legitimate look -- but the ceiling is well short of
# 100, where a hairline stops being a hairline.
_LINE_STRENGTH_RANGE = (0, 30)
_WASH_STRENGTH_RANGE = (0, 20)

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
# "auto" (2026-08-22) means FOLLOW THE CUSTOMER'S OWN SITE, and it is the new
# default for a reason that is really a bug report: this field never reached the
# widget at all. The frame emitted it as ``theme``, and the widget stopped
# reading ``theme`` on 2026-08-19 (the one-theme change) in favour of ``scheme``,
# which nothing ever sent. So every bar has been resolving light-or-dark from the
# host page regardless of what its owner picked -- which is exactly what "auto"
# means. Defaulting to it keeps every existing bar looking identical while the
# setting starts working for the owners who set it deliberately.
SURFACE_MODES = frozenset({"dark", "light", "auto"})
# How the docked bar rests. "compact" is a narrow pill that widens to the full
# composer on hover or focus; "full" is the whole-width bar. A visitor on a
# coarse pointer gets the full bar either way -- the widget will not hand a
# touch device a control that only opens with a gesture it cannot make.
BAR_RESTING = frozenset({"full", "compact"})
HERO_STYLES = frozenset({"gradient", "solid", "image"})
# Motion presets. "none" is not the same as the visitor's reduced-motion
# setting: this is the OWNER choosing a calmer bar for everyone, while the
# visitor's OS preference always wins on top of it (see tokens.css).
MOTION_PRESETS = frozenset({"none", "subtle", "lively", "expressive"})

# Per-preset (duration_ms, easing, travel_scale). Authored here rather than in
# CSS so the owner's choice is one stored word instead of five stored numbers,
# and so a preset can be retuned for every existing site at once.
# Every curve is a pure DECELERATION. An overshoot ("ease-out-back",
# cubic-bezier(0.34, 1.56, …)) sat here first and was wrong for this surface:
# real objects decelerate, this widget renders on somebody else's website where
# a bouncing panel reads as a toy, and the visitor came to ask a question rather
# than watch the chrome arrive. The presets differ in duration and travel, which
# is what "more motion" should mean, not in how much they wobble.
#
# The preset was called "spring" and is now "lively" — a name that promised
# overshoot while the curve no longer delivers it is a worse lie than a plain
# label. Renamed before anything shipped, so no stored value has to migrate.
_MOTION: dict[str, tuple[int, str, str]] = {
    "none": (0, "linear", "0"),
    "subtle": (160, "cubic-bezier(0.16, 1, 0.3, 1)", "1"),
    "lively": (240, "cubic-bezier(0.22, 1, 0.36, 1)", "1"),
    "expressive": (360, "cubic-bezier(0.22, 1, 0.36, 1)", "1.35"),
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
# Colour maths
# ---------------------------------------------------------------------------
#
# Everything below PARSES a validated hex into three ints and re-emits a literal
# this module builds character by character. No stored string reaches the
# stylesheet, which is the same posture the rest of the file keeps -- it just
# has more arithmetic in it now.


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    """`#abc` or `#aabbcc` -> (r, g, b). Assumes _HEX_RE has already passed."""
    v = value.lstrip("#")
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _luminance(rgb: tuple[int, int, int]) -> float:
    """Perceived lightness, 0 (black) to 1 (white).

    The sRGB coefficients rather than a plain mean, because a plain mean calls
    pure blue and pure yellow equally light and then picks white type for both.
    """
    r, g, b = (c / 255 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _mix(
    rgb: tuple[int, int, int], toward: tuple[int, int, int], amount: float
) -> tuple[int, int, int]:
    """Move `rgb` a fraction of the way toward another colour."""
    return tuple(  # type: ignore[return-value]
        _clamp(round(c + (t - c) * amount), 0, 255) for c, t in zip(rgb, toward)
    )


_WHITE = (255, 255, 255)
_BLACK = (0, 0, 0)


def _rgba(rgb: tuple[int, int, int], opacity_pct: int) -> str:
    """An `rgba(r, g, b, a)` literal we assemble from ints."""
    r, g, b = rgb
    alpha = _clamp(opacity_pct, 0, 100) / 100
    return f"rgba({r}, {g}, {b}, {alpha:g})"


def _surface_scale(base_hex: str, opacity: int) -> dict[str, str]:
    """The four surface steps plus a legible ink, from ONE owner colour.

    This exists because of a footgun documented in the widget's own tokens.css:
    a light `--pawbar-surface` with everything else left alone is white type on
    a white panel. The scale needs five values that agree with each other, and
    an owner picking a brand colour out of a swatch has no way to know that.

    So they pick the panel, and the direction is derived. "Raised" means lighter
    than the panel on a dark widget and whiter-still on a light one; "sunken" is
    the opposite; and the ink flips to whichever end of the range stays readable
    on the ground they chose. One decision in, a coherent widget out.
    """
    base = _hex_to_rgb(base_hex)
    dark = _luminance(base) < 0.5
    lift, drop = (_WHITE, _BLACK) if dark else (_BLACK, _WHITE)
    return {
        "--pawbar-surface": _rgba(base, opacity),
        # Popovers must OCCLUDE what is behind them, so this one is deliberately
        # close to opaque whatever the owner chose for the panel.
        "--pawbar-surface-strong": _rgba(_mix(base, lift, 0.06), max(opacity, 94)),
        "--pawbar-surface-raised": _rgba(_mix(base, lift, 0.12), min(100, opacity + 6)),
        "--pawbar-surface-sunken": _rgba(_mix(base, drop, 0.10), max(40, opacity - 30)),
        # Not pure white or pure black: both read as a harsher widget than the
        # surface underneath them deserves, and the near-values still clear
        # contrast comfortably at the opacities above.
        "--pawbar-ink": _rgba(_mix(_WHITE if dark else _BLACK, base, 0.06), 100),
    }


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
    preset: str = "lively"
    # The visitor's OS setting always wins regardless; this lets an owner ALSO
    # calm the bar for everyone. Off means the owner has opted out of honouring
    # it, which we do not offer — the field exists so the editor can show the
    # guarantee, and it is pinned True.
    honor_reduced_motion: bool = True

    @field_validator("preset")
    @classmethod
    def _known_preset(cls, v: str) -> str:
        return v if v in MOTION_PRESETS else "lively"

    @field_validator("honor_reduced_motion")
    @classmethod
    def _always_honored(cls, v: bool) -> bool:
        # Not a settable choice. A widget that ignores prefers-reduced-motion is
        # an accessibility defect on somebody else's website, and the owner does
        # not get to make that trade on their visitors' behalf.
        return True


class ColorAppearance(BaseModel):
    """Every colour in the widget the owner may name, beyond the accent.

    ALL OF THE HEX FIELDS DEFAULT TO "", AND "" MEANS "THE WIDGET DECIDES". That
    is the same rule the whole token map follows: a token we do not emit is a
    token the widget's own stylesheet still owns, so a later retune of the base
    scale reaches every site that never overrode it. Emitting defaults here
    would freeze every site to whatever the values were on the day they saved.

    Why this exists at all: the accent was the ONLY colour an owner could set,
    and the bubbles, the ring and the hero did not follow it — three literals in
    tokens.css that happened to spell the same blue. So "change my brand colour"
    produced a widget that disagreed with itself. The widget side of that is
    fixed (they derive from the accent now); this is the other half, for an
    owner who wants to name them individually.
    """

    # The panel ground. Setting THIS is the big one — see `_surface_scale`,
    # which derives the other three surface steps and a legible ink from it,
    # because a light surface with everything else left alone is white type on
    # a white panel and no colour picker can warn you about that.
    surface: str = ""
    surface_opacity: int = 86
    # Type and hairlines. Left "" it follows the surface; set explicitly it
    # wins, for an owner who wants warm-grey type on a near-black panel.
    ink: str = ""
    accent_fg: str = ""
    # The three speakers. "" leaves each deriving from the accent.
    user_bubble: str = ""
    assistant_bubble: str = ""
    owner_bubble: str = ""
    ring: str = ""
    # Deliberately separate from the accent, and this is the one place that
    # matters most: an unread count re-skinned to a calm brand colour stops
    # reading as "something needs you".
    unread: str = ""
    danger: str = ""
    # How present the hairlines and hover washes are, as a percentage of ink.
    line_strength: int = 11
    wash_strength: int = 5

    @field_validator(
        "surface",
        "ink",
        "accent_fg",
        "user_bubble",
        "assistant_bubble",
        "owner_bubble",
        "ring",
        "unread",
        "danger",
    )
    @classmethod
    def _hex_only(cls, v: str) -> str:
        v = (v or "").strip()
        return v if _HEX_RE.match(v) else ""

    @field_validator("surface_opacity")
    @classmethod
    def _bounded_opacity(cls, v: int) -> int:
        return _clamp(v, *_SURFACE_OPACITY_RANGE)

    @field_validator("line_strength")
    @classmethod
    def _bounded_line(cls, v: int) -> int:
        return _clamp(v, *_LINE_STRENGTH_RANGE)

    @field_validator("wash_strength")
    @classmethod
    def _bounded_wash(cls, v: int) -> int:
        return _clamp(v, *_WASH_STRENGTH_RANGE)

    def tokens(self) -> dict[str, str]:
        """Render to ``--pawbar-*``. Only what the owner actually named."""
        out: dict[str, str] = {}

        if self.surface:
            # One colour in, a coherent five-token palette out.
            out.update(_surface_scale(self.surface, self.surface_opacity))
        if self.ink:
            # An explicit ink beats the one derived above, which is why this
            # runs second rather than inside the branch.
            out["--pawbar-ink"] = _rgba(_hex_to_rgb(self.ink), 100)

        for token, value in (
            ("--pawbar-accent-fg", self.accent_fg),
            ("--pawbar-user-bubble", self.user_bubble),
            ("--pawbar-assistant-bubble", self.assistant_bubble),
            ("--pawbar-owner-bubble", self.owner_bubble),
            ("--pawbar-ring", self.ring),
            ("--pawbar-unread", self.unread),
            ("--pawbar-danger", self.danger),
        ):
            if value:
                out[token] = _rgba(_hex_to_rgb(value), 100)

        # Always emitted: these are numbers with a working default rather than
        # overrides, and the widget multiplies both to reach its second step.
        out["--pawbar-line-strength"] = f"{self.line_strength}%"
        out["--pawbar-wash-strength"] = f"{self.wash_strength}%"
        return out


class ConciergeAppearance(BaseModel):
    """The owner's full Paw Bar appearance. Every field defaults to today's look."""

    accent: str = "#3b6fe0"
    surface_mode: str = "auto"
    # How the docked bar rests: a narrow pill that widens on hover, or the full
    # composer at all times. See BAR_RESTING.
    bar_resting: str = "compact"
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
    colors: ColorAppearance = Field(default_factory=ColorAppearance)

    @field_validator("accent")
    @classmethod
    def _hex_accent(cls, v: str) -> str:
        v = (v or "").strip()
        return v if _HEX_RE.match(v) else ""

    @field_validator("surface_mode")
    @classmethod
    def _known_mode(cls, v: str) -> str:
        return v if v in SURFACE_MODES else "auto"

    @field_validator("bar_resting")
    @classmethod
    def _known_resting(cls, v: str) -> str:
        return v if v in BAR_RESTING else "compact"

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

        # The owner's named colours. Last, so an explicitly-named token wins over
        # anything derived above it.
        out.update(self.colors.tokens())

        duration, easing, travel = _MOTION.get(self.motion.preset, _MOTION["lively"])
        out["--pawbar-duration"] = f"{duration}ms"
        out["--pawbar-duration-fast"] = f"{max(0, duration // 2)}ms"
        out["--pawbar-duration-slow"] = f"{int(duration * 1.75)}ms"
        out["--pawbar-ease-emphasis"] = easing
        out["--pawbar-motion-scale"] = travel
        return out
