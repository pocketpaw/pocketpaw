# Tests for Durable Objects across a site's life, slice 4
# (ee/pocketpaw_ee/sites/durable_objects.py teardown + state, the publish wiring in
# sites.service, the draft Worker in sites.draft_worker and the delete cascade's
# ``do`` step). Cloudflare is faked throughout; nothing real is called.
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from bson import ObjectId
from pocketpaw_ee.cloud._core.errors import CloudError, ValidationError
from pocketpaw_ee.sites import delete_cascade, draft_worker, project_build
from pocketpaw_ee.sites import durable_objects as dobj

from tests.ee.sites.test_draft_worker import POCKET, FakeCF, Store, _build, _record
from tests.ee.sites.test_project_d1 import SITE_ID, _KVFake, _publish, _setup
from tests.ee.sites.test_project_engine import WORKER_MANIFEST, _Records


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setenv(dobj.FLAG_ENV, "1")
    monkeypatch.delenv(dobj.ACCOUNT_BUDGET_ENV, raising=False)


V1 = {"tag": "v1", "new_sqlite_classes": ["Room"]}
V2 = {"tag": "v2", "new_sqlite_classes": ["Chat"]}
V3_DELETE = {"tag": "v3", "deleted_classes": ["Chat"]}


def _do_manifest(base: dict, *migrations: dict, classes=("Room",)) -> dict:
    live = [c for m in migrations for c in m.get("new_sqlite_classes", [])]
    live = [c for c in live if not any(c in m.get("deleted_classes", []) for m in migrations)]
    return {
        **base,
        "bindingRequests": [{"type": "do", "name": "ROOM", "className": "Room"}],
        "durableObjects": {
            "bindings": [{"name": "ROOM", "className": "Room"}],
            "migrations": list(migrations),
            "exportedClasses": sorted(set(live) | set(classes)),
        },
    }


# ------------------------------------------------------------------ state


def test_state_from_history_reads_the_last_tag():
    state = dobj.DurableObjectState.from_history(["v1", "v2"], ["Room", "Chat"])
    assert state.migration_tag == "v2"
    assert state.applied_tags == ("v1", "v2")
    assert state.live_classes == ("Room", "Chat")
    empty = dobj.DurableObjectState.from_history([], [])
    assert empty.migration_tag is None and empty.applied_tags == ()


# --------------------------------------------------------------- teardown


class _TeardownCF:
    def __init__(self, *, rows_after: int = 0, fail: set[str] | None = None) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.rows_after = rows_after
        self.fail = fail or set()
        self.deleted = False

    def _maybe(self, name: str) -> None:
        if name in self.fail:
            raise ValidationError("sites.cloudflare_error", f"Cloudflare API 500: {name}")

    async def put_worker(self, **kw):
        self._maybe("put_worker")
        self.calls.append(("put_worker", kw))
        return True

    async def delete_account_script(self, name, *, force=False):
        self._maybe("delete")
        self.calls.append(("delete_account_script", (name, force)))
        self.deleted = True

    async def delete_worker(self, name, *, force=False):
        self._maybe("delete")
        self.calls.append(("delete_worker", (name, force)))
        self.deleted = True

    async def list_durable_object_namespaces(self):
        self._maybe("list")
        self.calls.append(("list", None))
        rows = self.rows_after if self.deleted else 1
        return [{"id": f"n{i}", "script": "s1", "class": "Room"} for i in range(rows)] + [
            {"id": "other", "script": "someone-else", "class": "X"}
        ]


@pytest.mark.asyncio
async def test_teardown_tombstones_then_force_deletes_then_verifies():
    cf = _TeardownCF()
    out = await dobj.teardown_script(
        cf, "s1", target="account", classes=["Room", "Chat"], migration_tag="v2"
    )
    assert out.ok is True
    names = [c for c, _ in cf.calls]
    assert names == ["put_worker", "delete_account_script", "list"]
    put = cf.calls[0][1]
    assert put["migrations"] == {
        "old_tag": "v2",
        "new_tag": dobj.TOMBSTONE_TAG,
        "steps": [{"deleted_classes": ["Room", "Chat"]}],
    }
    assert put["bindings"] == [] and put["target"] == "account"
    assert cf.calls[1][1] == ("s1", True)


