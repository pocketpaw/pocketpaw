# tests/ee/sites/test_draft_worker.py: the account-level draft Worker of a project
# pocket (design: docs/design/drafts/2026-10-08-sites-draft-worker.md, workspace root).
#
# Covers naming, the draft secret mapping, deploy-on-build through
# ``project_build.run_project_preview_build`` (Daytona and Cloudflare faked), the
# script-cap and failure fallbacks, cleanup on publish / delete, and the sweeper.
# Everything is behind ``PAW_SITES_DRAFT_WORKERS=1``; with it off nothing changes.
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites import build_job, draft_worker, preview_origin, project_build, slug

from tests.ee.sites.test_project_engine import (
    GEN,
    SOURCE,
    WORKER_MANIFEST,
    _bundle,
    _Records,
    _result,
    _Runner,
)

PREVIEW_BASE = "https://preview.paw.test"
POCKET = "64b7f0c2a1b2c3d4e5f60718"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PREVIEW_BASE_URL", PREVIEW_BASE)
    monkeypatch.setenv("PAW_SITES_DRAFT_WORKERS", "1")
    monkeypatch.setenv("PAW_SITES_PROJECT_DEPLOY_TARGET", "account")
    monkeypatch.setenv("PAW_CF_WORKERS_SUBDOMAIN", "acct")
    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    draft_worker._reset_caches()
    preview_origin._clear_caches()
    yield
    draft_worker._reset_caches()


class FakeCF:
    """The Cloudflare surface a draft deploy and its cleanup touch, in memory."""

    def __init__(self, scripts: int = 3) -> None:
        self.scripts = [f"site-{i}" for i in range(scripts)]
        self.calls: list[tuple[str, Any]] = []
        self.databases: dict[str, str] = {}
        self.kv: dict[str, str] = {}
        self.buckets: set[str] = set()
        self.d1_rows: dict[str, list[dict]] = {}
        self.batches: list[tuple[str, list]] = []
        self.fail: set[str] = set()
        self.list_fails = False
        self.do_scripts: set[str] = set()  # scripts holding Durable Object namespaces
        self.namespaces_fail = False
        self.namespaces_left = False  # a delete that leaves the namespaces behind
        self._ids = 0

    def _maybe_fail(self, name: str) -> None:
        if name in self.fail:
            raise ValidationError("sites.cloudflare_error", f"Cloudflare API 500: {name}")

    async def list_account_scripts(self) -> list[str]:
        if self.list_fails:
            raise ValidationError("sites.cloudflare_error", "Cloudflare API 500")
        return list(self.scripts)

    async def find_database(self, name):
        return self.databases.get(name)

    async def create_database(self, name):
        self._maybe_fail("create_database")
        self._ids += 1
        self.databases[name] = f"db-{self._ids}"
        self.calls.append(("create_database", name))
        return self.databases[name]

    async def delete_database(self, database_id):
        self._maybe_fail("delete_database")
        self.calls.append(("delete_database", database_id))
        self.databases = {k: v for k, v in self.databases.items() if v != database_id}

    async def find_kv_namespace(self, title):
        return self.kv.get(title)

    async def create_kv_namespace(self, title):
        self._ids += 1
        self.kv[title] = f"kv-{self._ids}"
        self.calls.append(("create_kv_namespace", title))
        return self.kv[title]

    async def delete_kv_namespace(self, namespace_id):
        self._maybe_fail("delete_kv_namespace")
        self.calls.append(("delete_kv_namespace", namespace_id))

    async def r2_bucket_exists(self, name):
        return name in self.buckets

    async def create_r2_bucket(self, name):
        self.buckets.add(name)
        return name

    async def delete_r2_bucket(self, name):
        self.calls.append(("delete_r2_bucket", name))
        self.buckets.discard(name)
        return True

    async def expire_r2_bucket_objects(self, name, *, max_age_seconds=86400):
        self.calls.append(("expire_r2_bucket_objects", name))

    async def query_d1(self, *, database_id, sql, params=None):
        if sql.startswith("SELECT name, sha256"):
            return list(self.d1_rows.get(database_id, []))
        return []

    async def query_d1_batch(self, *, database_id, statements):
        self._maybe_fail("query_d1_batch")
        self.batches.append((database_id, statements))
        return [[] for _ in statements]

    async def upload_assets(self, *, script_name, assets, salt, target):
        self._maybe_fail("upload_assets")
        self.calls.append(("upload_assets", script_name))
        return "jwt"

    async def put_worker(self, **kw):
        self._maybe_fail("put_worker")
        self.calls.append(("put_worker", kw))
        if kw["script_name"] not in self.scripts:
            self.scripts.append(kw["script_name"])
        steps = (kw.get("migrations") or {}).get("steps") or []
        if any(s.get("new_sqlite_classes") for s in steps):
            self.do_scripts.add(kw["script_name"])
        return True

    async def enable_workers_dev(self, script_name):
        self.calls.append(("enable_workers_dev", script_name))

    async def workers_dev_subdomain(self):
        return "acct"

    async def delete_account_script(self, script_name, *, force=False):
        self._maybe_fail("delete_account_script")
        self.calls.append(("delete_account_script", script_name))
        if force:
            self.calls.append(("delete_account_script_forced", (script_name, True)))
        if script_name in self.scripts:
            self.scripts.remove(script_name)
        if not self.namespaces_left:
            self.do_scripts.discard(script_name)

    async def list_durable_object_namespaces(self):
        if self.namespaces_fail:
            raise ValidationError("sites.cloudflare_error", "Cloudflare API 500")
        return [{"id": f"ns-{s}", "script": s, "class": "Room"} for s in sorted(self.do_scripts)]

    def named(self, name: str) -> list[Any]:
        return [arg for call, arg in self.calls if call == name]


