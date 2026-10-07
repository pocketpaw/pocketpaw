# Tests for a ``project`` site's own D1 database: the provisioner creating (or
# finding) the real database, the migrations ``project_d1`` applies over the D1 HTTP
# API, and the publish path end to end. Cloudflare is an httpx.MockTransport whose
# D1 query endpoint runs the SQL on a real in-memory SQLite, so splitting, tracking
# and the destructive-data check are exercised against an actual database.
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

import httpx
import pytest
from bson import ObjectId
from pocketpaw_ee.cloud._core.errors import CloudError, ValidationError
from pocketpaw_ee.sites import binding_provisioner as bp
from pocketpaw_ee.sites import project_build, project_d1
from pocketpaw_ee.sites.cloudflare_client import CloudflareClient

ACCT = "acct_1"
OK = {"success": True, "errors": [], "messages": []}
SITE_ID = "65f0c0ffee0000000000abcd"
DB_NAME = f"paw-site-{SITE_ID}"

ENTRIES = """CREATE TABLE `entries` (
\t`id` text PRIMARY KEY NOT NULL,
\t`title` text NOT NULL,
\t`body` text DEFAULT '' NOT NULL
);
--> statement-breakpoint
CREATE INDEX `entries_title_idx` ON `entries` (`title`);"""


def _err(status: int, code: int, message: str) -> httpx.Response:
    return httpx.Response(
        status, json={"success": False, "errors": [{"code": code, "message": message}]}
    )