@pytest.mark.asyncio
async def test_teardown_uses_the_dispatch_delete_on_wfp():
    cf = _TeardownCF()
    out = await dobj.teardown_script(
        cf, "s1", target="dispatch", classes=["Room"], migration_tag="v1"
    )
    assert out.ok and ("delete_worker", ("s1", True)) in cf.calls


@pytest.mark.asyncio
async def test_teardown_skips_the_tombstone_when_already_tombstoned():
    cf = _TeardownCF()
    out = await dobj.teardown_script(
        cf, "s1", target="account", classes=["Room"], migration_tag=dobj.TOMBSTONE_TAG
    )
    assert out.ok and [c for c, _ in cf.calls] == ["delete_account_script", "list"]


@pytest.mark.asyncio
async def test_a_failed_tombstone_still_force_deletes():
    cf = _TeardownCF(fail={"put_worker"})
    out = await dobj.teardown_script(
        cf, "s1", target="account", classes=["Room"], migration_tag="v1"
    )
    assert out.ok is True  # the namespace list proves it is gone
    assert [c for c, _ in cf.calls] == ["delete_account_script", "list"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cf",
    [_TeardownCF(rows_after=1), _TeardownCF(fail={"delete"}), _TeardownCF(fail={"list"})],
)
async def test_teardown_reports_failure_and_never_raises(cf):
    out = await dobj.teardown_script(
        cf, "s1", target="account", classes=["Room"], migration_tag="v1"
    )
    assert out.ok is False and out.error


# ---------------------------------------------------------- delete cascade


class _CascadeCF(_TeardownCF):
    async def delete_worker_route(self, _id):
        return None

    async def delete_custom_hostname(self, _id):
        return None

    async def delete_database(self, _id):
        return None


def _do_site(**kw) -> SimpleNamespace:
    site = SimpleNamespace(
        id="s1",
        workspace="w1",
        pocket_id="",
        script_name="s1",
        deploy_target="wfp",
        d1_database_id="",
        signed_key="",
        subscription_status="none",
        domains=[],
        kv_namespaces={},
        r2_buckets={},
        do_migration_tags=["v1"],
        do_classes=["Room"],
        delete_ledger={},
    )
    site.__dict__.update(kw)
    return site


async def _cascade_until_script(site, cf) -> None:
    async def save(_s):
        return None

    deps = SimpleNamespace(cloudflare=cf)
    # Stop after the script step: the later steps need deps this test does not fake.
    site.delete_ledger.update(
        {
            s: delete_cascade.OUTCOME_DONE
            for s in delete_cascade.CASCADE_STEPS
            if delete_cascade.CASCADE_STEPS.index(s)
            > delete_cascade.CASCADE_STEPS.index(delete_cascade.STEP_SCRIPT)
        }
    )
    await delete_cascade.run_cascade(site=site, deps=deps, save=save)


@pytest.mark.asyncio
async def test_cascade_tears_down_durable_objects_before_the_script_step():
    assert delete_cascade.CASCADE_STEPS.index(delete_cascade.STEP_DO) < (
        delete_cascade.CASCADE_STEPS.index(delete_cascade.STEP_SCRIPT)
    )
    site, cf = _do_site(), _CascadeCF()
    await _cascade_until_script(site, cf)
    assert site.delete_ledger[delete_cascade.STEP_DO] == delete_cascade.OUTCOME_DONE
    names = [c for c, _ in cf.calls]
    assert names[:3] == ["put_worker", "delete_worker", "list"]
    # The script step still runs, forced, and a 404 there is success.
    assert cf.calls[3] == ("delete_worker", ("s1", True))


@pytest.mark.asyncio
async def test_a_failed_do_teardown_never_fails_the_delete():
    site, cf = _do_site(), _CascadeCF(rows_after=1)
    await _cascade_until_script(site, cf)
    assert site.delete_ledger[delete_cascade.STEP_DO] == delete_cascade.OUTCOME_PARTIAL
    assert site.delete_ledger[delete_cascade.STEP_SCRIPT] == delete_cascade.OUTCOME_DONE


