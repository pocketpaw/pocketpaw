# tests/ee/agent/test_sites_mcp_server/test_edit_html_file.py
# Created: 2026-08-13 (feat/sites-html-edit-lane, HE-10) — coverage for the
# html-track edit tool ``edit_html_file`` on the in-process
# ``pocketpaw_sites_manager`` server. Modelled on its react sibling
# (test_edit_react_component.py), with the differences that matter to the agent:
#
#   1. Registration — the tool id rides the SAME server allowlist as publish + the
#      five create tools + the two sibling edit tools, and the extension provider
#      advertises it. Registration is not a formality here: it is the entire
#      difference between "the agent can change an html site" and "the agent's only
#      move is a second create_html_site that mints a SECOND site pocket at a
#      SECOND url".
#   2. THE ARGUMENT IS ``file_path``, NOT ``component_path``. An html site has no
#      component model — ``html-scaffold.ts`` writes the source map verbatim into
#      the served directory — so the schema names files. A test pins this because
#      "make it consistent with the siblings" is the obvious refactor and it would
#      break every call the description teaches.
#   3. PATH GUIDANCE. react's tool tells the agent to write under ``src/``; doing
#      that here produces a site whose pages the edge never serves, because an html
#      site's entry document is ``index.html`` at the ROOT. The description has to
#      say so explicitly, and a test asserts it does.
#   4. NARRATION — the success body must not claim the change is published or live.
#
# There is deliberately NO SmokeGateFailed case: html runs no build at all, so
# nothing can fail a smoke gate. The service-level persist + guard behaviour lives
# in tests/ee/sites/test_html_file_edit.py; this file pins the agent surface.
"""Tests for the html-track file edit tool (edit_html_file)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


@pytest.fixture(autouse=True)
def _default_sites_plan():
    """``edit_html_file`` runs the shared Sites plan gate before it touches the
    service. These tests use synthetic workspace ids with no seeded Workspace doc,
    so default the plan to one that unlocks Sites ("go"); the denial path has its
    own test below."""
    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="go"),
    ):
        yield


def _ok_result(
    *, created: bool = False, file_path: str = "index.html", unreferenced: bool = False
) -> dict:
    """What the service returns on a successful edit.

    ``unreferenced`` mirrors the service's own key (see
    ``sites/service.py::edit_html_file``): True when a CREATE landed a file nothing
    in the site links to. The stub carries it so this file pins the real shape — a
    stub that lags the service tests a payload the handler never receives."""
    return {
        "pocket_id": "pk1",
        "file_path": file_path,
        "created": created,
        "unreferenced": unreferenced,
    }


def _listed_tool(name: str = "edit_html_file"):
    """Fetch the tool as the SDK would list it, or skip when the SDK is absent."""
    import asyncio

    from mcp import types
    from pocketpaw_ee.agent.mcp_servers.sites import build_sites_manager_server

    built = build_sites_manager_server()
    if built is None:  # claude_agent_sdk absent — nothing to assert
        pytest.skip("claude_agent_sdk not installed")
    _name, server = built
    handler = server["instance"].request_handlers[types.ListToolsRequest]
    listed = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
    return listed.root.tools


# ---------------------------------------------------------------------------
# Registration — the tool rides the shared sites_manager allowlist
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_tool_id_on_shared_server_allowlist(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import EDIT_HTML_FILE_TOOL_ID, SITES_TOOL_IDS

        assert EDIT_HTML_FILE_TOOL_ID == "mcp__pocketpaw_sites_manager__edit_html_file"
        assert EDIT_HTML_FILE_TOOL_ID in SITES_TOOL_IDS

    def test_provider_advertises_edit_tool_id(self) -> None:
        """``tool_ids()`` feeds the claude_sdk allowlist loop AND the /sites surface
        scope. A tool the provider does not advertise is registered on the server
        and still unreachable from the surface that needs it."""
        from pocketpaw_ee.agent.mcp_servers.sites import EDIT_HTML_FILE_TOOL_ID
        from pocketpaw_ee.extensions import CloudSitesMcpProvider

        assert EDIT_HTML_FILE_TOOL_ID in CloudSitesMcpProvider().tool_ids()

    def test_the_built_server_actually_lists_the_tool(self) -> None:
        """The id constant and the registered tool are two different facts, and only
        this one proves the SDK will dispatch a call. An id in ``SITES_TOOL_IDS``
        with no tool behind it allow-lists a name that does not answer.

        Mutation: drop ``edit_html_file`` from the ``tools=[...]`` list in
        ``build_sites_manager_server`` and this fails while every id assertion above
        keeps passing."""
        names = {t.name for t in _listed_tool()}
        assert "edit_html_file" in names, sorted(names)

    def test_every_engine_that_can_be_created_can_now_be_edited(self) -> None:
        """The point of HE-10 stated as an invariant. html was the last engine with a
        create tool and no edit tool; if a future engine repeats that, this fails."""
        names = {t.name for t in _listed_tool()}
        assert {"create_html_site", "edit_html_file"} <= names
        assert {"create_react_site", "edit_react_component"} <= names
        assert {"create_svelte_site", "edit_svelte_component"} <= names

    def test_the_schema_names_a_file_not_a_component(self) -> None:
        """An html site has no components. Renaming this to ``component_path`` for
        symmetry with the siblings would contradict every example in the tool's own
        description and break the calls it teaches."""
        tool = next(t for t in _listed_tool() if t.name == "edit_html_file")
        props = (tool.inputSchema or {}).get("properties") or {}
        assert set(props) >= {"pocket_id", "file_path", "edits", "new_source", "create"}
        assert "component_path" not in props
        assert (tool.inputSchema or {}).get("required") == ["pocket_id", "file_path"]

    def test_the_description_steers_away_from_a_second_create(self) -> None:
        """The bug this tool exists to fix is behavioural, so the description has to
        say so: an agent that reaches for ``create_html_site`` on an edit request
        mints a second site pocket, and nothing in the schema stops it."""
        tool = next(t for t in _listed_tool() if t.name == "edit_html_file")
        description = tool.description or ""
        assert "create_html_site" in description
        assert "NEVER" in description

    def test_the_description_warns_off_reacts_src_prefix(self) -> None:
        """The most likely wrong guess on this track, because the agent has just been
        reading a react tool that requires ``src/``. An html file written to
        ``src/index.html`` is materialized to a path the edge never serves, and the
        site simply looks unchanged."""
        tool = next(t for t in _listed_tool() if t.name == "edit_html_file")
        description = tool.description or ""
        assert "index.html" in description
        assert "src/" in description  # named in order to be ruled out

    def test_the_description_tells_the_agent_to_keep_the_capture_plumbing(self) -> None:
        """A full-file rewrite is what "change the headline" tempts on a one-document
        track, and it is exactly the shape that silently drops a form's ``action``
        and hidden ``paw_*`` inputs. Losing them does not error: the form still
        renders, still submits, and every future enquiry goes nowhere."""
        tool = next(t for t in _listed_tool() if t.name == "edit_html_file")
        description = tool.description or ""
        assert "paw_site_id" in description
        assert "paw_key" in description


# ---------------------------------------------------------------------------
# Handler wiring — identity, plan gate, validation, response shape, errors
# ---------------------------------------------------------------------------


class TestEditHandler:
    @pytest.mark.asyncio
    async def test_success_returns_pocket_and_file_path(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        fake = AsyncMock(return_value=_ok_result())
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.edit_html_file", new=fake),
        ):
            out = await mcp._edit_html_file_handler(
                {
                    "pocket_id": "pk1",
                    "file_path": "index.html",
                    "new_source": "<!doctype html><h1>Hi</h1>",
                }
            )

        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        assert body["ok"] is True
        assert body["pocket_id"] == "pk1"
        assert body["file_path"] == "index.html"
        assert body["created"] is False
        # The service got the agent identity + the edit inputs.
        fake.assert_awaited_once()
        kwargs = fake.await_args.kwargs
        assert kwargs["user_id"] == "u1"
        assert kwargs["pocket_id"] == "pk1"
        assert kwargs["file_path"] == "index.html"
        assert kwargs["new_source"].startswith("<!doctype")
        assert kwargs["edits"] is None
        assert kwargs["create"] is False

    @pytest.mark.asyncio
    async def test_a_targeted_diff_reaches_the_service_as_edits(self) -> None:
        """The preferred form. The handler must forward the blocks rather than
        collapsing them into a rewrite."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        fake = AsyncMock(return_value=_ok_result())
        blocks = [{"old_string": "555-0100", "new_string": "555-0199"}]
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.edit_html_file", new=fake),
        ):
            out = await mcp._edit_html_file_handler(
                {"pocket_id": "pk1", "file_path": "index.html", "edits": blocks}
            )

        assert not out.get("is_error"), out
        kwargs = fake.await_args.kwargs
        assert kwargs["edits"] == blocks
        assert kwargs["new_source"] is None

    @pytest.mark.asyncio
    async def test_the_success_body_does_not_claim_the_change_is_live(self) -> None:
        """NARRATION. The agent relays this payload verbatim-ish, and an edit is a
        DRAFT — html publishes are the user's call. A body that said "published"
        would have the agent tell a user their change is online when the edge is
        still serving the old page."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_html_file",
                new=AsyncMock(return_value=_ok_result()),
            ),
        ):
            out = await mcp._edit_html_file_handler(
                {"pocket_id": "pk1", "file_path": "index.html", "new_source": "<h1/>"}
            )

        body = json.loads(out["content"][0]["text"])
        assert body["status"] == "draft"
        assert body["is_live"] is False
        message = body["message"].lower()
        assert "not" in message and "online" in message
        assert "published" not in message.replace("publish it", "")

    @pytest.mark.asyncio
    async def test_missing_identity_is_an_error(self) -> None:
        """Called outside a cloud chat stream there is no workspace/user, and
        proceeding would mis-tenant the write."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=(None, None)):
            out = await mcp._edit_html_file_handler(
                {"pocket_id": "pk1", "file_path": "index.html", "new_source": "<h1/>"}
            )
        assert out["is_error"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "args",
        [
            {"file_path": "index.html", "new_source": "<h1/>"},  # no pocket_id
            {"pocket_id": "pk1", "new_source": "<h1/>"},  # no file_path
            {"pocket_id": "pk1", "file_path": "index.html"},  # neither edit shape
            {  # both edit shapes
                "pocket_id": "pk1",
                "file_path": "index.html",
                "new_source": "<h1/>",
                "edits": [{"old_string": "a", "new_string": "b"}],
            },
            {"pocket_id": "pk1", "file_path": "index.html", "edits": []},  # empty diff
            {  # malformed block
                "pocket_id": "pk1",
                "file_path": "index.html",
                "edits": [{"old_string": "a"}],
            },
            {  # create without new_source
                "pocket_id": "pk1",
                "file_path": "about.html",
                "edits": [{"old_string": "a", "new_string": "b"}],
                "create": True,
            },
        ],
    )
    async def test_malformed_input_is_rejected_before_the_service_runs(self, args) -> None:
        """Every one of these is the caller's bug, and each gets a clear message the
        agent can act on rather than a stack trace. Crucially the service is never
        reached, so a bad call cannot half-write."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        fake = AsyncMock()
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.edit_html_file", new=fake),
        ):
            out = await mcp._edit_html_file_handler(args)

        assert out["is_error"] is True, out
        fake.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_service_rejection_is_relayed_by_code(self) -> None:
        """The agent has to know WHICH guard fired to fix and retry — a reserved path
        needs a different correction from an ambiguous old_string."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.cloud._core.errors import ValidationError

        err = ValidationError("site_edit.reserved_path", "`_paw/x` is generator-owned.")
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.edit_html_file", new=AsyncMock(side_effect=err)),
        ):
            out = await mcp._edit_html_file_handler(
                {
                    "pocket_id": "pk1",
                    "file_path": "_paw/x",
                    "new_source": "{}",
                    "create": True,
                }
            )

        assert out["is_error"] is True
        text = out["content"][0]["text"]
        assert "site_edit.reserved_path" in text
        assert "generator-owned" in text

    @pytest.mark.asyncio
    async def test_a_workspace_without_the_sites_plan_is_gated(self) -> None:
        """An edit mutates a site pocket, so it is gated on the same feature the
        create tools are. Without this a workspace that lost the plan could keep
        editing a site its own /sites list 403s."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        fake = AsyncMock()
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
                new=AsyncMock(return_value="free"),
            ),
            patch("pocketpaw_ee.sites.service.edit_html_file", new=fake),
        ):
            out = await mcp._edit_html_file_handler(
                {"pocket_id": "pk1", "file_path": "index.html", "new_source": "<h1/>"}
            )

        assert out["is_error"] is True
        fake.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_create_is_forwarded(self) -> None:
        """``create=true`` is how a page gets ADDED, and it has to reach the service
        for the existence inversion to apply."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        fake = AsyncMock(return_value=_ok_result(created=True, file_path="about.html"))
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.edit_html_file", new=fake),
        ):
            out = await mcp._edit_html_file_handler(
                {
                    "pocket_id": "pk1",
                    "file_path": "about.html",
                    "new_source": "<h1>About</h1>",
                    "create": True,
                }
            )

        assert not out.get("is_error"), out
        assert fake.await_args.kwargs["create"] is True
        assert json.loads(out["content"][0]["text"])["created"] is True


