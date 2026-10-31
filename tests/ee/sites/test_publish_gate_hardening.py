# tests/ee/sites/test_publish_gate_hardening.py — the publish gate checks what goes live,
# machine republishes cannot slip unvetted content out, and only the owner overrides it.
#
# H2: a svelte / react draft with no stored build is checked on the build publish
# makes, before the deploy; a checker crash lets the publish through with a metric log
# line. M3: a machine republish (plan sale, payment restamp, AI-visibility fix, an
# approved plan request) skips the gate only when the content is exactly what last
# passed it (or the owner confirmed); otherwise it is gated and refused like any other
# publish. M1: the agent cannot say "publish anyway": its request files a Tray card
# with the findings; an owner's override is written to the audit log with the findings.
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from pocketpaw_ee.cloud.pockets import service as pockets_service
from pocketpaw_ee.sites import publish_gate, verify
from pocketpaw_ee.sites import service as sites_service

from .test_verify_checks import _doc

_CAFE = _doc("<p>Find us at [Address].</p>")
_CLEAN = _doc("<p>Find us at 4 Mill Lane, Leeds.</p>")


def _html(index: str) -> dict[str, Any]:
    return {"id": "p1", "name": "Saltline", "engine": "html", "source": {"index.html": index}}


@pytest.fixture
def pocket(monkeypatch) -> dict[str, Any]:
    doc = _html(_CAFE)

    async def _plan(_ws: str) -> None:
        return None

    async def _get(_pid: str, _uid: str) -> dict[str, Any]:
        return doc

    monkeypatch.setattr(sites_service, "require_sites_plan", _plan)
    monkeypatch.setattr(pockets_service, "get", _get)
    return doc


class _Published(Exception):
    """Raised by the stub ``publish`` so a test stops at the deploy call."""

    def __init__(self, kw: dict[str, Any]) -> None:
        super().__init__("published")
        self.kw = kw