@pytest.mark.asyncio
async def test_a_site_without_dos_skips_the_step():
    site, cf = _do_site(do_classes=[], do_migration_tags=[]), _CascadeCF()
    await _cascade_until_script(site, cf)
    assert site.delete_ledger[delete_cascade.STEP_DO] == delete_cascade.OUTCOME_SKIPPED
    assert cf.calls == [("delete_worker", ("s1", False))]


@pytest.mark.asyncio
async def test_a_resumed_cascade_past_the_script_only_verifies():
    site, cf = _do_site(), _CascadeCF()
    site.delete_ledger[delete_cascade.STEP_SCRIPT] = delete_cascade.OUTCOME_DONE
    cf.deleted = True
    await _cascade_until_script(site, cf)
    assert [c for c, _ in cf.calls] == ["list"]  # never re-uploads a stub


# ------------------------------------------------------------------ publish


class _DOFake(_KVFake):
    """``_KVFake`` that answers an upload with the migration tag it carried."""

    def __init__(self) -> None:
        super().__init__()
        self.metadata: list[dict] = []
        self.put_status = 200
        self.applied_tag: str | None = None
        self.do_bindings: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and "/scripts/" in request.url.path:
            request.read()
            self.requests.append(request)
            body = request.content
            chunk = body[body.index(b'name="metadata"') :].split(b"\r\n\r\n", 1)[1]
            meta = json.loads(chunk[: chunk.index(b"\r\n--")])
            self.metadata.append(meta)
            if self.put_status != 200:
                return httpx.Response(self.put_status, json={"success": False, "errors": []})
            tag = (meta.get("migrations") or {}).get("new_tag")
            if tag:
                self.applied_tag = tag
            self.do_bindings = [
                b for b in meta.get("bindings", []) if b["type"] == "durable_object_namespace"
            ]
            result = {"id": SITE_ID, **({"migration_tag": tag} if tag else {})}
            return httpx.Response(200, json={"success": True, "result": result})
        if request.method == "GET" and "/scripts/" in request.url.path:
            # What reconcile reads: the script (its migration tag) and its settings.
            request.read()
            self.requests.append(request)
            if request.url.path.endswith("/settings"):
                result = {"bindings": list(self.do_bindings)}
            else:
                result = {"script": {"id": SITE_ID, "migration_tag": self.applied_tag}}
            return httpx.Response(200, json={"success": True, "result": result})
        return super().handler(request)


DO_BASE = {**WORKER_MANIFEST, "framework": "astro"}


async def _republish(monkeypatch, pocket_id, site_id, manifest, cf, **kw):
    from pocketpaw_ee.sites import service as sites_service

    from tests.ee.sites.test_project_d1 import _source
    from tests.ee.sites.test_project_engine import _bundle

    source = _source(**{"0001_x.sql": "CREATE TABLE x (id INTEGER);"})
    source["src/marker.txt"] = json.dumps(manifest)  # a new content hash per manifest
    store = sites_service._default_artifact_store()
    store.dist[
        (pocket_id, project_build.bundle_key(project_build.project_content_hash(source)))
    ] = _bundle(manifest)
    return await _publish(pocket_id, site_id, source, cf, **kw)


async def _first(monkeypatch, manifest):
    from tests.ee.sites.test_project_d1 import _source

    source = _source(**{"0001_x.sql": "CREATE TABLE x (id INTEGER);"})
    source["src/marker.txt"] = json.dumps(manifest)
    return await _setup(monkeypatch, source, manifest), source


