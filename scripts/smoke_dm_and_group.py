"""Smoke test — human DM and group chat flows persist correctly.

Covers:
1. User A and User B in the same workspace.
2. A creates a DM with B → group has type='dm', members=[A,B].
3. A sends a message in the DM → lands in `messages` collection.
4. B fetches DM messages → sees A's message.
5. A creates a group, adds B → both can post + read.
6. Server stores `name='DM'`; the client name resolution is verified by
   inspecting the populated members payload returned by GET /chat/groups/{id}.

The agent is never invoked here — DM and group writes use the cloud REST
message endpoint, not /chat/stream.
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


async def _register(http, full_name: str) -> dict:
    email = f"smoke-{uuid.uuid4().hex[:6]}@test.example"
    password = "Password1!"
    r = await http.post(
        "/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": full_name},
    )
    assert r.status_code == 201, r.text
    user_id = r.json()["id"]
    r = await http.post(
        "/api/v1/auth/bearer/login",
        data={"username": email, "password": password},
    )
    assert r.status_code == 200, r.text
    return {
        "user_id": user_id,
        "email": email,
        "full_name": full_name,
        "headers": {"Authorization": f"Bearer {r.json()['access_token']}"},
    }


async def main() -> int:
    db_name = f"smoke_dm_group_{uuid.uuid4().hex[:8]}"
    uri = f"mongodb://localhost:27017/{db_name}"
    secret = "smoke-secret"
    os.environ.update(
        {
            "POCKETPAW_LICENSE_KEY": _license_key(secret),
            "POCKETPAW_LICENSE_SECRET": secret,
            "AUTH_SECRET": "smoke-auth-secret-dm-group",
            "POCKETPAW_CLOUD_MONGO_URI": uri,
        }
    )
    os.environ.pop("POCKETPAW_MEMORY_BACKEND", None)

    import pocketpaw_ee.cloud.license as lic_mod
    from beanie import init_beanie
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from motor.motor_asyncio import AsyncIOMotorClient
    from pocketpaw_ee.cloud import mount_cloud
    from pocketpaw_ee.cloud.memory.bootstrap import register_default_backend
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

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
            _banner("1. register Alice (owner) + Bob (invitee), shared workspace")
            alice = await _register(http, "Alice")
            slug = f"smoke-{uuid.uuid4().hex[:6]}"
            r = await http.post(
                "/api/v1/workspaces",
                json={"name": "Smoke WS", "slug": slug},
                headers=alice["headers"],
            )
            workspace = r.json()
            await http.post(
                "/api/v1/auth/set-active-workspace",
                json={"workspace_id": workspace["_id"]},
                headers=alice["headers"],
            )

            # Invite Bob, then have Bob accept
            invitee_email = f"bob-{uuid.uuid4().hex[:6]}@test.example"
            r = await http.post(
                f"/api/v1/workspaces/{workspace['_id']}/invites",
                json={"email": invitee_email, "role": "member"},
                headers=alice["headers"],
            )
            invite_token = r.json()["token"]

            bob_password = "Password1!"
            r = await http.post(
                "/api/v1/auth/register",
                json={
                    "email": invitee_email,
                    "password": bob_password,
                    "full_name": "Bob",
                },
            )
            bob_user_id = r.json()["id"]
            r = await http.post(
                "/api/v1/auth/bearer/login",
                data={"username": invitee_email, "password": bob_password},
            )
            bob_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}
            await http.post(
                f"/api/v1/workspaces/invites/{invite_token}/accept",
                headers=bob_headers,
            )
            await http.post(
                "/api/v1/auth/set-active-workspace",
                json={"workspace_id": workspace["_id"]},
                headers=bob_headers,
            )
            bob = {"user_id": bob_user_id, "headers": bob_headers, "full_name": "Bob"}
            print(f"   alice={alice['user_id']} bob={bob['user_id']} ws={workspace['_id']}")

            # ── DM flow ──────────────────────────────────────────────────
            _banner("2. POST /api/v1/chat/dm/{bob_id} — Alice opens DM with Bob")
            r = await http.post(
                f"/api/v1/chat/dm/{bob['user_id']}",
                headers=alice["headers"],
            )
            assert r.status_code == 200, r.text
            dm = r.json()
            assert dm["type"] == "dm"
            assert sorted(c["_id"] if isinstance(c, dict) else c for c in dm["members"]) == sorted(
                [alice["user_id"], bob["user_id"]]
            )
            print(f"   dm group_id={dm['_id']} type={dm['type']} name={dm['name']!r}")

            _banner("3. populated members carry full_name (so client can resolve titles)")
            # The server returns members as populated objects in some shapes.
            # Verify by re-fetching the group with GET — the DM endpoint may
            # not re-populate, but the group GET will.
            r = await http.get(
                f"/api/v1/chat/groups/{dm['_id']}",
                headers=alice["headers"],
            )
            assert r.status_code == 200, r.text
            grp = r.json()
            members_objs = [m for m in grp.get("members", []) if isinstance(m, dict)]
            print(f"   populated members: {len(members_objs)}/{len(grp['members'])}")
            for m in members_objs:
                print(f"     - _id={m.get('_id')} name={m.get('name')!r} email={m.get('email')!r}")
            # From Alice's perspective, the "other member" is Bob.
            other_for_alice = next(
                (m for m in members_objs if m.get("_id") != alice["user_id"]), None
            )
            assert other_for_alice and other_for_alice["name"] == "Bob", (
                f"expected 'Bob' as other member, got {other_for_alice}"
            )

            _banner("4. Alice sends in DM, Bob reads it")
            r = await http.post(
                f"/api/v1/chat/groups/{dm['_id']}/messages",
                json={"content": "hey bob"},
                headers=alice["headers"],
            )
            assert r.status_code == 200, r.text
            sent = r.json()
            assert sent["sender"] == alice["user_id"]
            assert sent["senderType"] == "user"

            r = await http.get(
                f"/api/v1/chat/groups/{dm['_id']}/messages",
                headers=bob["headers"],
            )
            assert r.status_code == 200, r.text
            page = r.json()
            assert any(m["content"] == "hey bob" for m in page["items"])
            print(f"   bob sees {len(page['items'])} message(s) including alice's")

            _banner("5. Bob replies, Alice reads it")
            r = await http.post(
                f"/api/v1/chat/groups/{dm['_id']}/messages",
                json={"content": "hey alice"},
                headers=bob["headers"],
            )
            assert r.status_code == 200, r.text
            r = await http.get(
                f"/api/v1/chat/groups/{dm['_id']}/messages",
                headers=alice["headers"],
            )
            page = r.json()
            contents = [m["content"] for m in page["items"]]
            assert "hey bob" in contents and "hey alice" in contents
            print(f"   alice sees both: {contents}")

            _banner("6. raw Mongo — DM messages stored as context_type='group'")
            from pocketpaw_ee.cloud.models.message import Message

            rows = await Message.find({"group": dm["_id"]}).to_list()
            for m in rows:
                assert m.context_type == "group"
                assert not m.session_key, "DM messages must not have session_key"
            print(f"   {len(rows)} group-context rows in `messages` (no session_key)")

            # ── Group flow ───────────────────────────────────────────────
            _banner("7. Alice creates a group + adds Bob")
            r = await http.post(
                "/api/v1/chat/groups",
                json={"name": "engineering", "type": "private"},
                headers=alice["headers"],
            )
            assert r.status_code == 200, r.text
            grp = r.json()
            r = await http.post(
                f"/api/v1/chat/groups/{grp['_id']}/members",
                json={"user_ids": [bob["user_id"]], "role": "edit"},
                headers=alice["headers"],
            )
            assert r.status_code == 200, r.text

            _banner("8. Alice posts, Bob reads")
            r = await http.post(
                f"/api/v1/chat/groups/{grp['_id']}/messages",
                json={"content": "kicking off the engineering thread"},
                headers=alice["headers"],
            )
            assert r.status_code == 200, r.text
            r = await http.get(
                f"/api/v1/chat/groups/{grp['_id']}/messages",
                headers=bob["headers"],
            )
            page = r.json()
            assert any(m["content"] == "kicking off the engineering thread" for m in page["items"])
            print(f"   bob sees {len(page['items'])} group message(s)")

            _banner("9. Bob posts back")
            r = await http.post(
                f"/api/v1/chat/groups/{grp['_id']}/messages",
                json={"content": "joining"},
                headers=bob["headers"],
            )
            assert r.status_code == 200, r.text
            r = await http.get(
                f"/api/v1/chat/groups/{grp['_id']}/messages",
                headers=alice["headers"],
            )
            contents = [m["content"] for m in r.json()["items"]]
            assert "joining" in contents

            _banner("10. confirm sender_type for human messages")
            for m in r.json()["items"]:
                assert m["senderType"] == "user", (
                    f"expected senderType='user' for human, got {m['senderType']!r}"
                )
            print("   all group messages: senderType='user' (no agent rows)")

            print("\nSMOKE OK")
            return 0
    finally:
        client2 = AsyncIOMotorClient("mongodb://localhost:27017")
        await client2.drop_database(db_name)
        print(f"\n(dropped {db_name})")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
