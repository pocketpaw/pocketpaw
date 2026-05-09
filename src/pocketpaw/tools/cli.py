# Tool CLI dispatcher — allows agent to call any builtin tool via Bash.
#
# Updated: 2026-02-17 — added health_check, error_log, config_doctor tools
# Updated: 2026-03-27 — added add_widget, remove_widget tools
#
# Usage:
#   python -m pocketpaw.tools.cli <tool_name> '<json_args>'
#   python -m pocketpaw.tools.cli --list
#
# Examples:
#   python -m pocketpaw.tools.cli gmail_search '{"query": "is:unread"}'
#   python -m pocketpaw.tools.cli text_to_speech '{"text": "Hello world"}'
#   python -m pocketpaw.tools.cli health_check '{"include_connectivity": true}'

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from pocketpaw.tools.builtin import (
    AddWidgetTool,
    CalendarCreateTool,
    CalendarListTool,
    CalendarPrepTool,
    ClearSessionTool,
    ConfigDoctorTool,
    ConnectorActionsTool,
    ConnectorConnectTool,
    ConnectorExecuteTool,
    ConnectorListTool,
    CreatePocketTool,
    CreateSkillTool,
    DelegateToClaudeCodeTool,
    DeleteSessionTool,
    DiscordCLITool,
    DocsCreateTool,
    DocsReadTool,
    DocsSearchTool,
    DriveDownloadTool,
    DriveListTool,
    DriveShareTool,
    DriveUploadTool,
    ErrorLogTool,
    ForgetTool,
    GmailBatchModifyTool,
    GmailCreateLabelTool,
    GmailListLabelsTool,
    GmailModifyTool,
    GmailReadTool,
    GmailSearchTool,
    GmailSendTool,
    GmailTrashTool,
    HealthCheckTool,
    ImageGenerateTool,
    ListSessionsTool,
    NewSessionTool,
    OCRTool,
    OpenExplorerTool,
    RecallTool,
    RedditReadTool,
    RedditSearchTool,
    RedditTrendingTool,
    RememberTool,
    RemoveWidgetTool,
    RenameSessionTool,
    ResearchTool,
    SpeechToTextTool,
    SpotifyNowPlayingTool,
    SpotifyPlaybackTool,
    SpotifyPlaylistTool,
    SpotifySearchTool,
    SwitchSessionTool,
    TextToSpeechTool,
    TranslateTool,
    UrlExtractTool,
    WebSearchTool,
)

# All tools available via CLI (excluding shell/filesystem — those are SDK built-in)
_TOOLS = {
    t.name: t
    for t in [
        RememberTool(),
        RecallTool(),
        ForgetTool(),
        GmailSearchTool(),
        GmailReadTool(),
        GmailSendTool(),
        GmailListLabelsTool(),
        GmailCreateLabelTool(),
        GmailModifyTool(),
        GmailTrashTool(),
        GmailBatchModifyTool(),
        CalendarListTool(),
        CalendarCreateTool(),
        CalendarPrepTool(),
        WebSearchTool(),
        UrlExtractTool(),
        ImageGenerateTool(),
        TextToSpeechTool(),
        ResearchTool(),
        CreateSkillTool(),
        DelegateToClaudeCodeTool(),
        NewSessionTool(),
        ListSessionsTool(),
        SwitchSessionTool(),
        ClearSessionTool(),
        RenameSessionTool(),
        DeleteSessionTool(),
        SpeechToTextTool(),
        DriveListTool(),
        DriveDownloadTool(),
        DriveUploadTool(),
        DriveShareTool(),
        DocsReadTool(),
        DocsCreateTool(),
        DocsSearchTool(),
        SpotifySearchTool(),
        SpotifyNowPlayingTool(),
        SpotifyPlaybackTool(),
        SpotifyPlaylistTool(),
        OCRTool(),
        TranslateTool(),
        RedditSearchTool(),
        RedditReadTool(),
        RedditTrendingTool(),
        HealthCheckTool(),
        ErrorLogTool(),
        ConfigDoctorTool(),
        OpenExplorerTool(),
        DiscordCLITool(),
        CreatePocketTool(),
        AddWidgetTool(),
        RemoveWidgetTool(),
        ConnectorListTool(),
        ConnectorActionsTool(),
        ConnectorConnectTool(),
        ConnectorExecuteTool(),
    ]
}


