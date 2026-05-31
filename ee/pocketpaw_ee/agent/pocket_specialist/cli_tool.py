"""CLI shell command for backends that don't speak MCP (codex_cli,
opencode, gemini_cli, copilot_sdk).

Registered in ``src/pocketpaw/tools/cli.py`` as ``cloud_pocket_specialist_create``.
The dispatcher (``_run_cloud_handler``) hands the handler a single ``dict``
of args and JSON-encodes whatever dict the handler returns, so this module
returns a dict — not a JSON string.

Args dict shape::

    {
        "brief": "Natural-language pocket description",   # required
        "hints": {"name": "...", "color": "..."},         # optional
        "workspace_id": "ws-...",                          # optional fallback
        "user_id":      "user-...",                        # optional fallback
    }

Workspace / user identity is normally read from the per-stream ContextVar
accessors in ``ee.cloud.chat.agent_service`` (same approach as the MCP
tool in Task 9). For non-cloud-chat callers we accept ``workspace_id`` /
``user_id`` directly in the args — this matches the env-var fallback the
existing ``_cloud_list_pockets`` uses.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from pocketpaw.config import get_settings
from pocketpaw_ee.agent.pocket_specialist.runtime import (
    PocketSpecialistCreateInput,
    PocketSpecialistHints,
    run_specialist,
)
from pocketpaw_ee.cloud.chat.agent_service import (
    current_user_id,
    current_workspace_id,
)

log = logging.getLogger(__name__)


async def _cloud_pocket_specialist_create(args: dict[str, Any]) -> dict[str, Any]:
    """Run the pocket specialist from a CLI shell call.

    Returns a dict matching ``PocketSpecialistCreateOutput.model_dump()`` on
    success, or ``{"ok": False, "error": "..."}`` on failure.
    """
    brief = args.get("brief", "")
    raw_hints = args.get("hints")
    raw_spec = args.get("spec")

    workspace_id = (
        current_workspace_id()
        or args.get("workspace_id")
        or os.environ.get("POCKETPAW_WORKSPACE_ID", "")
    )
    user_id = current_user_id() or args.get("user_id") or os.environ.get("POCKETPAW_USER_ID", "")
    if not workspace_id or not user_id:
        return {
            "ok": False,
            "error": (
                "workspace_id / user_id missing — pocket_specialist_create "
                "requires workspace and user context. Either run from a "
                "cloud chat session or pass workspace_id + user_id in the "
                "args (or via POCKETPAW_WORKSPACE_ID / POCKETPAW_USER_ID)."
            ),
        }

    hints = PocketSpecialistHints(**raw_hints) if raw_hints else None

    try:
        payload = PocketSpecialistCreateInput(
            brief=brief,
            hints=hints,
            spec=raw_spec if isinstance(raw_spec, dict) else None,
        )
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError surfaces here
        return {"ok": False, "error": f"invalid input: {exc}"}

    try:
        out = await run_specialist(
            payload,
            workspace_id=workspace_id,
            user_id=user_id,
            settings=get_settings(),
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("pocket specialist run failed")
        return {"ok": False, "error": str(exc)}

    return out.model_dump()


__all__ = ["_cloud_pocket_specialist_create"]