@pytest.mark.asyncio
async def test_publish_stores_the_tag_history_and_classes(beanie_test_db, monkeypatch):
    from pocketpaw_ee.sites import service as sites_service

    monkeypatch.setattr(sites_service, "_site_paid", lambda _site: True)  # two classes
    m1 = _do_manifest(DO_BASE, V1)
    (pocket_id, site_id), source = await _first(monkeypatch, m1)
    cf = _DOFake()
    doc = await _publish(pocket_id, site_id, source, cf.client())
    assert doc.do_migration_tags == ["v1"] and doc.do_classes == ["Room"]
    assert cf.metadata[-1]["migrations"] == {
        "new_tag": "v1",
        "steps": [{"new_sqlite_classes": ["Room"]}],
    }

    # Same history again: nothing sent, nothing changes.
    doc = await _publish(pocket_id, site_id, source, cf.client())
    assert "migrations" not in cf.metadata[-1]
    assert doc.do_migration_tags == ["v1"]

    # An appended migration goes out with old_tag.
    m2 = _do_manifest(DO_BASE, V1, V2, classes=("Room", "Chat"))
    doc = await _republish(monkeypatch, pocket_id, site_id, m2, cf.client())
    assert cf.metadata[-1]["migrations"]["old_tag"] == "v1"
    assert doc.do_migration_tags == ["v1", "v2"] and doc.do_classes == ["Room", "Chat"]


@pytest.mark.asyncio
async def test_a_destructive_publish_needs_the_owner_to_confirm(beanie_test_db, monkeypatch):
    from pocketpaw_ee.sites import service as sites_service

    monkeypatch.setattr(sites_service, "_site_paid", lambda _site: True)  # two classes
    m2 = _do_manifest(DO_BASE, V1, V2, classes=("Room", "Chat"))
    (pocket_id, site_id), source = await _first(monkeypatch, m2)
    cf = _DOFake()
    await _publish(pocket_id, site_id, source, cf.client())

    m3 = _do_manifest(DO_BASE, V1, V2, V3_DELETE)
    uploads = len(cf.metadata)
    with pytest.raises(CloudError) as exc:
        await _republish(monkeypatch, pocket_id, site_id, m3, cf.client())
    assert exc.value.code == "sites.do_data_loss_unconfirmed"
    assert "Chat" in exc.value.message and "confirm_do_data_loss" in exc.value.message
    assert len(cf.metadata) == uploads  # nothing uploaded

    doc = await _republish(
        monkeypatch, pocket_id, site_id, m3, cf.client(), confirm_do_data_loss=["Chat"]
    )
    assert doc.do_migration_tags == ["v1", "v2", "v3"] and doc.do_classes == ["Room"]
    stored = await sites_service._SiteDoc.find_one({"_id": ObjectId(site_id)})
    assert stored.do_classes == ["Room"]


@pytest.mark.asyncio
async def test_a_failed_upload_writes_no_do_state(beanie_test_db, monkeypatch):
    from pocketpaw_ee.sites import service as sites_service

    m1 = _do_manifest(DO_BASE, V1)
    (pocket_id, site_id), source = await _first(monkeypatch, m1)
    cf = _DOFake()
    cf.put_status = 500
    with pytest.raises(CloudError):
        await _publish(pocket_id, site_id, source, cf.client())
    stored = await sites_service._SiteDoc.find_one({"_id": ObjectId(site_id)})
    assert stored is None or (stored.do_migration_tags == [] and stored.do_classes == [])


def test_publish_request_carries_the_do_confirmation():
    from pocketpaw_ee.sites.dto import PublishRequest

    assert PublishRequest(pocket_id="p").confirm_do_data_loss == []
    body = PublishRequest(pocket_id="p", confirm_do_data_loss=["Room"])
    assert body.confirm_do_data_loss == ["Room"]


# ------------------------------------------------------------------- drafts


@pytest.fixture
def _drafts(monkeypatch):
    """The draft Worker env from test_draft_worker (account target, flag on)."""
    from cryptography.fernet import Fernet
    from pocketpaw_ee.sites import preview_origin

    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", "https://preview.paw.test")
    monkeypatch.setenv("PAW_SITES_DRAFT_WORKERS", "1")
    monkeypatch.setenv("PAW_SITES_PROJECT_DEPLOY_TARGET", "account")
    monkeypatch.setenv("PAW_CF_WORKERS_SUBDOMAIN", "acct")
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    draft_worker._reset_caches()
    preview_origin._clear_caches()
    yield
    draft_worker._reset_caches()


def _draft_manifest(*migrations: dict, classes=("Room",)) -> dict:
    return _do_manifest({**WORKER_MANIFEST, "bindingRequests": []}, *migrations, classes=classes)


