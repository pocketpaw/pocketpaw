"""Fixtures for cloud chat tests.

Chat tests drive the service modules (``chat.message_service`` /
``chat.group_service`` / ``chat.unread_service``) against a real
in-memory mongomock-motor database (``mongo_db`` fixture in
``tests/cloud/conftest.py``) and assert on ``recording_bus.events``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from pocketpaw_ee.cloud.chat.domain import Group, Message

# ---------------------------------------------------------------------------
# Domain object builders (still used by a handful of pure-domain tests)
# ---------------------------------------------------------------------------


def make_domain_message(**overrides: Any) -> Message:
    """Build a chat domain ``Message`` with sensible defaults."""
    base: dict[str, Any] = {
        "id": "m1",
        "context_type": "group",
        "workspace_id": "w1",
        "group": "g1",
        "sender": "u1",
        "sender_type": "user",
        "agent": None,
        "content": "hi",
        "mentions": (),
        "reply_to": None,
        "thread_count": 0,
        "attachments": (),
        "reactions": (),
        "edited": False,
        "edited_at": None,
        "deleted": False,
        "session_key": None,
        "role": None,
        "created_at": datetime.now(UTC),
    }
    base.update(overrides)
    return Message(**base)


def make_domain_group(**overrides: Any) -> Group:
    """Build a chat domain ``Group`` with sensible defaults.

    Accepts list/dict inputs for tuple-typed fields and coerces them.
    """
    base: dict[str, Any] = {
        "id": "g1",
        "workspace_id": "w1",
        "name": "G",
        "slug": "g",
        "description": "",
        "icon": "",
        "color": "",
        "type": "private",
        "members": ("u1",),
        "member_roles": (),
        "agents": (),
        "pinned_messages": (),
        "owner": "u1",
        "archived": False,
        "last_message_at": None,
        "message_count": 0,
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    if isinstance(base["members"], list):
        base["members"] = tuple(base["members"])
    if isinstance(base["agents"], list):
        base["agents"] = tuple(base["agents"])
    if isinstance(base["pinned_messages"], list):
        base["pinned_messages"] = tuple(base["pinned_messages"])
    if isinstance(base["member_roles"], dict):
        base["member_roles"] = tuple(base["member_roles"].items())
    return Group(**base)


# ---------------------------------------------------------------------------
# Legacy beanie_memory_db fixture — kept for tests that still use it
# (mirrors mongo_db in tests/cloud/conftest.py with a different name).
# ---------------------------------------------------------------------------


@pytest.fixture()
async def beanie_memory_db():
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    db_name = f"test_chat_{uuid.uuid4().hex[:8]}"
    client = AsyncMongoMockClient()
    db = client[db_name]

    original = db.list_collection_names

    async def _safe_list_collection_names(*_args, **_kwargs):
        return await original()

    db.list_collection_names = _safe_list_collection_names  # type: ignore[method-assign]

    await init_beanie(database=db, document_models=[*ALL_DOCUMENTS, MemoryFactDoc])
    yield db
