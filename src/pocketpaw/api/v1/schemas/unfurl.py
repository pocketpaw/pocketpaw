# Link-unfurl (Open Graph preview) response schema.
# Created: 2026-06-10 — frozen wire contract for GET /api/v1/unfurl so the
#   paw-enterprise composer can render OG previews (title/description/image)
#   for pasted URLs. All metadata fields are nullable: a 200 with every field
#   null is valid (the page carried no usable OG/Twitter/<title> tags).

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
