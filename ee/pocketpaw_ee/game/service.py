# ee/pocketpaw_ee/game/service.py — Game-world composition service. Sole owner
# of the deterministic create path for Pocket type="game".
#
# Created: 2026-07-02 (feat/game-surface, PW-2) — mirrors the Paw Sites
# deterministic-create flow end to end: the chat agent provides the WORLD SPEC
# (cast of Souls, zones, optional dials) and this service validates it, fills
# missing dials from the v0 vibe→dials preset table, and persists the pocket
# DIRECTLY via ``pockets.service.agent_create`` stamped ``type="game"`` +
# ``pattern="living-world"`` — NO pocket_specialist, NO draft/redraft loop, NO
# subagent. The world spec rides the pocket's ``rippleSpec`` payload verbatim
# (the same place ripple-track sites carry theirs; the normalizer preserves
# non-UI sibling keys, proven by the dynamic-sites flow), with ``trusted=True``
# because the spec carries no widget tree for the catalog gate to inspect.
#
# ``require_game_plan`` mirrors ``sites.service.require_sites_plan``: the
# shared plan gate for the in-process game write path, reading the SAME plan
# source (``workspace_service.get_workspace_plan``) and the SAME feature table
# (``guards.abac.PLAN_FEATURES``) so a free-plan caller is denied identically
# to the Sites create path. The ``game`` feature sits on the same tiers as
# ``studio`` (go+).

from __future__ import annotations

import logging
from typing import Any

from pocketpaw_ee.cloud._core.errors import (
    Forbidden,
    Internal,
    NotFound,
    ValidationError,
)

logger = logging.getLogger(__name__)

_GAME_PLAN_FEATURE = "game"

# The seven feel dials every game world carries, 0.0–1.0 each. JUICE (the
# feedback/feel layer) is platform-provided — deliberately NOT a dial here.
WORLD_DIAL_KEYS: tuple[str, ...] = (
    "challenge",  # how hard the world pushes back (friction, stakes)
    "progress",  # how visibly effort compounds (growth, unlocks)
    "choice",  # how much the player steers (autonomy, forks that matter)
    "bonds",  # how deep Soul relationships run / how much they remember
    "mark",  # how permanently player actions change the world
    "pulse",  # the world's tempo and pressure (pacing, urgency)
    "spark",  # novelty and surprise (secrets, curiosity)
)

# v0 vibe→dials preset table. The bundled ``game`` skill ships the SAME table;
# the service applies it when ``world_spec`` omits ``dials`` so a skill-less
# caller (or a spec authored without dials) still lands a fully-dialed world.
# Keys are matched as SUBSTRINGS of the lowercased vibe, in this dict's
# insertion order (first hit wins); no hit → BALANCED_DIALS.
VIBE_DIAL_PRESETS: dict[str, dict[str, float]] = {
    "cozy": {
        "challenge": 0.2,
        "progress": 0.5,
        "choice": 0.6,
        "bonds": 0.9,
        "mark": 0.7,
        "pulse": 0.3,
        "spark": 0.5,
    },
    "tense": {
        "challenge": 0.8,
        "progress": 0.6,
        "choice": 0.5,
        "bonds": 0.4,
        "mark": 0.5,
        "pulse": 0.9,
        "spark": 0.4,
    },
    "mystery": {
        "challenge": 0.6,
        "progress": 0.7,
        "choice": 0.7,
        "bonds": 0.5,
        "mark": 0.4,
        "pulse": 0.5,
        "spark": 0.9,
    },
    "sandbox": {
        "challenge": 0.4,
        "progress": 0.4,
        "choice": 0.9,
        "bonds": 0.5,
        "mark": 0.8,
        "pulse": 0.3,
        "spark": 0.7,
    },
}

# The unknown-vibe default: dead center on every dial.
BALANCED_DIALS: dict[str, float] = {k: 0.5 for k in WORLD_DIAL_KEYS}


def resolve_dials(vibe: str) -> dict[str, float]:
    """Resolve the v0 dial preset for a free-text ``vibe``.

    Deterministic: the first ``VIBE_DIAL_PRESETS`` key (insertion order) that
    appears as a substring of the lowercased vibe wins; no hit → a copy of
    ``BALANCED_DIALS``. Always returns a fresh dict so callers can overlay
    author-provided dials without mutating the table.
    """
    lowered = (vibe or "").lower()
    for key, preset in VIBE_DIAL_PRESETS.items():
        if key in lowered:
            return dict(preset)
    return dict(BALANCED_DIALS)


