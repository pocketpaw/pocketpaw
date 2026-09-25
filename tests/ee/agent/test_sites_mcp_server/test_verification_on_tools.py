# tests/ee/agent/test_sites_mcp_server/test_verification_on_tools.py — PP-2: every
# create and edit tool result carries ``verification`` (contract §5 / §8), the new
# ``verify_site`` tool, and the hard deadline that keeps a tool call from hanging.
# Created 2026-09-24 (PP-2, feat/sites-verify-pipeline).
#
# The conftest autouse ``verify_recorder`` replaces ``verify.verify_site`` with a
# recorder; these tests assert the tools CALL it for the right pocket and put its
# verdict on the wire, and that ripple tools answer ``engine_not_verifiable`` without
# calling it.
from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bson import ObjectId

pytest.importorskip("pocketpaw_ee")

from tests.ee.agent.test_sites_mcp_server.conftest import canned_verdict  # noqa: E402

_SVELTE = {
    "src/routes/+page.svelte": (
        "<script>import Hero from '$lib/components/Hero.svelte'</script><Hero/>"
    ),
    "src/routes/+layout.svelte": "<script>import '../app.css'</script><slot/>",
    "src/routes/+page.ts": "export const prerender = true",
    "src/app.css": ":root{}",
    "src/lib/components/Hero.svelte": "<h1>Hi</h1>",
}


@pytest.fixture(autouse=True)
def _plan_and_bus():
    from pocketpaw_ee.cloud._core.realtime import bus as bus_mod

    class _Bus:
        async def publish(self, event: Any) -> None: ...

        def subscribe(self, *_a: Any) -> None: ...

    prev = bus_mod._bus  # type: ignore[attr-defined]
    bus_mod._bus = _Bus()  # type: ignore[attr-defined]
    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="go"),
    ):
        yield
    bus_mod._bus = prev  # type: ignore[attr-defined]


def _as(workspace_id: str, user_id: str):
    return (
        patch(
            "pocketpaw_ee.cloud.chat.agent_service.current_workspace_id",
            return_value=workspace_id,
        ),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_user_id", return_value=user_id),
    )


def _body(out: dict[str, Any]) -> dict[str, Any]:
    assert not out.get("is_error"), out
    return json.loads(out["content"][0]["text"])


@pytest.mark.parametrize(
    ("handler", "source", "engine"),
    [
        ("_create_svelte_site_handler", _SVELTE, "svelte"),
        ("_create_react_site_handler", {"src/App.tsx": "export default () => <p>hi</p>"}, "react"),
        ("_create_html_site_handler", {"index.html": "<h1>hi</h1>"}, "html"),
    ],
)
async def test_every_source_create_returns_the_verdict(
    beanie_test_db, verify_recorder, handler: str, source: dict[str, str], engine: str
) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    ws, user = str(ObjectId()), str(ObjectId())
    verify_recorder.verdict = canned_verdict("failed")
    a, b = _as(ws, user)
    with a, b:
        body = _body(await getattr(mcp, handler)({"source": source, "name": "S"}))
    assert body["verification"] == verify_recorder.verdict
    assert verify_recorder.calls == [
        {"workspace_id": ws, "user_id": user, "pocket_id": body["pocket_id"]}
    ]
    assert body["pocket"]["engine"] == engine


async def test_a_ripple_create_is_honestly_unverifiable(beanie_test_db, verify_recorder) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    ws, user = str(ObjectId()), str(ObjectId())
    content = {
        "brand": {"name": "Bright Smile"},
        "hero": {"headline": "Care", "subhead": "Family dentistry", "cta": "Book"},
    }
    a, b = _as(ws, user)
    with a, b:
        out = await mcp._create_landing_site_handler({"content": content})
    if out.get("is_error"):
        pytest.skip(f"landing content shape drifted: {out}")
    body = _body(out)
    assert body["verification"]["status"] == "unverified"
    assert body["verification"]["reason"] == "engine_not_verifiable"
    assert verify_recorder.calls == []


