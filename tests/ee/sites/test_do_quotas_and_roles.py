# Tests for the security-review fixes that live on the DO lifecycle slice:
# M3 (one tenant cannot exhaust the shared DO budget: per-workspace quota, a draft
# budget separate from published sites, rate-limited draft rotation) and M4 (a
# data-loss confirmation needs a workspace admin). Cloudflare is faked.
from __future__ import annotations

from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud._core.errors import Forbidden, ValidationError
from pocketpaw_ee.sites import bundle_deploy, draft_worker
from pocketpaw_ee.sites import durable_objects as dobj

from tests.ee.sites.test_draft_worker import POCKET, FakeCF, Store, _build, _record
from tests.ee.sites.test_durable_objects import _CF, do_build  # noqa: F401
from tests.ee.sites.test_durable_objects_lifecycle import V1, _draft_manifest, _drafts  # noqa: F401
from tests.ee.sites.test_project_engine import _Records


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv(dobj.FLAG_ENV, "1")
    for key in (
        dobj.ACCOUNT_BUDGET_ENV,
        dobj.DRAFT_BUDGET_ENV,
        dobj.WORKSPACE_QUOTA_ENV,
        draft_worker.DO_ROTATIONS_ENV,
    ):
        monkeypatch.delenv(key, raising=False)


# ------------------------------------------------------- draft vs published


class _NsCF:
    def __init__(self, scripts: list[str]):
        self.scripts = scripts

    async def list_durable_object_namespaces(self):
        return [{"id": f"n{i}", "script": s} for i, s in enumerate(self.scripts)]


def _vetted() -> dobj.VettedDurableObjects:
    return dobj.vet_durable_objects(
        {
            "durableObjects": {
                "bindings": [{"name": "ROOM", "className": "Room"}],
                "migrations": [{"tag": "v1", "new_sqlite_classes": ["Room"]}],
                "exportedClasses": ["Room"],
            }
        },
        paid=True,
    )


@pytest.mark.asyncio
async def test_drafts_and_published_sites_have_separate_budgets(monkeypatch):
    monkeypatch.setenv(dobj.ACCOUNT_BUDGET_ENV, "2")
    monkeypatch.setenv(dobj.DRAFT_BUDGET_ENV, "2")
    drafts_full = _NsCF(["paw-draft-a-1", "paw-draft-b-2", "site-1"])
    # Drafts filling their own budget never block a published site...
    await dobj.check_account_budget(drafts_full, _vetted(), target="account", draft=False)
    # ...and are refused themselves.
    with pytest.raises(ValidationError) as exc:
        await dobj.check_account_budget(drafts_full, _vetted(), target="account", draft=True)
    assert exc.value.code == "sites.do_account_budget"
    sites_full = _NsCF(["site-1", "site-2", "paw-draft-a-1"])
    await dobj.check_account_budget(sites_full, _vetted(), target="account", draft=True)
    with pytest.raises(ValidationError):
        await dobj.check_account_budget(sites_full, _vetted(), target="account", draft=False)


# -------------------------------------------------------- workspace quota


def test_workspace_quota(monkeypatch):
    vetted = _vetted()
    dobj.check_workspace_quota(4, vetted)  # default 5: 4 + 1 fits
    with pytest.raises(ValidationError) as exc:
        dobj.check_workspace_quota(5, vetted)
    assert exc.value.code == "sites.do_workspace_quota"
    monkeypatch.setenv(dobj.WORKSPACE_QUOTA_ENV, "1")
    with pytest.raises(ValidationError):
        dobj.check_workspace_quota(1, vetted)


@pytest.mark.asyncio
async def test_deploy_asks_the_quota_only_when_a_class_is_new(do_build):  # noqa: F811
    asked: list[int] = []

    async def used() -> int:
        asked.append(1)
        return 5

    with pytest.raises(ValidationError) as exc:
        await bundle_deploy.deploy_bundle(
            _CF().client(), script_name="s", build_dir=do_build, salt="ws", do_quota_used=used
        )
    assert exc.value.code == "sites.do_workspace_quota" and asked == [1]
    # Up to date: nothing new, the quota is never consulted.
    cf = _CF()
    await bundle_deploy.deploy_bundle(
        cf.client(),
        script_name="s",
        build_dir=do_build,
        salt="ws",
        do_quota_used=used,
        do_state=dobj.DurableObjectState.from_history(["v1"], ["Room"]),
    )
    assert asked == [1]


