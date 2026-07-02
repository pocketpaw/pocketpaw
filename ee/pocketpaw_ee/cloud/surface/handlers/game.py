# game.py — /game surface preamble.
#
# Created: 2026-07-02 (feat/game-surface) — Orients the chat agent when the
# user is on the /game surface (describe→compose a living world). Without it
# the surface falls back to GENERIC and the agent builds a dashboard pocket
# instead of composing a world — the same drift the /studio preamble was
# created to fix. Static orientation — no live data to fake.
#
# Mirrors the layout of handlers/studio.py: an async ``build_preamble``
# returning an XML-ish ``<surface>`` + ``<orientation>`` + ``<procedure>``
# block. The procedure PREFERS the bundled ``game`` skill (invoked by intent —
# no slash command), and points the fallback at the in-process game MCP tool
# (``create_game_world``). Tool errors are relayed plainly so the agent never
# fakes a created world.
#
# The /game SurfaceProfile sets ``ripple_mode="off"`` (see surface_registry.py)
# so the agent does not inherit the ~20k-char "default to ui-spec" ripple LAW
# and build a dashboard instead of composing a world.

from __future__ import annotations

from pocketpaw_ee.cloud.surface.domain import SurfaceMeta


async def build_preamble(workspace_id: str, user_id: str, meta: SurfaceMeta) -> str:
    """Render the /game surface preamble — describe→compose a living world."""
    route = meta.route_path or "/game"
    return (
        f'<surface kind="game" route="{route}" />\n'
        "<game-orientation>\n"
        "The user is on the GAME surface, a CREATION canvas. They describe a "
        "living world — a vibe, a place, a cast — and you COMPOSE it: the "
        'world becomes a Pocket of type="game" with a small cast of NPCs, '
        "zones, and seven feel dials set from the vibe. The NPCs are Souls — "
        "they carry persistent memory, relationships, and grudges, so the "
        "world remembers what happens in it. This surface is creation-first, "
        "NOT play-first: the deliverable is the composed world, not a play "
        "session. This is also NOT a dashboard — do not build KPI widgets, "
        "charts, or a ui-spec. Talk about the work as 'the world', 'the "
        "cast', or 'the game' — never as a 'pocket' or 'dashboard'.\n"
        "</game-orientation>\n"
        "<game-procedure>\n"
        "Treat the user's message on this surface as a request to COMPOSE a "
        "living world. PREFER the `game` skill — invoke it by intent (no "
        "slash command needed); it owns the vibe→dials→world flow, the dial "
        "preset table, and the foreground-cast rule (3-6 Souls). If that "
        "skill is unavailable, fall back directly to the game MCP tool: call "
        "`mcp__pocketpaw_game__create_game_world` (args: `name`, `vibe`, and "
        "a `world_spec` object carrying `cast` (3-6 Souls: name, archetype, "
        "persona, ocean), `zones`, and optionally `dials` — omit `dials` and "
        "the tool fills them from the vibe preset). The tool composes and "
        "persists the world deterministically; you do NOT hand-build widgets "
        "and you do NOT use the pocket specialist.\n"
        "If the tool returns ok=false, relay its error message PLAINLY (e.g. "
        "an invalid world_spec, or a plan that doesn't include /game) — NEVER "
        "claim a phantom world was created. After it succeeds, briefly "
        "describe the world that now exists: its cast, its zones, its mood.\n"
        "</game-procedure>"
    )


__all__ = ["build_preamble"]
