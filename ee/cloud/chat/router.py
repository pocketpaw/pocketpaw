"""Chat domain — REST endpoints + WebSocket handler.

REST routes live under ``/chat`` and require an enterprise license.
The WebSocket endpoint at ``/ws/cloud`` authenticates via JWT query param.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect

from ee.cloud.chat.schemas import (
    AddGroupAgentRequest,
    AddGroupMembersRequest,
    CreateGroupRequest,
    EditMessageRequest,
    ReactRequest,
    SendMessageRequest,
    UpdateGroupAgentRequest,
    UpdateGroupRequest,
    WsInbound,
    WsOutbound,
)
from ee.cloud.chat.service import GroupService, MessageService
from ee.cloud.chat.ws import manager
from ee.cloud.license import require_license
from ee.cloud.shared.deps import current_user_id, current_workspace_id

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
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
):
    return await GroupService.create_group(workspace_id, user_id, body)


@_licensed.get("/groups")
async def list_groups(
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
):
    return await GroupService.list_groups(workspace_id, user_id)


@_licensed.get("/groups/{group_id}")
async def get_group(
    group_id: str,
    user_id: str = Depends(current_user_id),
):
    return await GroupService.get_group(group_id, user_id)


@_licensed.patch("/groups/{group_id}")
async def update_group(
    group_id: str,
    body: UpdateGroupRequest,
    user_id: str = Depends(current_user_id),
):
    return await GroupService.update_group(group_id, user_id, body)


@_licensed.post("/groups/{group_id}/archive")
async def archive_group(
    group_id: str,
    user_id: str = Depends(current_user_id),
):
    await GroupService.archive_group(group_id, user_id)
    return {"ok": True}


@_licensed.post("/groups/{group_id}/join")
async def join_group(
    group_id: str,
    user_id: str = Depends(current_user_id),
):
    await GroupService.join_group(group_id, user_id)
    return {"ok": True}


@_licensed.post("/groups/{group_id}/leave")
async def leave_group(
    group_id: str,
    user_id: str = Depends(current_user_id),
):
    await GroupService.leave_group(group_id, user_id)
    return {"ok": True}


@_licensed.post("/groups/{group_id}/members")
async def add_members(
    group_id: str,
    body: AddGroupMembersRequest,
    user_id: str = Depends(current_user_id),
):
    await GroupService.add_members(group_id, user_id, body.user_ids)
    return {"ok": True}


@_licensed.delete("/groups/{group_id}/members/{target_user_id}", status_code=204)
async def remove_member(
    group_id: str,
    target_user_id: str,
    user_id: str = Depends(current_user_id),
):
    await GroupService.remove_member(group_id, user_id, target_user_id)


# ---------------------------------------------------------------------------
# Group Agents
# ---------------------------------------------------------------------------


@_licensed.post("/groups/{group_id}/agents")
async def add_group_agent(
    group_id: str,
    body: AddGroupAgentRequest,
    user_id: str = Depends(current_user_id),
):
    await GroupService.add_agent(group_id, user_id, body)
    return {"ok": True}


@_licensed.patch("/groups/{group_id}/agents/{agent_id}")
async def update_group_agent(
    group_id: str,
    agent_id: str,
    body: UpdateGroupAgentRequest,
    user_id: str = Depends(current_user_id),
):
    await GroupService.update_agent(group_id, user_id, agent_id, body)
    return {"ok": True}


@_licensed.delete("/groups/{group_id}/agents/{agent_id}", status_code=204)
async def remove_group_agent(
    group_id: str,
    agent_id: str,
    user_id: str = Depends(current_user_id),
):
    await GroupService.remove_agent(group_id, user_id, agent_id)


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
    return await MessageService.get_messages(group_id, user_id, cursor, limit)


@_licensed.post("/groups/{group_id}/messages")
async def send_message(
    group_id: str,
    body: SendMessageRequest,
    user_id: str = Depends(current_user_id),
):
    return await MessageService.send_message(group_id, user_id, body)


@_licensed.patch("/messages/{message_id}")
async def edit_message(
    message_id: str,
    body: EditMessageRequest,
    user_id: str = Depends(current_user_id),
):
    return await MessageService.edit_message(message_id, user_id, body)


@_licensed.delete("/messages/{message_id}", status_code=204)
async def delete_message(
    message_id: str,
    user_id: str = Depends(current_user_id),
):
    await MessageService.delete_message(message_id, user_id)


@_licensed.post("/messages/{message_id}/react")
async def react_to_message(
    message_id: str,
    body: ReactRequest,
    user_id: str = Depends(current_user_id),
):
    return await MessageService.toggle_reaction(message_id, user_id, body.emoji)


@_licensed.get("/messages/{message_id}/thread")
async def get_thread(
    message_id: str,
    user_id: str = Depends(current_user_id),
):
    return await MessageService.get_thread(message_id, user_id)


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


@_licensed.post("/groups/{group_id}/pin/{message_id}")
async def pin_message(
    group_id: str,
    message_id: str,
    user_id: str = Depends(current_user_id),
):
    await MessageService.pin_message(group_id, user_id, message_id)
    return {"ok": True}


@_licensed.delete("/groups/{group_id}/pin/{message_id}", status_code=204)
async def unpin_message(
    group_id: str,
    message_id: str,
    user_id: str = Depends(current_user_id),
):
    await MessageService.unpin_message(group_id, user_id, message_id)


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@_licensed.get("/groups/{group_id}/search")
async def search_messages(
    group_id: str,
    q: str = Query(..., min_length=1),
    user_id: str = Depends(current_user_id),
):
    return await MessageService.search_messages(group_id, user_id, q)


# ---------------------------------------------------------------------------
# DMs
# ---------------------------------------------------------------------------


@_licensed.post("/dm/{target_user_id}")
async def get_or_create_dm(
    target_user_id: str,
    workspace_id: str = Depends(current_workspace_id),
    user_id: str = Depends(current_user_id),
):
    return await GroupService.get_or_create_dm(workspace_id, user_id, target_user_id)


# Include licensed REST routes
router.include_router(_licensed)


# ---------------------------------------------------------------------------
# WebSocket endpoint
# ---------------------------------------------------------------------------


@router.websocket("/ws/cloud")
async def websocket_endpoint(websocket: WebSocket, token: str = Query(...)):
    """Cloud WebSocket -- authenticate via JWT token, then handle typed JSON messages."""
    import jwt as pyjwt

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

    # Accept and register connection
    await websocket.accept()
    await manager.connect(websocket, user_id)

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

            await _handle_ws_message(user_id, msg)

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception("WebSocket error for user=%s", user_id)
    finally:
        last_user = await manager.disconnect(websocket)
        if last_user:
            # Start grace period before marking offline
            pass  # Presence broadcast handled by event handlers (Task 19)


# ---------------------------------------------------------------------------
# WebSocket message dispatcher
# ---------------------------------------------------------------------------


async def _handle_ws_message(user_id: str, msg: WsInbound) -> None:
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


async def _ws_message_send(user_id: str, msg: WsInbound) -> None:
    if not msg.group_id or not msg.content:
        return

    body = SendMessageRequest(
        content=msg.content,
        reply_to=msg.reply_to,
        mentions=msg.mentions,
        attachments=msg.attachments,
    )
    result = await MessageService.send_message(msg.group_id, user_id, body)

    from beanie import PydanticObjectId

    from ee.cloud.models.group import Group

    group = await Group.get(PydanticObjectId(msg.group_id))
    if group:
        result_data = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        await manager.broadcast_to_group(
            msg.group_id,
            group.members,
            WsOutbound(type="message.new", data=result_data),
            exclude_user=user_id,
        )
        await manager.send_to_user(
            user_id,
            WsOutbound(type="message.sent", data=result_data),
        )


async def _ws_message_edit(user_id: str, msg: WsInbound) -> None:
    if not msg.message_id or not msg.content:
        return

    await MessageService.edit_message(
        msg.message_id, user_id, EditMessageRequest(content=msg.content)
    )

    from beanie import PydanticObjectId

    from ee.cloud.models.group import Group
    from ee.cloud.models.message import Message

    message = await Message.get(PydanticObjectId(msg.message_id))
    if message:
        group = await Group.get(PydanticObjectId(message.group))
        if group:
            await manager.broadcast_to_group(
                message.group,
                group.members,
                WsOutbound(
                    type="message.edited",
                    data={
                        "message_id": msg.message_id,
                        "content": msg.content,
                        "edited_at": str(message.edited_at),
                    },
                ),
            )


async def _ws_message_delete(user_id: str, msg: WsInbound) -> None:
    if not msg.message_id:
        return

    # Fetch message before deleting so we know which group to broadcast to
    from beanie import PydanticObjectId

    from ee.cloud.models.group import Group
    from ee.cloud.models.message import Message

    message = await Message.get(PydanticObjectId(msg.message_id))

    await MessageService.delete_message(msg.message_id, user_id)

    if message:
        group = await Group.get(PydanticObjectId(message.group))
        if group:
            await manager.broadcast_to_group(
                message.group,
                group.members,
                WsOutbound(type="message.deleted", data={"message_id": msg.message_id}),
            )


async def _ws_message_react(user_id: str, msg: WsInbound) -> None:
    if not msg.message_id or not msg.emoji:
        return

    await MessageService.toggle_reaction(msg.message_id, user_id, msg.emoji)

    from beanie import PydanticObjectId

    from ee.cloud.models.group import Group
    from ee.cloud.models.message import Message

    message = await Message.get(PydanticObjectId(msg.message_id))
    if message:
        group = await Group.get(PydanticObjectId(message.group))
        if group:
            await manager.broadcast_to_group(
                message.group,
                group.members,
                WsOutbound(
                    type="message.reaction",
                    data={
                        "message_id": msg.message_id,
                        "emoji": msg.emoji,
                        "user_id": user_id,
                    },
                ),
            )


async def _ws_typing(user_id: str, msg: WsInbound, *, active: bool) -> None:
    if not msg.group_id:
        return

    if active:
        manager.start_typing(msg.group_id, user_id)
    else:
        manager.stop_typing(msg.group_id, user_id)

    from beanie import PydanticObjectId

    from ee.cloud.models.group import Group

    group = await Group.get(PydanticObjectId(msg.group_id))
    if group:
        await manager.broadcast_to_group(
            msg.group_id,
            group.members,
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

    from beanie import PydanticObjectId

    from ee.cloud.models.group import Group

    group = await Group.get(PydanticObjectId(msg.group_id))
    if group:
        await manager.broadcast_to_group(
            msg.group_id,
            group.members,
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
