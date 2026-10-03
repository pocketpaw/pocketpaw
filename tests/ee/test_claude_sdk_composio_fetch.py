# tests/ee/test_claude_sdk_composio_fetch.py — composio's tool fetch is paid once,
# not on every Claude SDK client build.
#
# ``CloudComposioMcpProvider.build_server`` runs inside every ``_get_mcp_servers``
# call, and a turn can build twice (prewarm, then dispatch). The meta-tool list it
# serves comes from ``GET backend.composio.dev/api/v3.1/tools``. The provider keeps
# a user's tools per (user, backend) for ``composio_mcp_url_ttl_seconds``, so the
# second build must not fetch. The cache is unit-tested in
# tests/cloud/composio/test_providers.py, which the default CI run does not
# collect; this pins it end to end through the backend.

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from claude_agent_sdk import tool
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud.composio import providers as composio_providers
from pocketpaw_ee.extensions import CloudComposioMcpProvider

from pocketpaw.config import Settings
from tests.test_claude_sdk_tool_scoping import _backend


@tool("COMPOSIO_SEARCH_TOOLS", "Search Composio tools", {"type": "object", "properties": {}})
async def _search(args: dict) -> dict:
    return {"content": []}


def test_two_client_builds_fetch_composio_tools_once(monkeypatch: pytest.MonkeyPatch) -> None:
    fetch = MagicMock(return_value=[_search])  # stands in for GET /api/v3.1/tools

    class _Composio:
        def __init__(self, **kwargs: object) -> None:
            self.tools = SimpleNamespace(get=fetch)

    settings = Settings(composio_api_key="test-key", composio_enterprise_id="ent")
    ctx = RequestContext(
        user_id="alice",
        workspace_id="ws1",
        request_id="r1",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )
    monkeypatch.setenv("COMPOSIO_API_KEY", "test-key")  # the provider setdefault()s it
    monkeypatch.setattr(composio_providers, "get_settings", lambda: settings)
    monkeypatch.setattr(composio_providers, "_resolve_ctx", lambda: ctx)
    monkeypatch.setattr(
        composio_providers, "_provider_class_for", lambda kind: (_Composio, MagicMock)
    )
    backend = _backend()
    composio_providers.reset_cache_for_tests()
    try:
        with (
            patch(
                "pocketpaw._registry.providers",
                side_effect=lambda g: (
                    [CloudComposioMcpProvider()] if g == "pocketpaw.mcp_servers" else []
                ),
            ),
            patch("pocketpaw.mcp.config.load_mcp_config", return_value=[]),
        ):
            first = backend._get_mcp_servers()
            second = backend._get_mcp_servers()
    finally:
        composio_providers.reset_cache_for_tests()

    assert "composio" in first and "composio" in second
    assert fetch.call_count == 1
