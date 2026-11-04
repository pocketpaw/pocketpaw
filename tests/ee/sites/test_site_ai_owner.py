# tests/ee/sites/test_site_ai_owner.py: owner routes for a site's AI settings,
# presets, budget and usage. Covers workspace isolation (another workspace's pocket
# is a 404), ``sites.ai_manage`` on every mutation (members can read, not write),
# the audit row per mutation, plan-bound model choices, and the read-only fields.
from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.ee.sites.site_ai_harness import OTHER_WS, WS, client, make_pocket, make_site

BASE = "/api/v1/sites/by-pocket/{pid}/ai"


@pytest.mark.asyncio
async def test_settings_defaults_and_plan_models(beanie_test_db):
    pid = await make_pocket()
    await make_site(pid, plan_tier="site")
    async with client() as c:
        resp = await c.get(BASE.format(pid=pid) + "/settings")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "enabled": True,
        "browser_access": False,
        "require_preset_for_browser": False,
        "allowed_models": ["fast"],
        "plan_models": ["fast", "vision", "embed"],
        "keep_transcripts": False,
    }


@pytest.mark.asyncio
async def test_settings_put_is_audited_and_bound_to_the_plan(beanie_test_db):
    pid = await make_pocket()
    await make_site(pid, plan_tier="site")
    url = BASE.format(pid=pid) + "/settings"
    with patch("pocketpaw_ee.guards.audit.log_privileged_action") as audit:
        async with client() as c:
            ok = await c.put(
                url, json={"browser_access": True, "allowed_models": ["fast", "vision"]}
            )
            smart = await c.put(url, json={"allowed_models": ["smart"]})
            readonly = await c.put(url, json={"plan_models": ["smart"]})
    assert ok.status_code == 200, ok.text
    assert ok.json()["browser_access"] is True
    assert ok.json()["allowed_models"] == ["fast", "vision"]
    assert smart.status_code == 422
    assert smart.json()["error"]["code"] == "site_ai.model_not_in_plan"
    assert readonly.status_code == 422
    assert audit.call_count == 1
    assert audit.call_args.kwargs["action"] == "sites.ai.settings.update"
    assert audit.call_args.kwargs["resource_id"] == pid


@pytest.mark.asyncio
async def test_members_read_but_cannot_change_anything(beanie_test_db):
    pid = await make_pocket()
    base = BASE.format(pid=pid)
    async with client(role="member") as c:
        assert (await c.get(base + "/settings")).status_code == 200
        assert (await c.get(base + "/budget")).status_code == 200
        assert (await c.get(base + "/presets")).status_code == 200
        assert (await c.get(base + "/usage")).status_code == 200
        writes = [
            await c.put(base + "/settings", json={"browser_access": True}),
            await c.put(base + "/budget", json={"daily_cap_micro": 1}),
            await c.post(base + "/presets", json={"name": "a", "system_prompt": "x"}),
        ]
    for resp in writes:
        assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_another_workspace_gets_404_everywhere(beanie_test_db):
    pid = await make_pocket(workspace=OTHER_WS)
    async with client() as c:
        base = BASE.format(pid=pid)
        rows = [
            await c.get(base + "/settings"),
            await c.put(base + "/settings", json={"browser_access": True}),
            await c.get(base + "/budget"),
            await c.put(base + "/budget", json={"daily_cap_micro": 1}),
            await c.get(base + "/presets"),
            await c.post(base + "/presets", json={"name": "a", "system_prompt": "x"}),
            await c.get(base + "/usage"),
        ]
    for resp in rows:
        assert resp.status_code == 404, resp.text
    from pocketpaw_ee.cloud.models.site_ai import SiteAiPresetDoc, SiteAiSettingsDoc

    assert await SiteAiPresetDoc.find_all().count() == 0
    assert await SiteAiSettingsDoc.find_all().count() == 0


@pytest.mark.asyncio
async def test_preset_crud_bumps_version_and_scopes_by_site(beanie_test_db):
    pid = await make_pocket()
    other = await make_pocket()
    base = BASE.format(pid=pid)
    body = {"name": "Helper", "system_prompt": "Be brief.", "model": "fast"}
    with patch("pocketpaw_ee.guards.audit.log_privileged_action") as audit:
        async with client() as c:
            created = await c.post(base + "/presets", json=body)
            assert created.status_code == 201, created.text
            preset = created.json()
            assert preset["version"] == 1 and preset["id"].startswith("pre_")
            updated = await c.put(
                base + f"/presets/{preset['id']}", json={**body, "system_prompt": "Be kind."}
            )
            listed = await c.get(base + "/presets")
            cross = await c.get(BASE.format(pid=other) + f"/presets/{preset['id']}")
            bad = await c.post(base + "/presets", json={**body, "model": "gpt-4o"})
            deleted = await c.delete(base + f"/presets/{preset['id']}")
            gone = await c.get(base + f"/presets/{preset['id']}")
    assert updated.json()["version"] == 2 and updated.json()["system_prompt"] == "Be kind."
    assert [p["id"] for p in listed.json()] == [preset["id"]]
    assert cross.status_code == 404
    assert bad.status_code == 422
    assert deleted.status_code == 204 and gone.status_code == 404
    actions = [call.kwargs["action"] for call in audit.call_args_list]
    assert actions == [
        "sites.ai.preset.create",
        "sites.ai.preset.update",
        "sites.ai.preset.delete",
    ]


