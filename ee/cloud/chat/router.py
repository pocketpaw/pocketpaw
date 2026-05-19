"""Chat domain — REST endpoints + WebSocket handler.

REST routes live under ``/chat`` and require an enterprise license.
The WebSocket endpoint at ``/ws/cloud`` authenticates via JWT query param.

Updated 2026-04-19 (Task 19, Cluster A sub-PR 4): presence events are now
emitted on WS connect and disconnect. PresenceOnline fires immediately when
a user's first socket accepts; PresenceOffline fires after the existing
30s grace window so quick reloads don't flap the online indicator.

Updated 2026-04-20: on connect, also send the new socket a snapshot of
currently-online workspace peers. Without this, a user who joins after
their peers are already online never learns they're there — the server
only broadcasts presence deltas, not the current set.

2026-04-19 (Cluster E sub-PR 2): added ``GET /chat/messages/search`` — a
workspace-wide message search that delegates to
``message_service.search_workspace_messages`` and inherits its per-group
scope filter.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from ee.cloud.chat import group_service, message_service, unread_service
from ee.cloud.chat.agent_router import router as agent_router
from ee.cloud.chat.schemas import (
    AddGroupAgentRequest,
    AddGroupMembersRequest,
    CreateGroupRequest,
    CreateThreadRequest,
    EditMessageRequest,
    ReactRequest,
    SendMessageRequest,
    UpdateGroupAgentRequest,
    UpdateGroupRequest,
    UpdateMemberRoleRequest,
    UpdateUiStateRequest,
    WsInbound,
    WsOutbound,
)
from ee.cloud.chat.ws import PRESENCE_GRACE_SECONDS, manager
from ee.cloud.license import get_license, require_license
from ee.cloud.realtime.emit import emit
from ee.cloud.realtime.events import PresenceOffline, PresenceOnline
from ee.cloud.shared.deps import (
    current_user,
    current_user_id,
    current_workspace_id,
    require_group_action,
)
from ee.cloud.shared.errors import Forbidden as CloudForbidden
from ee.cloud.workspace import service as workspace_service
from pocketpaw.ee.guards.deps import check_workspace_action
from pocketpaw.ee.guards.rbac import Forbidden as GuardForbidden

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Chat"])

# REST endpoints require license
_licensed = APIRouter(prefix="/chat", dependencies=[Depends(require_license)])


# ---------------------------------------------------------------------------
# Groups
# ---------------------------------------------------------------------------


@_licensed.post("/groups")
async def create_group(
    body: CreateGroupRequest,
    user: Any = Depends(current_user),
    workspace_id: str = Depends(current_workspace_id),
):
    # Workspace-visible rooms (channels/public) are admin-only; private and DM
    # are available to any workspace member.
    action = "channel.create" if body.type in ("channel", "public") else "group.create"
    try:
        check_workspace_action(user, workspace_id, action)
    except GuardForbidden as exc:
        raise CloudForbidden(exc.code, exc.detail or "Access denied") from exc
    return await group_service.create_group(workspace_id, str(user.id), body)


@_licensed.get("/groups")
async def list_groups(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
):
    return await group_service.list_groups(workspace_id, user_id)


@_licensed.get("/groups/{group_id}")
async def get_group(
    group_id: str,
    user_id: str = Depends(current_user_id),
):
    return await group_service.get_group(group_id, user_id)


@_licensed.patch(
    "/groups/{group_id}",
    dependencies=[Depends(require_group_action("group.admin"))],
)
async def update_group(
    group_id: str,
    body: UpdateGroupRequest,
    user_id: str = Depends(current_user_id),
):
    return await group_service.update_group(group_id, user_id, body)


@_licensed.post(
    "/groups/{group_id}/archive",
    dependencies=[Depends(require_group_action("group.admin"))],
)
async def archive_group(
    group_id: str,
    user_id: str = Depends(current_user_id),
):
    await group_service.archive_group(group_id, user_id)
    return {"ok": True}


@_licensed.post("/groups/{group_id}/join")
async def join_group(
    group_id: str,
    user_id: str = Depends(current_user_id),
):
    await group_service.join_group(group_id, user_id)
    return {"ok": True}


@_licensed.post("/groups/{group_id}/leave")
async def leave_group(
    group_id: str,
    user_id: str = Depends(current_user_id),
):
    await group_service.leave_group(group_id, user_id)
    return {"ok": True}


@_licensed.post(
    "/groups/{group_id}/members",
    dependencies=[Depends(require_group_action("group.admin"))],
)
async def add_members(
    group_id: str,
    body: AddGroupMembersRequest,
    user_id: str = Depends(current_user_id),
):
    added = await group_service.add_members(group_id, user_id, body.user_ids, body.role)
    return {"ok": True, "added": added}


@_licensed.delete(
    "/groups/{group_id}/members/{target_user_id}",
    status_code=204,
    dependencies=[Depends(require_group_action("group.admin"))],
)
async def remove_member(
    group_id: str,
    target_user_id: str,
    user_id: str = Depends(current_user_id),
):
    await group_service.remove_member(group_id, user_id, target_user_id)


@_licensed.patch(
    "/groups/{group_id}/members/{target_user_id}/role",
    dependencies=[Depends(require_group_action("group.admin"))],
)
async def update_member_role(
    group_id: str,
    target_user_id: str,
    body: UpdateMemberRoleRequest,
    user_id: str = Depends(current_user_id),
):
    new_role = await group_service.set_member_role(group_id, user_id, target_user_id, body.role)
    return {"ok": True, "role": new_role}


# ---------------------------------------------------------------------------
# Group Agents
# ---------------------------------------------------------------------------


@_licensed.post(
    "/groups/{group_id}/agents",
    dependencies=[Depends(require_group_action("group.admin"))],
)
async def add_group_agent(
    group_id: str,
    body: AddGroupAgentRequest,
    user_id: str = Depends(current_user_id),
):
    await group_service.add_agent(group_id, user_id, body)
    return {"ok": True}


@_licensed.patch(
    "/groups/{group_id}/agents/{agent_id}",
    dependencies=[Depends(require_group_action("group.admin"))],
)
async def update_group_agent(
    group_id: str,
    agent_id: str,
    body: UpdateGroupAgentRequest,
    user_id: str = Depends(current_user_id),
):
    await group_service.update_agent(group_id, user_id, agent_id, body)
    return {"ok": True}


@_licensed.delete(
    "/groups/{group_id}/agents/{agent_id}",
    status_code=204,
    dependencies=[Depends(require_group_action("group.admin"))],
)
async def remove_group_agent(
    group_id: str,
    agent_id: str,
    user_id: str = Depends(current_user_id),
):
    await group_service.remove_agent(group_id, user_id, agent_id)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


@_licensed.get("/groups/{group_id}/messages")
async def get_messages(
    group_id: str,
    user_id: str = Depends(current_user_id),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
):
    return await message_service.get_messages(group_id, user_id, cursor, limit)


@_licensed.post("/groups/{group_id}/messages")
async def send_message(
    group_id: str,
    body: SendMessageRequest,
    user_id: str = Depends(current_user_id),
):
    return await message_service.send_message(group_id, user_id, body)


@_licensed.patch("/messages/{message_id}")
async def edit_message(
    message_id: str,
    body: EditMessageRequest,
    user_id: str = Depends(current_user_id),
):
    return await message_service.edit_message(message_id, user_id, body)


@_licensed.delete("/messages/{message_id}", status_code=204)
async def delete_message(
    message_id: str,
    user_id: str = Depends(current_user_id),
):
    await message_service.delete_message(message_id, user_id)


@_licensed.post("/messages/{message_id}/react")
async def react_to_message(
    message_id: str,
    body: ReactRequest,
    user_id: str = Depends(current_user_id),
):
    return await message_service.toggle_reaction(message_id, user_id, body.emoji)


@_licensed.get("/messages/{message_id}/thread")
async def get_thread(
    message_id: str,
    user_id: str = Depends(current_user_id),
):
    return await message_service.get_thread(message_id, user_id)


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


@_licensed.post("/groups/{group_id}/threads")
async def create_thread(
    group_id: str,
    body: CreateThreadRequest,
    user_id: str = Depends(current_user_id),
):
    """Create a thread from a message. The message_id comes from the request body."""
    return await message_service.create_thread(group_id, user_id, body.message_id)


@_licensed.get("/groups/{group_id}/threads")
async def list_active_threads(
    group_id: str,
    user_id: str = Depends(current_user_id),
):
    """List active threads in a group."""
    return await message_service.get_active_threads(group_id, user_id)


@_licensed.get("/groups/{group_id}/threads/{thread_id}/messages")
async def get_thread_messages(
    group_id: str,
    thread_id: str,
    user_id: str = Depends(current_user_id),
    cursor: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
):
    """Get all messages in a thread (parent + replies), oldest-first."""
    return await message_service.get_thread_messages(thread_id, user_id, group_id=group_id, cursor=cursor, limit=limit)


@_licensed.post("/groups/{group_id}/threads/{thread_id}/close")
async def close_thread(
    group_id: str,
    thread_id: str,
    user_id: str = Depends(current_user_id),
):
    """Close/archive a thread so it no longer appears in the active list."""
    await message_service.close_thread(group_id, user_id, thread_id)
    return {"ok": True}


@_licensed.patch("/messages/{message_id}/ui-state")
async def patch_message_ui_state(
    message_id: str,
    body: UpdateUiStateRequest,
    user_id: str = Depends(current_user_id),
):
    """Persist Ripple inline-UI state for a single ``ui-spec`` block.

    Used by chat-inline interactive widgets (kanban drag-drop, form
    inputs, etc.) so client-side state survives reload. See
    ``message_service.patch_ui_state`` for authz semantics.
    """
    return await message_service.patch_ui_state(message_id, user_id, body.spec_id, body.state)


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


@_licensed.post("/groups/{group_id}/pin/{message_id}")
async def pin_message(
    group_id: str,
    message_id: str,
    user_id: str = Depends(current_user_id),
):
    await message_service.pin_message(group_id, user_id, message_id)
    return {"ok": True}


@_licensed.delete("/groups/{group_id}/pin/{message_id}", status_code=204)
async def unpin_message(
    group_id: str,
    message_id: str,
    user_id: str = Depends(current_user_id),
):
    await message_service.unpin_message(group_id, user_id, message_id)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@_licensed.get("/groups/{group_id}/search")
async def search_messages(
    group_id: str,
    q: str = Query(..., min_length=1),
    user_id: str = Depends(current_user_id),
):
    return await message_service.search_messages(group_id, user_id, q)


@_licensed.get("/messages/search")
async def search_workspace_messages(
    q: str = Query(..., min_length=1, max_length=200),
    limit: int = Query(50, ge=1, le=100),
    user_id: str = Depends(current_user_id),
    workspace_id: str = Depends(current_workspace_id),
):
    """Workspace-wide message search.

    Results are scoped to groups the caller can already read: public /
    channel groups in the workspace plus private / DM groups where the
    caller is a member. The query is regex-escaped before it hits Mongo,
    and capped at 100 results. Cluster E sub-PR 2.
    """
    return await message_service.search_workspace_messages(workspace_id, user_id, q, limit=limit)


# ---------------------------------------------------------------------------
# DMs
# ---------------------------------------------------------------------------


@_licensed.post("/dm/{target_user_id}")
async def get_or_create_dm(
    target_user_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
):
    return await group_service.get_or_create_dm(workspace_id, user_id, target_user_id)


@_licensed.post("/dm-agent/{agent_id}")
async def get_or_create_agent_dm(
    agent_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
):
    """Find or create a 1:1 DM between the caller and an agent."""
    return await group_service.get_or_create_agent_dm(workspace_id, user_id, agent_id)


# ---------------------------------------------------------------------------
# Unreads
# ---------------------------------------------------------------------------


@_licensed.get("/unreads")
async def list_unreads(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
):
    return await unread_service.list_unreads(user_id, workspace_id)


# ---------------------------------------------------------------------------
# Mentions
# ---------------------------------------------------------------------------


@_licensed.get("/mentions/suggest")
async def suggest_mentions(
    q: str = Query("", max_length=64),
    types: str = Query("user,agent,channel"),
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
):
    from ee.cloud.agents import service as agents_service
    from ee.cloud.auth import service as auth_service

    kinds = {k.strip() for k in types.split(",") if k.strip()}
    q_lower = q.lower()
    results: list[dict] = []

    if "user" in kinds:
        results.extend(await auth_service.suggest_workspace_members(workspace_id, q))

    if "agent" in kinds:
        results.extend(await agents_service.suggest_for_mentions(workspace_id, q))

    if "channel" in kinds:
        results.extend(await group_service.suggest_channels(workspace_id, q))

    # Broadcast tokens — always offered, filtered by prefix match when q is set.
    for token, display in (("here", "@here"), ("channel", "@channel"), ("everyone", "@everyone")):
        if not q or token.startswith(q_lower):
            results.append({"type": token, "id": "", "display_name": display})

    return results


# Include licensed REST routes
router.include_router(_licensed)
router.include_router(agent_router)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@router.websocket("/ws/cloud")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    """Cloud WebSocket -- authenticate via JWT token, then handle typed JSON messages."""
    import jwt as pyjwt

    # Gate realtime behind the enterprise license (parity with REST /chat routes).
    lic = get_license()
    if lic is None or lic.expired:
        await websocket.close(code=4003, reason="Enterprise license required")
        return

    secret = os.environ.get("AUTH_SECRET", "change-me-in-production-please")
    try:
        payload = pyjwt.decode(token, secret, algorithms=["HS256"], audience=["fastapi-users:auth"])
        user_id = payload.get("sub")
        if not user_id:
            await websocket.close(code=4001, reason="Invalid token")
            return
    except Exception:
        await websocket.close(code=4001, reason="Invalid token")
        return

    # Accept and register connection. If this was the user's first active
    # socket, announce them as online so every workspace peer's UI flips
    # the presence dot immediately.
    await websocket.accept()
    was_offline_before = not manager.is_online(user_id)
    await manager.connect(websocket, user_id)
    if was_offline_before:
        await emit(PresenceOnline(data={"user_id": user_id}))

    # Send a one-shot snapshot of currently-online peers to THIS socket so the
    # client's presence store is seeded before any deltas arrive. Goes directly
    # to the socket (not the bus) because the payload is addressed to this
    # connection only. Without it, a late joiner sees only their own dot until
    # someone else flaps online/offline.
    try:
        peer_ids = await workspace_service.list_peer_ids(user_id)
        for peer_id in peer_ids:
            if manager.is_online(peer_id):
                await websocket.send_json({"type": "presence.online", "data": {"user_id": peer_id}})
    except Exception:
        logger.exception("Failed to send presence snapshot to user=%s", user_id)

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                data = json.loads(raw)
                msg = WsInbound.model_validate(data)
            except Exception:
                await websocket.send_json(
                    WsOutbound(
                        type="error",
                        data={"code": "invalid_message", "message": "Invalid message format"},
                    ).model_dump(mode="json")
                )
                continue

            await _handle_ws_message(websocket, user_id, msg)

    except WebSocketDisconnect:
        pass
    except RuntimeError as e:
        # Starlette raises RuntimeError("WebSocket is not connected...") when
        # receive_text() is called after the socket already transitioned to
        # DISCONNECTED (e.g. a concurrent send/recv consumed the disconnect
        # frame). Treat as a normal disconnect.
        if "not connected" not in str(e).lower():
            logger.exception("WebSocket error for user=%s", user_id)
    except Exception:
        logger.exception("WebSocket error for user=%s", user_id)
    finally:
        last_user = await manager.disconnect(websocket)
        if last_user:
            # Kick off the grace-period offline broadcast. We delay a fixed
            # window (`PRESENCE_GRACE_SECONDS`) so quick page reloads don't
            # flap the online indicator. ``manager.connect`` cancels the
            # pending task on reconnect so the offline event never fires if
            # the user came back within the window.
            await _schedule_presence_offline(last_user)


async def _ws_thread_create(user_id: str, msg: WsInbound) -> None:
    if not msg.group_id or not msg.message_id:
        return
    await message_service.create_thread(msg.group_id, user_id, msg.message_id)


async def _ws_thread_close(user_id: str, msg: WsInbound) -> None:
    if not msg.group_id or not msg.message_id:
        return
    await message_service.close_thread(msg.group_id, user_id, msg.message_id)


async def _ws_thread_send(user_id: str, msg: WsInbound) -> None:
    if not msg.group_id or not msg.content or not msg.message_id:
        return
    body = SendMessageRequest(
        content=msg.content,
        thread_id=msg.message_id,  # the parent message id
        reply_to=msg.reply_to,
        mentions=msg.mentions,
        attachments=msg.attachments,
    )
    await message_service.send_message(msg.group_id, user_id, body)


async def _schedule_presence_offline(user_id: str) -> None:
    """Queue a delayed ``presence.offline`` broadcast.

    Registers the task with ``manager._offline_tasks`` so ``ConnectionManager.connect``
    automatically cancels it when the user reconnects within the grace window.
    """

    async def _emit_after_delay() -> None:
        try:
            await asyncio.sleep(PRESENCE_GRACE_SECONDS)
            # If the user reconnected while we were asleep the manager would
            # have cancelled this task; double-check before emitting so races
            # on shutdown don't ship a stale offline event.
            if manager.is_online(user_id):
                return
            await emit(PresenceOffline(data={"user_id": user_id}))
        except asyncio.CancelledError:
            # Reconnect within the grace window — the manager cancels us.
            raise

    # Cancel any pending offline task (shouldn't happen normally, but belt
    # and braces) and register the new one.
    prev = manager._offline_tasks.pop(user_id, None)
    if prev:
        prev.cancel()
    manager._offline_tasks[user_id] = asyncio.create_task(_emit_after_delay())


# ---------------------------------------------------------------------------
# WebSocket message dispatcher
# ---------------------------------------------------------------------------


async def _handle_ws_message(websocket: WebSocket, user_id: str, msg: WsInbound) -> None:
    """Dispatch validated WebSocket message to the appropriate handler."""
    if msg.type == "message.send":
        await _ws_message_send(user_id, msg)
    elif msg.type == "message.edit":
        await _ws_message_edit(user_id, msg)
    elif msg.type == "message.delete":
        await _ws_message_delete(user_id, msg)
    elif msg.type == "message.react":
        await _ws_message_react(user_id, msg)
    elif msg.type == "typing.start":
        await _ws_typing(user_id, msg, active=True)
    elif msg.type == "typing.stop":
        await _ws_typing(user_id, msg, active=False)
    elif msg.type == "presence.update":
        pass  # Will be wired in Task 19
    elif msg.type == "read.ack":
        await _ws_read_ack(user_id, msg)
    elif msg.type == "thread.create":
        await _ws_thread_create(user_id, msg)
    elif msg.type == "thread.close":
        await _ws_thread_close(user_id, msg)
    elif msg.type == "thread.send":
        await _ws_thread_send(user_id, msg)
    elif msg.type == "room.join":
        if msg.group_id:
            members = await group_service.list_member_ids(msg.group_id)
            if user_id in members:
                manager.join_room(websocket, msg.group_id)
    elif msg.type == "room.leave":
        manager.leave_room(websocket)


async def _ws_message_send(user_id: str, msg: WsInbound) -> None:
    if not msg.group_id or not msg.content:
        return

    body = SendMessageRequest(
        content=msg.content,
        reply_to=msg.reply_to,
        mentions=msg.mentions,
        attachments=msg.attachments,
    )
    await message_service.send_message(msg.group_id, user_id, body)


async def _ws_message_edit(user_id: str, msg: WsInbound) -> None:
    if not msg.message_id or not msg.content:
        return

    await message_service.edit_message(
        msg.message_id, user_id, EditMessageRequest(content=msg.content)
    )


async def _ws_message_delete(user_id: str, msg: WsInbound) -> None:
    if not msg.message_id:
        return

    await message_service.delete_message(msg.message_id, user_id)


async def _ws_message_react(user_id: str, msg: WsInbound) -> None:
    if not msg.message_id or not msg.emoji:
        return

    await message_service.toggle_reaction(msg.message_id, user_id, msg.emoji)


async def _ws_typing(user_id: str, msg: WsInbound, *, active: bool) -> None:
    if not msg.group_id:
        return

    members = await group_service.list_member_ids(msg.group_id)
    if user_id not in members:
        return

    if active:
        manager.start_typing(msg.group_id, user_id)
    else:
        manager.stop_typing(msg.group_id, user_id)

    await manager.send_to_room(
        msg.group_id,
        WsOutbound(
            type="typing",
            data={
                "group_id": msg.group_id,
                "user_id": user_id,
                "active": active,
            },
        ),
        exclude_user=user_id,
    )


async def _ws_read_ack(user_id: str, msg: WsInbound) -> None:
    if not msg.group_id or not msg.message_id:
        return

    members = await group_service.list_member_ids(msg.group_id)
    if user_id not in members:
        return

    await unread_service.mark_read(user_id, msg.group_id, msg.message_id)

    await manager.send_to_room(
        msg.group_id,
        WsOutbound(
            type="read.receipt",
            data={
                "group_id": msg.group_id,
                "user_id": user_id,
                "last_read": msg.message_id,
            },
        ),
        exclude_user=user_id,
    )