def validate_world_spec(spec: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems with a ``world_spec`` (empty
    list = valid). Fails the create CLOSED with an actionable message rather
    than persisting a spec the world runtime can't wake up. Pure — no
    identity / Mongo needed (mirrors ``sites_create._validate_dynamic_spec``).

    The minimum contract for a living world: a non-empty ``cast`` of Souls
    (each a dict with a non-empty string ``name``; ``ocean`` when present must
    be a dict), a non-empty ``zones`` list of strings, and — when provided —
    ``dials`` restricted to the seven known keys with numeric values in
    [0, 1]. ``dials`` may be omitted entirely (the create fills them from the
    vibe preset). The 3–6 Souls foreground-cast rule stays skill guidance,
    not a hard gate.
    """
    problems: list[str] = []

    cast = spec.get("cast")
    if not isinstance(cast, list) or not cast:
        problems.append("a `cast` array declaring at least one Soul ({name, archetype, persona})")
    else:
        for i, member in enumerate(cast):
            if not isinstance(member, dict) or not (
                isinstance(member.get("name"), str) and member["name"].strip()
            ):
                problems.append(f"cast[{i}] must be an object with a non-empty string `name`")
            elif "ocean" in member and not isinstance(member["ocean"], dict):
                problems.append(f"cast[{i}].ocean must be an object (the OCEAN sketch) when given")

    zones = spec.get("zones")
    if not isinstance(zones, list) or not zones:
        problems.append("a `zones` array naming at least one place in the world")
    else:
        for i, zone in enumerate(zones):
            if not isinstance(zone, str) or not zone.strip():
                problems.append(f"zones[{i}] must be a non-empty string")

    dials = spec.get("dials")
    if dials is not None and dials != {}:
        if not isinstance(dials, dict):
            problems.append("`dials` must be an object of the seven feel dials when given")
        else:
            unknown = sorted(set(dials) - set(WORLD_DIAL_KEYS))
            if unknown:
                problems.append(
                    f"unknown dial keys: {', '.join(unknown)} (the seven dials are "
                    f"{', '.join(WORLD_DIAL_KEYS)}; JUICE is platform-provided, not a dial)"
                )
            for key in WORLD_DIAL_KEYS:
                if key in dials:
                    value = dials[key]
                    if isinstance(value, bool) or not isinstance(value, int | float):
                        problems.append(f"dial `{key}` must be a number between 0 and 1")
                    elif not 0 <= value <= 1:
                        problems.append(f"dial `{key}` must be between 0 and 1 (got {value})")

    return problems


async def require_game_plan(workspace_id: str) -> None:
    """Raise cloud Forbidden('plan.feature_denied') unless the workspace's plan
    includes the Game ("game") feature.

    The shared plan gate for the in-process game write path (the
    ``create_game_world`` MCP handler). Reads the plan with the SAME source of
    truth (``workspace_service.get_workspace_plan``) and the SAME feature table
    (``guards.abac.PLAN_FEATURES``) as the HTTP ``require_plan_feature``
    dependency — mirroring ``sites.service.require_sites_plan`` so a free-plan
    caller is denied identically to the Sites create path. A missing workspace
    surfaces as NotFound, and the error message names the minimum plan that
    unlocks Game. Imports are local to keep the game service importable without
    eagerly pulling the cloud workspace/guards modules."""
    from pocketpaw_ee.cloud.workspace import service as workspace_service
    from pocketpaw_ee.guards.abac import PLAN_FEATURES

    plan = await workspace_service.get_workspace_plan(workspace_id)
    if plan is None:
        raise NotFound("workspace", workspace_id)
    if _GAME_PLAN_FEATURE not in PLAN_FEATURES.get(plan, set()):
        # Name the minimum plan that unlocks the feature, like the Sites gate.
        # Walks the consumer ladder cheapest-first; ``game`` lives on go+, so
        # this resolves to "Go".
        needed = next(
            (
                p
                for p in ("free", "go", "pro", "pro_max", "enterprise")
                if _GAME_PLAN_FEATURE in PLAN_FEATURES.get(p, set())
            ),
            "go",
        )
        raise Forbidden(
            "plan.feature_denied",
            f"Game worlds require the {needed.capitalize()} plan — upgrade, or "
            "switch to a workspace that has it.",
        )


async def create_game_world(
    *,
    workspace_id: str,
    user_id: str,
    name: str,
    vibe: str,
    world_spec: dict[str, Any],
) -> dict:
    """Create a living-world game pocket deterministically. Returns the wire
    pocket view dict (the same agent-view shape ``agent_create`` returns).

    ``world_spec`` shape::

        {
          "cast":  [{"name": str, "archetype": str, "persona": str,
                     "ocean": {...}}, ...],   # 3-6 Souls (skill guidance)
          "zones": ["...", ...],              # the places in the world
          "dials": {"challenge": 0-1, "progress": 0-1, "choice": 0-1,
                    "bonds": 0-1, "mark": 0-1, "pulse": 0-1, "spark": 0-1},
          "vibe":  "..."                      # optional; defaults to ``vibe``
        }

    ``dials`` is optional: omitted (or partially given) dials are filled from
    the v0 ``VIBE_DIAL_PRESETS`` table matched against ``vibe`` (unknown vibe →
    ``BALANCED_DIALS``), so every persisted world carries all seven dials.

    Persists DIRECTLY via ``pockets.service.agent_create`` stamped
    ``type_="game"`` + ``pattern="living-world"`` with the world spec as the
    ``ripple_spec`` payload — the same place ripple-track sites carry their
    spec; the normalizer adds its envelope keys and PRESERVES the world keys
    (cast/zones/dials/vibe) as siblings, exactly as the dynamic-sites blocks
    ride through. ``trusted=True`` matches the sites deterministic path: the
    spec is validated here in code and carries no widget tree for the strict
    catalog gate to inspect (the logged catalog walk + embed audit still run).

    Raises ``ValidationError`` on a malformed spec/inputs and ``Internal`` when
    the persist fails. The plan gate (``require_game_plan``) is run by the MCP
    handler before this, mirroring the sites create handlers.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValidationError("game.invalid_input", "name is required — the world's name")
    if not isinstance(vibe, str) or not vibe.strip():
        raise ValidationError(
            "game.invalid_input", "vibe is required — the one-line feel of the world"
        )
    if not isinstance(world_spec, dict) or not world_spec:
        raise ValidationError(
            "game.invalid_input", "world_spec is required — the cast/zones/dials object"
        )

    problems = validate_world_spec(world_spec)
    if problems:
        raise ValidationError(
            "game.invalid_world_spec",
            "world_spec is not a valid living world — it needs " + "; ".join(problems),
        )

    # Fill the dials: author-provided values win per dial; the vibe preset
    # (or the balanced default) supplies the rest, so the persisted world
    # always carries all seven.
    provided_raw = world_spec.get("dials")
    provided: dict[str, Any] = provided_raw if isinstance(provided_raw, dict) else {}
    dials = {**resolve_dials(vibe), **provided}

    spec = {**world_spec, "dials": dials, "vibe": world_spec.get("vibe") or vibe}

    # Persist DIRECTLY through the pockets service — NO pocket_specialist, NO
    # draft/redraft loop (the sites deterministic-create regime). agent_create
    # emits PocketCreated, so this write is evented at the chokepoint.
    from pocketpaw_ee.cloud.pockets.service import agent_create

    view, pocket_id, err = await agent_create(
        workspace_id=workspace_id,
        owner_id=user_id,
        name=name.strip(),
        description=f"A living world — {spec['vibe']}",
        type_="game",
        pattern="living-world",
        ripple_spec=spec,
        trusted=True,
    )
    if err is not None or view is None or pocket_id is None:
        raise Internal("game.create_failed", f"create failed: {err or 'no view returned'}")
    return view


__all__ = [
    "BALANCED_DIALS",
    "VIBE_DIAL_PRESETS",
    "WORLD_DIAL_KEYS",
    "create_game_world",
    "require_game_plan",
    "resolve_dials",
    "validate_world_spec",
]