class Store:
    """Artifact store with the draft-preview surface plus ``purge_previews``."""

    def __init__(self) -> None:
        self.dist: dict[tuple[str, str], bytes] = {}
        self.tokens: dict[tuple[str, str], str] = {}

    def read(self, pocket_id, content_hash):
        return None

    def write(self, *a):
        pass

    def write_dist(self, pocket_id, content_hash, data):
        self.dist[(pocket_id, content_hash)] = data
        return True

    def read_dist(self, pocket_id, content_hash):
        return self.dist.get((pocket_id, content_hash))

    def write_preview_token(self, pocket_id, content_hash, token):
        self.tokens[(pocket_id, content_hash)] = token
        return True

    def read_preview_token(self, pocket_id, content_hash):
        return self.tokens.get((pocket_id, content_hash))

    def resolve_preview_token(self, token):
        for key, value in self.tokens.items():
            if value == token:
                return key
        return None

    def purge_previews(self, pocket_id):
        for key in [k for k in self.tokens if k[0] == pocket_id]:
            del self.tokens[key]
        for key in [k for k in self.dist if k[0] == pocket_id and not k[1].endswith(".bundle")]:
            del self.dist[key]
        return True


SECRETS = {
    "STRIPE_KEY": "sk_live_x",
    "STRIPE_KEY__DRAFT": "sk_test_x",
    "RESEND_KEY": "re_x",
    "BETTER_AUTH_SECRET": "prod-signing",
}


def _deps(
    cf: FakeCF,
    registry: Any,
    *,
    paid: bool = False,
    secrets: dict | None = None,
    alive: bool = True,
):
    async def _context(pocket_id):
        return "ws1", paid

    async def _secrets(workspace_id, pocket_id):
        return dict(SECRETS if secrets is None else secrets)

    async def _alive(pocket_id, workspace_id):
        return alive

    return {
        "cf": cf,
        "registry": registry,
        "context": _context,
        "secrets_reader": _secrets,
        "alive": _alive,
    }


MIGRATING_SOURCE = {
    **SOURCE,
    "migrations/0001_entries.sql": "CREATE TABLE entries (id INTEGER PRIMARY KEY);",
    "seed/0001.sql": "INSERT INTO entries (id) VALUES (1);",
}


async def _build(
    store: Store,
    records: _Records,
    cf: FakeCF,
    registry: Any,
    *,
    content_hash: str = "h1",
    manifest: dict | None = None,
    index: bool = True,
    source: dict | None = None,
    **deps_kw: Any,
) -> dict:
    runner = _Runner(_result(artifact=_bundle(manifest or WORKER_MANIFEST, index=index)))
    return await project_build.run_project_preview_build(
        {},
        POCKET,
        content_hash,
        {"source": source or MIGRATING_SOURCE},
        600,
        _runner=runner,
        _store=store,
        _verify_store=records,
        _gen_uploads=GEN,
        _draft=_deps(cf, registry, **deps_kw),
    )


