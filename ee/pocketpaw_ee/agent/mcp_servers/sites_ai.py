# ee/pocketpaw_ee/agent/mcp_servers/sites_ai.py — the /sites agent's site AI tools.
#
# Three tools on the ``pocketpaw_sites_manager`` server (registered in sites.py,
# ids in SITES_TOOL_IDS so the /sites allow-list keeps them):
#   get_site_ai_settings   settings, budget (caps + spend) and presets of one site,
#                          plus ``how_to``: the build guidance for AI features.
#   set_site_ai_settings   turn AI on, browser access, the preset rule, allowed models,
#                          and push the "set a budget" card (SSE
#                          ``site_ai_budget_requested``) so the owner picks caps.
#   upsert_site_ai_preset  create or update a preset (system prompt, model, caps).
# The writes need ``sites.ai_manage`` (ADMIN), checked against the chatting user;
# a denial comes back as ``{ok: false, denied: true}``. The agent never sets money:
# caps and transcripts are the owner's, on the card or in Site settings > AI.
#
# The pocketpaw-site skill is at its byte cap, so the AI-apps guidance lives in
# ``HOW_TO`` (returned by get_site_ai_settings) and in the tool descriptions.

from __future__ import annotations

import logging
from typing import Any

from ._audit import record_tool_call

logger = logging.getLogger(__name__)

SERVER_NAME = "pocketpaw_sites_manager"
GET_SITE_AI_SETTINGS_TOOL_ID = f"mcp__{SERVER_NAME}__get_site_ai_settings"
SET_SITE_AI_SETTINGS_TOOL_ID = f"mcp__{SERVER_NAME}__set_site_ai_settings"
UPSERT_SITE_AI_PRESET_TOOL_ID = f"mcp__{SERVER_NAME}__upsert_site_ai_preset"
SITE_AI_TOOL_IDS = (
    GET_SITE_AI_SETTINGS_TOOL_ID,
    SET_SITE_AI_SETTINGS_TOOL_ID,
    UPSERT_SITE_AI_PRESET_TOOL_ID,
)

HOW_TO = (
    "AI features call Paw's AI API; never put a provider key in code or ask for one. "
    "Default: call AI from the site's server code. Apply the `paw-ai` recipe and use "
    "`pawAi(env).chat({model, messages, tools?, response_format?})` or `.stream(...)` "
    "(OpenAI-shaped; model is an alias: fast, smart or vision; `embed` via .embed). "
    "The platform sets env.PAW_GATEWAY_TOKEN, PAW_API_BASE and PAW_SITE_ID. Tool calls "
    "come back to your code to run; Paw never runs them. Browser calls (`Paw.ai.chat` / "
    "`Paw.ai.stream` in paw.js) only when the app truly needs them: turn on "
    "browser_access with set_site_ai_settings, prefer a preset (upsert_site_ai_preset) "
    "so visitors can't change the system prompt, and keep secrets out of the page. "
    "Every AI screen needs a friendly resting state for `budget_exhausted` / "
    "`unavailable` (the site's AI budget ran out or is paused): never a raw error. "
    "Free sites get AI in the draft preview only."
)


