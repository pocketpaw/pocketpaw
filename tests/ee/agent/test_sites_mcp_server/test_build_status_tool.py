# tests/ee/agent/test_sites_mcp_server/test_build_status_tool.py
# Created: 2026-08-11 (feat/sites-react-edit-lane, RX-4) — the AGENT-facing half of the
# build-visibility fix. Two things under test:
#
#   1. ``_publish_handler``'s widened success body. It used to hand-build five keys
#      (id / pocket_id / name / url / deployed) while ``_to_response`` — the wire the
#      frontend polls — also carried ``build_status`` / ``build_reason`` /
#      ``build_job_id``. On react (the only engine with ``build_runs_async`` True) that
#      meant a first publish handed the agent ``url=""`` while the create skill told it
#      to "show the user the returned url", and a re-publish handed it the PREVIOUS
#      deploy's url as though the change were live.
#   2. The new READ-ONLY ``get_site_build_status`` tool, which is the only way to learn
#      how an async build ENDED — ``publish`` returns before the build starts.
#
# THE NARRATION IS THE FIX, not the data. A boolean the agent does not consult changes
# nothing, so the assertions below check the ``message`` prose as strictly as the
# fields: on a queued build it must NOT read as a live site, and it must point at the
# status tool so the queued state is not a dead end.
#
# Registration gets its own tests because id-in-tuple and tool-registered are separate
# facts and the /sites allow-list is a hard whitelist: an id missing from
# ``SITES_TOOL_IDS`` is filtered out with NO error, and every handler-level test in this
# file would still pass.
"""Tests for the widened publish response and the get_site_build_status tool."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


class _Doc:
    """The Site-doc fields the publish handler reads onto its response."""

    def __init__(self, **kw) -> None:
        self.id = kw.pop("id", "site_abc")
        self.pocket_id = kw.pop("pocket_id", "pk1")
        self.name = kw.pop("name", "Bright Smile")
        self.url = kw.pop("url", "")
        self.deployed = kw.pop("deployed", False)
        for key in ("build_status", "build_reason", "build_job_id"):
            if key in kw:
                setattr(self, key, kw[key])


def _body(envelope: dict) -> dict:
    assert not envelope.get("is_error"), envelope
    return json.loads(envelope["content"][0]["text"])


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_status_tool_id_is_on_the_shared_allowlist(self) -> None:
        """The assertion that catches the whole class of bug: ``sites_allow`` is built
        from ``SITES_TOOL_IDS`` and is a hard whitelist on /sites, so a tool absent from
        the tuple never reaches the agent and errors nowhere."""
        from pocketpaw_ee.agent.mcp_servers.sites import (
            GET_SITE_BUILD_STATUS_TOOL_ID,
            SITES_TOOL_IDS,
        )

        assert (
            GET_SITE_BUILD_STATUS_TOOL_ID == "mcp__pocketpaw_sites_manager__get_site_build_status"
        )
        assert GET_SITE_BUILD_STATUS_TOOL_ID in SITES_TOOL_IDS

    def test_provider_advertises_the_status_tool(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import GET_SITE_BUILD_STATUS_TOOL_ID
        from pocketpaw_ee.extensions import CloudSitesMcpProvider

        assert GET_SITE_BUILD_STATUS_TOOL_ID in CloudSitesMcpProvider().tool_ids()

    def test_the_built_server_lists_the_status_tool(self) -> None:
        """An id in the tuple with no tool behind it whitelists a name that never
        answers, so the registration is checked separately from the constant."""
        import asyncio

        from mcp import types
        from pocketpaw_ee.agent.mcp_servers.sites import build_sites_manager_server

        built = build_sites_manager_server()
        if built is None:
            pytest.skip("claude_agent_sdk not installed")
        _name, server = built
        handler = server["instance"].request_handlers[types.ListToolsRequest]
        listed = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
        tool = next((t for t in listed.root.tools if t.name == "get_site_build_status"), None)
        assert tool is not None, sorted(t.name for t in listed.root.tools)
        assert (tool.inputSchema or {}).get("required") == ["pocket_id"]
        # The description must gate on is_live, or the agent falls back to reading
        # ``url``/``deployed`` directly, which is exactly the bug.
        assert "is_live" in (tool.description or "")


# ---------------------------------------------------------------------------
# The widened publish response
# ---------------------------------------------------------------------------


class TestPublishResponseCarriesBuildState:
    @pytest.mark.asyncio
    async def test_a_live_publish_says_live_and_gives_the_url(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites as mcp

        doc = _Doc(url="https://bright.paw.test/", deployed=True, build_status="built")
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.publish_pocket", new=AsyncMock(return_value=doc)),
        ):
            body = _body(await mcp._publish_handler({"pocket_id": "pk1"}))

        assert body["site"]["is_live"] is True
        assert body["site"]["url"] == "https://bright.paw.test/"
        assert body["site"]["build_status"] == "built"
        assert "live" in body["message"].lower()

    @pytest.mark.asyncio
    async def test_a_first_async_publish_does_not_read_as_a_live_site(self) -> None:
        """The reported defect. The row is exactly what ``_enqueue_static_build`` inserts
        on a first react publish: ``url=""``, ``deployed=False``, ``build_status="queued"``.

        The old five-key body gave the agent an empty url and nothing else, next to a
        skill instruction to show it. So this asserts BOTH halves of the fix: the fields
        say not-live, and the prose says so too and points at the status tool.

        THE MUTATION THAT BREAKS THIS: revert the body to the five hand-built keys.
        """
        from pocketpaw_ee.agent.mcp_servers import sites as mcp

        doc = _Doc(url="", deployed=False, build_status="queued", build_job_id="job-1")
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.publish_pocket", new=AsyncMock(return_value=doc)),
        ):
            body = _body(await mcp._publish_handler({"pocket_id": "pk1"}))

        site = body["site"]
        assert site["is_live"] is False
        assert site["build_in_progress"] is True
        assert site["build_status"] == "queued"
        assert site["build_job_id"] == "job-1"
        assert site["url"] == ""

        message = body["message"].lower()
        # The prose has to carry the conclusion, because the original defect was
        # narration rather than data: a boolean the agent does not consult fixes
        # nothing. Assert what the message TELLS the agent to do, not the absence of
        # substrings — "is live" legitimately appears inside "do NOT say it is live".
        assert "not live" in message
        assert "do not show" in message
        # It must route the agent to the follow-up read, or "queued" is a dead end.
        assert "get_site_build_status" in body["message"]

    @pytest.mark.asyncio
    async def test_a_republish_over_a_live_site_is_not_reported_as_current(self) -> None:
        """The subtler half: a rebuild deliberately KEEPS the previous deploy's url and
        ``deployed=True`` so a working site is never reported as down. Both of those say
        "live" while the url serves the pre-change page, so the response must lean on
        ``build_status`` and say the change is not live yet."""
        from pocketpaw_ee.agent.mcp_servers import sites as mcp

        doc = _Doc(url="https://bright.paw.test/", deployed=True, build_status="building")
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.publish_pocket", new=AsyncMock(return_value=doc)),
        ):
            body = _body(await mcp._publish_handler({"pocket_id": "pk1"}))

        assert body["site"]["is_live"] is False
        assert body["site"]["build_in_progress"] is True
        # The url is still reported (it is real), but the message forbids showing it as
        # the current version.
        assert body["site"]["url"] == "https://bright.paw.test/"
        assert "previous version" in body["message"].lower()

    @pytest.mark.asyncio
    async def test_a_failed_build_relays_the_reason_not_a_url(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites as mcp

        doc = _Doc(url="", deployed=False, build_status="failed", build_reason="build:exit_1")
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.publish_pocket", new=AsyncMock(return_value=doc)),
        ):
            body = _body(await mcp._publish_handler({"pocket_id": "pk1"}))

        assert body["site"]["build_reason"] == "build:exit_1"
        assert body["site"]["is_live"] is False
        assert "failed" in body["message"].lower()

    @pytest.mark.asyncio
    async def test_the_original_five_keys_are_still_present(self) -> None:
        """Widening, not replacing. The frontend-facing shape of this tool is contract
        for anything already reading it, so the old keys must survive."""
        from pocketpaw_ee.agent.mcp_servers import sites as mcp

        doc = _Doc(url="https://x/", deployed=True, build_status="built")
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.publish_pocket", new=AsyncMock(return_value=doc)),
        ):
            body = _body(await mcp._publish_handler({"pocket_id": "pk1"}))

        assert set(body["site"]) >= {"id", "pocket_id", "name", "url", "deployed"}
        assert body["site"]["id"] == "site_abc"
        assert body["site"]["pocket_id"] == "pk1"


# ---------------------------------------------------------------------------
# get_site_build_status
# ---------------------------------------------------------------------------


class TestStatusHandler:
    @pytest.mark.asyncio
    async def test_settled_build_is_reported_live(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites as mcp

        state = {
            "pocket_id": "pk1",
            "site_id": "site_abc",
            "name": "Bright Smile",
            "published": True,
            "url": "https://bright.paw.test/",
            "deployed": True,
            "build_status": "built",
            "build_reason": None,
            "build_job_id": "job-9",
            "build_in_progress": False,
            "is_live": True,
        }
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.site_build_status",
                new=AsyncMock(return_value=state),
            ),
        ):
            body = _body(await mcp._get_site_build_status_handler({"pocket_id": "pk1"}))

        assert body["ok"] is True
        assert body["is_live"] is True
        assert body["url"] == "https://bright.paw.test/"
        assert "live" in body["message"].lower()

    @pytest.mark.asyncio
    async def test_in_flight_build_is_not_reported_live(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites as mcp

        state = {
            "pocket_id": "pk1",
            "site_id": "site_abc",
            "name": "n",
            "published": True,
            "url": "",
            "deployed": False,
            "build_status": "building",
            "build_reason": None,
            "build_job_id": "job-9",
            "build_in_progress": True,
            "is_live": False,
        }
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.site_build_status",
                new=AsyncMock(return_value=state),
            ),
        ):
            body = _body(await mcp._get_site_build_status_handler({"pocket_id": "pk1"}))

        assert body["is_live"] is False
        message = body["message"].lower()
        assert "still building" in message
        assert "not live" in message

    @pytest.mark.asyncio
    async def test_never_published_is_answered_not_errored(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites as mcp

        state = {
            "pocket_id": "pk1",
            "site_id": None,
            "name": "",
            "published": False,
            "url": "",
            "deployed": False,
            "build_status": "none",
            "build_reason": None,
            "build_job_id": None,
            "build_in_progress": False,
            "is_live": False,
        }
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.site_build_status",
                new=AsyncMock(return_value=state),
            ),
        ):
            body = _body(await mcp._get_site_build_status_handler({"pocket_id": "pk1"}))

        assert body["published"] is False
        assert "never been published" in body["message"].lower()

    @pytest.mark.asyncio
    async def test_failed_build_relays_the_reason(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites as mcp

        state = {
            "pocket_id": "pk1",
            "site_id": "s",
            "name": "n",
            "published": True,
            "url": "",
            "deployed": False,
            "build_status": "failed",
            "build_reason": "infra:sandbox_lost",
            "build_job_id": "job-9",
            "build_in_progress": False,
            "is_live": False,
        }
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.site_build_status",
                new=AsyncMock(return_value=state),
            ),
        ):
            body = _body(await mcp._get_site_build_status_handler({"pocket_id": "pk1"}))

        assert body["build_reason"] == "infra:sandbox_lost"
        assert "build_reason" in body["message"]

    @pytest.mark.asyncio
    async def test_missing_identity_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites as mcp

        with patch.object(mcp, "_identity", return_value=(None, None)):
            out = await mcp._get_site_build_status_handler({"pocket_id": "pk1"})
        assert out.get("is_error") is True
        assert "workspace and user context" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_pocket_id_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites as mcp

        with patch.object(mcp, "_identity", return_value=("ws1", "u1")):
            out = await mcp._get_site_build_status_handler({})
        assert out.get("is_error") is True
        assert "`pocket_id`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_cloud_error_is_relayed_by_code(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites as mcp
        from pocketpaw_ee.cloud._core.errors import Forbidden

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.site_build_status",
                new=AsyncMock(side_effect=Forbidden("pocket.access_denied", "nope")),
            ),
        ):
            out = await mcp._get_site_build_status_handler({"pocket_id": "pk1"})
        assert out.get("is_error") is True
        assert "pocket.access_denied" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_an_error_not_a_success(self) -> None:
        """A read that blew up must not surface as "nothing is building"."""
        from pocketpaw_ee.agent.mcp_servers import sites as mcp

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.site_build_status",
                new=AsyncMock(side_effect=RuntimeError("mongo went away")),
            ),
        ):
            out = await mcp._get_site_build_status_handler({"pocket_id": "pk1"})
        assert out.get("is_error") is True
        assert "could not read the build status" in out["content"][0]["text"]