class _FakeCF:
    """D1 (list / create / query on a real SQLite per database) plus the WfP upload."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.dbs: dict[str, tuple[str, sqlite3.Connection]] = {}  # uuid -> (name, conn)
        self.puts: list[dict] = []
        self.next_id = 0
        self.create_fails = False

    def add_db(self, name: str) -> str:
        self.next_id += 1
        uuid = f"00000000-0000-4000-8000-{self.next_id:012d}"
        self.dbs[uuid] = (name, sqlite3.connect(":memory:", isolation_level=None))
        return uuid

    def conn(self, uuid: str) -> sqlite3.Connection:
        return self.dbs[uuid][1]

    def _run(self, conn: sqlite3.Connection, sql: str, params: list) -> dict:
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description or []]
        rows = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        return {"success": True, "results": rows, "meta": {}}

    def handler(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        path = request.url.path.split(f"/accounts/{ACCT}", 1)[1]
        m = request.method
        if path == "/d1/database" and m == "GET":
            wanted = request.url.params.get("name", "")
            rows = [{"uuid": u, "name": n} for u, (n, _) in self.dbs.items() if wanted in n]
            return httpx.Response(200, json={**OK, "result": rows})
        if path == "/d1/database" and m == "POST":
            name = json.loads(request.content)["name"]
            if self.create_fails or any(n == name for n, _ in self.dbs.values()):
                return _err(400, 7502, "A database with that name already exists")
            uuid = self.add_db(name)
            return httpx.Response(200, json={**OK, "result": {"uuid": uuid, "name": name}})
        if (qm := re.fullmatch(r"/d1/database/([\w-]+)/query", path)) and m == "POST":
            conn = self.conn(qm.group(1))
            body = json.loads(request.content)
            items = body["batch"] if "batch" in body else [body]
            conn.execute("SAVEPOINT b")
            try:
                out = [self._run(conn, i["sql"], i.get("params") or []) for i in items]
            except sqlite3.Error as exc:
                conn.execute("ROLLBACK TO b")
                conn.execute("RELEASE b")
                return _err(400, 7500, f"{exc}: SQLITE_ERROR")
            conn.execute("RELEASE b")
            return httpx.Response(200, json={**OK, "result": out})
        if (dm := re.fullmatch(r"/d1/database/([\w-]+)", path)) and m == "DELETE":
            if self.dbs.pop(dm.group(1), None) is None:
                return _err(404, 7404, "database not found")
            return httpx.Response(200, json={**OK, "result": None})
        if path.endswith("/assets-upload-session"):
            return httpx.Response(200, json={**OK, "result": {"jwt": "jwt", "buckets": []}})
        if "/workers/dispatch/namespaces/" in path and m == "PUT":
            self.puts.append({"path": path})
            return httpx.Response(200, json={**OK, "result": {"id": SITE_ID}})
        return _err(404, 7003, f"unrouted {m} {path}")

    def client(self) -> CloudflareClient:
        return CloudflareClient(
            account_id=ACCT,
            api_token="tok",
            zone_id="zone",
            dispatch_namespace="paw-sites",
            _transport=httpx.MockTransport(self.handler),
        )

    def calls(self, kind: str) -> list[httpx.Request]:
        if kind == "create":
            return [
                r for r in self.requests if r.method == "POST" and r.url.path.endswith("/database")
            ]
        if kind == "batch":
            return [
                r
                for r in self.requests
                if r.url.path.endswith("/query") and b'"batch"' in r.content
            ]
        if kind == "upload":
            return [
                r
                for r in self.requests
                if r.url.path.endswith("/assets-upload-session") or r.method == "PUT"
            ]
        raise AssertionError(kind)


class _Site:
    def __init__(self, **kw: Any) -> None:
        self.id = SITE_ID
        self.workspace = "ws_1"
        self.plan_tier = "free"
        self.subscription_status = "none"
        self.d1_database_id = ""
        self.kv_namespaces: dict[str, str] = {}
        self.r2_buckets: dict[str, str] = {}
        self.saved: list[str] = []
        self.__dict__.update(kw)


async def _save(site: _Site) -> None:
    site.saved.append(site.d1_database_id)


D1_REQ = {"type": "d1", "name": "DB", "id": "someone-elses"}


def _mig(name: str, sql: str) -> project_d1.Migration:
    return project_d1.migrations_from_source({f"migrations/{name}": sql})[0]


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


# ------------------------------------------------------------- statements


def test_splits_drizzle_breakpoints_without_a_trailing_semicolon():
    assert project_d1.split_statements(ENTRIES) == [
        "CREATE TABLE `entries` (\n\t`id` text PRIMARY KEY NOT NULL,\n\t`title` text NOT NULL,"
        "\n\t`body` text DEFAULT '' NOT NULL\n)",
        "CREATE INDEX `entries_title_idx` ON `entries` (`title`)",
    ]


def test_never_splits_inside_strings_identifiers_comments_or_trigger_bodies():
    sql = (
        "INSERT INTO t VALUES ('a;b', 'it''s; --> statement-breakpoint');\n"
        "-- a comment; with a semicolon\n"
        'SELECT "x;y", [p;q] FROM t /* ; */;'
        "CREATE TRIGGER tr AFTER INSERT ON t BEGIN UPDATE t SET a = 1; DELETE FROM u; END;\n"
        "SELECT 1"
    )
    assert project_d1.split_statements(sql) == [
        "INSERT INTO t VALUES ('a;b', 'it''s; --> statement-breakpoint')",
        'SELECT "x;y", [p;q] FROM t',
        "CREATE TRIGGER tr AFTER INSERT ON t BEGIN UPDATE t SET a = 1; DELETE FROM u; END",
        "SELECT 1",
    ]
    with pytest.raises(ValidationError):
        project_d1.split_statements("SELECT 'never closed")


def test_migrations_come_from_top_level_sql_files_in_name_order():
    found = project_d1.migrations_from_source(
        {
            "migrations/20261001_b.sql": "SELECT 2",
            "migrations/0001_a.sql": "SELECT 1",
            "migrations/meta/_journal.json": "{}",
            "migrations/meta/0001_snapshot.json": "{}",
            "src/migrations/0000_x.sql": "SELECT 0",
            "package.json": "{}",
        }
    )
    assert [m.name for m in found] == ["0001_a.sql", "20261001_b.sql"]
    with pytest.raises(ValidationError) as exc:
        project_d1.migrations_from_source({"migrations/bad name'.sql": "SELECT 1"})
    assert exc.value.code == "sites.migration_invalid"


def test_drizzle_table_rebuild_is_not_destructive_but_real_drops_are():
    rebuild = _mig(
        "0002_rebuild.sql",
        "CREATE TABLE `__new_entries` (`id` text PRIMARY KEY NOT NULL);--> statement-breakpoint\n"
        'INSERT INTO `__new_entries`("id") SELECT "id" FROM `entries`;--> statement-breakpoint\n'
        "DROP TABLE `entries`;--> statement-breakpoint\n"
        "ALTER TABLE `__new_entries` RENAME TO `entries`;",
    )
    assert project_d1.destructive_targets(rebuild) == []
    risky = _mig(
        "0003_risky.sql",
        'DROP TABLE IF EXISTS `old`;\nALTER TABLE "Main"."x" DROP COLUMN y;\n'
        "DELETE FROM z;\nDELETE FROM w WHERE a = 'WHERE';\nDELETE FROM q -- WHERE\n;",
    )
    assert [t for t, _ in project_d1.destructive_targets(risky)] == ["old", "x", "z", "q"]


# --------------------------------------------------------- provisioning


@pytest.mark.asyncio
async def test_d1_is_created_once_then_reused_without_cloudflare_calls():
    fake, site = _FakeCF(), _Site()
    cf = fake.client()

    res = await bp.ensure_bindings(site, [D1_REQ], cloudflare=cf, save=_save, paid=False)

    (uuid,) = fake.dbs
    assert fake.dbs[uuid][0] == DB_NAME
    assert res.d1_database_id == uuid and site.d1_database_id == uuid
    assert site.saved == [uuid]  # persisted right after the create

    fake.requests.clear()
    again = await bp.ensure_bindings(site, [D1_REQ], cloudflare=cf, save=_save, paid=False)
    assert fake.requests == [] and again.d1_database_id == uuid and len(fake.dbs) == 1


@pytest.mark.asyncio
async def test_a_database_created_before_a_crash_is_found_by_name_not_duplicated():
    fake = _FakeCF()
    existing = fake.add_db(DB_NAME)
    fake.add_db(f"{DB_NAME}-other")  # a substring match must not be taken
    site = _Site()

    res = await bp.ensure_bindings(site, [D1_REQ], cloudflare=fake.client(), save=_save, paid=False)

    assert res.d1_database_id == existing and site.d1_database_id == existing
    assert fake.calls("create") == [] and len(fake.dbs) == 2


@pytest.mark.asyncio
async def test_a_create_race_falls_back_to_the_database_the_other_publish_made():
    fake = _FakeCF()

    class _Racing(CloudflareClient):
        async def find_database(self, name: str) -> str | None:
            found = await super().find_database(name)
            if found is None and not fake.dbs:
                fake.add_db(name)  # the other publish wins between find and create
            return found

    cf = _Racing(
        account_id=ACCT,
        api_token="tok",
        zone_id="zone",
        dispatch_namespace="paw-sites",
        _transport=httpx.MockTransport(fake.handler),
    )
    site = _Site()
    res = await bp.ensure_bindings(site, [D1_REQ], cloudflare=cf, save=_save, paid=False)
    assert len(fake.dbs) == 1 and res.d1_database_id == next(iter(fake.dbs))


@pytest.mark.asyncio
async def test_a_stored_placeholder_id_is_replaced_by_a_real_database():
    fake, site = _FakeCF(), _Site(d1_database_id="derived-placeholder")
    res = await bp.ensure_bindings(
        site,
        [D1_REQ],
        cloudflare=fake.client(),
        save=_save,
        paid=False,
        derived_d1_id="derived-placeholder",
    )
    assert res.d1_database_id in fake.dbs and site.d1_database_id == res.d1_database_id


@pytest.mark.asyncio
async def test_a_pinned_d1_is_left_alone_and_two_d1_bindings_are_refused():
    fake, site = _FakeCF(), _Site()
    await bp.ensure_bindings(
        site, [D1_REQ], cloudflare=fake.client(), save=_save, paid=False, provision_d1=False
    )
    assert fake.requests == [] and site.d1_database_id == ""
    with pytest.raises(ValidationError) as exc:
        await bp.ensure_bindings(
            site,
            [D1_REQ, {"type": "d1", "name": "OTHER"}],
            cloudflare=fake.client(),
            save=_save,
            paid=True,
        )
    assert exc.value.code == "sites.binding_cap" and fake.requests == []


# ------------------------------------------------------------- migrations


@pytest.mark.asyncio
async def test_migrations_apply_in_order_are_tracked_and_skipped_on_republish():
    fake = _FakeCF()
    db = fake.add_db(DB_NAME)
    cf = fake.client()
    migrations = project_d1.migrations_from_source(
        {
            "migrations/0002_seed.sql": "INSERT INTO entries (id, title) VALUES ('1', 'a;b');",
            "migrations/0001_entries.sql": ENTRIES,
        }
    )

    applied = await project_d1.apply_migrations(cf, db, migrations)

    assert applied == ["0001_entries.sql", "0002_seed.sql"]
    conn = fake.conn(db)
    assert conn.execute("SELECT title FROM entries").fetchall() == [("a;b",)]
    tracked = conn.execute("SELECT name, sha256, applied_at FROM _paw_migrations ORDER BY name")
    rows = tracked.fetchall()
    assert [(n, s) for n, s, _ in rows] == [(m.name, m.sha256) for m in migrations]
    assert all(at for _, _, at in rows)

    fake.requests.clear()
    assert await project_d1.apply_migrations(cf, db, migrations) == []
    assert fake.calls("batch") == []


@pytest.mark.asyncio
async def test_an_applied_migration_that_changed_is_refused():
    fake = _FakeCF()
    db = fake.add_db(DB_NAME)
    cf = fake.client()
    await project_d1.apply_migrations(cf, db, [_mig("0001_entries.sql", ENTRIES)])

    edited = _mig("0001_entries.sql", ENTRIES.replace("`title`", "`name`"))
    with pytest.raises(ValidationError) as exc:
        await project_d1.apply_migrations(
            cf, db, [edited, _mig("0002_more.sql", "CREATE TABLE more (id text)")]
        )
    assert exc.value.code == "sites.migration_changed"
    assert "0001_entries.sql" in exc.value.message
    assert "more" not in _tables(fake.conn(db))


@pytest.mark.asyncio
async def test_destructive_migration_is_refused_only_when_the_table_holds_rows():
    fake = _FakeCF()
    db = fake.add_db(DB_NAME)
    cf = fake.client()
    first = _mig("0001_entries.sql", ENTRIES)
    drop = _mig("0002_drop.sql", "DROP TABLE `entries`;")

    # A table created and dropped in one publish holds nothing: no refusal.
    assert await project_d1.apply_migrations(cf, db, [first, drop]) == [
        "0001_entries.sql",
        "0002_drop.sql",
    ]

    # So does an existing table that is empty.
    fake1 = _FakeCF()
    db1 = fake1.add_db(DB_NAME)
    await project_d1.apply_migrations(fake1.client(), db1, [first])
    assert await project_d1.apply_migrations(fake1.client(), db1, [first, drop]) == [
        "0002_drop.sql"
    ]

    fake2 = _FakeCF()
    db2 = fake2.add_db(DB_NAME)
    cf2 = fake2.client()
    seed = _mig("0002_seed.sql", "INSERT INTO entries (id, title) VALUES ('1', 'kept')")
    await project_d1.apply_migrations(cf2, db2, [first, seed])
    drop3 = _mig("0003_drop.sql", "DROP TABLE `entries`;")
    with pytest.raises(ValidationError) as exc:
        await project_d1.apply_migrations(cf2, db2, [first, seed, drop3])
    assert exc.value.code == "sites.migration_destructive"
    assert "0003_drop.sql" in exc.value.message and "confirm" in exc.value.message
    assert fake2.conn(db2).execute("SELECT count(*) FROM entries").fetchone() == (1,)

    applied = await project_d1.apply_migrations(
        cf2, db2, [first, seed, drop3], confirm_destructive=True
    )
    assert applied == ["0003_drop.sql"] and "entries" not in _tables(fake2.conn(db2))


@pytest.mark.asyncio
async def test_a_failing_migration_names_itself_and_rolls_back_its_batch():
    fake = _FakeCF()
    db = fake.add_db(DB_NAME)
    broken = _mig("0002_broken.sql", "CREATE TABLE ok (id text);\nINSERT INTO nope VALUES (1);")
    later = _mig("0003_later.sql", "CREATE TABLE later (id text)")

    with pytest.raises(ValidationError) as exc:
        await project_d1.apply_migrations(
            fake.client(), db, [_mig("0001_entries.sql", ENTRIES), broken, later]
        )

    assert exc.value.code == "sites.migration_failed"
    assert "0002_broken.sql" in exc.value.message and "no such table" in exc.value.message
    tables = _tables(fake.conn(db))
    assert "ok" not in tables and "later" not in tables
    tracked = fake.conn(db).execute("SELECT name FROM _paw_migrations").fetchall()
    assert tracked == [("0001_entries.sql",)]


# ------------------------------------------------------------ the publish


WORKER_D1_MANIFEST = {
    "slug": "x",
    "assetsDir": "dist/client",
    "workerEntry": ".paw/worker/index.js",
    "workerModuleDir": ".paw/worker",
    "mainModule": "index.js",
    "workerModules": [".paw/worker/index.js"],
    "compat": {"date": "2026-09-01", "flags": ["nodejs_compat"]},
    "bindingRequests": [{"type": "d1", "name": "DB"}, {"type": "kv", "name": "CACHE"}],
    "framework": "astro",
}


def _source(**migrations: str) -> dict[str, str]:
    from tests.ee.sites.test_project_engine import SOURCE

    return {**SOURCE, **{f"migrations/{name}": sql for name, sql in migrations.items()}}


async def _publish(pocket_id: str, site_id: str, source: dict, cf: Any, **kw: Any) -> Any:
    from pocketpaw_ee.sites import service as sites_service

    return await sites_service._deploy_site_doc(
        workspace_id="ws1",
        user_id="u1",
        pocket_id=pocket_id,
        site_id=site_id,
        signed_key="k",
        site_name="Proj",
        ripple_spec=None,
        theme={},
        engine="project",
        source=source,
        pattern="landing",
        cloudflare=cf,
        **kw,
    )


class _KVFake(_FakeCF):
    """``_FakeCF`` plus the KV namespace list / create the CACHE binding needs."""

    def __init__(self) -> None:
        super().__init__()
        self.kv: dict[str, str] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.split(f"/accounts/{ACCT}", 1)[1]
        if path == "/storage/kv/namespaces":
            request.read()
            self.requests.append(request)
            if request.method == "GET":
                rows = [{"id": i, "title": t} for i, t in self.kv.items()]
                return httpx.Response(200, json={**OK, "result": rows})
            title = json.loads(request.content)["title"]
            ns_id = f"ns{len(self.kv) + 1:030d}"
            self.kv[ns_id] = title
            return httpx.Response(200, json={**OK, "result": {"id": ns_id, "title": title}})
        return super().handler(request)


async def _setup(monkeypatch, source: dict, manifest: dict = WORKER_D1_MANIFEST):
    from pocketpaw_ee.sites import service as sites_service

    from tests.ee.sites.test_project_engine import _bundle, _make_project_pocket, _Store

    pocket_id = await _make_project_pocket()
    site_id = str(sites_service._live_object_id("ws1", pocket_id))
    store = _Store()
    content_hash = project_build.project_content_hash(source)
    store.dist[(pocket_id, project_build.bundle_key(content_hash))] = _bundle(manifest)
    monkeypatch.setattr(sites_service, "_default_artifact_store", lambda: store)
    monkeypatch.setenv("PAW_CF_DEPLOY_MODE", "wfp")
    return pocket_id, site_id


def _put_bindings(fake: _FakeCF) -> list[dict]:
    put = next(r for r in fake.requests if r.method == "PUT" and "/scripts/" in r.url.path)
    body = put.content
    chunk = body[body.index(b'name="metadata"') :].split(b"\r\n\r\n", 1)[1]
    return json.loads(chunk[: chunk.index(b"\r\n--")])["bindings"]


@pytest.mark.asyncio
async def test_first_publish_with_no_site_doc_creates_d1_migrates_and_binds(
    beanie_test_db, monkeypatch
):
    from pocketpaw_ee.sites import service as sites_service

    source = _source(**{"0001_entries.sql": ENTRIES})
    pocket_id, site_id = await _setup(monkeypatch, source)
    assert await sites_service._SiteDoc.find_one({"_id": ObjectId(site_id)}) is None
    fake = _KVFake()

    doc = await _publish(pocket_id, site_id, source, fake.client())

    (uuid,) = fake.dbs
    assert doc.deployed is True and doc.d1_database_id == uuid
    assert doc.kv_namespaces and list(doc.kv_namespaces) == ["CACHE"]  # KV on first publish
    assert "entries" in _tables(fake.conn(uuid))
    bindings = {b["name"]: b for b in _put_bindings(fake)}
    assert bindings["DB"] == {"type": "d1", "name": "DB", "id": uuid}

    # Re-publish: same database, nothing created, nothing re-applied.
    fake.requests.clear()
    again = await _publish(pocket_id, site_id, source, fake.client())
    assert again.d1_database_id == uuid and len(fake.dbs) == 1
    assert fake.calls("create") == [] and fake.calls("batch") == []


@pytest.mark.asyncio
async def test_a_failed_migration_aborts_before_any_upload(beanie_test_db, monkeypatch):
    source = _source(**{"0001_bad.sql": "INSERT INTO nope VALUES (1)"})
    pocket_id, site_id = await _setup(monkeypatch, source)
    fake = _KVFake()

    with pytest.raises(CloudError) as exc:
        await _publish(pocket_id, site_id, source, fake.client())

    assert exc.value.code == "sites.migration_failed" and "0001_bad.sql" in exc.value.message
    assert fake.calls("upload") == []


@pytest.mark.asyncio
async def test_a_destructive_migration_needs_the_owner_to_confirm(beanie_test_db, monkeypatch):
    seeded = _source(
        **{
            "0001_entries.sql": ENTRIES,
            "0002_seed.sql": "INSERT INTO entries (id, title) VALUES ('1', 'x')",
        }
    )
    pocket_id, site_id = await _setup(monkeypatch, seeded)
    fake = _KVFake()
    await _publish(pocket_id, site_id, seeded, fake.client())

    dropping = {**seeded, "migrations/0003_drop.sql": "DROP TABLE entries"}
    await _setup_bundle(pocket_id, dropping, monkeypatch)
    fake.requests.clear()
    with pytest.raises(CloudError) as exc:
        await _publish(pocket_id, site_id, dropping, fake.client())
    assert exc.value.code == "sites.migration_destructive"
    assert fake.calls("upload") == []

    await _publish(pocket_id, site_id, dropping, fake.client(), confirm_destructive_migrations=True)
    assert "entries" not in _tables(fake.conn(next(iter(fake.dbs))))


async def _setup_bundle(pocket_id: str, source: dict, monkeypatch) -> None:
    from pocketpaw_ee.sites import service as sites_service

    from tests.ee.sites.test_project_engine import _bundle

    store = sites_service._default_artifact_store()
    content_hash = project_build.project_content_hash(source)
    store.dist[(pocket_id, project_build.bundle_key(content_hash))] = _bundle(WORKER_D1_MANIFEST)


@pytest.mark.asyncio
async def test_a_bundle_without_d1_touches_no_database(beanie_test_db, monkeypatch):
    from tests.ee.sites.test_project_engine import STATIC_MANIFEST

    source = _source(**{"0001_entries.sql": ENTRIES})
    pocket_id, site_id = await _setup(monkeypatch, source, STATIC_MANIFEST)
    fake = _KVFake()

    doc = await _publish(pocket_id, site_id, source, fake.client())

    assert doc.deployed is True and doc.d1_database_id == ""
    assert fake.dbs == {} and not any("/d1/" in r.url.path for r in fake.requests)


def test_publish_request_carries_the_confirmation():
    from pocketpaw_ee.sites.dto import PublishRequest

    assert PublishRequest(pocket_id="p").confirm_destructive_migrations is False
    body = PublishRequest(pocket_id="p", confirm_destructive_migrations=True)
    assert body.confirm_destructive_migrations is True


@pytest.mark.asyncio
async def test_the_delete_cascade_removes_the_database_the_provisioner_made():
    from types import SimpleNamespace

    from pocketpaw_ee.sites import delete_cascade

    fake, site = _FakeCF(), _Site()
    cf = fake.client()
    await bp.ensure_bindings(site, [D1_REQ], cloudflare=cf, save=_save, paid=False)
    assert fake.dbs

    outcome = await delete_cascade._delete_d1(site=site, deps=SimpleNamespace(cloudflare=cf))
    assert outcome == delete_cascade.OUTCOME_DONE and fake.dbs == {}


@pytest.mark.asyncio
async def test_a_dynamic_sites_pinned_d1_is_not_provisioned_again(beanie_test_db):
    from pocketpaw_ee.sites import service as sites_service

    fake, site = _FakeCF(), _Site(pocket_id="pk1")

    async def _set(fields: dict) -> None:
        pass

    site.set = _set
    provision = sites_service._bundle_provisioner(site, fake.client(), d1_database_id="dynamic-d1")
    res = await provision([D1_REQ])

    assert res.d1_database_id == "dynamic-d1"
    assert fake.dbs == {} and not any("/d1/" in r.url.path for r in fake.requests)
