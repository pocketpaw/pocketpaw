# Link-unfurl (Open Graph preview) response schema.
# Created: 2026-06-10 — frozen wire contract for GET /api/v1/unfurl so the
#   paw-enterprise composer can render OG previews (title/description/image)
#   for pasted URLs. All metadata fields are nullable: a 200 with every field
#   null is valid (the page carried no usable OG/Twitter/<title> tags).
# 2026-09-26 (feat/unfurl-richer-previews) — two additive, nullable fields so
#   chat can render Discord/WhatsApp-style embeds: ``theme_color`` (the page's
#   <meta name="theme-color">, hex only, drives the embed's accent stripe) and
#   ``large_image`` (hero image vs small side thumbnail, from twitter:card and
#   og:image:width). Old clients ignore them; nothing existing changed.

from __future__ import annotations

from pydantic import BaseModel


class UnfurlResponse(BaseModel):
    """Open Graph / link-preview metadata for a single URL.

    ``url`` is the final URL after redirects. The remaining fields are the
    scraped metadata, each null when the page did not provide it.
    """

    url: str
    title: str | None = None
    description: str | None = None
    image: str | None = None
    site_name: str | None = None
    favicon: str | None = None
    # "#rgb" / "#rrggbb" only — anything else is dropped, so a client can put
    # it straight into a style without sanitising.
    theme_color: str | None = None
    # True: render `image` as a full-width hero. False: a small thumbnail.
    # Null when there is no image.
    large_image: bool | None = None
