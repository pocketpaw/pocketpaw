# tests/ee/agent/test_sites_mcp_server/test_edit_react_component.py
# Created: 2026-08-11 (feat/sites-react-edit-lane, RX-3) — coverage for the
# react-track edit tool ``edit_react_component`` on the in-process
# ``pocketpaw_sites_manager`` server. Modelled on its svelte sibling
# (test_edit_svelte_component.py), with the differences that matter to the agent:
#
#   1. Registration — the tool id rides the SAME server allowlist as publish +
#      the five create tools + edit_svelte_component, and the extension provider
#      advertises it. Registration is not a formality here: it is the entire
#      difference between "the agent can change a react site" and "the agent's only
#      move is a second create_react_site that mints a SECOND site pocket".
#   2. Handler wiring — identity gating, the Sites plan gate, input validation
#      (including the `create` rules the svelte tool has no equivalent of), the
#      success-body shape, and error MAPPING by code so the agent relays WHICH
#      guard rejected the call.
#   3. NARRATION — the success body must not claim the change is published or live.
#      An edit is a draft; the agent reads this payload and tells the user.
#
# There is deliberately NO SmokeGateFailed case: react edits do not build, so
# nothing can fail a smoke gate. The service-level persist + guard behaviour lives
# in tests/ee/sites/test_react_component_edit.py; this file pins the agent surface.
"""Tests for the react-track component edit tool (edit_react_component)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("pocketpaw_ee")


@pytest.fixture(autouse=True)
def _default_sites_plan():
    """``edit_react_component`` runs the shared Sites plan gate
    (sites.service.require_sites_plan) before it touches the service. These tests
    use synthetic workspace ids with no seeded Workspace doc, so default the plan to
    one that unlocks Sites ("go"); the denial path has its own test below."""
    with patch(
        "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
        new=AsyncMock(return_value="go"),
    ):
        yield


def _ok_result(*, created: bool = False, unreferenced: bool = False) -> dict:
    """What the service returns on a successful edit.

    ``unreferenced`` mirrors the service's own key (see
    ``sites/service.py::edit_react_component``): True when a CREATE landed a file
    that nothing imports. The stub carries it so this file pins the real shape —
    a stub that lags the service tests a payload the handler never receives."""
    return {
        "pocket_id": "pk1",
        "component_path": "src/components/Hero.tsx",
        "created": created,
        "unreferenced": unreferenced,
    }


# ---------------------------------------------------------------------------
# Registration — the tool rides the shared sites_manager allowlist
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_tool_id_on_shared_server_allowlist(self) -> None:
        from pocketpaw_ee.agent.mcp_servers.sites import (
            EDIT_REACT_COMPONENT_TOOL_ID,
            SITES_TOOL_IDS,
        )

        assert EDIT_REACT_COMPONENT_TOOL_ID == "mcp__pocketpaw_sites_manager__edit_react_component"
        assert EDIT_REACT_COMPONENT_TOOL_ID in SITES_TOOL_IDS

    def test_provider_advertises_edit_tool_id(self) -> None:
        """``tool_ids()`` feeds the claude_sdk allowlist loop AND the /sites surface
        scope. A tool the provider does not advertise is registered on the server
        and still unreachable from the surface that needs it."""
        from pocketpaw_ee.agent.mcp_servers.sites import EDIT_REACT_COMPONENT_TOOL_ID
        from pocketpaw_ee.extensions import CloudSitesMcpProvider

        assert EDIT_REACT_COMPONENT_TOOL_ID in CloudSitesMcpProvider().tool_ids()

    def test_the_built_server_actually_lists_the_tool(self) -> None:
        """The id constant and the registered tool are two different facts, and only
        this one proves the SDK will dispatch a call. An id in ``SITES_TOOL_IDS``
        with no tool behind it allow-lists a name that does not answer."""
        import asyncio

        from mcp import types
        from pocketpaw_ee.agent.mcp_servers.sites import build_sites_manager_server

        built = build_sites_manager_server()
        if built is None:  # claude_agent_sdk absent — nothing to assert
            pytest.skip("claude_agent_sdk not installed")
        _name, server = built
        handler = server["instance"].request_handlers[types.ListToolsRequest]
        listed = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
        names = {t.name for t in listed.root.tools}
        assert "edit_react_component" in names, sorted(names)

    def test_the_schema_declares_the_create_flag(self) -> None:
        """``create`` is the argument the svelte edit tool has no equivalent of, and
        the only way to ADD a section. If it drops off the schema the agent cannot
        pass it and "add a testimonials section" silently becomes impossible."""
        import asyncio

        from mcp import types
        from pocketpaw_ee.agent.mcp_servers.sites import build_sites_manager_server

        built = build_sites_manager_server()
        if built is None:
            pytest.skip("claude_agent_sdk not installed")
        _name, server = built
        handler = server["instance"].request_handlers[types.ListToolsRequest]
        listed = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
        tool = next(t for t in listed.root.tools if t.name == "edit_react_component")
        props = (tool.inputSchema or {}).get("properties") or {}
        assert set(props) >= {"pocket_id", "component_path", "edits", "new_source", "create"}
        assert (tool.inputSchema or {}).get("required") == ["pocket_id", "component_path"]

    def test_the_description_steers_away_from_a_second_create(self) -> None:
        """The bug this tool exists to fix is behavioural, so the description has to
        say so: an agent that reaches for ``create_react_site`` on an edit request
        mints a second site pocket, and nothing in the schema stops it."""
        import asyncio

        from mcp import types
        from pocketpaw_ee.agent.mcp_servers.sites import build_sites_manager_server

        built = build_sites_manager_server()
        if built is None:
            pytest.skip("claude_agent_sdk not installed")
        _name, server = built
        handler = server["instance"].request_handlers[types.ListToolsRequest]
        listed = asyncio.run(handler(types.ListToolsRequest(method="tools/list")))
        tool = next(t for t in listed.root.tools if t.name == "edit_react_component")
        description = tool.description or ""
        assert "create_react_site" in description
        assert "NEVER" in description


# ---------------------------------------------------------------------------
# Handler wiring — identity, plan gate, validation, response shape, errors
# ---------------------------------------------------------------------------


class TestEditHandler:
    @pytest.mark.asyncio
    async def test_success_returns_pocket_and_component_path(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        fake = AsyncMock(return_value=_ok_result())
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.edit_react_component", new=fake),
        ):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "new_source": "export default function Hero() { return <section/>; }",
                }
            )

        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        assert body["ok"] is True
        assert body["pocket_id"] == "pk1"
        assert body["component_path"] == "src/components/Hero.tsx"
        assert body["created"] is False
        # The service got the agent identity + the edit inputs.
        fake.assert_awaited_once()
        kwargs = fake.await_args.kwargs
        assert kwargs["user_id"] == "u1"
        assert kwargs["pocket_id"] == "pk1"
        assert kwargs["new_source"].startswith("export default")
        assert kwargs["edits"] is None
        assert kwargs["create"] is False

    @pytest.mark.asyncio
    async def test_success_payload_says_draft_and_never_claims_it_is_live(self) -> None:
        """The agent narrates this payload verbatim-ish, so a publish claim in it is
        a lie told to the user. A react edit stages a draft: nothing is built,
        nothing is deployed, and publishing is a separate call the user asks for."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_react_component",
                new=AsyncMock(return_value=_ok_result()),
            ),
        ):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "new_source": "x",
                }
            )

        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        assert body["status"] == "draft"
        assert body["is_live"] is False
        message = (body.get("message") or "").lower()
        assert "draft" in message

        text = out["content"][0]["text"].lower()
        # No completed-state publish claim. "publish" survives as an instruction
        # ("offer to publish it"), but never as something that already happened.
        assert "published" not in text
        assert "republished" not in text
        assert "live at" not in text
        assert "now live" not in text
        # ``is_live`` is a JSON key with an underscore, so the bare phrase must be
        # absent.
        assert "is live" not in text

    @pytest.mark.asyncio
    async def test_edits_input_forwarded_to_service(self) -> None:
        """The targeted-diff form: the handler forwards the blocks and does NOT
        require ``new_source``."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        fake = AsyncMock(return_value=_ok_result())
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.edit_react_component", new=fake),
        ):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "edits": [{"old_string": "Bright", "new_string": "Brighter"}],
                }
            )

        assert not out.get("is_error"), out
        kwargs = fake.await_args.kwargs
        assert kwargs["edits"] == [{"old_string": "Bright", "new_string": "Brighter"}]
        assert kwargs.get("new_source") is None

    @pytest.mark.asyncio
    async def test_edits_arriving_as_a_json_string_are_coerced(self) -> None:
        """Some backends hand a structured argument through as a JSON STRING.
        ``coerce_json_object_args`` is what stops that reading as "no edits given",
        which would surface as the exactly-one error on a call that supplied one."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        fake = AsyncMock(return_value=_ok_result())
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.edit_react_component", new=fake),
        ):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "edits": json.dumps([{"old_string": "a", "new_string": "b"}]),
                }
            )

        assert not out.get("is_error"), out
        assert fake.await_args.kwargs["edits"] == [{"old_string": "a", "new_string": "b"}]

    @pytest.mark.asyncio
    async def test_create_is_forwarded_with_new_source(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        fake = AsyncMock(return_value=_ok_result(created=True))
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.edit_react_component", new=fake),
        ):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Testimonials.tsx",
                    "new_source": "export default function Testimonials() { return <section/>; }",
                    "create": True,
                }
            )

        assert not out.get("is_error"), out
        assert json.loads(out["content"][0]["text"])["created"] is True
        assert fake.await_args.kwargs["create"] is True

    @pytest.mark.asyncio
    async def test_create_without_new_source_is_error(self) -> None:
        """Rejected at the surface, before the service: there is nothing for
        ``edits`` to search against in a file that does not exist yet."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        fake = AsyncMock(return_value=_ok_result())
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch("pocketpaw_ee.sites.service.edit_react_component", new=fake),
        ):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/New.tsx",
                    "edits": [{"old_string": "a", "new_string": "b"}],
                    "create": True,
                }
            )
        assert out.get("is_error") is True
        assert "`new_source`" in out["content"][0]["text"]
        fake.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_neither_edits_nor_new_source_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=("ws1", "u1")):
            out = await mcp._edit_react_component_handler(
                {"pocket_id": "pk1", "component_path": "src/components/Hero.tsx"}
            )
        assert out.get("is_error") is True
        text = out["content"][0]["text"]
        assert "edits" in text and "new_source" in text

    @pytest.mark.asyncio
    async def test_both_edits_and_new_source_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=("ws1", "u1")):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "new_source": "whole file",
                    "edits": [{"old_string": "a", "new_string": "b"}],
                }
            )
        assert out.get("is_error") is True
        assert "not both" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_malformed_edits_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=("ws1", "u1")):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "edits": [{"old_string": "only-old"}],
                }
            )
        assert out.get("is_error") is True
        assert "`edits`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_non_string_new_source_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=("ws1", "u1")):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "new_source": {"not": "a string"},
                }
            )
        assert out.get("is_error") is True
        assert "`new_source`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_component_path_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=("ws1", "u1")):
            out = await mcp._edit_react_component_handler({"pocket_id": "pk1", "new_source": "x"})
        assert out.get("is_error") is True
        assert "`component_path`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_pocket_id_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=("ws1", "u1")):
            out = await mcp._edit_react_component_handler(
                {"component_path": "src/components/Hero.tsx", "new_source": "x"}
            )
        assert out.get("is_error") is True
        assert "`pocket_id`" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_missing_identity_is_error(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with patch.object(mcp, "_identity", return_value=(None, None)):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "new_source": "x",
                }
            )
        assert out.get("is_error") is True
        assert "workspace and user context" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_plan_gate_denies_a_workspace_without_sites(self) -> None:
        """An edit mutates a site pocket, so it is gated on the same "sites" feature
        the create tools are. Without the gate a workspace that lost the plan could
        keep editing a site its own /sites list 403s.

        THE MUTATION THAT BREAKS THIS: delete the ``_require_sites_plan_or_error``
        call from the handler. Run: the service was reached on a free plan.
        """
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        fake = AsyncMock(return_value=_ok_result())
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.cloud.workspace.service.get_workspace_plan",
                new=AsyncMock(return_value="free"),
            ),
            patch("pocketpaw_ee.sites.service.edit_react_component", new=fake),
        ):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "new_source": "x",
                }
            )
        assert out.get("is_error") is True
        assert "plan.feature_denied" in out["content"][0]["text"]
        fake.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "code,message",
        [
            ("site_edit.reserved_path", "`package.json` resolves to a generator-owned path"),
            ("site_edit.path_outside_source", "`README.md` is outside the source tree"),
            ("pocket.not_react_site", "This pocket is not a react Paw Site"),
            ("pocket.react_component_exists", "already exists in this site's source map"),
            ("site_edit.ambiguous_match", "old_string matches 3 times"),
        ],
    )
    async def test_validation_errors_are_relayed_by_code(self, code: str, message: str) -> None:
        """Every guard's rejection reaches the agent BY CODE, so it can tell a
        retry-able diff problem from a refused path from a wrong-engine pocket. A
        generic "edit failed" would make all five look like the same transient
        failure and invite a blind retry."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.cloud._core.errors import ValidationError

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_react_component",
                new=AsyncMock(side_effect=ValidationError(code, message)),
            ),
        ):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "new_source": "x",
                }
            )
        assert out.get("is_error") is True
        text = out["content"][0]["text"]
        assert code in text
        assert message in text

    @pytest.mark.asyncio
    async def test_not_found_is_relayed_by_code(self) -> None:
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp
        from pocketpaw_ee.cloud._core.errors import NotFound

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_react_component",
                new=AsyncMock(side_effect=NotFound("site_component", "src/components/X.tsx")),
            ),
        ):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/X.tsx",
                    "new_source": "x",
                }
            )
        assert out.get("is_error") is True
        assert "site_component.not_found" in out["content"][0]["text"]

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_an_error_not_a_success(self) -> None:
        """A non-CloudError must not fall through as a successful edit — the agent
        would tell the user a change landed that did not."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_react_component",
                new=AsyncMock(side_effect=RuntimeError("mongo went away")),
            ),
        ):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "new_source": "x",
                }
            )
        assert out.get("is_error") is True
        assert "edit failed" in out["content"][0]["text"]


# ---------------------------------------------------------------------------
# The unreferenced-create warning — the payload the agent narrates
# ---------------------------------------------------------------------------
#
# THE INCIDENT (2026-09-01): a user attached an image to an already-published react
# site and asked for it in the hero. The agent created a component, this payload
# came back a clean success, and the agent reported the image as added. Nothing
# imported the new file, so the page never changed and the user could not find the
# component anywhere. The service now answers "does anything reach this file"; these
# pin that the answer reaches the agent as an OUTSTANDING STEP rather than a footnote.


class TestUnreferencedCreateWarning:
    @pytest.mark.asyncio
    async def test_orphan_create_tells_the_agent_the_page_is_unchanged(self) -> None:
        """The message must name the next call AND forbid the premature report. A
        payload that only said "saved to the draft" is what produced the incident.

        THE MUTATION THAT BREAKS THIS: drop the `if unreferenced:` branch. Run: the
        orphan create narrated as an ordinary success again."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_react_component",
                new=AsyncMock(return_value=_ok_result(created=True, unreferenced=True)),
            ),
        ):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "new_source": "export default function Hero() { return <section/>; }",
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
        assert "src/App.tsx" in message, "the outstanding call must name where to wire it"
        assert "NOTHING IMPORTS IT" in message
        # The half that stops the false report the user actually hit.
        assert "Do NOT tell the user" in message
        # The draft/not-live contract is still carried — this is additive.
        assert "NOT online yet" in message

    @pytest.mark.asyncio
    async def test_a_wired_create_carries_no_warning(self) -> None:
        """A warning on every create teaches the agent to skim the field. Only an
        actually-unreachable file gets one.

        THE MUTATION THAT BREAKS THIS: warn unconditionally on `created`. Run: a
        create the agent had already wired up still nagged."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_react_component",
                new=AsyncMock(return_value=_ok_result(created=True, unreferenced=False)),
            ),
        ):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "new_source": "x",
                    "create": True,
                }
            )

        body = json.loads(out["content"][0]["text"])
        assert body["unreferenced"] is False
        assert "HALF DONE" not in body["message"]
        assert "NOT online yet" in body["message"]

    @pytest.mark.asyncio
    async def test_a_service_without_the_key_degrades_to_no_warning(self) -> None:
        """Forward/backward compatibility: an older service dict (no
        ``unreferenced``) must not KeyError the handler mid-turn. Absent reads as
        "nothing to warn about" — the pre-change behaviour — never as a crash that
        loses an edit the service already persisted."""
        from pocketpaw_ee.agent.mcp_servers import sites_create as mcp

        legacy = {
            "pocket_id": "pk1",
            "component_path": "src/components/Hero.tsx",
            "created": True,
        }
        with (
            patch.object(mcp, "_identity", return_value=("ws1", "u1")),
            patch(
                "pocketpaw_ee.sites.service.edit_react_component",
                new=AsyncMock(return_value=legacy),
            ),
        ):
            out = await mcp._edit_react_component_handler(
                {
                    "pocket_id": "pk1",
                    "component_path": "src/components/Hero.tsx",
                    "new_source": "x",
                    "create": True,
                }
            )

        assert not out.get("is_error"), out
        body = json.loads(out["content"][0]["text"])
        assert body["unreferenced"] is False
        assert "HALF DONE" not in body["message"]