def _record(records: _Records, content_hash: str = "h1") -> dict:
    job_id = build_job._preview_job_id(POCKET, content_hash)
    return records.read(POCKET, project_build.build_record_key(job_id))


# ---------------------------------------------------------------- naming + secrets


def test_script_names_are_reserved_unguessable_and_rotate():
    a = draft_worker.new_script_name(POCKET)
    b = draft_worker.new_script_name(POCKET)
    assert a != b
    assert a.startswith(f"paw-draft-{POCKET}-") and len(a) <= 63
    assert slug.has_reserved_prefix(a)  # no user slug can ever claim it
    assert draft_worker.pocket_of_script(a) == POCKET
    assert draft_worker.pocket_of_script("acme-bakery") is None
    assert draft_worker.pocket_of_script(f"paw-site-{POCKET}") is None


def test_drafts_bind_only_draft_values_never_production_secrets():
    out = draft_worker.draft_secrets(SECRETS, signing_value="draft-signing")
    assert out["STRIPE_KEY"] == "sk_test_x"  # NAME__DRAFT binds as NAME
    assert "STRIPE_KEY__DRAFT" not in out
    assert "RESEND_KEY" not in out  # a production value never reaches a draft
    assert "sk_live_x" not in out.values() and "re_x" not in out.values()
    assert out["BETTER_AUTH_SECRET"] == "draft-signing"
    for name in draft_worker.SIGNING_SECRETS:
        assert out[name] == "draft-signing"


def test_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv("PAW_SITES_DRAFT_WORKERS")
    assert draft_worker.enabled() is False


# ---------------------------------------------------------------- deploy on build


async def test_worker_build_deploys_a_draft_worker_and_previews_full():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    out = await _build(store, records, cf, registry)

    assert out["status"] == "built"
    assert out["preview_mode"] == "full"
    assert _record(records)["preview_mode"] == "full"
    assert _record(records).get("draft_worker_reason") is None

    rec = await registry.get(POCKET)
    assert rec.state == "live" and rec.deployed_hash == "h1"
    assert rec.script.startswith(f"paw-draft-{POCKET}-")
    assert rec.host == f"{rec.script}.acct.workers.dev"
    assert cf.named("enable_workers_dev") == [rec.script]

    (put,) = cf.named("put_worker")
    assert put["script_name"] == rec.script and put["target"] == "account"
    bindings = {b["name"]: b for b in put["bindings"]}
    # Draft-only resources, never the site's own.
    assert bindings["DB"]["id"] == rec.d1_database_id
    assert cf.named("create_database") == [f"paw-draft-{POCKET}"]
    assert all(t.startswith(f"paw-draft{POCKET}-") for t in cf.named("create_kv_namespace"))
    # Platform env points at the preview origin and wins over owner values.
    token = store.tokens[(POCKET, "h1")]
    url = preview_origin.preview_url_for(token).rstrip("/")
    assert bindings["BETTER_AUTH_URL"] == {
        "type": "plain_text",
        "name": "BETTER_AUTH_URL",
        "text": url,
    }
    assert bindings["PAW_SITE_URL"]["text"] == url
    assert bindings["STRIPE_KEY"]["text"] == "sk_test_x"
    assert "STRIPE_KEY__DRAFT" not in bindings
    assert "RESEND_KEY" not in bindings
    assert bindings["BETTER_AUTH_SECRET"]["text"] not in ("prod-signing", "")
    # The guard wrapper is the entry; only a caller with the per-draft key gets in.
    assert put["main_module"] == draft_worker.GUARD_MODULE
    guard = next(m for m in put["modules"] if m.name == draft_worker.GUARD_MODULE)
    assert b'from "./index.js"' in guard.content and b"x-paw-draft-key" in guard.content
    assert {m.name for m in put["modules"]} >= {"index.js", draft_worker.GUARD_MODULE}
    key = bindings["PAW_DRAFT_KEY"]
    assert key["type"] == "secret_text" and len(key["text"]) >= 32
    assert rec.draft_key_enc and key["text"] not in rec.draft_key_enc
    # Draft limits + full-rate observability.
    assert put["observability"]["head_sampling_rate"] == 1.0
    assert put["limits"]["cpu_ms"] == 50

    # Migrations then the seed ran against the draft database.
    sqls = [s for _db, batch in cf.batches for s, _p in batch]
    assert any("CREATE TABLE entries" in s for s in sqls)
    assert any("INSERT INTO entries" in s for s in sqls)
    assert rec.seeded is True


