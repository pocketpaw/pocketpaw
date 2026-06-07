# tests/cloud/chat/test_message_search.py — Coverage for the workspace-wide
# message search added in Cluster E sub-PR 2.
# Uses the shared mongomock-motor `beanie_memory_db` fixture so we exercise
# the real Mongo queries (scope filter + regex escape) without a DB service.
# Created: 2026-04-19

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat import message_service
from pocketpaw_ee.cloud.models.group import Group
from pocketpaw_ee.cloud.models.message import Message


async def _mk_channel(ws: str, name: str, members: list[str]) -> Group:
    g = Group(
        name=name,
        slug=name,
        workspace=ws,
        type="channel",
        owner=members[0],
        members=members,
    )
    await g.insert()
    return g


async def _mk_private(ws: str, name: str, members: list[str]) -> Group:
    g = Group(
        name=name,
        slug=name,
        workspace=ws,
        type="private",
        owner=members[0],
        members=members,
    )
    await g.insert()
    return g


async def _mk_msg(group_id: str, sender: str, content: str) -> Message:
    m = Message(
        context_type="group",
        group=group_id,
        sender=sender,
        sender_type="user",
        content=content,
    )
    await m.insert()
    return m


@pytest.mark.asyncio
async def test_search_workspace_returns_public_channel_hits(beanie_memory_db):
    """A channel in the workspace is visible to any workspace member,
    even without explicit membership. The search should find a hit."""
    ch = await _mk_channel("w1", "general", members=["u-owner"])
    await _mk_msg(str(ch.id), "u-owner", "launch day report is ready")
    await _mk_msg(str(ch.id), "u-owner", "quick standup reminder")

    hits = await message_service.search_workspace_messages("w1", user_id="u-other", query="launch")

    assert [h["content"] for h in hits] == ["launch day report is ready"]


@pytest.mark.asyncio
async def test_search_workspace_skips_private_non_members(beanie_memory_db):
    """A private room the caller is NOT in must not leak results, even
    if the content matches the query perfectly."""
    secret = await _mk_private("w1", "secret-room", members=["u-owner"])
    await _mk_msg(str(secret.id), "u-owner", "top-secret launch plan")

    hits = await message_service.search_workspace_messages("w1", user_id="u-other", query="launch")

    assert hits == []


@pytest.mark.asyncio
async def test_search_workspace_respects_workspace_scope(beanie_memory_db):
    """A hit in workspace B must not appear when searching workspace A."""
    ch_a = await _mk_channel("w-a", "general-a", members=["u"])
    ch_b = await _mk_channel("w-b", "general-b", members=["u"])
    await _mk_msg(str(ch_a.id), "u", "hello from A")
    await _mk_msg(str(ch_b.id), "u", "hello from B")

    hits = await message_service.search_workspace_messages("w-a", "u", "hello")

    assert [h["content"] for h in hits] == ["hello from A"]


@pytest.mark.asyncio
async def test_search_workspace_escapes_regex_metachars(beanie_memory_db):
    """The user's query is escaped — special regex chars should be
    treated as literals, not as the regex operators Mongo would otherwise
    honor. Protects against both injection and ReDoS."""
    ch = await _mk_channel("w1", "general", members=["u"])
    # `.*` and `$` appear in content but as literal text.
    await _mk_msg(str(ch.id), "u", "price is $9.99 per seat")
    await _mk_msg(str(ch.id), "u", "discount: free .* upgrade")

    # If we forgot to escape, `$9.99` would blow up or match the wrong row.
    hits_literal_dollar = await message_service.search_workspace_messages("w1", "u", "$9.99")
    assert len(hits_literal_dollar) == 1
    assert "$9.99" in hits_literal_dollar[0]["content"]

    # `.*` must be treated as text, not as "any character zero-or-more".
    hits_literal_star = await message_service.search_workspace_messages("w1", "u", ".*")
    assert len(hits_literal_star) == 1
    assert ".* upgrade" in hits_literal_star[0]["content"]


@pytest.mark.asyncio
async def test_search_workspace_caps_limit(beanie_memory_db):
    """The `limit` query param is clamped to <=100 to keep a fat query
    from paging the whole journal into memory."""
    ch = await _mk_channel("w1", "general", members=["u"])
    for i in range(5):
        await _mk_msg(str(ch.id), "u", f"message {i} keyword")

    hits = await message_service.search_workspace_messages("w1", "u", "keyword", limit=200)

    # We have 5 rows. The cap is 100; all 5 still come back.
    assert len(hits) == 5


@pytest.mark.asyncio
async def test_search_workspace_empty_query_returns_empty(beanie_memory_db):
    ch = await _mk_channel("w1", "general", members=["u"])
    await _mk_msg(str(ch.id), "u", "some content here")

    assert await message_service.search_workspace_messages("w1", "u", "") == []
    assert await message_service.search_workspace_messages("w1", "u", "   ") == []
