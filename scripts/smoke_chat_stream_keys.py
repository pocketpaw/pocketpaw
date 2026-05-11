"""Smoke test — bus-style session_key ('websocket:X') normalizes to match
the UI's Session.sessionId ('websocket_X') so chat history round-trips.

This simulates what happens when /api/v1/chat/stream is hit: the agent loop
writes memory entries with session_key='websocket:<chat_id>', while the
front-end created a Session with sessionId='websocket_<chat_id>' and asks
for history via GET /api/v1/sessions/{sessionId}/history.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import sys
import uuid
from unittest.mock import AsyncMock, MagicMock, patch


def _banner(t: str) -> None:
    print(f"\n=== {t} ===")


def _license_key(secret: str = "smoke-secret") -> str:
    from datetime import datetime, timedelta

    payload = {
        "org": "smoke",
        "plan": "enterprise",
        "seats": 100,
        "exp": (datetime.now(tz=None) + timedelta(days=365)).strftime("%Y-%m-%d"),
    }
    s = json.dumps(payload)
    sig = hashlib.sha256(f"{secret}:{s}".encode()).hexdigest()
    return base64.b64encode(f"{s}.{sig}".encode()).decode()


async def main() -> int:
    db_name = f"smoke_chat_stream_keys_{uuid.uuid4().hex[:8]}"
    uri = f"mongodb://localhost:27017/{db_name}"

    secret = "smoke-secret"
    os.environ.update(
        {
            "POCKETPAW_LICENSE_KEY": _license_key(secret),
            "POCKETPAW_LICENSE_SECRET": secret,
            "AUTH_SECRET": "smoke-auth-secret-chat-stream-keys",
            "POCKETPAW_CLOUD_MONGO_URI": uri,
        }
    )
    os.environ.pop("POCKETPAW_MEMORY_BACKEND", None)

    from beanie import init_beanie
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from motor.motor_asyncio import AsyncIOMotorClient

    import ee.cloud.license as lic_mod
    from ee.cloud import mount_cloud
    from ee.cloud.memory.bootstrap import register_default_backend
    from ee.cloud.memory.documents import MemoryFactDoc
    from ee.cloud.models import ALL_DOCUMENTS

    lic_mod._cached_license = None
    lic_mod._license_error = None

    _banner("0. init_beanie + mount_cloud")
    AsyncIOMotorClient("mongodb://localhost:27017")
    await init_beanie(
        connection_string=uri,
        document_models=[*ALL_DOCUMENTS, MemoryFactDoc],
    )
    register_default_backend()

    app = FastAPI()
    mock_pool = MagicMock()
    mock_pool.start = AsyncMock()
    mock_pool.stop = AsyncMock()
    with patch("pocketpaw.agents.pool.get_agent_pool", return_value=mock_pool):
        mount_cloud(app)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            _banner("1. register user + workspace")
            email = f"smoke-{uuid.uuid4().hex[:6]}@test.example"
            password = "Password1!"
            r = await http.post(
                "/api/v1/auth/register",
                json={"email": email, "password": password, "full_name": "Smoke"},
            )
            assert r.status_code == 201, r.text
            r = await http.post(
                "/api/v1/auth/bearer/login",
                data={"username": email, "password": password},
            )
            headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
            slug = f"smoke-{uuid.uuid4().hex[:6]}"
            r = await http.post(
                "/api/v1/workspaces",
                json={"name": "Smoke WS", "slug": slug},
                headers=headers,
            )
            workspace = r.json()
            await http.post(
                "/api/v1/auth/set-active-workspace",
                json={"workspace_id": workspace["_id"]},
                headers=headers,
            )

            _banner("2. POST /api/v1/sessions (UI creates session)")
            r = await http.post(
                "/api/v1/sessions",
                json={"title": "Chat Stream Session"},
                headers=headers,
            )
            session = r.json()
            ui_session_id = session["sessionId"]  # "websocket_<uuid>"
            chat_id = ui_session_id.removeprefix("websocket_")
            bus_session_key = f"websocket:{chat_id}"  # what agent_loop writes
            print(f"   ui_session_id   = {ui_session_id}")
            print(f"   bus_session_key = {bus_session_key}")

            _banner("3. agent loop writes using bus-style key via MemoryManager")
            from pocketpaw.memory.manager import get_memory_manager

            manager = get_memory_manager()
            await manager.add_to_session(bus_session_key, "user", "hi from user")
            await manager.add_to_session(bus_session_key, "assistant", "hi back from agent")
            print("   wrote 2 entries with bus_session_key")

            _banner("4. raw Mongo — messages normalized to UI key form")
            from ee.cloud.models.message import Message

            by_ui_key = await Message.find({"session_key": ui_session_id}).to_list()
            by_bus_key = await Message.find({"session_key": bus_session_key}).to_list()
            print(f"   session_key={ui_session_id!r} ->{len(by_ui_key)} msgs")
            print(f"   session_key={bus_session_key!r} ->{len(by_bus_key)} msgs")
            if len(by_ui_key) != 2 or len(by_bus_key) != 0:
                print("\nSMOKE FAILED: session_key normalization not applied")
                return 4

            _banner("5. GET /api/v1/sessions/{ui_session_id}/history")
            r = await http.get(f"/api/v1/sessions/{ui_session_id}/history", headers=headers)
            assert r.status_code == 200, r.text
            hist = r.json()
            contents = [m["content"] for m in hist.get("messages", [])]
            print(f"   history = {contents}")
            assert contents == ["hi from user", "hi back from agent"], (
                f"history order/content mismatch: {contents}"
            )

            print("\nSMOKE OK")
            return 0
    finally:
        client2 = AsyncIOMotorClient("mongodb://localhost:27017")
        await client2.drop_database(db_name)
        print(f"\n(dropped {db_name})")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