async def test_redeploy_reuses_the_draft_and_keeps_the_signing_secret():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    await _build(store, records, cf, registry, content_hash="h1")
    first = await registry.get(POCKET)
    await _build(store, records, cf, registry, content_hash="h2")
    second = await registry.get(POCKET)
    assert second.script == first.script and second.deployed_hash == "h2"
    assert len(cf.named("create_database")) == 1
    secrets = [
        {b["name"]: b.get("text") for b in put["bindings"]}["BETTER_AUTH_SECRET"]
        for put in cf.named("put_worker")
    ]
    assert secrets[0] == secrets[1]
    # The seed runs once, on the fresh database only.
    seeds = [s for _db, b in cf.batches for s, _p in b if s.startswith("INSERT INTO entries")]
    assert len(seeds) == 1


async def test_flag_off_keeps_the_static_preview(monkeypatch):
    monkeypatch.delenv("PAW_SITES_DRAFT_WORKERS")
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    out = await _build(store, records, cf, registry)
    assert out["preview_mode"] == "static"
    assert cf.calls == []
    assert await registry.get(POCKET) is None


async def test_dispatch_target_does_not_deploy_drafts(monkeypatch):
    monkeypatch.setenv("PAW_SITES_PROJECT_DEPLOY_TARGET", "dispatch")
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    out = await _build(store, records, cf, registry)
    assert out["preview_mode"] == "static"
    assert _record(records)["draft_worker_reason"] == "draft_worker:disabled"
    assert cf.named("put_worker") == []


async def test_a_failed_deploy_falls_back_to_static_and_never_fails_the_build():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    cf.fail.add("put_worker")
    out = await _build(store, records, cf, registry)
    assert out["status"] == "built"
    assert out["preview_mode"] == "static"
    assert _record(records)["draft_worker_reason"] == "draft_worker:deploy_failed"
    rec = await registry.get(POCKET)
    # What was provisioned is kept for the next build or for cleanup; nothing proxies.
    assert rec.d1_database_id and rec.deployed_hash == ""


async def test_a_failed_deploy_without_an_index_is_server_only():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    cf.fail.add("put_worker")
    out = await _build(store, records, cf, registry, index=False)
    assert out["preview_mode"] == project_build.PREVIEW_SERVER_ONLY


async def test_server_only_template_previews_full_through_the_draft():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    out = await _build(store, records, cf, registry, index=False)
    assert out["preview_mode"] == "full"
    # A token is minted for the proxied draft even with no static files to serve.
    assert (POCKET, "h1") in store.tokens and (POCKET, "h1") not in store.dist


async def test_migration_failure_is_its_own_rung():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    cf.fail.add("query_d1_batch")
    out = await _build(store, records, cf, registry)
    assert out["preview_mode"] == "static"
    assert _record(records)["draft_worker_reason"] == "draft_worker:migration_failed"
    assert cf.named("put_worker") == []


async def test_a_changed_applied_migration_recreates_the_draft_database():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    await _build(store, records, cf, registry, content_hash="h1")
    old_db = (await registry.get(POCKET)).d1_database_id
    cf.d1_rows[old_db] = [{"name": "0001_entries.sql", "sha256": "something-else"}]
    out = await _build(store, records, cf, registry, content_hash="h2")
    assert out["preview_mode"] == "full"
    rec = await registry.get(POCKET)
    assert old_db in cf.named("delete_database")
    assert rec.d1_database_id != old_db
    put = cf.named("put_worker")[-1]
    assert {b["name"]: b for b in put["bindings"]}["DB"]["id"] == rec.d1_database_id


