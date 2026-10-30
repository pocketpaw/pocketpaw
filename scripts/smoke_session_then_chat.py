"""Smoke test — desired flow: create session via API, chat with that session_id,
verify messages are stored keyed by that session_id.

Exercises the path we WANT:
1. POST /api/v1/sessions                           → returns {sessionId: "..."}
2. chat_persistence.save_user_message(sid, "hi")   → must write Message with
                                                     context_type="pocket",
                                                     session_key=sid
3. Raw Mongo: find messages by session_key=sid    → must match
4. GET /api/v1/sessions/{sid}/history             → must return the message

Usage:
    uv run python scripts/smoke_session_then_chat.py

Requires MongoDB running at localhost:27017. Uses a throwaway DB dropped on
completion.
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


def _banner(title: str) -> None:
    print(f"\n=== {title} ===")


def _make_license_key(secret: str = "smoke-secret") -> str:
    from datetime import datetime, timedelta

    payload = {
        "org": "smoke-org",
        "plan": "enterprise",
        "seats": 100,
        "exp": (datetime.now(tz=None) + timedelta(days=365)).strftime("%Y-%m-%d"),
    }
    payload_str = json.dumps(payload)
    sig = hashlib.sha256(f"{secret}:{payload_str}".encode()).hexdigest()
    raw = f"{payload_str}.{sig}"
    return base64.b64encode(raw.encode()).decode()


async def main() -> int:
    db_name = f"smoke_session_chat_{uuid.uuid4().hex[:8]}"
    uri = f"mongodb://localhost:27017/{db_name}"

    secret = "smoke-secret"
    env = {
        "POCKETPAW_LICENSE_KEY": _make_license_key(secret),
        "POCKETPAW_LICENSE_SECRET": secret,
        "AUTH_SECRET": "smoke-auth-secret",
        "POCKETPAW_CLOUD_MONGO_URI": uri,
    }
    env.pop("POCKETPAW_MEMORY_BACKEND", None)
    os.environ.update(env)

    # --- Set up FastAPI app with cloud routes mounted --------------------
    import pocketpaw_ee.cloud.license as lic_mod
    from beanie import init_beanie
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from motor.motor_asyncio import AsyncIOMotorClient
    from pocketpaw_ee.cloud import mount_cloud
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    lic_mod._cached_license = None
    lic_mod._license_error = None

    _banner("0. init_beanie + mount_cloud")
    await init_beanie(
        connection_string=uri,
        document_models=[*ALL_DOCUMENTS, MemoryFactDoc],
    )

    # Flip memory backend default (normally done by init_cloud_db but we
    # init Beanie directly so drop the DB at the end)
    from pocketpaw_ee.cloud.memory.bootstrap import register_default_backend

    register_default_backend()

    app = FastAPI()
    mock_pool = MagicMock()
    mock_pool.start = AsyncMock()
    mock_pool.stop = AsyncMock()
    with patch("pocketpaw.agents.pool.get_agent_pool", return_value=mock_pool):
        mount_cloud(app)

    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
            # --- 1. Register user + workspace -----------------------------
            _banner("1. register user + create workspace")
            email = f"smoke-{uuid.uuid4().hex[:6]}@test.example"
            password = "Password1!"

            r = await http.post(
                "/api/v1/auth/register",
                json={"email": email, "password": password, "full_name": "Smoke"},
            )
            assert r.status_code == 201, r.text
            print(f"   user_id={r.json()['id']}")

            r = await http.post(
                "/api/v1/auth/bearer/login",
                data={"username": email, "password": password},
            )
            assert r.status_code == 200, r.text
            headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

            slug = f"smoke-{uuid.uuid4().hex[:6]}"
            r = await http.post(
                "/api/v1/workspaces",
                json={"name": "Smoke WS", "slug": slug},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            workspace = r.json()
            await http.post(
                "/api/v1/auth/set-active-workspace",
                json={"workspace_id": workspace["_id"]},
                headers=headers,
            )
            print(f"   workspace_id={workspace['_id']}")

            # --- 2. Create session via API --------------------------------
            _banner("2. POST /api/v1/sessions")
            r = await http.post(
                "/api/v1/sessions",
                json={"title": "Smoke Session"},
                headers=headers,
            )
            assert r.status_code == 200, r.text
            session = r.json()
            session_id = session["sessionId"]
            print(f"   sessionId={session_id}")
            print("   context_type in DB should be 'pocket' (no pocket/group/agent)")

            # --- 3. Send a chat message tagged with that session_id -------
            _banner("3. simulate chat with save_user_message(session_id, ...)")
            from pocketpaw_ee.cloud.shared.chat_persistence import save_user_message

            await save_user_message(session_id, "hello from smoke — user msg")
            print("   called save_user_message OK")

            # --- 4. Raw Mongo: find message by session_key=session_id ----
            _banner("4. raw Mongo — messages where session_key == sessionId")
            from pocketpaw_ee.cloud.models.message import Message

            rows = await Message.find({"session_key": session_id}).to_list()
            print(f"   found {len(rows)} messages with session_key={session_id!r}")
            if not rows:
                # Fall back to diagnostic: what DID land?
                all_msgs = await Message.find().to_list()
                print(f"   (fallback) total messages in DB: {len(all_msgs)}")
                for m in all_msgs[:5]:
                    print(
                        f"     - id={m.id} context_type={m.context_type!r} "
                        f"group={m.group!r} session_key={m.session_key!r} content={m.content!r}"
                    )
                print("\nSMOKE FAILED: messages did not land keyed by session_id")
                return 1

            for r in rows:
                assert r.context_type == "pocket", (
                    f"expected context_type='pocket', got {r.context_type!r}"
                )
                assert r.session_key == session_id
                assert r.role in ("user", "assistant", "system")
                assert not r.group, f"pocket rows must not carry group, got {r.group!r}"
            print("   all rows: context_type=pocket, session_key matches, no group")

            # --- 5. History via API ---------------------------------------
            _banner("5. GET /api/v1/sessions/{sid}/history")
            r = await http.get(f"/api/v1/sessions/{session_id}/history", headers=headers)
            assert r.status_code == 200, r.text
            hist = r.json()
            print(f"   history: {hist}")
            assert any(
                m["content"] == "hello from smoke — user msg" for m in hist.get("messages", [])
            ), "history did not include the message we just sent"

            print("\nSMOKE OK")
            return 0
    finally:
        client2 = AsyncIOMotorClient("mongodb://localhost:27017")
        await client2.drop_database(db_name)
        print(f"\n(dropped {db_name})")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
