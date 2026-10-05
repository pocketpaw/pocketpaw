# src/pocketpaw/paw_bar/appearance.py — the owner's Paw Bar appearance.
#
# The owner edits this in paw-enterprise; the frame renders it into the
# ``--pawbar-*`` maps the widget layers as ``tokens`` and ``tokensDark``.
#
# FOLLOW THE SITE BY DEFAULT. The bar reads the host website's own look (accent,
# page background and text, font, button radius; paw-bar lib/site-theme.ts) and
# layers: bar defaults < site theme < ``tokens`` < ``tokensDark``. So every token
# emitted here OVERRIDES the site. A facet the owner has not set therefore emits
# nothing: ``accent`` "" , ``font`` "site", ``radius`` None and every ``colors``
# field "" all mean "follow the site". ``sites.migrate_appearance_follow_site``
# moved rows still on the old defaults (#3b6fe0 / system / 20) to these.
#
# Token names are the ones the bar reads: ``colors.surface`` becomes
# ``--pawbar-bg`` + ``--pawbar-frame-bg`` at the bar's own glass alpha,
# ``colors.ink`` ``--pawbar-fg`` + ``--pawbar-frame-fg``, the bubbles
# ``--pawbar-bubble-bg`` / ``--pawbar-owner-bubble-bg``. Fields the bar has no
# surface for (hero, motion, unread, line/wash strength, surface opacity) are
# still accepted and stored, because paw-enterprise still writes them, but they
# render nothing.
#
# SECURITY: every value ends up as the right-hand side of a CSS custom property
# in a document the widget serves, so an unvalidated value is a style injection
# and a URL is an exfiltration channel. Nothing is passed through: colours are
# re-emitted from parsed components, lengths are clamped ints formatted here,
# fonts come from a fixed roster, URLs must be https or a small base64 raster
# data: image (SVG is refused because it can carry script).
#
# Two colour sets: ``accent`` + ``colors`` are the light (or pinned) palette,
# ``accent_dark`` + ``colors_dark`` apply when the bar resolves dark. A dark field
# left "" falls back to its light value (so with light following, dark follows).

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

# An owner font is chosen from fixed stacks rather than typed, which is why
# ``font`` is an enum and not a string. ``FONT_SITE`` ("site", the default) emits
# no font token, so the bar wears the host page's own font.
FONT_SITE = "site"
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
# How the closed bar sits on the page: the docked bar, or a round icon in the
# corner ``position`` names.
LAUNCHER_STYLES = frozenset({"bar", "icon"})
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
# How large the bar renders. The frame emits it as ``barSize``; the widget owns
# what each step measures.
BAR_SIZES = frozenset({"sm", "md", "lg"})
HERO_STYLES = frozenset({"gradient", "solid", "image"})
# Motion presets. Stored for the editor; the bar owns its own motion and always
# honours the visitor's reduced-motion setting, so nothing renders from these.
MOTION_PRESETS = frozenset({"none", "subtle", "lively", "expressive"})

# The bar's own glass alpha for a solid page colour (paw-bar lib/site-theme.ts
# PILL_ALPHA / FRAME_ALPHA). An owner ``colors.surface`` is applied the same way
# a detected site background is, so the two look alike.
_PILL_ALPHA_PCT = 78
_FRAME_ALPHA_PCT = {"light": 82, "dark": 55}


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(int(value), high))


# Inline images are stored on the Site doc and written into the frame HTML, so
# they are capped. Raster only: an SVG data URL is a document that can run script.
_DATA_URL_MAX_CHARS = 200_000
_DATA_IMAGE_RE = re.compile(r"^data:image/(?:png|jpeg|webp|gif);base64,[A-Za-z0-9+/]+={0,2}$")


def _safe_image_url(value: str) -> str:
    """An https:// URL or a small raster data:image/ URL, or "".

    http:// is refused rather than upgraded: the bar renders on the owner's own
    https site, so a plain-http asset is a mixed-content block in every browser
    — accepting it would store a value that can only ever fail. A data: URL must
    be base64 png, jpeg, webp or gif and at most ``_DATA_URL_MAX_CHARS`` long.
    Everything else (javascript:, vbscript:, file:, //host, data:image/svg+xml)
    is refused outright.
    """
    v = (value or "").strip()
    if not v:
        return ""
    lowered = v.lower()
    if lowered.startswith("https://"):
        # Nothing may terminate the url() token and start a new declaration.
        return "" if any(c in v for c in "()\"'\\ \n\r\t;") else v
    if lowered.startswith("data:image/"):
        # The whole value must be a raster base64 payload. That grammar has no
        # quote, paren, backslash or whitespace, so it cannot leave url().
        if len(v) > _DATA_URL_MAX_CHARS:
            return ""
        return v if _DATA_IMAGE_RE.match(v) else ""
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