@pytest.mark.asyncio
async def test_budget_put_validates_and_reports_read_only_figures(beanie_test_db):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCounterDoc
    from pocketpaw_ee.site_ai.service import counter_key, day_period, month_period

    pid = await make_pocket()
    await make_site(pid, plan_tier="staff")
    await SiteAiCounterDoc(
        key=counter_key(WS, pid, "day", day_period()), workspace=WS, pocket_id=pid, spent_micro=7
    ).insert()
    await SiteAiCounterDoc(
        key=counter_key(WS, pid, "month", month_period()),
        workspace=WS,
        pocket_id=pid,
        spent_micro=9,
    ).insert()
    url = BASE.format(pid=pid) + "/budget"
    async with client() as c:
        ok = await c.put(url, json={"daily_cap_micro": 5_000_000, "alert_thresholds": [80, 50, 80]})
        neg = await c.put(url, json={"daily_cap_micro": -1})
        thresholds = await c.put(url, json={"alert_thresholds": [0]})
        spent = await c.put(url, json={"spent_today_micro": 0})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["daily_cap_micro"] == 5_000_000
    assert body["alert_thresholds"] == [50, 80]
    assert body["spent_today_micro"] == 7 and body["spent_month_micro"] == 9
    assert body["allowance_month_micro"] == 2_000 * 1_000_000
    assert neg.status_code == 422
    assert thresholds.status_code == 422
    assert spent.status_code == 422


@pytest.mark.asyncio
async def test_usage_groups_settled_calls_by_day_model_and_caller(beanie_test_db):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc

    pid = await make_pocket()
    rows = [
        ("r1", "2026-11-01", "fast", "worker", 100, "settled"),
        ("r2", "2026-11-01", "smart", "browser", 50, "settled"),
        ("r3", "2026-11-02", "fast", "preview", 25, "settled"),
        ("r4", "2026-11-02", "fast", "worker", 999, "held"),
        ("r5", "2026-11-02", "fast", "worker", 999, "released"),
    ]
    for run_id, day, model, caller, micro, state in rows:
        await SiteAiCallDoc(
            run_id=run_id,
            workspace=WS,
            pocket_id=pid,
            site_id="s",
            caller=caller,
            model=model,
            actual_micro=micro,
            state=state,
            day=day,
        ).insert()
    url = BASE.format(pid=pid) + "/usage"
    async with client() as c:
        resp = await c.get(url, params={"from": "2026-11-01", "to": "2026-11-03"})
        bad = await c.get(url, params={"from": "2026-01-01", "to": "2026-11-03"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["from"] == "2026-11-01" and body["to"] == "2026-11-03"
    assert body["total_micro"] == 175 and body["calls"] == 3
    assert body["by_day"] == [
        {"day": "2026-11-01", "micro": 150, "calls": 2},
        {"day": "2026-11-02", "micro": 25, "calls": 1},
        {"day": "2026-11-03", "micro": 0, "calls": 0},
    ]
    assert {m["model"]: m["micro"] for m in body["by_model"]} == {"fast": 125, "smart": 50}
    assert {m["caller"]: m["calls"] for m in body["by_caller"]} == {
        "browser": 1,
        "preview": 1,
        "worker": 1,
    }
    assert bad.status_code == 422


def test_plan_tables_cover_every_tier():
    from pocketpaw_ee.cloud.billing import site_plans

    for tier in [*site_plans.list_site_plans(), *site_plans.list_partner_plans()]:
        assert tier.key in site_plans._SITE_PLAN_AI_MODELS
        assert tier.key in site_plans._SITE_PLAN_AI_PUBLISHED
        assert tier.key in site_plans._SITE_PLAN_AI_ALLOWANCE_MICRO
    assert site_plans.site_ai_published("free") is False
    assert site_plans.site_ai_published(None) is False
    assert site_plans.site_ai_published("pro") is True  # legacy alias of ``site``
    assert site_plans.site_ai_models("nonsense") == ("fast",)
    assert site_plans.site_ai_allowance_micro("free") == 0


def test_ai_manage_is_admin():
    from pocketpaw_ee.guards.actions import ACTIONS

    assert ACTIONS["sites.ai_manage"].minimum.name == "ADMIN"
