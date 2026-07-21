# service.py — Plan a project from a prompt, and compose it (CS-1).
#
# Created 2026-07-21 (feat/codescaffold). Two operations, deliberately split:
#
#   plan(prompt)      pure, instant, side-effect free — what we INTEND to build
#   compose(recipes)  shells the vendored engine — the actual source map
#
# The split exists so the user gets a confirmation step. Composition writes a
# whole project's worth of code; showing "I'll set up auth and a database,
# because you said 'sign-in'" and letting them edit that list before anything
# happens is the difference between a tool and a slot machine. It also means the
# expensive half never runs for a prompt that was misread.
#
# There is no persistence here and no project row. Composition returns a source
# map to the caller; CS-2 is what materializes one into a runtime. Keeping this
# stateless means the same endpoint serves a Daytona VM and an in-tab
# WebContainer without knowing which asked.
from __future__ import annotations

import logging

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.codescaffold import domain, engine
from pocketpaw_ee.cloud.codescaffold.dto import (
    RecipeChoice,
    ScaffoldComposeRequest,
    ScaffoldComposeResponse,
    ScaffoldPlanRequest,
    ScaffoldPlanResponse,
    ScaffoldRequirements,
)

logger = logging.getLogger(__name__)


async def plan(
    workspace_id: str,
    user_id: str,
    body: ScaffoldPlanRequest | dict,
) -> ScaffoldPlanResponse:
    """Turn a prompt into an intended project. Pure — no engine, no VM, no writes.

    Tenancy is carried for logging and metering; there is no resource to
    authorize against, because nothing is read or created.
    """
    body = ScaffoldPlanRequest.model_validate(body)

    matches = domain.match_recipes(body.prompt)
    recipe_ids = [m.id for m in matches]

    logger.debug(
        "codescaffold.plan ws=%s user=%s recipes=%s",
        workspace_id,
        user_id,
        recipe_ids,
    )

    requires = domain.requirements_for(recipe_ids)

    return ScaffoldPlanResponse(
        starter=domain.STARTER,
        projectName=domain.derive_project_name(body.prompt),
        recipes=[
            RecipeChoice(
                id=m.id,
                capability=domain.BY_ID[m.id].capability,
                summary=domain.BY_ID[m.id].summary,
                why=m.reason,
            )
            for m in matches
        ],
        secrets=domain.secrets_for(recipe_ids),
        requires=ScaffoldRequirements(
            install=requires.install,
            nativeToolchain=requires.nativeToolchain,
            rawSockets=requires.rawSockets,
            reasons=requires.reasons,
        ),
    )


async def compose(
    workspace_id: str,
    user_id: str,
    body: ScaffoldComposeRequest | dict,
) -> ScaffoldComposeResponse:
    """Compose the base template plus the requested recipes into a source map.

    Validates the recipe ids against the catalog HERE rather than leaving it to
    the engine. The engine would also refuse, but this is a user-supplied list
    reaching a subprocess argv — checking it against a known set before it gets
    there means an unknown id is a 400 with a helpful message instead of a
    subprocess round trip and a 422.
    """
    body = ScaffoldComposeRequest.model_validate(body)

    unknown = [r for r in body.recipes if r not in domain.BY_ID]
    if unknown:
        raise CloudError(
            400,
            "codescaffold.unknown_recipe",
            f"Unknown recipe(s): {', '.join(sorted(unknown))}",
        )

    # De-duplicate while preserving the caller's order. The engine tolerates
    # repeats (recipes are idempotent), but a repeated id in the applied order
    # would read as a bug to anyone looking at the response.
    requested: list[str] = []
    for rid in body.recipes:
        if rid not in requested:
            requested.append(rid)

    payload = await engine.compose(requested)

    files: dict[str, str] = payload["files"]
    order: list[str] = payload.get("order", [])

    logger.info(
        "codescaffold.compose ws=%s user=%s recipes=%s files=%d",
        workspace_id,
        user_id,
        order,
        len(files),
    )

    return ScaffoldComposeResponse(
        starter=domain.STARTER,
        projectName=body.projectName or domain.FALLBACK_PROJECT_NAME,
        order=order,
        # The ENGINE's secret list, not the catalog's. They should agree, and
        # the tests assert they do — but the engine's is derived from the
        # manifests it actually applied, which is the one that would be right if
        # they ever drifted.
        secrets=payload.get("secrets", []),
        files=files,
        fileCount=len(files),
    )


__all__ = ["compose", "plan"]