@pytest.mark.asyncio
@pytest.mark.usefixtures("_drafts")
async def test_a_draft_stores_its_tag_history_after_the_upload():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    out = await _build(store, records, cf, registry, manifest=_draft_manifest(V1))
    assert out["preview_mode"] == "full"
    rec = await registry.get(POCKET)
    assert rec.do_migration_tags == ["v1"] and rec.do_classes == ["Room"]
    put = cf.named("put_worker")[-1]
    assert put["migrations"]["new_tag"] == "v1"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_drafts")
async def test_a_draft_allows_destructive_migrations_without_confirmation():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    m2 = _draft_manifest(V1, V2, classes=("Room", "Chat"))
    await _build(store, records, cf, registry, manifest=m2, content_hash="h1", paid=True)
    m3 = _draft_manifest(V1, V2, V3_DELETE)
    out = await _build(store, records, cf, registry, manifest=m3, content_hash="h2", paid=True)
    assert out["preview_mode"] == "full"
    rec = await registry.get(POCKET)
    assert rec.do_migration_tags == ["v1", "v2", "v3"] and rec.do_classes == ["Room"]
    assert cf.named("put_worker")[-1]["migrations"]["old_tag"] == "v2"


@pytest.mark.asyncio
@pytest.mark.usefixtures("_drafts")
async def test_a_diverged_draft_history_rotates_to_a_fresh_script():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    await _build(store, records, cf, registry, manifest=_draft_manifest(V1), content_hash="h1")
    old = (await registry.get(POCKET)).script
    rewritten = {"tag": "v1-rewritten", "new_sqlite_classes": ["Room"]}
    out = await _build(
        store, records, cf, registry, manifest=_draft_manifest(rewritten), content_hash="h2"
    )
    assert out["preview_mode"] == "full"
    rec = await registry.get(POCKET)
    assert rec.script != old and rec.script.startswith(f"paw-draft-{POCKET}-")
    assert rec.do_migration_tags == ["v1-rewritten"]
    # The old script's objects were torn down (tombstone, then a forced delete).
    tomb = [p for p in cf.named("put_worker") if p["script_name"] == old][-1]
    assert tomb["migrations"]["steps"] == [{"deleted_classes": ["Room"]}]
    assert (old, True) in cf.named("delete_account_script_forced")
    fresh = cf.named("put_worker")[-1]
    assert fresh["script_name"] == rec.script and "old_tag" not in fresh["migrations"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("_drafts")
async def test_a_new_draft_class_counts_against_the_account_budget():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    cf.namespaces_fail = True
    out = await _build(store, records, cf, registry, manifest=_draft_manifest(V1))
    assert out["preview_mode"] == "static"
    assert _record(records)["draft_worker_reason"] == "draft_worker:cap"
    assert cf.named("put_worker") == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_drafts")
async def test_purging_a_draft_with_dos_tears_them_down():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    await _build(store, records, cf, registry, manifest=_draft_manifest(V1))
    script = (await registry.get(POCKET)).script
    ok = await draft_worker.purge_pocket_drafts(
        POCKET, reason="published", cf=cf, registry=registry, store=store
    )
    assert ok is True
    tomb = cf.named("put_worker")[-1]
    assert tomb["script_name"] == script
    assert tomb["migrations"] == {
        "old_tag": "v1",
        "new_tag": dobj.TOMBSTONE_TAG,
        "steps": [{"deleted_classes": ["Room"]}],
    }
    assert (script, True) in cf.named("delete_account_script_forced")
    rec = await registry.get(POCKET)
    assert rec.do_migration_tags == [] and rec.do_classes == []


@pytest.mark.asyncio
@pytest.mark.usefixtures("_drafts")
async def test_a_failed_draft_do_teardown_is_left_for_the_sweeper():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    await _build(store, records, cf, registry, manifest=_draft_manifest(V1))
    cf.namespaces_left = True
    ok = await draft_worker.purge_pocket_drafts(
        POCKET, reason="published", cf=cf, registry=registry, store=store
    )
    assert ok is False
    rec = await registry.get(POCKET)
    assert rec.state == "deleting" and rec.retry_after is not None
