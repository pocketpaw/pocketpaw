"""Pre-stream errors must land as HTTP 4xx, not SSE error frames."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from httpx import AsyncClient
from pocketpaw_ee.cloud.chat import agent_router as mod
from pocketpaw_ee.cloud.chat.agent_service import InvalidScope
from pocketpaw_ee.cloud.shared.errors import CloudError, NotFound


@pytest.mark.asyncio
async def test_invalid_scope_returns_400(cloud_app_client: AsyncClient):
    async def raise_invalid(**_):
        raise InvalidScope("nope")

    with patch.object(mod, "resolve_scope_context", raise_invalid):
        resp = await cloud_app_client.post("/cloud/chat/group/g1/agent", json={"content": "x"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "scope.invalid"


@pytest.mark.asyncio
async def test_not_member_returns_403(cloud_app_client: AsyncClient):
    async def raise_forbidden(**_):
        raise CloudError(403, "group.not_member", "Caller is not a group member")

    with patch.object(mod, "resolve_scope_context", raise_forbidden):
        resp = await cloud_app_client.post("/cloud/chat/group/g1/agent", json={"content": "x"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "group.not_member"


@pytest.mark.asyncio
async def test_not_found_returns_404(cloud_app_client: AsyncClient):
    async def raise_nf(**_):
        raise NotFound("group", "g1")

    with patch.object(mod, "resolve_scope_context", raise_nf):
        resp = await cloud_app_client.post("/cloud/chat/group/g1/agent", json={"content": "x"})
    # NotFound maps to 404 — either because it inherits from CloudError with
    # status_code=404, or because the router explicitly handles it.
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "group.not_found"