# ---------------------------------------------------------------------------
# The unreferenced-create warning — the payload the agent narrates
# ---------------------------------------------------------------------------
#
# The react tool's signal on this track. It matters MORE here, not less: an unwired
# react component is invisible, while an unlinked html page is written and deployed
# and simply cannot be navigated to — a state an agent can very plausibly describe
# as finished. See tests/ee/sites/test_html_file_edit.py for the scan itself.


class TestUnreferencedCreateWarning:
    @pytest.mark.asyncio
    async def test_orphan_create_tells_the_agent_nothing_links_to_the_page(self) -> None:
        """The message must name the next call AND forbid the premature report.

        THE MUTATION THAT BREAKS THIS: drop the `if unreferenced:` branch. Run: the
        unreachable page narrated as an ordinary success."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_html_file",
                new=AsyncMock(
                    return_value=_ok_result(
                        created=True, file_path="about/index.html", unreferenced=True
                    )
                ),
            ),
        ):
            out = await mcp._edit_html_file_handler(
                {
                    "pocket_id": "pk1",
                    "file_path": "about/index.html",
                    "new_source": "<!doctype html><html><body><h1>About</h1></body></html>",
                    "create": True,
                }
            )

        # Advice, not a rejection: the file WAS written, so ok stays true and the
        # agent must not retry the create.
        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        assert body["ok"] is True
        assert body["created"] is True
        assert body["unreferenced"] is True

        message = body["message"]
        assert "about/index.html" in message
        assert "NOTHING IN THE SITE LINKS TO IT" in message
        # The wiring step is a LINK here, not react's import.
        assert "link" in message.lower()
        assert "index.html" in message
        # The half that stops the false report.
        assert "Do NOT tell the user" in message
        # The draft/not-live contract is still carried — this is additive.
        assert "NOT online yet" in message

    @pytest.mark.asyncio
    async def test_a_linked_create_carries_no_warning(self) -> None:
        """A warning on every create teaches the agent to skim the field.

        THE MUTATION THAT BREAKS THIS: warn unconditionally on `created`. Run: a
        page the agent had already linked still nagged."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_html_file",
                new=AsyncMock(
                    return_value=_ok_result(
                        created=True, file_path="about/index.html", unreferenced=False
                    )
                ),
            ),
        ):
            out = await mcp._edit_html_file_handler(
                {
                    "pocket_id": "pk1",
                    "file_path": "about/index.html",
                    "new_source": "<h1>About</h1>",
                    "create": True,
                }
            )

        body = json.loads(out["content"][0]["text"])
        assert body["unreferenced"] is False
        assert "HALF DONE" not in body["message"]
        assert "NOT online yet" in body["message"]

    @pytest.mark.asyncio
    async def test_a_service_without_the_key_degrades_to_no_warning(self) -> None:
        """An older service dict (no ``unreferenced``) must not KeyError the handler
        mid-turn. Absent reads as "nothing to warn about" — the pre-change
        behaviour — never as a crash that loses an edit already persisted."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        legacy = {"pocket_id": "pk1", "file_path": "about/index.html", "created": True}
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_html_file",
                new=AsyncMock(return_value=legacy),
            ),
        ):
            out = await mcp._edit_html_file_handler(
                {
                    "pocket_id": "pk1",
                    "file_path": "about/index.html",
                    "new_source": "<h1>About</h1>",
                    "create": True,
                }
            )

        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        assert body["unreferenced"] is False
        assert "HALF DONE" not in body["message"]