def _legible_ink(base: tuple[int, int, int]) -> tuple[int, int, int]:
    """Type that reads on ``base``: near-black on a light ground, near-white on a
    dark one. Not pure black or white, which read harsher than the ground deserves.

    Used for a surface the owner set without an ink (a light surface with the bar's
    own light type left alone is white on white) and for the visitor bubble's text.
    """
    dark = _luminance(base) < 0.5
    return _mix(_WHITE if dark else _BLACK, base, 0.06)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class LauncherAppearance(BaseModel):
    # "bar" or "icon". See LAUNCHER_STYLES.
    style: str = "bar"
    position: str = "bottom-right"
    label: str = ""
    icon_url: str = ""

    @field_validator("style")
    @classmethod
    def _known_style(cls, v: str) -> str:
        return v if v in LAUNCHER_STYLES else "bar"

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

    ALL OF THE HEX FIELDS DEFAULT TO "", AND "" MEANS "FOLLOW THE SITE". A token
    we do not emit is left to the site theme the loader detected, and under that
    to the bar's own defaults; an emitted one overrides both.
    """

    # The page ground: ``--pawbar-bg`` (the pill) and ``--pawbar-frame-bg`` (the
    # thread) at the bar's own glass alpha. With no ``ink`` it also sets a legible
    # type colour, since a light ground under the bar's light dark-mode type is
    # unreadable and no colour picker can warn about that.
    surface: str = ""
    # Stored, renders nothing: the bar's glass alpha is fixed.
    surface_opacity: int = 86
    # Type: ``--pawbar-fg`` and ``--pawbar-frame-fg``. Set explicitly it wins over
    # the one derived from ``surface``.
    ink: str = ""
    accent_fg: str = ""
    # The three speakers. "" leaves each deriving from the accent.
    user_bubble: str = ""
    assistant_bubble: str = ""
    owner_bubble: str = ""
    ring: str = ""
    # Stored, renders nothing: the bar has no unread badge.
    unread: str = ""
    danger: str = ""
    # Stored, render nothing: the bar derives its hairlines and washes itself.
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

    def over(self, light: ColorAppearance) -> ColorAppearance:
        """This set with every field it leaves unset taken from ``light``.

        Hex fields are unset when "". The numbers have no "unset" value, so one
        still at its default counts as unset: a dark palette nobody touched then
        inherits the light numbers instead of quietly resetting them.
        """
        merged: dict[str, object] = {}
        for name, field in type(self).model_fields.items():
            value = getattr(self, name)
            unset = value == "" if isinstance(value, str) else value == field.default
            merged[name] = getattr(light, name) if unset else value
        return type(self)(**merged)

    def tokens(self, scheme: str = "light") -> dict[str, str]:
        """Render to ``--pawbar-*``. Only what the owner actually named.

        ``scheme`` picks the frame's glass alpha for ``surface`` ("light" for the
        ``tokens`` map, "dark" for ``tokensDark``), as the bar does for a site bg.
        """
        out: dict[str, str] = {}

        if self.surface:
            base = _hex_to_rgb(self.surface)
            out["--pawbar-bg"] = _rgba(base, _PILL_ALPHA_PCT)
            out["--pawbar-frame-bg"] = _rgba(base, _FRAME_ALPHA_PCT.get(scheme, 82))
            ink = _rgba(_legible_ink(base), 100)
            out["--pawbar-fg"] = ink
            out["--pawbar-frame-fg"] = ink
        if self.ink:
            # An explicit ink beats the one derived above.
            ink = _rgba(_hex_to_rgb(self.ink), 100)
            out["--pawbar-fg"] = ink
            out["--pawbar-frame-fg"] = ink
        if self.user_bubble:
            bubble = _hex_to_rgb(self.user_bubble)
            out["--pawbar-bubble-bg"] = _rgba(bubble, 100)
            # The bar's own bubble text flips with the scheme, so an owner colour
            # needs text chosen for it or it is unreadable in one of the two.
            out["--pawbar-bubble-fg"] = _rgba(_legible_ink(bubble), 100)

        for token, value in (
            ("--pawbar-accent-fg", self.accent_fg),
            ("--pawbar-assistant-bubble", self.assistant_bubble),
            ("--pawbar-owner-bubble-bg", self.owner_bubble),
            ("--pawbar-ring", self.ring),
            ("--pawbar-danger", self.danger),
        ):
            if value:
                out[token] = _rgba(_hex_to_rgb(value), 100)
        return out


class ConciergeAppearance(BaseModel):
    """The owner's full Paw Bar appearance. Every look field defaults to
    following the website (no token), so a fresh concierge wears the site's look."""

    # "" = follow the site's accent. A #rgb / #rrggbb overrides it.
    accent: str = ""
    surface_mode: str = "auto"
    # How the docked bar rests: a narrow pill that widens on hover, or the full
    # composer at all times. See BAR_RESTING.
    bar_resting: str = "compact"
    # Small, medium or large. See BAR_SIZES.
    size: str = "sm"
    # None = follow the site's button radius. A number (clamped 0-32) overrides it.
    radius: int | None = None
    blur: int = 28
    # "site" = follow the site's font. A FONT_STACKS key overrides it.
    font: str = FONT_SITE
    # "Powered by Paw Sites" under the bar. Hiding it is plan-gated like the site
    # badge: the settings PATCH refuses False with a 402 on a site that is not
    # entitled, and the frame emits ``poweredBy`` as this OR not entitled.
    show_branding: bool = True
    # Who the visitor is talking to. Rendered in the conversation header and the
    # Messages list; "" falls back to the widget's own generic copy.
    agent_name: str = ""
    agent_subtitle: str = ""
    agent_avatar_url: str = ""
    # Team faces on the Home card. Capped at 3 — the card shows three.
    team_avatar_urls: list[str] = Field(default_factory=list)
    # The owner's logo, shown by the widget where it brands the bar. https or a
    # small raster data: URL (see _safe_image_url); "" means none.
    logo_url: str = ""
    # The dark palette. "" / untouched fields fall back to ``accent`` and
    # ``colors``; see ``tokens_dark``.
    accent_dark: str = ""

    launcher: LauncherAppearance = Field(default_factory=LauncherAppearance)
    hero: HeroAppearance = Field(default_factory=HeroAppearance)
    motion: MotionAppearance = Field(default_factory=MotionAppearance)
    colors: ColorAppearance = Field(default_factory=ColorAppearance)
    colors_dark: ColorAppearance = Field(default_factory=ColorAppearance)

    @field_validator("accent", "accent_dark")
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

    @field_validator("size")
    @classmethod
    def _known_size(cls, v: str) -> str:
        return v if v in BAR_SIZES else "sm"

    @field_validator("font")
    @classmethod
    def _known_font(cls, v: str) -> str:
        return v if v in FONT_STACKS else FONT_SITE

    @field_validator("radius")
    @classmethod
    def _bounded_radius(cls, v: int | None) -> int | None:
        return None if v is None else _clamp(v, *_RADIUS_RANGE)

    @field_validator("blur")
    @classmethod
    def _bounded_blur(cls, v: int) -> int:
        return _clamp(v, *_BLUR_RANGE)

    @field_validator("agent_name", "agent_subtitle")
    @classmethod
    def _bounded_text(cls, v: str) -> str:
        return (v or "").strip()[:60]

    @field_validator("agent_avatar_url", "logo_url")
    @classmethod
    def _safe_avatar(cls, v: str) -> str:
        return _safe_image_url(v)

    @field_validator("team_avatar_urls")
    @classmethod
    def _safe_team(cls, v: list[str]) -> list[str]:
        return [u for u in (_safe_image_url(x) for x in (v or [])) if u][:3]

    # -- rendering ---------------------------------------------------------

    def tokens(self) -> dict[str, str]:
        """Render to the ``--pawbar-*`` map the widget layers over the site theme.

        Only facets the owner set are emitted. An absent token is left to the
        site's detected look, and under that to the bar's own defaults.

        Nothing here interpolates a stored string into a value. Colors are
        re-emitted from the validated hex, lengths are formatted from clamped
        ints, and the font is looked up in a fixed table by key.
        """
        return self._render(self.accent, self.colors, "light")

    def tokens_dark(self) -> dict[str, str]:
        """The same map as ``tokens()``, rendered with the dark palette.

        The widget applies it over ``tokens`` whenever the bar resolves dark. Any
        dark field left unset falls back to its light value; the one difference
        from ``tokens()`` for an untouched dark set is the frame's glass alpha
        under an owner ``surface`` (the bar uses a lighter glass in dark).
        """
        return self._render(
            self.accent_dark or self.accent, self.colors_dark.over(self.colors), "dark"
        )

    def _render(self, accent: str, colors: ColorAppearance, scheme: str) -> dict[str, str]:
        out: dict[str, str] = {}
        if accent:
            out["--pawbar-accent"] = accent
        if self.radius is not None:
            out["--pawbar-radius"] = f"{self.radius}px"
        out["--pawbar-blur"] = f"{self.blur}px"
        if self.font in FONT_STACKS:
            out["--pawbar-font"] = FONT_STACKS[self.font]
        # The owner's named colours. Last, so an explicitly-named token wins.
        out.update(colors.tokens(scheme))
        return out
