"""Smoke test — create a new session from pocket sidebar.

Mirrors what paw-enterprise's PocketChatSidebar does:
1. Register user + workspace.
2. Create a Pocket via POST /api/v1/pockets.
3. Create a session under that pocket via POST /api/v1/pockets/{id}/sessions.
4. Verify a Session doc exists in Mongo with pocket=<pocket_id> and
   context_type=="pocket".
5. Send a chat message using the returned sessionId and verify it lands in
   messages keyed by session_key.
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
    db_name = f"smoke_pocket_session_{uuid.uuid4().hex[:8]}"
    uri = f"mongodb://localhost:27017/{db_name}"

    secret = "smoke-secret"
    os.environ.update(
        {
            "POCKETPAW_LICENSE_KEY": _license_key(secret),
            "POCKETPAW_LICENSE_SECRET": secret,
            "AUTH_SECRET": "smoke-auth-secret-for-pocket-sessions",
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
            # 1. register user + workspace
            _banner("1. register user + workspace")
            email = f"smoke-{uuid.uuid4().hex[:6]}@test.example"
            password = "Password1!"

            r = await http.post(
                "/api/v1/auth/register",
                json={"email": email, "password": password, "full_name": "Smoke"},
            )
            assert r.status_code == 201, r.text
            user_id = r.json()["id"]

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

            # 2. create pocket
            _banner("2. POST /api/v1/pockets")
            r = await http.post(
                "/api/v1/pockets",
                json={"name": "Smoke Pocket"},
                headers=headers,
            )
            print(f"   status={r.status_code}")
            if r.status_code not in (200, 201):
                print(f"   body={r.text}")
                return 2
            pocket = r.json()
            pocket_id = pocket.get("_id") or pocket.get("id")
            print(f"   pocket_id={pocket_id}")

            # 3. create session under pocket (what the sidebar does)
            _banner("3. POST /api/v1/pockets/{pocket_id}/sessions")
            r = await http.post(
                f"/api/v1/pockets/{pocket_id}/sessions",
                json={"title": "New Chat"},
                headers=headers,
            )
            print(f"   status={r.status_code}")
            if r.status_code != 200:
                print(f"   body={r.text}")
                return 3
            session = r.json()
            session_id = session["sessionId"]
            print(f"   sessionId={session_id} pocket={session.get('pocket')}")

            # 4. verify in Mongo
            _banner("4. raw Mongo — sessions collection")
            from ee.cloud.models.session import Session

            rows = await Session.find(Session.sessionId == session_id).to_list()
            print(f"   found {len(rows)} session rows with sessionId={session_id!r}")
            if not rows:
                all_s = await Session.find().to_list()
                print(f"   (fallback) total sessions in DB: {len(all_s)}")
                for s in all_s[:5]:
                    print(
                        f"     - sessionId={s.sessionId!r} pocket={s.pocket!r} "
                        f"group={s.group!r} context_type={s.context_type!r}"
                    )
                print("\nSMOKE FAILED: session not persisted")
                return 4

            s = rows[0]
            assert s.pocket == pocket_id, f"pocket link mismatch: {s.pocket!r} != {pocket_id!r}"
            assert s.context_type == "pocket", (
                f"expected context_type='pocket', got {s.context_type!r}"
            )
            assert s.owner == user_id
            assert s.workspace == workspace["_id"]
            print(f"   OK: pocket={s.pocket} context_type={s.context_type} owner={s.owner}")

            # 5. send a message using that session_id
            _banner("5. chat via save_user_message(session_id, ...)")
            from ee.cloud.shared.chat_persistence import save_user_message

            await save_user_message(session_id, "hello pocket chat")

            from ee.cloud.models.message import Message

            msgs = await Message.find({"session_key": session_id}).to_list()
            print(f"   found {len(msgs)} messages with session_key={session_id!r}")
            assert msgs and msgs[0].context_type == "pocket"

            # 6. history endpoint
            _banner("6. GET /api/v1/sessions/{sid}/history")
            r = await http.get(f"/api/v1/sessions/{session_id}/history", headers=headers)
            assert r.status_code == 200, r.text
            hist = r.json()
            print(f"   {len(hist.get('messages', []))} messages in history")
            assert any(m["content"] == "hello pocket chat" for m in hist.get("messages", []))

            print("\nSMOKE OK")
            return 0
    finally:
        client2 = AsyncIOMotorClient("mongodb://localhost:27017")
        await client2.drop_database(db_name)
        print(f"\n(dropped {db_name})")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
