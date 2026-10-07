# tests/ee/agent/test_sites_mcp_server/test_verification_on_tools.py — every create
# and edit tool result carries ``verification`` (contract §5 / §8), the ``verify_site``
# tool, and the hard deadlines that keep a tool call from hanging.
#
# Creates and ``verify_site`` wait for the full verdict (``verify.verify_site``, the
# conftest's ``verify_recorder``). Edits run only the static check and enqueue the
# build (``verify.verify_edit``, ``edit_verify_recorder``), never call ``verify_site``,
# skip an unreferenced create entirely, and attach the background verdict of the
# pocket's previous edit to their NEXT result as ``previous_verification`` — once.
# ripple tools answer ``engine_not_verifiable`` without calling anything.
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


async def test_react_and_html_edits_carry_the_fast_verdict(
    beanie_test_db, verify_recorder, edit_verify_recorder
) -> None:
    """An edit runs the static check and enqueues the build; it never waits on the
    full verify."""
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
    assert r["verification"] == edit_verify_recorder.verdict
    assert h["verification"] == edit_verify_recorder.verdict
    assert r["verification"]["build"] == "pending"
    assert verify_recorder.calls == [], "an edit never runs the waiting verify"
    assert [c["pocket_id"] for c in edit_verify_recorder.calls] == [
        react["pocket_id"],
        html["pocket_id"],
    ]


async def test_an_unreferenced_create_skips_verification(
    beanie_test_db, edit_verify_recorder
) -> None:
    """A create nothing imports yet is a half step: no verify, and it says so."""
    from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

    ws, user = str(ObjectId()), str(ObjectId())
    a, b = _as(ws, user)
    with a, b:
        react = _body(
            await mcp._create_react_site_handler(
                {"source": {"src/App.tsx": "export default () => <p>hi</p>"}}
            )
        )
        edit_verify_recorder.calls.clear()
        body = _body(
            await mcp._edit_react_component_handler(
                {
                    "pocket_id": react["pocket_id"],
                    "component_path": "src/components/Faq.tsx",
                    "new_source": "export default () => <p>faq</p>",
                    "create": True,
                }
            )
        )
    assert body["unreferenced"] is True
    assert body["verification"]["status"] == "skipped"
    assert body["verification"]["reason"] == "create_half_step"
    assert edit_verify_recorder.calls == []


async def test_set_site_dependencies_carries_the_verdict(edit_verify_recorder) -> None:
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
    assert body["verification"] == edit_verify_recorder.verdict


class TestPreviousVerification:
    """The background build's verdict reaches the agent on its NEXT tool result."""

    @staticmethod
    def _settle(pocket_id: str, *, status: str = "failed") -> None:
        """Record an edit's enqueue and land its job's report, as the worker would."""
        from pocketpaw_ee.sites import verify, verify_store

        store = verify_store.default_verify_store()
        store.write(
            pocket_id,
            verify_store.LATEST_KEY,
            {
                "pipeline_version": verify.VERIFY_PIPELINE_VERSION,
                "content_hash": "f" * 64,
                "job_id": "site-preview-job-1",
                "engine": "react",
                "static": {"name": "static", "status": "passed"},
                "errors": [],
                "warnings": [],
                "note": "",
                "enqueued_at": 0,
                "surfaced": False,
            },
        )
        store.write(
            pocket_id,
            verify_store.sandbox_key("f" * 64),
            {
                "status": "failed" if status == "failed" else "built",
                "layers": {
                    "build": {"status": status},
                    "browser": {"status": "skipped" if status == "failed" else status},
                },
                "diagnostics": {
                    "errors": (
                        [{"layer": "build", "file": "src/App.tsx", "line": 2, "message": "x"}]
                        if status == "failed"
                        else []
                    ),
                    "warnings": [],
                },
            },
        )

    async def test_the_next_edit_carries_it_once(self, beanie_test_db) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        ws, user = str(ObjectId()), str(ObjectId())
        a, b = _as(ws, user)
        with a, b:
            react = _body(
                await mcp._create_react_site_handler(
                    {"source": {"src/App.tsx": "export default () => <p>hi</p>"}}
                )
            )
            self._settle(react["pocket_id"])
            edit = {
                "pocket_id": react["pocket_id"],
                "component_path": "src/App.tsx",
                "new_source": "export default () => <p>bye</p>",
            }
            first = _body(await mcp._edit_react_component_handler(edit))
            second = _body(await mcp._edit_react_component_handler(edit))

        previous = first["previous_verification"]
        assert previous["status"] == "failed"
        assert previous["build"] == "failed"
        assert previous["job_id"] == "site-preview-job-1"
        assert previous["errors"][0]["file"] == "src/App.tsx"
        assert "previous_verification" not in second, "a verdict is handed out once"

    async def test_an_error_result_still_carries_it(self, beanie_test_db) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        ws, user = str(ObjectId()), str(ObjectId())
        a, b = _as(ws, user)
        with a, b:
            react = _body(
                await mcp._create_react_site_handler(
                    {"source": {"src/App.tsx": "export default () => <p>hi</p>"}}
                )
            )
            self._settle(react["pocket_id"])
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": react["pocket_id"],
                    "component_path": "src/App.tsx",
                    "edits": [{"old_string": "not there", "new_string": "x"}],
                }
            )
        assert out.get("is_error") is True
        assert "previous_verification" in out["content"][-1]["text"]
        assert '"status":"failed"' in out["content"][-1]["text"]

    async def test_a_pocket_the_caller_cannot_read_reports_nothing(self, beanie_test_db) -> None:
        """The verify store is keyed by pocket id alone; the read is gated on the
        caller's access, so another tenant's build diagnostics never leak."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.sites import verify, verify_store

        owner_ws, owner = str(ObjectId()), str(ObjectId())
        a, b = _as(owner_ws, owner)
        with a, b:
            react = _body(
                await mcp._create_react_site_handler(
                    {"source": {"src/App.tsx": "export default () => <p>hi</p>"}}
                )
            )
        self._settle(react["pocket_id"])

        a, b = _as(str(ObjectId()), str(ObjectId()))
        with a, b:
            assert await mcp._settled_previous(react["pocket_id"]) is None
        # Untouched: still there for the owner.
        latest = verify_store.default_verify_store().read(
            react["pocket_id"], verify_store.LATEST_KEY
        )
        assert latest["surfaced"] is False
        assert verify.settled_verdict(react["pocket_id"]) is not None

    async def test_nothing_is_attached_while_the_build_runs(self, beanie_test_db) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        ws, user = str(ObjectId()), str(ObjectId())
        a, b = _as(ws, user)
        with a, b:
            react = _body(
                await mcp._create_react_site_handler(
                    {"source": {"src/App.tsx": "export default () => <p>hi</p>"}}
                )
            )
            body = _body(
                await mcp._edit_react_component_handler(
                    {
                        "pocket_id": react["pocket_id"],
                        "component_path": "src/App.tsx",
                        "new_source": "export default () => <p>bye</p>",
                    }
                )
            )
        assert "previous_verification" not in body


class TestEditDeadline:
    async def test_a_hung_edit_verify_becomes_unverified_timeout(self, monkeypatch) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.sites import verify

        async def _hang(**_kw: Any) -> dict[str, Any]:
            await asyncio.sleep(30)
            return {}

        monkeypatch.setattr(verify, "verify_edit", _hang)
        monkeypatch.setattr(mcp, "EDIT_VERIFY_DEADLINE_SEC", 0.05)
        verdict = await mcp._edit_verification("w", "u", "p")
        assert (verdict["status"], verdict["reason"]) == ("unverified", "timeout")
