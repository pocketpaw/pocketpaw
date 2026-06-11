# studio.py — /studio surface preamble.
#
# Created: 2026-06-10 (feat/studio-code-migration) — Orients the chat agent when
# the user is on the /studio surface (describe→generate media: image + video).
# Without it the surface falls back to GENERIC and the agent builds a dashboard
# pocket instead of generating media — the same drift the /sites preamble was
# created to fix. Static orientation — no live data to fake.
#
# Mirrors the layout of handlers/sites.py: an async ``build_preamble`` returning
# an XML-ish ``<surface>`` + ``<orientation>`` + ``<procedure>`` block. The
# procedure PREFERS the bundled ``studio`` skill (invoked by intent — no slash
# command), and points the fallback at the in-process media MCP tools
# (``mcp__pocketpaw_media__image_generate`` / ``__video_generate``). Provider /
# key errors are relayed plainly so the agent never fakes a generated asset.
#
# The /studio SurfaceProfile sets ``ripple_mode="off"`` (see service.py) so the
# agent does not inherit the ~20k-char "default to ui-spec" ripple LAW and build
# a dashboard instead of generating media.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the /studio surface preamble — describe→generate media."""
    route = meta.route_path or "/studio"
    return (
        f'<surface kind="studio" route="{route}" />\n'
        "<studio-orientation>\n"
        "The user is on the STUDIO surface, a media-generation canvas. They "
        "describe an image or a video and you GENERATE it, then lay the result "
        "out as a tile in a gallery pocket. This is NOT a dashboard — do not "
        "build KPI widgets, charts, or a ui-spec. The deliverable is generated "
        "MEDIA (images and short videos), shown in a responsive gallery that "
        "opens on the canvas automatically. Talk about the work as 'images', "
        "'videos', 'media', or 'the gallery' — never as a 'pocket' or "
        "'dashboard'.\n"
        "</studio-orientation>\n"
        "<studio-procedure>\n"
        "Treat the user's message on this surface as a request to GENERATE "
        "media. PREFER the `studio` skill — invoke it by intent (no slash "
        "command needed); it owns the generate→gallery flow and chooses image "
        "vs video by the request. If that skill is unavailable, fall back "
        "directly to the media MCP tools: call "
        "`mcp__pocketpaw_media__image_generate` for an image / poster / "
        "illustration (args: `prompt`, optional `aspect_ratio`), or "
        "`mcp__pocketpaw_media__video_generate` for a short video / clip / "
        "animation (args: `prompt`, optional `duration`, `aspect_ratio`). Each "
        "tool produces the asset and adds it to the gallery pocket; the canvas "
        "opens automatically. Video generation is async and may take up to ~2 "
        "minutes — tell the user it is rendering.\n"
        "Relay any provider or key error PLAINLY (e.g. 'Set "
        "POCKETPAW_GOOGLE_API_KEY to enable image generation' or 'Set "
        "POCKETPAW_REPLICATE_API_TOKEN to enable video generation') — NEVER "
        "claim a phantom image or video. After it succeeds, briefly say what was "
        "added to the gallery.\n"
        "</studio-procedure>"
    )


__all__ = ["build_preamble"]