async def test_missing_required_secrets_fall_back():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    # RESEND_KEY is set for production but has no RESEND_KEY__DRAFT: still missing.
    required = ["OPENAI_KEY", "RESEND_KEY", "STRIPE_KEY"]
    manifest = {**WORKER_MANIFEST, "requiredSecrets": required}
    out = await _build(store, records, cf, registry, manifest=manifest)
    assert out["preview_mode"] == "static"
    assert _record(records)["draft_worker_reason"] == "draft_worker:secrets_missing"
    assert _record(records)["draft_secrets_missing"] == ["OPENAI_KEY", "RESEND_KEY"]
    assert cf.named("put_worker") == []


async def test_paid_bindings_on_free_fall_back_not_entitled():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    manifest = {**WORKER_MANIFEST, "bindingRequests": [{"type": "r2", "name": "FILES"}]}
    out = await _build(store, records, cf, registry, manifest=manifest)
    assert _record(records)["draft_worker_reason"] == "draft_worker:not_entitled"
    assert out["preview_mode"] == "static"


# ---------------------------------------------------------------- script cap


async def test_at_the_cap_no_new_script_is_created(monkeypatch):
    monkeypatch.setenv("PAW_SITES_DRAFT_SCRIPT_CAP", "5")
    store, records, cf, registry = (
        Store(),
        _Records(),
        FakeCF(scripts=5),
        draft_worker.MemoryRegistry(),
    )
    out = await _build(store, records, cf, registry)
    assert out["preview_mode"] == "static"
    assert _record(records)["draft_worker_reason"] == "draft_worker:cap"
    assert cf.named("put_worker") == [] and cf.named("create_database") == []


async def test_an_unreadable_script_count_fails_closed():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    cf.list_fails = True
    out = await _build(store, records, cf, registry)
    assert _record(records)["draft_worker_reason"] == "draft_worker:cap"
    assert out["preview_mode"] == "static"


async def test_redeploying_an_existing_draft_is_exempt_from_the_cap(monkeypatch):
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    await _build(store, records, cf, registry, content_hash="h1")
    monkeypatch.setenv("PAW_SITES_DRAFT_SCRIPT_CAP", "1")
    draft_worker._reset_caches()
    cf.list_fails = True  # never consulted for a redeploy
    out = await _build(store, records, cf, registry, content_hash="h2")
    assert out["preview_mode"] == "full"


# ---------------------------------------------------------------- cleanup


async def _live_draft() -> tuple[Store, FakeCF, Any]:
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    manifest = {
        **WORKER_MANIFEST,
        "bindingRequests": [{"type": "d1", "name": "DB"}, {"type": "kv", "name": "CACHE"}],
    }
    await _build(store, records, cf, registry, manifest=manifest)
    store.tokens[(POCKET, "h0")] = "a" * 32  # an older html/static draft token
    store.dist[(POCKET, "h0")] = b"old"
    return store, cf, registry


async def test_publish_purges_every_draft_and_remembers_the_published_hash():
    store, cf, registry = await _live_draft()
    rec = await registry.get(POCKET)
    ok = await draft_worker.purge_pocket_drafts(
        POCKET,
        reason="published",
        published_hash="h1",
        published_url="https://acme.acct.workers.dev",
        cf=cf,
        registry=registry,
        store=store,
    )
    assert ok is True
    assert rec.script in cf.named("delete_account_script")
    assert rec.d1_database_id in cf.named("delete_database")
    assert cf.named("delete_kv_namespace") == list(rec.kv_namespaces.values())
    assert not [k for k in store.tokens if k[0] == POCKET]
    assert (POCKET, "h0") not in store.dist
    assert (POCKET, "h1.bundle") in store.dist  # the publish bundle stays
    after = await registry.get(POCKET)
    assert after.state == "purged" and after.script == "" and after.d1_database_id == ""
    assert after.published_hash == "h1"
    assert after.published_url == "https://acme.acct.workers.dev"
    assert await draft_worker.proxy_target(POCKET, "h1", registry=registry) is None


async def test_purge_is_idempotent():
    store, cf, registry = await _live_draft()
    for _ in range(2):
        assert await draft_worker.purge_pocket_drafts(
            POCKET, reason="published", cf=cf, registry=registry, store=store
        )
    assert len(cf.named("delete_account_script")) == 1


