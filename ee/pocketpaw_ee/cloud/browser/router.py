# router.py — import / inspect / delete a workspace's browser storage state.
# Created: 2026-09-06 (BR-5, feat/browser-surface-profile).
#
# WHY THIS EXISTS. The /browser agent is forbidden from typing passwords (the
# ``type`` tool refuses credential fields in code) and the user cannot see the
# server-side browser to log in by hand. Without this route, /browser only works
# on public pages. With it, the user exports their OWN authenticated session
# from their own browser and imports it once; the agent gets a session that is
# already logged in and never sees a password.
#
#   PUT    /api/v1/workspaces/{id}/browser/storage-state  — import
#   GET    /api/v1/workspaces/{id}/browser/storage-state  — counts only
#   DELETE /api/v1/workspaces/{id}/browser/storage-state  — forget it
#
# THE SECURITY SHAPE, which is the point of the module:
#   * Admin-only + workspace-scoped through ``require_action("workspace.update")``
#     against the PATH workspace, the same guard the SSO config routes use. One
#     tenant cannot touch another's profile; the guard 403s before any handler
#     body runs.
#   * The imported blob is a CREDENTIAL. GET returns counts, domains and a
#     timestamp — never a value. Nothing here logs the body, and the 422 message
#     names the offending field and index but never its content.
#   * The body is read RAW and hand-validated rather than through a Pydantic
#     model: FastAPI's 422 for a model failure echoes the offending ``input``
#     back to the caller, which would put cookie values in an error response.
#   * DELETE closes the live session first, then removes the whole profile
#     directory — deleting only the JSON would leave the cookies Chromium has
#     already persisted inside the profile.

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, Request

from pocketpaw.browser import profile
from pocketpaw_ee.cloud._core.deps import require_action
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Browser"])

_PATH = "/workspaces/{workspace_id}/browser/storage-state"


def _ws(workspace_id: str) -> str:
    """Reject a workspace id that would not be a safe directory name.

    The role guard already pins the caller to this workspace, so this is
    belt-and-braces against a traversal-shaped id ever reaching the filesystem.
    """
    try:
        return profile.safe_workspace_id(workspace_id)
    except profile.InvalidStorageState as exc:
        raise ValidationError("browser.workspace_invalid", str(exc)) from None


@router.put(_PATH)
async def import_storage_state(
    workspace_id: str,
    request: Request,
    _admin=Depends(require_action("workspace.update")),
) -> dict:
    """Import a Playwright storage state or a plain cookie-export array.

    Nothing is written until the whole blob validates, so a malformed or hostile
    file leaves no trace.
    """
    workspace_id = _ws(workspace_id)
    raw = await request.body()
    if len(raw) > profile.MAX_STATE_BYTES:
        raise ValidationError(
            "browser.storage_state_too_large",
            f"Storage state is larger than {profile.MAX_STATE_BYTES} bytes.",
        )
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise ValidationError("browser.storage_state_invalid", "Body is not valid JSON.") from None

    try:
        state = profile.validate_storage_state(parsed)
    except profile.InvalidStorageState as exc:
        # ``exc`` names the field and index only — never the value.
        raise ValidationError("browser.storage_state_invalid", str(exc)) from None

    # A profile already open with the old cookies would keep using them.
    await _close_session(workspace_id)
    return profile.write_state(workspace_id, state)


@router.get(_PATH)
async def get_storage_state(
    workspace_id: str,
    _admin=Depends(require_action("workspace.update")),
) -> dict:
    """Counts, domains and an import timestamp. NEVER a cookie value."""
    summary = profile.summarize(_ws(workspace_id))
    if summary is None:
        raise NotFound("browser_storage_state", workspace_id)
    return summary


@router.delete(_PATH, status_code=204)
async def delete_storage_state(
    workspace_id: str,
    _admin=Depends(require_action("workspace.update")),
) -> None:
    """Forget the imported session and the profile that persisted its cookies."""
    workspace_id = _ws(workspace_id)
    await _close_session(workspace_id)
    profile.delete_profile(workspace_id)


async def _close_session(workspace_id: str) -> None:
    """Close this workspace's live browser, so the profile files are not held
    open and the next run rebuilds from the state on disk.

    Only reaches a browser running in THIS process. If the agent run that owns
    the browser lives in another worker, that session keeps its cookies until
    idle cleanup closes it.
    """
    from pocketpaw.browser.session import get_browser_session_manager

    try:
        await get_browser_session_manager().close_session(workspace_id)
    except Exception:  # noqa: BLE001 — never fail an import on a browser teardown
        logger.warning("could not close browser session for workspace during profile write")


__all__ = ["router"]