# ── Cloud pocket commands ──
#
# These wrap the cloud-mode write helpers in
# ``ee.cloud.pockets.agent_context``. They write to MongoDB (whereas the
# legacy ``add_widget`` / ``create_pocket`` / ``remove_widget`` tools above
# only emit a "mutation instruction" string for the local desktop frontend
# to apply). Subprocess agents — Codex, gemini-cli, opencode — invoke
# these via the shell tool to perform real cloud-side pocket edits.
#
# Per-turn context comes from env vars set by the agent backend:
#
#   POCKETPAW_MONGO_URI       Mongo connection string (required)
#   POCKETPAW_WORKSPACE_ID    Workspace owning the pocket (required for
#                             cloud_list_pockets; ignored elsewhere)
#   POCKETPAW_USER_ID         Owner used for cloud_list_pockets scoping
#   POCKETPAW_SESSION_ID      Mongo ObjectId of the chat session
#   POCKETPAW_POCKET_ID       Default pocket id when the JSON body omits
#                             one (saves the agent a round-trip)
#
# The dispatcher boots Beanie + the realtime EventBus on first cloud_*
# call in this process and leaves them open for subsequent calls in the
# same invocation (cheap when the agent batches multiple edits).
#
# About the bus: ``ee.cloud.pockets.service`` ends every mutation with
# ``await emit(PocketUpdated(...))``. That emit asserts a bus is wired
# even when there are no subscribers — hence we call ``init_realtime()``
# here too. The bus we install in the subprocess has its own (empty)
# connection manager, so emits succeed but don't reach the parent
# FastAPI process's WebSocket clients. paw-enterprise won't see the
# mutation through realtime push for CLI-driven edits — the user sees
# the agent's chat reply and refreshes (or the parent re-emits when it
# observes the cloud_* tool result; the latter is a follow-up).
async def _ensure_cloud_runtime_initialized() -> None:
    import os

    from ee.cloud.shared.db import get_client, init_cloud_db

    if get_client() is None:
        mongo_uri = os.environ.get("POCKETPAW_MONGO_URI") or os.environ.get("CLOUD_MONGODB_URI")
        if not mongo_uri:
            raise RuntimeError(
                "POCKETPAW_MONGO_URI / CLOUD_MONGODB_URI not set — the agent "
                "backend must export one before spawning the cloud_* CLI command."
            )
        await init_cloud_db(mongo_uri)

    # init_realtime is idempotent; safe to call after every Beanie boot.
    from ee.cloud import init_realtime

    init_realtime()


# Backward-compat alias — older tests stub this name.
_ensure_cloud_db_initialized = _ensure_cloud_runtime_initialized


async def _cloud_get_pocket(args: dict) -> dict:
    import os

    from ee.cloud.pockets.agent_context import fetch_pocket_for_agent

    pocket_id = args.get("pocket_id") or os.environ.get("POCKETPAW_POCKET_ID", "")
    return await fetch_pocket_for_agent(pocket_id)


async def _cloud_add_widget(args: dict) -> dict:
    import os

    from ee.cloud.pockets.agent_context import add_widget_for_agent

    pocket_id = args.get("pocket_id") or os.environ.get("POCKETPAW_POCKET_ID", "")
    return await add_widget_for_agent(pocket_id, args.get("widget", {}))


async def _cloud_update_widget(args: dict) -> dict:
    import os

    from ee.cloud.pockets.agent_context import update_widget_for_agent

    pocket_id = args.get("pocket_id") or os.environ.get("POCKETPAW_POCKET_ID", "")
    return await update_widget_for_agent(
        pocket_id, args.get("widget_id", ""), args.get("fields", {})
    )


async def _cloud_remove_widget(args: dict) -> dict:
    import os

    from ee.cloud.pockets.agent_context import remove_widget_for_agent

    pocket_id = args.get("pocket_id") or os.environ.get("POCKETPAW_POCKET_ID", "")
    return await remove_widget_for_agent(pocket_id, args.get("widget_id", ""))


