"""Daytona context resolver — find the active workspace VM sandbox.

Resolves the ONE workspace-level Daytona VM and scopes *work_dir* to the
active project subdirectory (when a ``project_name`` is available via the
``current_project_name`` ContextVar, set by the code surface handler).

Returns ``None`` when no sandbox is found — the caller falls back to local FS.

Updated: 2026-07-10 — workspace-level VM; legacy per-project sandbox removed.
"""

from __future__ import annotations

import contextvars
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pocketpaw_ee.cloud.daytona.client import DaytonaClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Context value object
# ---------------------------------------------------------------------------


@dataclass
class DaytonaContext:
    """Resolved Daytona sandbox context for the current operation.

    Attributes:
        client: The Daytona SDK client wrapper.
        sandbox_id: The provisioned sandbox (VM) ID.
        project_dir: The sandbox's user-root directory
            (e.g. ``/home/daytona``).
        work_dir: The effective working directory. When a project is scoped,
            this is ``{workspace_root}/{project_name}/`` inside the VM.
            Otherwise the sandbox root.
        workspace_root: The VM workspace root (e.g. ``/workspace``) where
            projects live as subdirectories.
        project_name: The human-friendly project name (when scoped).
    """

    client: DaytonaClient
    sandbox_id: str
    project_dir: str
    work_dir: str
    workspace_root: str = ""
    project_name: str | None = None


# ---------------------------------------------------------------------------
# ContextVar-based identity resolution (cloud chat context)
# ---------------------------------------------------------------------------

# ContextVar for the current project name — set by the code surface handler
# when a cloud project is active. Read by resolve_daytona_context() so tools
# automatically scope to the correct project subdirectory.
_current_project_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_project_name", default=None
)


def set_current_project_name(name: str | None) -> None:
    """Set the project name for the current chat turn."""
    _current_project_name.set(name)


def get_current_project_name() -> str | None:
    """Get the project name for the current chat turn."""
    return _current_project_name.get()


def _resolve_chat_identity() -> tuple[str | None, str | None, str | None]:
    """Read workspace_id, user_id, and project_name from chat ContextVars.

    Returns ``(workspace_id, user_id, project_name)`` or
    ``(None, None, None)`` when not in a cloud chat run.
    """
    ws_id = None
    uid = None
    try:
        from pocketpaw_ee.cloud.chat.agent_service import (
            current_user_id,
            current_workspace_id,
        )

        ws_id = current_workspace_id()
        uid = current_user_id()
    except ImportError:
        pass

    pname = get_current_project_name()
    return (ws_id, uid, pname)


# ---------------------------------------------------------------------------
# Main resolver
# ---------------------------------------------------------------------------


async def resolve_daytona_context(
    workspace_id: str | None = None,
    user_id: str | None = None,
    project_name: str | None = None,
) -> DaytonaContext | None:
    """Resolve the active workspace VM sandbox for the current context.

    Args:
        workspace_id: Explicit workspace ID. Falls back to chat ContextVars.
        user_id: Explicit user ID. Falls back to chat ContextVars.
        project_name: When given, *work_dir* is scoped to the project
            subdirectory inside the sandbox VM.

    Returns:
        A ``DaytonaContext`` if a sandbox is found, or ``None``.
    """
    # ---- Resolve identity ----
    if not workspace_id or not user_id or not project_name:
        ctx_ws, ctx_uid, ctx_pname = _resolve_chat_identity()
        workspace_id = workspace_id or ctx_ws
        user_id = user_id or ctx_uid
        project_name = project_name or ctx_pname

    if not workspace_id:
        logger.debug("resolve_daytona_context: no workspace_id — no sandbox context")
        return None

    # ── Path 1: Workspace-level VM (primary) ──
    from pocketpaw_ee.cloud.daytona.store import (
        get_workspace_vm_config,
        get_workspace_vm_sandbox_id,
    )

    sandbox_id = get_workspace_vm_sandbox_id(workspace_id)
    if sandbox_id:
        from pocketpaw_ee.cloud.daytona.client import get_daytona_client

        client = get_daytona_client()
        if client is None:
            logger.debug("resolve_daytona_context: Daytona not configured")
            return None

        try:
            project_dir = await client.get_project_dir(sandbox_id)
        except Exception as exc:
            logger.warning(
                "resolve_daytona_context: failed to get sandbox dirs for %s: %s",
                sandbox_id,
                exc,
            )
            return None

        config = get_workspace_vm_config(workspace_id)
        workspace_root = config.get("root_dir", "/workspace")

        if project_name:
            work_dir = f"{workspace_root}/{project_name}".replace("//", "/")
        else:
            work_dir = project_dir

        return DaytonaContext(
            client=client,
            sandbox_id=sandbox_id,
            project_dir=project_dir,
            work_dir=work_dir,
            workspace_root=workspace_root,
            project_name=project_name,
        )

    logger.debug(
        "resolve_daytona_context: no workspace VM found for %s (project=%s)",
        workspace_id,
        project_name,
    )
    return None


async def resolve_daytona_context_for_project(
    workspace_id: str, user_id: str, project_name: str
) -> DaytonaContext | None:
    """Convenience wrapper that takes all three identity params explicitly."""
    return await resolve_daytona_context(
        workspace_id=workspace_id,
        user_id=user_id,
        project_name=project_name,
    )


__all__ = [
    "DaytonaContext",
    "resolve_daytona_context",
    "resolve_daytona_context_for_project",
    "set_current_project_name",
    "get_current_project_name",
]
