# service.py — Plan a project from a prompt, and fetch it (CS-1, rewritten CS-1b).
#
# Created 2026-07-21. REWRITTEN 2026-07-22 for starters. Two operations, still
# deliberately split:
#
#   plan(prompt)      pure, instant — which framework, and what it will be called
#   compose(starter)  fetches a pinned npm tarball and returns the source map
#
# The split buys a confirmation step. Scaffolding drops a whole project on
# someone; showing "React, because you said 'react', called booking" and letting
# them change it before anything happens is the difference between a tool and a
# slot machine. It also means the network half never runs for a misread prompt.
#
# No persistence, no project row, no runtime. Composition returns a source map;
# materializing one is `websandbox.scaffold`'s job, and an import-linter contract
# keeps this module from reaching a sandbox.
from __future__ import annotations

import json
import logging

from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.codescaffold import domain, registry
from pocketpaw_ee.cloud.codescaffold.dto import (
    ScaffoldComposeRequest,
    ScaffoldComposeResponse,
    ScaffoldPlanRequest,
    ScaffoldPlanResponse,
    ScaffoldRequirements,
    ScaffoldStartersResponse,
    StarterChoice,
    StarterSummary,
)

logger = logging.getLogger(__name__)


def _source(starter: domain.Starter) -> str:
    return f"{starter.package}@{starter.version}"


def _requirements(starter: domain.Starter) -> ScaffoldRequirements:
    requires = domain.requirements_for(starter)
    return ScaffoldRequirements(
        install=requires.install,
        nativeToolchain=requires.nativeToolchain,
        rawSockets=requires.rawSockets,
        reasons=requires.reasons,
    )


def list_starters() -> ScaffoldStartersResponse:
    """The catalog, with no prompt and no network.

    Synchronous and un-tenanted on purpose: it reads a module constant, touches
    no row, and returns the same bytes for every caller. Making it `async` and
    threading a workspace through would imply per-tenant catalogs, which would be
    a lie. The ROUTER still gates it on a license and a workspace — that is about
    who may see the product surface, not about what the answer is.
    """
    return ScaffoldStartersResponse(
        starters=[
            StarterSummary(
                id=s.id,
                label=s.label,
                summary=s.summary,
                source=_source(s),
                devPort=s.dev_port,
                requires=_requirements(s),
            )
            for s in domain.STARTERS
        ],
        default=domain.DEFAULT_STARTER_ID,
    )


async def plan(
    workspace_id: str,
    user_id: str,
    body: ScaffoldPlanRequest | dict,
) -> ScaffoldPlanResponse:
    """Turn a prompt into an intended project. Pure — no download, no VM, no writes."""
    body = ScaffoldPlanRequest.model_validate(body)

    match = domain.match_starter(body.prompt)

    logger.debug(
        "codescaffold.plan ws=%s user=%s starter=%s matched=%s",
        workspace_id,
        user_id,
        match.starter.id,
        match.matched,
    )

    return ScaffoldPlanResponse(
        starter=StarterChoice(
            id=match.starter.id,
            label=match.starter.label,
            summary=match.starter.summary,
            why=match.reason,
            matched=match.matched,
            source=_source(match.starter),
        ),
        projectName=domain.derive_project_name(body.prompt),
        devPort=match.starter.dev_port,
        requires=_requirements(match.starter),
    )


def _rename_package(files: dict[str, str], project_name: str) -> None:
    """Stamp the project's name into `package.json`, in place.

    Small, and the thing whose absence made the previous implementation's
    `projectName` cosmetic: it was derived, returned, and then never written
    anywhere, so every scaffolded project was called whatever the template author
    named theirs.

    Failures are swallowed by design. A template whose package.json we cannot
    parse is a template we should still scaffold — the wrong name is a blemish,
    a refused project is a broken feature.
    """
    raw = files.get("package.json")
    if not raw or not project_name:
        return
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("codescaffold: package.json did not parse; leaving the name alone")
        return
    if not isinstance(parsed, dict):
        return
    parsed["name"] = project_name
    # Two-space indent + trailing newline: what every one of these templates
    # already uses, so the diff a user sees is one line rather than the file.
    files["package.json"] = json.dumps(parsed, indent=2) + "\n"


async def compose(
    workspace_id: str,
    user_id: str,
    body: ScaffoldComposeRequest | dict,
) -> ScaffoldComposeResponse:
    """Fetch a starter and return it as a source map.

    Validates the id against the catalog before anything is fetched: it is a
    user-supplied string that would otherwise reach a URL builder, and an unknown
    id should be a 400 that names the options rather than a failed download.
    """
    body = ScaffoldComposeRequest.model_validate(body)

    starter = domain.BY_ID.get(body.starter)
    if starter is None:
        raise CloudError(
            400,
            "codescaffold.unknown_starter",
            f"Unknown starter '{body.starter}'. Available: " + ", ".join(sorted(domain.BY_ID)),
        )

    template = await registry.fetch_template(starter)

    project_name = body.projectName or domain.FALLBACK_PROJECT_NAME
    _rename_package(template.files, project_name)

    logger.info(
        "codescaffold.compose ws=%s user=%s starter=%s files=%d",
        workspace_id,
        user_id,
        starter.id,
        template.file_count,
    )

    return ScaffoldComposeResponse(
        starter=starter.id,
        projectName=project_name,
        devPort=starter.dev_port,
        files=template.files,
        assets=template.assets,
        fileCount=template.file_count,
    )


__all__ = ["compose", "list_starters", "plan"]
