# tests/ee/sites/test_publish_gate.py — a live publish refuses a draft that still shows
# placeholders, made-up numbers or forms that save nothing (pocketpaw_ee.sites.publish_gate).
#
# Pins: the 409 ``sites.publish_blocked`` shape the client reads (owner-worded findings,
# ``more``, ``override``), the ``confirm_placeholders`` override, previews never gated,
# the gate running before any build in ``publish_pocket``, failed form exercises
# (SC-FORM-01, recorded by play_test_site) blocking too, and the fail-open rule when the
# checks cannot run. Mutations: tests/mutations/sites_publish_gate.json.
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import publish_gate, verify
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.sites.dto import PublishRequest

from .test_verify_checks import _doc
from .test_verify_pipeline import MemoryVerifyStore

_CAFE = _doc(
    "<p>Find us at [Address]. Call us on [Phone].</p>"
    "<p>WhatsApp 9876543210</p>"
    '<form id="book"><input type="email" name="email"><button>Book</button></form>'
)
_CLEAN = _doc('<p>Find us at 4 Mill Lane, Leeds.</p><a href="/menu.html">Menu</a>')


def _html(index: str) -> dict[str, Any]:
    return {"engine": "html", "source": {"index.html": index, "menu.html": _CLEAN}}


class TestFindings:
    def test_page_labels_are_for_owners(self) -> None:
        assert publish_gate.page_label("index.html") == "the home page"
        assert publish_gate.page_label("about/index.html") == 'the "about" page'
        assert publish_gate.page_label("opening-hours.html") == 'the "opening hours" page'

    async def test_each_item_is_one_owner_worded_finding(self) -> None:
        findings = await publish_gate.blocking_findings(
            _html(_CAFE), pocket_id="p1", store=MemoryVerifyStore()
        )
        messages = [f["message"] for f in findings]
        assert 'Your site still shows "[Address]" on the home page.' in messages
        assert 'Your site still shows "[Phone]" on the home page.' in messages
        assert any("9876543210" in m and "made up" in m for m in messages)
        assert any(m.startswith('The "book" form on the home page') for m in messages)
        assert {f["reason"] for f in findings} == {
            "placeholder",
            "invented_contact",
            "form_saves_nothing",
        }
        assert set(findings[0]) == {"rule", "reason", "page", "page_label", "text", "message"}

    async def test_a_clean_draft_has_none(self) -> None:
        assert await publish_gate.blocking_findings(_html(_CLEAN), pocket_id="p1") == []


class TestEnforce:
    async def test_refusal_carries_the_findings(self) -> None:
        with pytest.raises(publish_gate.PublishBlocked) as info:
            await publish_gate.gate_publish(
                _html(_CAFE),
                workspace_id="w",
                user_id="u",
                pocket_id="p1",
                store=MemoryVerifyStore(),
            )
        exc = info.value
        assert exc.status_code == 409
        assert exc.code == "sites.publish_blocked"
        assert exc.message.startswith("Your site isn't ready to go live yet.")
        body = exc.to_dict()["error"]
        assert body["details"]["override"] == "confirm_placeholders"
        assert body["details"]["more"] == 0
        assert len(body["details"]["findings"]) == 4

    async def test_owner_confirmation_publishes_past_the_findings(self) -> None:
        with patch("pocketpaw_ee.cloud.audit.service.record", new=AsyncMock()):
            await publish_gate.gate_publish(
                _html(_CAFE),
                workspace_id="w",
                user_id="u",
                pocket_id="p1",
                confirmed=True,
                store=MemoryVerifyStore(),
            )

    async def test_a_check_that_cannot_run_lets_the_publish_through(self, monkeypatch) -> None:
        def _crash(*_a: Any, **_k: Any) -> dict:
            raise RuntimeError("artifact store down")

        monkeypatch.setattr(verify, "publish_checks", _crash)
        await publish_gate.gate_publish(
            _html(_CAFE),
            workspace_id="w",
            user_id="u",
            pocket_id="p1",
            store=MemoryVerifyStore(),
        )

    async def test_findings_past_the_cap_are_counted(self, monkeypatch) -> None:
        many = "".join(f"<p>[Phone] {'x' * i}</p><p>[NEED: item {i}]</p>" for i in range(30))
        with pytest.raises(publish_gate.PublishBlocked) as info:
            await publish_gate.gate_publish(
                _html(_doc(many)),
                workspace_id="w",
                user_id="u",
                pocket_id="p1",
                store=MemoryVerifyStore(),
            )
        details = info.value.details
        assert len(details["findings"]) == publish_gate.MAX_FINDINGS
        assert details["more"] == 31 - publish_gate.MAX_FINDINGS

    async def test_a_failed_form_exercise_blocks(self) -> None:
        source = {"paw.template.json": '{"build": "none"}', "index.html": _CLEAN}
        pocket = {"engine": "project", "source": source}
        store = MemoryVerifyStore()
        content_hash = verify.publish_content_hash(pocket)
        entry = {
            "id": publish_gate.FORM_RULE,
            "page": "index.html",
            "detail": "form 'signup' answered HTTP 500",
            "level": "error",
            "items": ["signup"],
            "outcome": "the site answered with an error",
        }
        verify.record_form_results("p1", content_hash, "/", [entry], store=store)
        with pytest.raises(publish_gate.PublishBlocked) as info:
            await publish_gate.gate_publish(
                pocket, workspace_id="w", user_id="u", pocket_id="p1", store=store
            )
        (finding,) = info.value.details["findings"]
        assert finding["reason"] == "form_failed"
        assert finding["message"] == (
            'The "signup" form on the home page didn\'t work when we tried it '
            "(the site answered with an error)."
        )
        # A later passing run on the same path clears it.
        verify.record_form_results("p1", content_hash, "/", [], store=store)
        await publish_gate.gate_publish(
            pocket, workspace_id="w", user_id="u", pocket_id="p1", store=store
        )