@pytest.fixture
def published(monkeypatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def _publish(**kw: Any) -> Any:
        calls.append(kw)
        raise _Published(kw)

    monkeypatch.setattr(sites_service, "publish", _publish)
    return calls


# ── H2: the gate reads what goes live ───────────────────────────────────────────


async def test_a_draft_with_no_stored_build_is_checked_on_the_publish_build(
    pocket, published, monkeypatch
) -> None:
    pocket.update(engine="svelte", source={"src/routes/+page.svelte": "<p>x</p>"})
    monkeypatch.setattr(
        verify, "publish_checks", lambda *_a, **_k: {"warnings": [], "skipped": "no_built_output"}
    )
    with pytest.raises(_Published):
        await sites_service.publish_pocket(workspace_id="w", user_id="u", pocket_id="p1")
    assert published[0]["gate_built_output"] is True


def test_the_built_output_check_refuses_placeholders(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text(_CAFE, encoding="utf-8")
    with pytest.raises(publish_gate.PublishBlocked):
        publish_gate.check_built_output(str(tmp_path), "html", pocket_id="p1")
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    publish_gate.check_built_output(str(tmp_path), "html", pocket_id="p1")


async def test_the_deploy_stops_before_anything_goes_live(tmp_path: Path, monkeypatch) -> None:
    from pocketpaw_ee.sites.generator_client import BuildResult

    (tmp_path / "index.html").write_text(_CAFE, encoding="utf-8")

    async def _build(*_a: Any, **_k: Any) -> BuildResult:
        return BuildResult(project_dir=str(tmp_path), ripple_version=None)

    async def _no_kwargs(*_a: Any, **_k: Any) -> dict:
        return {}

    async def _embed(**_k: Any) -> None:
        raise AssertionError("reached the deploy steps with [Address] on the page")

    monkeypatch.setattr(sites_service, "_build_or_cloud_error", _build)
    monkeypatch.setattr(sites_service, "_turnstile_build_kwargs", _no_kwargs)
    monkeypatch.setattr(sites_service, "_embed_concierge_bar", _embed)
    monkeypatch.setattr(sites_service, "build_runs_async", lambda *_a, **_k: False)
    with pytest.raises(publish_gate.PublishBlocked):
        await sites_service._deploy_site_doc(
            workspace_id="w",
            user_id="u",
            pocket_id="p1",
            site_id="507f1f77bcf86cd799439011",
            signed_key="k",
            site_name="Saltline",
            ripple_spec=None,
            theme={},
            engine="html",
            source={"index.html": _CAFE},
            gate_built_output=True,
        )


async def test_a_checker_crash_lets_the_publish_through_with_a_metric(
    pocket, published, monkeypatch, caplog
) -> None:
    def _crash(*_a: Any, **_k: Any) -> dict:
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(verify, "publish_checks", _crash)
    with caplog.at_level(logging.WARNING), pytest.raises(_Published):
        await sites_service.publish_pocket(workspace_id="w", user_id="u", pocket_id="p1")
    assert "metric=sites_publish_gate_fail_open" in caplog.text


# ── M3: machine republishes are exempt only for content that already passed ────


async def test_a_machine_republish_of_gated_content_skips_the_gate(
    pocket, published, monkeypatch
) -> None:
    pocket["source"] = {"index.html": _CLEAN}
    with pytest.raises(_Published):
        await sites_service.publish_pocket(workspace_id="w", user_id="u", pocket_id="p1")
    # The owner's publish passed the gate and was remembered; the restamp is exempt.

    async def _boom(*_a: Any, **_k: Any) -> list:
        raise AssertionError("the gate ran on content that already passed it")

    monkeypatch.setattr(publish_gate, "evaluate", _boom)
    with pytest.raises(_Published):
        await sites_service.publish_pocket(
            workspace_id="w", user_id="u", pocket_id="p1", machine_republish=True
        )


async def test_a_machine_republish_of_new_content_is_gated(pocket, published) -> None:
    pocket["source"] = {"index.html": _CLEAN}
    with pytest.raises(_Published):
        await sites_service.publish_pocket(workspace_id="w", user_id="u", pocket_id="p1")
    pocket["source"] = {"index.html": _CAFE}  # an unvetted draft edit since then
    with pytest.raises(publish_gate.PublishBlocked):
        await sites_service.publish_pocket(
            workspace_id="w", user_id="u", pocket_id="p1", machine_republish=True
        )
    assert len(published) == 1


async def test_the_owners_confirmation_is_remembered_for_restamps(pocket, published) -> None:
    with pytest.raises(_Published):
        await sites_service.publish_pocket(
            workspace_id="w", user_id="u", pocket_id="p1", confirm_placeholders=True
        )
    with pytest.raises(_Published):
        await sites_service.publish_pocket(
            workspace_id="w", user_id="u", pocket_id="p1", machine_republish=True
        )
    assert len(published) == 2


# ── M1: only the owner overrides, and it is audited ──────────────────────────────


async def test_an_owner_override_is_audited_with_the_findings(pocket, published) -> None:
    with (
        patch("pocketpaw_ee.cloud.audit.service.record", new=AsyncMock()) as record,
        pytest.raises(_Published),
    ):
        await sites_service.publish_pocket(
            workspace_id="w", user_id="u", pocket_id="p1", confirm_placeholders=True
        )
    record.assert_awaited_once()
    args, kwargs = record.await_args
    assert args[:3] == ("w", "u", "sites.publish_gate_overridden")
    assert kwargs["target_id"] == "p1"
    assert kwargs["metadata"]["findings"][0]["text"] == "[Address]"


async def test_a_confirm_with_nothing_to_override_is_not_audited(pocket, published) -> None:
    pocket["source"] = {"index.html": _CLEAN}
    with (
        patch("pocketpaw_ee.cloud.audit.service.record", new=AsyncMock()) as record,
        pytest.raises(_Published),
    ):
        await sites_service.publish_pocket(
            workspace_id="w", user_id="u", pocket_id="p1", confirm_placeholders=True
        )
    record.assert_not_awaited()


async def _agent(args: dict[str, Any], publish: Any, request: Any) -> dict:
    from pocketpaw_ee.agent.mcp_servers import sites as sites_mcp

    with (
        patch("pocketpaw_ee.cloud.chat.agent_service.current_workspace_id", return_value="w"),
        patch("pocketpaw_ee.cloud.chat.agent_service.current_user_id", return_value="u"),
        patch("pocketpaw_ee.sites.service.publish_pocket", new=publish),
        patch("pocketpaw_ee.sites.service.request_publish_pocket", new=request),
    ):
        return await sites_mcp._publish_handler({"pocket_id": "p1", **args})


class _Card:
    id = "act-9"
    parameters = {
        "_artifact_change": {
            "publish_findings": [{"message": 'Your site still shows "[Address]" on the home page.'}]
        }
    }


async def test_the_agent_cannot_publish_anyway_it_files_a_card() -> None:
    import json

    publish, request = AsyncMock(), AsyncMock(return_value=_Card())
    out = await _agent({"confirm_placeholders": True}, publish, request)
    publish.assert_not_awaited()
    request.assert_awaited_once()
    body = json.loads(out["content"][0]["text"])
    assert body["approval_required"] is True and body["action_id"] == "act-9"
    assert body["publish_findings"][0]["message"].startswith("Your site still shows")
    assert "Publish anyway" in body["message"]


async def test_a_blocked_agent_publish_explains_the_owner_path() -> None:
    findings = await publish_gate.blocking_findings(_html(_CAFE), pocket_id="p1")
    publish = AsyncMock(side_effect=publish_gate.refusal(findings))
    out = await _agent({}, publish, AsyncMock())
    text = out["content"][0]["text"]
    assert publish.await_args.kwargs.get("confirm_placeholders") in (None, False)
    assert "Tray" in text and "confirm_placeholders: true" in text


async def test_the_approval_executor_audits_a_publish_anyway(monkeypatch, pocket) -> None:
    from pocketpaw_ee.versions import instinct_executor
    from pocketpaw_ee.versions import service as versions

    marks: list[str] = []

    class _Store:
        async def mark_executed(self, *_a: Any) -> None:
            marks.append("executed")

        async def mark_failed(self, *_a: Any) -> None:
            marks.append("failed")

        async def update_parameters(self, *_a: Any) -> None:
            return None

    class _Action:
        id = "act-1"
        approved_by = "owner-1"
        parameters = {
            "_artifact_change": {
                "scope_type": "pocket",
                "scope_id": "p1",
                "to_version_id": "v2",
                "workspace": "w",
                "user_id": "u",
            }
        }

    deploys: list[dict] = []

    async def _deploy(**kw: Any) -> None:
        deploys.append(kw)

    monkeypatch.setattr(versions, "publish", AsyncMock())
    monkeypatch.setattr(sites_service, "publish_pocket", _deploy)
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: _Store())
    with patch("pocketpaw_ee.cloud.audit.service.record", new=AsyncMock()) as record:
        await instinct_executor.execute_approved_change(_Action(), confirm_placeholders=True)
    assert marks == ["executed"]
    args, _kw = record.await_args
    assert args[:3] == ("w", "owner-1", "sites.publish_gate_overridden")
    assert deploys[0]["machine_republish"] is True


# ── a skipped or partial check never vouches for content ───────────────────────


@pytest.mark.parametrize(
    "checks",
    [
        {"warnings": [], "skipped": "no_built_output"},
        {"warnings": [], "skipped": "check_crashed"},
        {"warnings": [], "pages_skipped": 3, "note": "3 pages not checked"},
    ],
    ids=["no-build", "crashed", "pages-skipped"],
)
async def test_an_incomplete_check_puts_the_hash_on_hold(pocket, published, monkeypatch, checks):
    pocket["source"] = {"index.html": _CLEAN}
    monkeypatch.setattr(verify, "publish_checks", lambda *_a, **_k: checks)
    with pytest.raises(_Published):
        await sites_service.publish_pocket(workspace_id="w", user_id="u", pocket_id="p1")
    assert publish_gate.last_gated("p1") == ""


async def test_the_tray_path_does_not_vouch_for_an_unchecked_draft(monkeypatch, pocket) -> None:
    from pocketpaw_ee.versions import instinct_executor
    from pocketpaw_ee.versions import service as versions

    class _Store:
        async def mark_executed(self, *_a: Any) -> None:
            return None

        async def mark_failed(self, *_a: Any) -> None:
            return None

        async def update_parameters(self, *_a: Any) -> None:
            return None

    class _Action:
        id = "act-2"
        approved_by = "owner-1"
        parameters = {
            "_artifact_change": {
                "scope_type": "pocket",
                "scope_id": "p1",
                "to_version_id": "v2",
                "workspace": "w",
                "user_id": "u",
            }
        }

    async def _deploy(**_kw: Any) -> None:
        return None

    pocket["source"] = {"index.html": _CLEAN}
    monkeypatch.setattr(
        verify, "publish_checks", lambda *_a, **_k: {"warnings": [], "skipped": "no_built_output"}
    )
    monkeypatch.setattr(versions, "publish", AsyncMock())
    monkeypatch.setattr(sites_service, "publish_pocket", _deploy)
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: _Store())
    await instinct_executor.execute_approved_change(_Action())
    assert publish_gate.last_gated("p1") == ""