async def _cloud_list_pockets(args: dict) -> dict:
    import os

    from ee.cloud.pockets import service as pockets_service

    workspace_id = args.get("workspace_id") or os.environ.get("POCKETPAW_WORKSPACE_ID", "")
    user_id = args.get("user_id") or os.environ.get("POCKETPAW_USER_ID", "")
    if not workspace_id or not user_id:
        return {
            "ok": False,
            "error": (
                "workspace_id / user_id missing — the agent backend must export "
                "POCKETPAW_WORKSPACE_ID + POCKETPAW_USER_ID before spawning."
            ),
        }
    pockets = await pockets_service.agent_list(workspace_id, user_id)
    return {"ok": True, "pockets": pockets}


async def _cloud_pocket_specialist_create_wrapper(args: dict) -> dict:
    """Lazy-import wrapper so importing the CLI module doesn't pull in the
    pocket-specialist runtime (and its deep_agents/claude_agent_sdk
    dependencies) unless the command is actually called."""
    from ee.agent.pocket_specialist.cli_tool import _cloud_pocket_specialist_create

    return await _cloud_pocket_specialist_create(args)


_CLOUD_HANDLERS: dict[str, Any] = {
    "cloud_list_pockets": _cloud_list_pockets,
    "cloud_get_pocket": _cloud_get_pocket,
    "cloud_add_widget": _cloud_add_widget,
    "cloud_update_widget": _cloud_update_widget,
    "cloud_remove_widget": _cloud_remove_widget,
    "cloud_pocket_specialist_create": _cloud_pocket_specialist_create_wrapper,
}


async def _run_cloud_handler(handler, args: dict) -> str:
    """Init Beanie + EventBus if needed, run the handler, return its result
    as a single JSON line. Never raises — failures land in
    ``{ok: false, error}``."""
    try:
        await _ensure_cloud_runtime_initialized()
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"ok": False, "error": str(exc)})
    try:
        result = await handler(args)
    except Exception as exc:  # noqa: BLE001
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return json.dumps(result, default=str)


def _print_tool_list() -> None:
    """Print all available tools with descriptions."""
    print("Available PocketPaw tools:\n")
    for name, tool in sorted(_TOOLS.items()):
        desc = tool.description.split(".")[0]  # first sentence
        print(f"  {name:30s} {desc}")
    print("\nCloud pocket commands (write to Mongo via ee.cloud.pockets):")
    for name in sorted(_CLOUD_HANDLERS):
        print(f"  {name}")
    print(f"\nTotal: {len(_TOOLS) + len(_CLOUD_HANDLERS)} commands")
    print("\nUsage: python -m pocketpaw.tools.cli <name> '<json_args>'")
    print("       python -m pocketpaw.tools.cli <name> -        (read JSON from stdin)")


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        _print_tool_list()
        sys.exit(0)

    if sys.argv[1] == "--list":
        _print_tool_list()
        sys.exit(0)

    tool_name = sys.argv[1]
    cloud_handler = _CLOUD_HANDLERS.get(tool_name)
    tool = _TOOLS.get(tool_name)

    if cloud_handler is None and tool is None:
        print(f"Error: Unknown tool '{tool_name}'", file=sys.stderr)
        all_names = sorted({*_TOOLS, *_CLOUD_HANDLERS})
        print(f"Available: {', '.join(all_names)}", file=sys.stderr)
        sys.exit(1)

    # Parse JSON args — prefer stdin to avoid bash $-expansion issues with CLI args
    args_str = ""
    if len(sys.argv) > 2 and sys.argv[2] != "-":
        args_str = sys.argv[2]
    elif not sys.stdin.isatty():
        args_str = sys.stdin.read().strip()
    if not args_str:
        args_str = "{}"
    try:
        args = json.loads(args_str)
    except json.JSONDecodeError as e:
        print(f"Error: Invalid JSON args: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(args, dict):
        print("Error: Args must be a JSON object", file=sys.stderr)
        sys.exit(1)

    # Cloud handler path: async, JSON-in/JSON-out, lazy Beanie init.
    if cloud_handler is not None:
        result = asyncio.run(_run_cloud_handler(cloud_handler, args))
        print(result)
        return

    # Legacy local-tool path.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        result = asyncio.run(tool.execute(**args))
    else:
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as ex:
            result = ex.submit(asyncio.run, tool.execute(**args)).result()
    print(result)


if __name__ == "__main__":
    main()