@pytest.mark.asyncio
async def test_publish_refuses_a_class_past_the_workspace_quota(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud._core.errors import CloudError
    from pocketpaw_ee.cloud.models.site import Site

    from tests.ee.sites.test_durable_objects_lifecycle import (
        DO_BASE,
        _do_manifest,
        _DOFake,
        _first,
    )
    from tests.ee.sites.test_project_d1 import _publish

    monkeypatch.setenv(dobj.WORKSPACE_QUOTA_ENV, "2")
    for i in range(2):  # two other sites of the same workspace hold a class each
        await Site(
            workspace="ws1",
            pocket_id=f"pk-other-{i}",
            owner="u1",
            name=f"o{i}",
            do_classes=["Room"],
        ).insert()
    (pocket_id, site_id), source = await _first(monkeypatch, _do_manifest(DO_BASE, V1))
    cf = _DOFake()
    with pytest.raises(CloudError) as exc:
        await _publish(pocket_id, site_id, source, cf.client())
    assert exc.value.code == "sites.do_workspace_quota"
    assert cf.metadata == []


# -------------------------------------------------------------- drafts


@pytest.mark.asyncio
@pytest.mark.usefixtures("_drafts")
async def test_a_draft_past_the_workspace_quota_falls_back():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    for i in range(5):
        await registry.put(
            draft_worker.DraftRecord(
                pocket_id=f"64b7f0c2a1b2c3d4e5f6071{i}", workspace="ws1", do_classes=["Room"]
            )
        )
    out = await _build(store, records, cf, registry, manifest=_draft_manifest(V1))
    assert out["preview_mode"] != "full"
    assert _record(records)["draft_worker_reason"] == "draft_worker:cap"
    assert cf.named("put_worker") == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_drafts")
async def test_draft_rotation_is_rate_limited(monkeypatch):
    monkeypatch.setenv(draft_worker.DO_ROTATIONS_ENV, "1")
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    await _build(store, records, cf, registry, manifest=_draft_manifest(V1), content_hash="h1")
    first = (await registry.get(POCKET)).script

    def rewritten(tag):
        return _draft_manifest({"tag": tag, "new_sqlite_classes": ["Room"]})

    out = await _build(store, records, cf, registry, manifest=rewritten("r1"), content_hash="h2")
    assert out["preview_mode"] == "full"
    second = (await registry.get(POCKET)).script
    assert second != first
    out = await _build(store, records, cf, registry, manifest=rewritten("r2"), content_hash="h3")
    assert out["preview_mode"] != "full"
    assert _record(records, "h3")["draft_worker_reason"] == "draft_worker:do_rotation_limit"
    assert (await registry.get(POCKET)).script == second  # no third script


# ---------------------------------------------------------------- M4


def _user(role: str, workspace_id: str = "ws-1"):
    return SimpleNamespace(
        id="u-1", workspaces=[SimpleNamespace(workspace=workspace_id, role=role)]
    )


@pytest.mark.asyncio
async def test_only_an_admin_may_confirm_data_loss(monkeypatch):
    from unittest.mock import AsyncMock

    from pocketpaw_ee.guards import deps as guards_deps
    from pocketpaw_ee.sites.router import _require_data_loss_confirmer

    monkeypatch.setattr(guards_deps, "_has_action_override", AsyncMock(return_value=False))
    # Nothing to confirm: anyone who may publish.
    await _require_data_loss_confirmer(_user("member"), "ws-1", [])
    for role in ("member", "editor"):
        with pytest.raises(Forbidden) as exc:
            await _require_data_loss_confirmer(_user(role), "ws-1", ["Room"])
        assert exc.value.code == "sites.data_loss_confirm_forbidden"
    await _require_data_loss_confirmer(_user("admin"), "ws-1", ["Room"])
    await _require_data_loss_confirmer(_user("owner"), "ws-1", ["Room"])
    with pytest.raises(Forbidden):
        await _require_data_loss_confirmer(_user("owner", "elsewhere"), "ws-1", ["Room"])


def test_confirming_data_loss_sits_above_publishing():
    from pocketpaw_ee.guards.actions import ACTIONS
    from pocketpaw_ee.guards.rbac import WorkspaceRole

    rule = ACTIONS["sites.confirm_data_loss"]
    assert rule.minimum.level >= WorkspaceRole.ADMIN.level