class TestPublishPocket:
    @pytest.fixture
    def pocket(self, monkeypatch) -> dict[str, Any]:
        doc = {"name": "Saltline", **_html(_CAFE)}

        async def _plan(_ws: str) -> None:
            return None

        async def _get(_pid: str, _uid: str) -> dict[str, Any]:
            return doc

        monkeypatch.setattr(sites_service, "require_sites_plan", _plan)
        monkeypatch.setattr(pockets_service, "get", _get)
        return doc

    async def test_live_publish_is_refused_before_any_build(self, pocket, monkeypatch) -> None:
        async def _no_build(**_kw: Any) -> Any:
            raise AssertionError("built a draft the gate should have refused")

        monkeypatch.setattr(sites_service, "publish", _no_build)
        with pytest.raises(publish_gate.PublishBlocked):
            await sites_service.publish_pocket(workspace_id="ws1", user_id="u1", pocket_id="p1")

    async def test_confirm_placeholders_reaches_the_gate(self, pocket, monkeypatch) -> None:
        seen: list[bool] = []

        class Stop(Exception):
            pass

        async def _gate(_pocket: Any, **kw: Any) -> bool:
            seen.append(kw["confirmed"])
            raise Stop

        monkeypatch.setattr(publish_gate, "gate_publish", _gate)
        with pytest.raises(Stop):
            await sites_service.publish_pocket(
                workspace_id="ws1", user_id="u1", pocket_id="p1", confirm_placeholders=True
            )
        assert seen == [True]

    async def test_a_preview_is_never_gated(self, pocket, monkeypatch) -> None:
        async def _enforce(*_a: Any, **_k: Any) -> None:
            raise AssertionError("a draft preview went through the publish gate")

        async def _preview(**_kw: Any) -> str:
            return "preview-doc"

        monkeypatch.setattr(publish_gate, "gate_publish", _enforce)
        monkeypatch.setattr(sites_service, "publish", _preview)
        out = await sites_service.publish_pocket(
            workspace_id="ws1", user_id="u1", pocket_id="p1", preview=True
        )
        assert out == "preview-doc"

    def test_the_rest_body_carries_the_override(self) -> None:
        assert PublishRequest(pocket_id="p").confirm_placeholders is False
        assert PublishRequest(pocket_id="p", confirm_placeholders=True).confirm_placeholders


class TestAgentTool:
    async def _call(self, args: dict[str, Any], effect: Any) -> tuple[dict, AsyncMock]:
        from pocketpaw_ee.agent.mcp_servers import sites as sites_mcp

        mock = AsyncMock(side_effect=effect)
        with (
            patch("pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", return_value="w"),
            patch("pocketpaw_ee.cloud.chat.agent_service.current_user_id", return_value="u"),
            patch("pocketpaw_ee.sites.service.publish_pocket", new=mock),
        ):
            out = await sites_mcp._publish_handler(args)
        return out, mock

    async def test_a_refusal_lists_each_finding_and_the_next_step(self) -> None:
        findings = await publish_gate.blocking_findings(_html(_CAFE), pocket_id="p1")
        out, mock = await self._call({"pocket_id": "p1"}, publish_gate.PublishBlocked(findings))
        text = out["content"][0]["text"]
        assert out["is_error"] is True
        assert "sites.publish_blocked: NOT published." in text
        assert '- Your site still shows "[Address]" on the home page.' in text
        assert "never invent" in text and "play_test_site" in text
        assert "confirm_placeholders" not in mock.await_args.kwargs

    async def test_a_truthy_non_true_override_is_a_plain_publish(self) -> None:
        _out, mock = await self._call(
            {"pocket_id": "p1", "confirm_placeholders": "yes"}, RuntimeError("stop")
        )
        assert "confirm_placeholders" not in mock.await_args.kwargs