async def test_a_failed_cleanup_is_left_deleting_and_the_sweeper_finishes_it():
    store, cf, registry = await _live_draft()
    cf.fail.add("delete_database")
    ok = await draft_worker.purge_pocket_drafts(
        POCKET, reason="published", published_hash="h1", cf=cf, registry=registry, store=store
    )
    assert ok is False
    rec = await registry.get(POCKET)
    assert rec.state == "deleting" and rec.attempts == 1 and "delete_database" in rec.last_error
    # Nothing proxies while a purge is pending.
    assert await draft_worker.proxy_target(POCKET, "h1", registry=registry) is None

    cf.fail.clear()
    rec.retry_after = None
    await registry.put(rec)
    out = await draft_worker.sweep_draft_workers(cf=cf, registry=registry, store=store)
    assert out["purged"] == 1
    after = await registry.get(POCKET)
    assert after.state == "purged" and after.published_hash == "h1"


async def test_forget_removes_the_row_for_a_deleted_site_or_pocket():
    store, cf, registry = await _live_draft()
    assert await draft_worker.purge_pocket_drafts(
        POCKET, reason="pocket_deleted", forget=True, cf=cf, registry=registry, store=store
    )
    assert await registry.get(POCKET) is None


async def test_sweeper_reaps_idle_drafts_and_orphan_scripts(monkeypatch):
    store, cf, registry = await _live_draft()
    rec = await registry.get(POCKET)
    rec.deployed_at = datetime.now(UTC) - timedelta(days=8)
    await registry.put(rec)
    orphan = draft_worker.new_script_name("64b7f0c2a1b2c3d4e5f60799")
    cf.scripts.append(orphan)
    out = await draft_worker.sweep_draft_workers(cf=cf, registry=registry, store=store)
    assert out["purged"] == 1 and out["orphans"] == 1
    assert orphan in cf.named("delete_account_script")
    assert (await registry.get(POCKET)).state == "purged"


async def test_sweeper_keeps_fresh_drafts(monkeypatch):
    store, cf, registry = await _live_draft()
    out = await draft_worker.sweep_draft_workers(cf=cf, registry=registry, store=store)
    assert out["purged"] == 0
    assert (await registry.get(POCKET)).state == "live"


async def test_sweeper_is_a_no_op_with_the_flag_off(monkeypatch):
    monkeypatch.delenv("PAW_SITES_DRAFT_WORKERS")
    out = await draft_worker.sweep_draft_workers(
        cf=FakeCF(), registry=draft_worker.MemoryRegistry(), store=Store()
    )
    assert out == {"purged": 0, "orphans": 0, "failed": 0}


# ---------------------------------------------------------------- proxy target


async def test_proxy_target_only_for_the_deployed_hash():
    store, cf, registry = await _live_draft()
    rec = await registry.get(POCKET)
    target = await draft_worker.proxy_target(POCKET, "h1", registry=registry)
    assert target is not None and target.host == rec.host
    draft_worker._reset_caches()
    # A superseded hash of a proxied draft is a 404, not a static fallback.
    assert (
        await draft_worker.proxy_target(POCKET, "h0", registry=registry) is draft_worker.SUPERSEDED
    )


def test_record_round_trips_through_json():
    rec = draft_worker.DraftRecord(pocket_id=POCKET, script="s", kv_namespaces={"A": "1"})
    assert draft_worker.DraftRecord(**json.loads(json.dumps(rec.to_dict()))) == rec


# ---------------------------------------------------------------- deploy / purge race


async def test_a_purge_during_the_deploy_wins_and_the_upload_is_removed():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    real_put = cf.put_worker

    async def _put_then_publish(**kw):
        # The site is published while this draft is uploading.
        await draft_worker.purge_pocket_drafts(
            POCKET, reason="published", published_hash="hp", cf=cf, registry=registry, store=store
        )
        return await real_put(**kw)

    cf.put_worker = _put_then_publish
    out = await _build(store, records, cf, registry)
    assert out["preview_mode"] == "static"
    assert _record(records)["draft_worker_reason"] == "draft_worker:superseded"
    rec = await registry.get(POCKET)
    assert rec.state == "purged" and rec.published_hash == "hp" and rec.deployed_hash == ""
    uploaded = cf.named("put_worker")[0]["script_name"]
    assert uploaded not in cf.scripts  # the late upload was deleted again
    assert not cf.databases  # and so was the draft database it bound
    assert await draft_worker.proxy_target(POCKET, "h1", registry=registry) is None


