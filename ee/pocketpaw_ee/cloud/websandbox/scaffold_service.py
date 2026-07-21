# scaffold_service.py — Compose a project and bring it up in a VM (CS-2).
#
# Created 2026-07-22 (feat/codescaffold-daytona). The one operation that joins
# the two halves: ask `codescaffold` for a source map, hand it to `scaffold` to
# materialize and start.
#
# The import direction is the whole point and runs ONE WAY: this module imports
# codescaffold; codescaffold imports nothing from here, and a contract enforces
# that. Composition stays runtime-blind, so CS-3 materializes the identical map
# in a browser tab without touching any of this file.
from __future__ import annotations

import logging

from pocketpaw_ee.cloud._core.errors import CloudError, ConflictError
from pocketpaw_ee.cloud.codescaffold import service as codescaffold_service
from pocketpaw_ee.cloud.daytona.client import DaytonaClient, get_daytona_client
from pocketpaw_ee.cloud.websandbox import scaffold
from pocketpaw_ee.cloud.websandbox import service as websandbox_service
from pocketpaw_ee.cloud.websandbox.constants import WEBSANDBOX_WORKDIR
from pocketpaw_ee.cloud.websandbox.dto import (
    ScaffoldIntoSandboxRequest,
    ScaffoldIntoSandboxResponse,
    ScaffoldStepResponse,
)

logger = logging.getLogger(__name__)


async def scaffold_into_sandbox(
    workspace_id: str,
    user_id: str,
    row_id: str,
    body: ScaffoldIntoSandboxRequest | dict,
    *,
    client: DaytonaClient | None = None,
    bring_up=scaffold.bring_up,  # noqa: ANN001 — DI seam; tests never touch a VM
) -> ScaffoldIntoSandboxResponse:
    """Fetch the requested starter and bring the project up in the sandbox.

    Authorization first, VM second — `authorize_sandbox` is the fail-closed
    oracle and it runs before anything is uploaded, exactly as the git and edit
    paths do.

    The starter is fetched BEFORE the VM is touched. A download that fails
    should fail without having half-written a project into somebody's workspace.
    """
    body = ScaffoldIntoSandboxRequest.model_validate(body)

    # Mirrors `git._require_client` / `preview._require_client`: an unconfigured
    # runtime is a clean 503, never a crash on a None.
    resolved = client if client is not None else get_daytona_client()
    if resolved is None:
        raise CloudError(
            503, "websandbox.daytona_unavailable", "The sandbox runtime is not configured"
        )

    row = await websandbox_service.get_sandbox(workspace_id, user_id, row_id)
    if not row.sandbox_id:
        raise ConflictError("websandbox.not_ready", "Sandbox is not provisioned yet")
    await websandbox_service.authorize_sandbox(workspace_id, user_id, row.sandbox_id)

    composed = await codescaffold_service.compose(
        workspace_id,
        user_id,
        {"starter": body.starter, "projectName": body.projectName},
    )

    result = await bring_up(
        resolved,
        row.sandbox_id,
        composed.files,
        WEBSANDBOX_WORKDIR,
        # The starter's own port, not a global default: Next serves on 3000 and
        # Vite on 5173, and a preview pane pointed at the wrong one shows
        # nothing with no indication why.
        port=body.port or composed.devPort,
        assets=composed.assets,
    )

    failed = result.failed_step
    logger.info(
        "websandbox.scaffold ws=%s row=%s starter=%s files=%d running=%s failed=%s",
        workspace_id,
        row_id,
        composed.starter,
        composed.fileCount,
        result.running,
        failed.name if failed else None,
    )

    return ScaffoldIntoSandboxResponse(
        projectName=composed.projectName,
        starter=composed.starter,
        fileCount=composed.fileCount,
        port=result.port,
        running=result.running,
        steps=[
            ScaffoldStepResponse(
                name=s.name,
                ok=s.ok,
                exitCode=s.exitCode,
                output=s.output,
                durationMs=s.durationMs,
            )
            for s in result.steps
        ],
        failedStep=failed.name if failed else None,
    )


__all__ = ["scaffold_into_sandbox"]