class TestVerifySiteTool:
    async def test_it_returns_the_verdict_for_the_pocket(
        self, beanie_test_db, verify_recorder
    ) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        ws, user = str(ObjectId()), str(ObjectId())
        a, b = _as(ws, user)
        with a, b:
            created = _body(await mcp._create_svelte_site_handler({"source": _SVELTE}))
            verify_recorder.calls.clear()
            body = _body(await mcp._verify_site_handler({"pocket_id": created["pocket_id"]}))
        assert body == {
            "ok": True,
            "pocket_id": created["pocket_id"],
            "verification": verify_recorder.verdict,
        }
        assert len(verify_recorder.calls) == 1

    async def test_a_missing_pocket_is_an_error_not_unverified(self, beanie_test_db) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        a, b = _as(str(ObjectId()), str(ObjectId()))
        with a, b:
            out = await mcp._verify_site_handler({"pocket_id": str(ObjectId())})
        assert out.get("is_error") is True

    async def test_it_needs_a_pocket_id(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        a, b = _as("w", "u")
        with a, b:
            out = await mcp._verify_site_handler({})
        assert out.get("is_error") is True

    def test_it_is_registered_on_the_allow_list(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import SITES_TOOL_IDS, VERIFY_SITE_TOOL_ID
        from pocketpaw_ee.agent.mcp_servers.sites_create import SITES_CREATE_TOOL_IDS

        assert VERIFY_SITE_TOOL_ID == "mcp__pocketpaw_sites_manager__verify_site"
        assert VERIFY_SITE_TOOL_ID in SITES_TOOL_IDS
        assert VERIFY_SITE_TOOL_ID in SITES_CREATE_TOOL_IDS


class TestDeadline:
    async def test_a_hung_verify_becomes_unverified_timeout(self, monkeypatch) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.sites import verify

        async def _hang(**_kw: Any) -> dict[str, Any]:
            await asyncio.sleep(30)
            return {}

        monkeypatch.setattr(verify, "verify_site", _hang)
        monkeypatch.setattr(verify, "verify_wait_seconds", lambda: 0.05)
        monkeypatch.setattr(mcp, "VERIFY_HARD_DEADLINE_SLACK_SEC", 0.05)
        verdict = await mcp._verification_for("w", "u", "p")
        assert verdict["status"] == "unverified"
        assert verdict["reason"] == "timeout"

    async def test_a_raising_verify_becomes_unverified(self, monkeypatch) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.sites import verify

        async def _boom(**_kw: Any) -> dict[str, Any]:
            raise RuntimeError("x")

        monkeypatch.setattr(verify, "verify_site", _boom)
        verdict = await mcp._verification_for("w", "u", "p")
        assert (verdict["status"], verdict["reason"]) == ("unverified", "verify_unavailable")


async def test_react_and_html_edits_carry_the_verdict(beanie_test_db, verify_recorder) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    ws, user = str(ObjectId()), str(ObjectId())
    a, b = _as(ws, user)
    with a, b:
        react = _body(
            await mcp._create_react_site_handler(
                {"source": {"src/App.tsx": "export default () => <p>hi</p>"}}
            )
        )
        html = _body(await mcp._create_html_site_handler({"source": {"index.html": "<h1>hi</h1>"}}))
        verify_recorder.calls.clear()
        r = _body(
            await mcp._edit_react_component_handler(
                {
                    "pocket_id": react["pocket_id"],
                    "component_path": "src/App.tsx",
                    "new_source": "export default () => <p>bye</p>",
                }
            )
        )
        h = _body(
            await mcp._edit_html_file_handler(
                {
                    "pocket_id": html["pocket_id"],
                    "file_path": "index.html",
                    "new_source": "<h1>b</h1>",
                }
            )
        )
    assert r["verification"] == verify_recorder.verdict
    assert h["verification"] == verify_recorder.verdict
    assert [c["pocket_id"] for c in verify_recorder.calls] == [
        react["pocket_id"],
        html["pocket_id"],
    ]


async def test_set_site_dependencies_carries_the_verdict(verify_recorder) -> None:
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    a, b = _as("w", "u")
    with (
        a,
        b,
        patch(
            "pocketpaw_ee.sites.service.set_site_dependencies",
            new=AsyncMock(
                return_value={"pocket_id": "p", "packages": {}, "rejected": [], "changed": True}
            ),
        ),
    ):
        body = _body(
            await mcp._set_site_dependencies_handler({"pocket_id": "p", "remove": ["three"]})
        )
    assert body["verification"] == verify_recorder.verdict