async def test_a_row_deleted_mid_deploy_is_not_resurrected():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    real_put = cf.put_worker

    async def _put_then_delete_pocket(**kw):
        await draft_worker.purge_pocket_drafts(
            POCKET, reason="pocket_deleted", forget=True, cf=cf, registry=registry, store=store
        )
        return await real_put(**kw)

    cf.put_worker = _put_then_delete_pocket
    await _build(store, records, cf, registry)
    assert await registry.get(POCKET) is None
    assert not [s for s in cf.scripts if s.startswith("paw-draft-")]


async def test_a_deleted_pocket_is_never_uploaded():
    store, records, cf, registry = Store(), _Records(), FakeCF(), draft_worker.MemoryRegistry()
    out = await _build(store, records, cf, registry, alive=False)
    assert out["preview_mode"] == "static"
    assert _record(records)["draft_worker_reason"] == "draft_worker:gone"
    assert cf.named("put_worker") == [] and cf.named("upload_assets") == []
    assert not cf.databases


async def test_cas_refuses_a_stale_version():
    registry = draft_worker.MemoryRegistry()
    rec = draft_worker.DraftRecord(pocket_id=POCKET)
    assert await registry.cas(rec, None) is True and rec.version == 1
    stale = draft_worker.DraftRecord(pocket_id=POCKET, version=0)
    assert await registry.cas(stale, 0) is False
    assert await registry.cas(rec, 1) is True and rec.version == 2


# ---------------------------------------------------------------- cap reservation


async def test_a_new_draft_reserves_its_slot_and_a_failure_releases_it(monkeypatch):
    monkeypatch.setenv("PAW_SITES_DRAFT_SCRIPT_CAP", "4")
    cf = FakeCF(scripts=3)
    assert await draft_worker._reserve_slot(cf) is True
    # A second deploy in the same window sees the reserved slot.
    assert await draft_worker._reserve_slot(cf) is False
    draft_worker._release_slot()
    assert await draft_worker._reserve_slot(cf) is True


async def test_a_failed_new_deploy_gives_its_slot_back(monkeypatch):
    monkeypatch.setenv("PAW_SITES_DRAFT_SCRIPT_CAP", "4")
    store, records, registry = Store(), _Records(), draft_worker.MemoryRegistry()
    cf = FakeCF(scripts=3)
    cf.fail.add("put_worker")
    await _build(store, records, cf, registry)
    assert await draft_worker._reserve_slot(cf) is True


# ---------------------------------------------------------------- environment tag


def test_the_env_tag_scopes_names_and_the_orphan_match(monkeypatch):
    untagged = draft_worker.new_script_name(POCKET)
    monkeypatch.setenv("PAW_SITES_DRAFT_ENV_TAG", "Stg_1")
    tagged = draft_worker.new_script_name(POCKET)
    assert tagged.startswith(f"paw-draft-stg1-{POCKET}-") and len(tagged) <= 63
    assert draft_worker.draft_database_name(POCKET) == f"paw-draft-stg1-{POCKET}"
    assert draft_worker.pocket_of_script(tagged) == POCKET
    assert draft_worker.pocket_of_script(untagged) is None
    monkeypatch.setenv("PAW_SITES_DRAFT_ENV_TAG", "prod")
    assert draft_worker.pocket_of_script(tagged) is None
    monkeypatch.delenv("PAW_SITES_DRAFT_ENV_TAG")
    assert draft_worker.pocket_of_script(untagged) == POCKET
    assert draft_worker.pocket_of_script(tagged) is None


async def test_the_orphan_sweep_leaves_other_deployments_scripts_alone(monkeypatch):
    monkeypatch.setenv("PAW_SITES_DRAFT_ENV_TAG", "stg")
    other = f"paw-draft-{POCKET}-0123456789abcdef"  # an untagged deployment's draft
    mine = draft_worker.new_script_name("64b7f0c2a1b2c3d4e5f60799")
    cf = FakeCF()
    cf.scripts += [other, mine]
    out = await draft_worker.sweep_draft_workers(
        cf=cf, registry=draft_worker.MemoryRegistry(), store=Store()
    )
    assert out["orphans"] == 1
    assert cf.named("delete_account_script") == [mine]
