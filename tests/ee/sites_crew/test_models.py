# tests/ee/sites_crew/test_models.py — locks the DesignBrief crew-baton contract.
#
# Created: 2026-07-06 (SC-1 / feat/sites-crew-brief) — covers a minimal brief,
# a fully-populated brief with nested design system / color-scale aliases /
# sections / assets, a JSON round-trip, the AssetRef url validator, and
# StageResult carrying a brief.

from __future__ import annotations

import pytest
from pocketpaw_ee.sites_crew.models import (
    AssetRef,
    Branding,
    ColorScale,
    DesignBrief,
    DesignDirection,
    DesignSystem,
    Section,
    StageResult,
    Typography,
)
from pydantic import ValidationError


def test_minimal_brief_needs_only_goal():
    brief = DesignBrief(goal="Sell dentist appointments")
    assert brief.goal == "Sell dentist appointments"
    assert brief.engine == "ripple"
    assert brief.pattern == "landing"
    assert brief.sitemap == []
    assert isinstance(brief.design_direction, DesignDirection)
    assert isinstance(brief.branding, Branding)
    assert brief.branding.design_system is None
    assert brief.open_questions == []


def _fully_populated_brief() -> DesignBrief:
    design_system = DesignSystem(
        name="Aurora",
        colors={
            "primary": ColorScale.model_validate({"50": "#eef", "500": "#3355ff", "900": "#001"}),
            "neutral": ColorScale(s100="#f5f5f5", s900="#111111"),
        },
        typography={
            "display": Typography(family="Inter", size="3rem", weight="700"),
            "body": Typography(family="Inter", size="1rem", line_height="1.6"),
        },
        spacing={"md": "1rem", "lg": "2rem"},
        rounded={"md": "8px"},
        elevation={"card": "0 1px 3px rgba(0,0,0,.1)"},
        components={"button": {"default": {"bg": "var(--primary-500)"}}},
        rationale="Calm, clinical, trustworthy. Avoid neon.",
        tokens_css=":root{--primary-500:#3355ff;}",
    )
    return DesignBrief(
        goal="Book more cleanings",
        audience="Local families",
        engine="svelte",
        pattern="landing",
        sitemap=[
            Section(id="hero", role="hero", heading="Brighter smiles"),
            Section(id="pricing", role="pricing"),
            Section(id="footer", role="footer"),
        ],
        design_direction=DesignDirection(
            references=["https://example.com/inspo"],
            layout_notes="Single column, big hero.",
            mood="warm, clean",
        ),
        branding=Branding(
            design_system=design_system,
            voice="Friendly and reassuring",
            logo_asset=AssetRef(url="https://cdn.example.com/logo.svg", kind="logo"),
            favicon_asset=AssetRef(url="https://cdn.example.com/fav.ico", kind="favicon"),
        ),
        copy={"hero": {"headline": "Brighter smiles, closer than you think"}},
        asset_manifest=[
            AssetRef(
                url="https://images.pexels.com/photo/1.jpg",
                kind="image",
                alt="smiling patient",
                source="pexels",
            ),
        ],
        open_questions=["What are the clinic hours?"],
    )


def test_fully_populated_brief_constructs():
    brief = _fully_populated_brief()
    assert brief.engine == "svelte"
    assert len(brief.sitemap) == 3
    assert brief.sitemap[0].role == "hero"
    ds = brief.branding.design_system
    assert ds is not None
    # Color-scale aliases populate the python-named fields.
    assert ds.colors["primary"].s50 == "#eef"
    assert ds.colors["primary"].s500 == "#3355ff"
    assert ds.colors["neutral"].s900 == "#111111"
    assert ds.typography["display"].family == "Inter"
    assert brief.asset_manifest[0].source == "pexels"


def test_json_round_trip_reconstructs_equal_brief():
    brief = _fully_populated_brief()
    dumped = brief.model_dump(mode="json")
    restored = DesignBrief.model_validate(dumped)
    assert restored == brief


def test_color_scale_alias_round_trips_by_alias():
    scale = ColorScale.model_validate({"500": "#abc"})
    dumped = scale.model_dump(by_alias=True, exclude_none=True)
    assert dumped == {"500": "#abc"}
    assert ColorScale.model_validate(dumped).s500 == "#abc"


@pytest.mark.parametrize("bad_url", ["", "file:///etc/passwd", "/var/data/logo.png", "logo.png"])
def test_asset_ref_rejects_non_http_url(bad_url):
    with pytest.raises(ValidationError):
        AssetRef(url=bad_url, kind="image")


@pytest.mark.parametrize(
    "good_url",
    ["https://images.pexels.com/photo/1.jpg", "http://localhost:8000/blob/abc"],
)
def test_asset_ref_accepts_http_url(good_url):
    asset = AssetRef(url=good_url, kind="image")
    assert asset.url == good_url


def test_stage_result_carries_brief():
    brief = DesignBrief(goal="Ship a landing page")
    result = StageResult(stage="designer", status="ok", brief=brief, notes="drafted sitemap")
    assert result.stage == "designer"
    assert result.status == "ok"
    assert result.brief is not None
    assert result.brief.goal == "Ship a landing page"


def test_stage_result_error_has_no_brief():
    result = StageResult(stage="frontend", status="error", error="build failed")
    assert result.brief is None
    assert result.error == "build failed"
