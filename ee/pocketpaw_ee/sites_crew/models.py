# pocketpaw_ee/sites_crew/models.py — the DesignBrief baton for the Paw Sites crew.
#
# Created: 2026-07-06 (SC-1 / feat/sites-crew-brief) — the pydantic v2 data
# contract a multi-stage site-authoring crew (Designer → Branding → Frontend)
# threads between stages. Pure data, no I/O and no orchestration: each stage
# reads the incoming ``DesignBrief``, enriches its layer, and hands the same
# baton forward. ``DesignSystem`` mirrors the DESIGN.md-format design-system
# layer (color scales, typography, spacing, component tokens, compiled
# ``tokens_css``) so both the ripple and svelte engines share one source of
# truth. ``StageResult`` wraps a brief with a stage outcome for the runner.

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "AssetRef",
    "Section",
    "Typography",
    "ColorScale",
    "DesignSystem",
    "Branding",
    "DesignDirection",
    "DesignBrief",
    "StageStatus",
    "StageResult",
]


class AssetRef(BaseModel):
    """A resolved asset — always a real fetchable URL, never a local path.

    ``url`` must be an ``http(s)`` URL: a stock-provider link (pexels /
    unsplash) or a ``deliver.py`` blob URL. Container paths, ``file://``, and
    bare local paths are rejected so a downstream engine never emits a broken
    or host-leaking reference.
    """

    url: str
    kind: Literal["image", "logo", "favicon", "icon", "video"]
    alt: str = ""
    source: str = ""

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, v: str) -> str:
        if not v or not (v.startswith("http://") or v.startswith("https://")):
            raise ValueError(
                "AssetRef.url must be an http(s) URL (stock provider or blob URL), "
                f"not a local/empty/file path: {v!r}"
            )
        return v


class Section(BaseModel):
    """One ordered site section with its conversion role."""

    id: str  # lowercase slug, e.g. "hero"
    role: str  # nav/hero/services/proof/pricing/cta/lead_form/footer/faq/custom
    heading: str = ""
    notes: str = ""


class Typography(BaseModel):
    """A named type style (display / heading / body / caption …)."""

    family: str
    size: str = ""
    weight: str = ""
    line_height: str = ""
    letter_spacing: str = ""


class ColorScale(BaseModel):
    """A role color scale, keyed by the conventional 50..900 steps.

    Fields are addressable both by python name (``s500``) and by the CSS-scale
    alias (``"500"``) via ``populate_by_name=True``. Every step is optional so a
    partial scale (just a few anchors) is valid.
    """

    model_config = ConfigDict(populate_by_name=True)

    s50: str | None = Field(default=None, alias="50")
    s100: str | None = Field(default=None, alias="100")
    s200: str | None = Field(default=None, alias="200")
    s300: str | None = Field(default=None, alias="300")
    s400: str | None = Field(default=None, alias="400")
    s500: str | None = Field(default=None, alias="500")
    s600: str | None = Field(default=None, alias="600")
    s700: str | None = Field(default=None, alias="700")
    s800: str | None = Field(default=None, alias="800")
    s900: str | None = Field(default=None, alias="900")


class DesignSystem(BaseModel):
    """The DESIGN.md-format design-system layer — shared by both engines.

    Carries the token surface (colors, typography, spacing, rounded, elevation,
    component tokens with states), a prose ``rationale`` (mood, do's/don'ts,
    anti-patterns), and the compiled ``tokens_css`` (CSS custom properties) that
    is the single source of truth downstream.
    """

    name: str
    colors: dict[str, ColorScale] = Field(default_factory=dict)  # primary/secondary/…
    typography: dict[str, Typography] = Field(default_factory=dict)  # display/heading/…
    spacing: dict[str, str] = Field(default_factory=dict)
    rounded: dict[str, str] = Field(default_factory=dict)
    elevation: dict[str, str] = Field(default_factory=dict)
    components: dict[str, dict[str, Any]] = Field(default_factory=dict)
    rationale: str = ""
    tokens_css: str = ""


class Branding(BaseModel):
    """The branding layer the Branding stage produces."""

    design_system: DesignSystem | None = None
    voice: str = ""
    logo_asset: AssetRef | None = None
    favicon_asset: AssetRef | None = None


class DesignDirection(BaseModel):
    """Visual direction gathered from the interview — references and mood."""

    references: list[str] = Field(default_factory=list)  # URLs
    layout_notes: str = ""
    mood: str = ""


class DesignBrief(BaseModel):
    """The top-level baton threaded through the site-authoring crew.

    The Designer stage fills goal/audience/sitemap/design_direction, Branding
    fills ``branding.design_system``, and Frontend consumes copy + assets to
    build. ``open_questions`` records what the interview did NOT resolve.
    """

    goal: str
    audience: str = ""
    engine: Literal["ripple", "svelte"] = "ripple"
    pattern: Literal["landing", "dynamic"] = "landing"
    sitemap: list[Section] = Field(default_factory=list)
    design_direction: DesignDirection = Field(default_factory=DesignDirection)
    branding: Branding = Field(default_factory=Branding)
    copy: dict[str, Any] = Field(default_factory=dict)  # section_id -> copy blocks
    asset_manifest: list[AssetRef] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)


StageStatus = Literal["ok", "skipped", "error"]


class StageResult(BaseModel):
    """The outcome of one crew stage, wrapping the (possibly enriched) brief."""

    stage: Literal["designer", "branding", "frontend"]
    status: StageStatus
    brief: DesignBrief | None = None
    notes: str = ""
    error: str = ""
