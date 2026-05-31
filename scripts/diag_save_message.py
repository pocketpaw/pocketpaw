"""Simulate the WebSocket persistence path and surface any silent error.

This runs the same code the WS adapter runs when you send a chat message —
but with logger upgraded to DEBUG and no exception swallowing.
"""

from __future__ import annotations

import asyncio
import logging


async def main() -> int:
    logging.basicConfig(
        level=logging.DEBUG, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    print("1. init_cloud_db ...")
    from pocketpaw_ee.cloud.shared.db import init_cloud_db

    await init_cloud_db("mongodb://localhost:27017/paw-enterprise")

    print("\n2. _ensure_cloud_session('diag-chat-1') ...")
    from pocketpaw_ee.cloud.shared.chat_persistence import _ensure_cloud_session

    info = await _ensure_cloud_session("diag-chat-1")
    print(f"   returned: {info}")
    if not info:
        print("   -> _ensure_cloud_session returned None; persistence will be skipped.")
        return 2

    print("\n3. save_user_message('diag-chat-1', 'hello from diag') ...")
    from pocketpaw_ee.cloud.models.message import Message

    msg = Message(
        group=info["group_id"],
        sender=info["user_id"],
        sender_type="user",
        content="hello from diag",
    )
    await msg.insert()
    print(f"   saved message id={msg.id}")

    print("\n4. verify in DB ...")
    found = await Message.find({"group": info["group_id"]}).to_list()
    print(f"   {len(found)} messages in group {info['group_id']}")
    for m in found:
        print(f"     - {m.id} {m.sender_type}: {m.content!r}")

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