def _error_response(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def _success_response(body: dict[str, Any]) -> dict[str, Any]:
    import json

    return {"content": [{"type": "text", "text": json.dumps(body, default=str)}]}


def _identity() -> tuple[str | None, str | None]:
    try:
        from pocketpaw_ee.cloud.chat.agent_service import current_user_id, current_workspace_id

        return current_workspace_id(), current_user_id()
    except Exception:  # noqa: BLE001
        return None, None


async def _start(tool: str, args: dict) -> tuple[str, str, str, dict | None]:
    """(workspace, user, pocket, error). Records the call and checks the plan."""
    from pocketpaw_ee.agent.mcp_servers.sites_create import _require_sites_plan_or_error

    workspace_id, user_id = _identity()
    pocket_id = args.get("pocket_id")
    if not workspace_id or not user_id:
        return "", "", "", _error_response(f"{tool} needs a cloud chat session.")
    record_tool_call(
        workspace_id=workspace_id,
        user_id=user_id,
        pocket_id=pocket_id if isinstance(pocket_id, str) else None,
        tool_server="pocketpaw_sites",
        tool_name=f"_{tool}",
        status="ok",
        ok=True,
    )
    if not isinstance(pocket_id, str) or not pocket_id.strip():
        return "", "", "", _error_response(f"{tool} requires a `pocket_id`.")
    if (gate := await _require_sites_plan_or_error(workspace_id, tool=tool)) is not None:
        return "", "", "", gate
    return workspace_id, user_id, pocket_id.strip(), None


async def _load_user(user_id: str) -> Any | None:
    """The User doc ``check_workspace_action`` reads memberships from (test seam)."""
    from beanie import PydanticObjectId

    from pocketpaw_ee.cloud.models.user import User

    try:
        return await User.get(PydanticObjectId(user_id))
    except Exception:  # noqa: BLE001 - a malformed id is no user
        return None


async def _gate_manage(tool: str, workspace_id: str, user_id: str) -> dict | None:
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.guards.deps import check_workspace_action
    from pocketpaw_ee.guards.rbac import Forbidden

    user = await _load_user(user_id)
    if user is None:
        return _error_response(f"{tool} could not resolve the calling user.")
    try:
        await check_workspace_action(user, workspace_id, "sites.ai_manage")
    except (CloudError, Forbidden) as exc:
        return _success_response(
            {
                "ok": False,
                "denied": True,
                "code": getattr(exc, "code", "forbidden"),
                "message": "Changing a site's AI settings needs a workspace admin. "
                "Ask an admin to turn it on in Site settings > AI.",
            }
        )
    return None


def _push_budget_card(pocket_id: str, budget: Any, purpose: str) -> None:
    """Per-run SSE so the chat renders the "set a budget" card. Best effort."""
    try:
        from pocketpaw_ee.cloud.chat.agent_service import push_sse_event

        push_sse_event(
            "site_ai_budget_requested",
            {"pocket_id": pocket_id, "purpose": purpose, "budget": budget.model_dump(mode="json")},
        )
    except Exception:  # noqa: BLE001
        logger.debug("set_site_ai_settings: SSE push failed (non-fatal)", exc_info=True)


async def _get_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.site_ai import service

    workspace_id, _user_id, pocket_id, err = await _start("get_site_ai_settings", args)
    if err is not None:
        return err
    try:
        settings = await service.get_settings(workspace_id, pocket_id)
        budget = await service.get_budget(workspace_id, pocket_id)
        presets = await service.list_presets(workspace_id, pocket_id)
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    return _success_response(
        {
            "ok": True,
            "pocket_id": pocket_id,
            "settings": settings.model_dump(mode="json"),
            "budget": budget.model_dump(mode="json"),
            "presets": [
                {"id": p.id, "name": p.name, "model": p.model, "version": p.version}
                for p in presets
            ],
            "how_to": HOW_TO,
        }
    )


async def _set_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.site_ai import service
    from pocketpaw_ee.site_ai.dto import SiteAiSettingsPut

    tool = "set_site_ai_settings"
    workspace_id, user_id, pocket_id, err = await _start(tool, args)
    if err is not None:
        return err
    if (denied := await _gate_manage(tool, workspace_id, user_id)) is not None:
        return denied
    fields = ("enabled", "browser_access", "require_preset_for_browser", "allowed_models")
    try:
        body = SiteAiSettingsPut.model_validate({k: args[k] for k in fields if k in args})
        settings = await service.put_settings(workspace_id, user_id, pocket_id, body)
        budget = await service.get_budget(workspace_id, pocket_id)
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    except ValueError as exc:
        return _error_response(f"invalid settings: {exc}")
    purpose = str(args.get("purpose") or "").strip()[:200]
    _push_budget_card(pocket_id, budget, purpose)
    return _success_response(
        {
            "ok": True,
            "pocket_id": pocket_id,
            "settings": settings.model_dump(mode="json"),
            "message": "Saved. A card in chat asks the owner to confirm the AI budget "
            "(daily and monthly caps); calls stop at the cap. " + HOW_TO,
        }
    )


async def _upsert_handler(args: dict) -> dict:
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.site_ai import service
    from pocketpaw_ee.site_ai.dto import SiteAiPresetBody

    tool = "upsert_site_ai_preset"
    workspace_id, user_id, pocket_id, err = await _start(tool, args)
    if err is not None:
        return err
    if (denied := await _gate_manage(tool, workspace_id, user_id)) is not None:
        return denied
    fields = ("name", "system_prompt", "model", "max_output_tokens", "temperature")
    preset_id = args.get("preset_id")
    try:
        body = SiteAiPresetBody.model_validate({k: args[k] for k in fields if k in args})
        if isinstance(preset_id, str) and preset_id.strip():
            preset = await service.update_preset(
                workspace_id, user_id, pocket_id, preset_id.strip(), body
            )
        else:
            preset = await service.create_preset(workspace_id, user_id, pocket_id, body)
    except CloudError as exc:
        return _error_response(f"{exc.code}: {exc.message}")
    except ValueError as exc:
        return _error_response(f"invalid preset: {exc}")
    return _success_response(
        {
            "ok": True,
            "pocket_id": pocket_id,
            "preset": {"id": preset.id, "name": preset.name, "version": preset.version},
            "message": f'Preset saved. Send `preset: "{preset.id}"` in chat calls; its '
            "system prompt, model and caps replace what the caller sends.",
        }
    )


_POCKET = {"type": "string", "description": "Id of the site pocket."}
_MODELS = {"type": "array", "items": {"type": "string", "enum": ["fast", "smart", "vision"]}}


def make_site_ai_tools(tool: Any) -> list[Any]:
    """Build the three site AI SDK tools for the sites manager server."""

    @tool(
        "get_site_ai_settings",
        (
            "Read a site's AI settings (on/off, browser access, preset rule, allowed "
            "models, plan models), its budget (caps and spend in micro-credits; 1 credit "
            "= 1,000,000 micro = $0.01) and presets. Call this before building any AI "
            "feature (chat, assistant, extraction, writing tool, vision): its `how_to` "
            "says how to call AI from the site."
        ),
        {
            "type": "object",
            "properties": {"pocket_id": _POCKET},
            "required": ["pocket_id"],
            "additionalProperties": False,
        },
    )
    async def get_site_ai_settings(args):  # type: ignore[no-untyped-def]
        return await _get_handler(args)

    @tool(
        "set_site_ai_settings",
        (
            "Change a site's AI settings and show the owner a card to confirm the AI "
            "budget. `browser_access` lets the page call AI directly (off by default; "
            "server-side calls with pawAi(env) need no switch); "
            "`require_preset_for_browser` makes browser calls name a preset; "
            "`allowed_models` is a subset of the plan's fast/smart/vision. `purpose` is "
            "one sentence the owner reads on the budget card. You cannot set caps or "
            "transcripts: the owner does."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": _POCKET,
                "enabled": {"type": "boolean"},
                "browser_access": {"type": "boolean"},
                "require_preset_for_browser": {"type": "boolean"},
                "allowed_models": _MODELS,
                "purpose": {"type": "string", "description": "What the AI feature does."},
            },
            "required": ["pocket_id", "purpose"],
            "additionalProperties": False,
        },
    )
    async def set_site_ai_settings(args):  # type: ignore[no-untyped-def]
        return await _set_handler(args)

    @tool(
        "upsert_site_ai_preset",
        (
            "Create (no preset_id) or update a site AI preset: a saved system prompt, "
            "model alias and output cap that callers reference by id. Use one for every "
            "browser-side AI feature so visitors can't replace the instructions; Worker "
            "code may use one too. Returns the preset id to send as `preset`."
        ),
        {
            "type": "object",
            "properties": {
                "pocket_id": _POCKET,
                "preset_id": {"type": "string", "description": "Omit to create."},
                "name": {"type": "string"},
                "system_prompt": {"type": "string"},
                "model": {"type": "string", "enum": ["fast", "smart", "vision"]},
                "max_output_tokens": {"type": "integer", "minimum": 1, "maximum": 32000},
                "temperature": {"type": "number", "minimum": 0, "maximum": 2},
            },
            "required": ["pocket_id", "name", "system_prompt"],
            "additionalProperties": False,
        },
    )
    async def upsert_site_ai_preset(args):  # type: ignore[no-untyped-def]
        return await _upsert_handler(args)

    return [get_site_ai_settings, set_site_ai_settings, upsert_site_ai_preset]


__all__ = [
    "GET_SITE_AI_SETTINGS_TOOL_ID",
    "HOW_TO",
    "SET_SITE_AI_SETTINGS_TOOL_ID",
    "SITE_AI_TOOL_IDS",
    "UPSERT_SITE_AI_PRESET_TOOL_ID",
    "make_site_ai_tools",
]
