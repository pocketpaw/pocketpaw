# tests/ee/sites/test_destructive_migration_approval.py — a destructive project-site
# migration on the approval path: agent publish -> request-publish Action -> approve
# -> publish_pocket -> project_d1.apply_migrations.
#
# The rule under test: the review Action records which migrations would delete data
# (migration, table, statement) so the Tray can show them, and the confirm reaches the
# migrator ONLY when a human approves that one Action. The confirm is bound to the
# migrations' content hashes, so a migration that changed after the review is still
# refused. Bulk approve never carries it, and an approve-time edit cannot widen it.
from __future__ import annotations

import hashlib
import sqlite3
from typing import Any

import pytest
from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.cloud.models.site import Site
from pocketpaw_ee.sites import project_d1
from pocketpaw_ee.sites import service as sites_service
from pocketpaw_ee.versions import service as versions

from pocketpaw.instinct.store import InstinctStore

WS = "ws-destructive"
POCKET = "pocket-destructive"
USER = "user-destructive"
DB = "d1-real-uuid"

CREATE = "CREATE TABLE notes (id INTEGER PRIMARY KEY, body TEXT);"
DROP = "DROP TABLE notes;"


class _SqliteCF:
    """The two D1 calls project_d1 makes, run on a real in-memory SQLite."""

    def __init__(self) -> None:
        self.conn = sqlite3.connect(":memory:", isolation_level=None)
        self.queries: list[str] = []

    def _rows(self, sql: str, params: list) -> list[dict]:
        cur = self.conn.execute(sql, params)
        cols = [c[0] for c in cur.description or []]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

    async def query_d1(self, *, database_id: str, sql: str, params: list | None = None):
        assert database_id == DB
        self.queries.append(sql)
        return self._rows(sql, params or [])

    async def query_d1_batch(self, *, database_id: str, statements):
        assert database_id == DB
        return [self._rows(sql, params) for sql, params in statements]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _db_with_rows() -> tuple[_SqliteCF, list[project_d1.Migration]]:
    """A database that already ran 0001 and holds a row, plus the pending 0002 drop."""
    cf = _SqliteCF()
    source = {"migrations/0001_init.sql": CREATE}
    await project_d1.apply_migrations(cf, DB, project_d1.migrations_from_source(source))
    cf.conn.execute("INSERT INTO notes (body) VALUES ('keep me')")
    source["migrations/0002_drop.sql"] = DROP
    return cf, project_d1.migrations_from_source(source)


# ── project_d1: what is destructive, and a confirm bound to content ─────────────


@pytest.mark.asyncio
async def test_find_destructive_names_the_migration_table_and_statement():
    cf, migrations = await _db_with_rows()

    found = await project_d1.find_destructive(cf, DB, migrations)

    assert found == [
        {
            "migration": "0002_drop.sql",
            "sha256": _sha(DROP),
            "key": f"0002_drop.sql:{_sha(DROP)}",
            "table": "notes",
            "statement": "DROP TABLE notes",
        }
    ]


@pytest.mark.asyncio
async def test_find_destructive_ignores_an_empty_table():
    cf = _SqliteCF()
    source = {"migrations/0001_init.sql": CREATE, "migrations/0002_drop.sql": DROP}
    await project_d1.apply_migrations(
        cf, DB, project_d1.migrations_from_source({"migrations/0001_init.sql": CREATE})
    )
    migrations = project_d1.migrations_from_source(source)
    assert await project_d1.find_destructive(cf, DB, migrations) == []


@pytest.mark.asyncio
async def test_a_confirm_naming_the_reviewed_migration_applies_it():
    cf, migrations = await _db_with_rows()

    done = await project_d1.apply_migrations(
        cf, DB, migrations, confirm_destructive=[f"0002_drop.sql:{_sha(DROP)}"]
    )

    assert done == ["0002_drop.sql"]


@pytest.mark.asyncio
async def test_a_confirm_for_different_content_is_still_refused():
    """The owner reviewed one DROP; the file changed to another before the deploy."""
    cf, migrations = await _db_with_rows()

    with pytest.raises(ValidationError) as exc:
        await project_d1.apply_migrations(
            cf, DB, migrations, confirm_destructive=[f"0002_drop.sql:{_sha('DROP TABLE x;')}"]
        )

    assert exc.value.code == "sites.migration_destructive"
    assert cf.conn.execute("SELECT count(*) FROM notes").fetchone() == (1,)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True),
        (False, False),
        (None, False),
        ("yes", False),
        (["a:1", 2], ["a:1", "2"]),
        ((), False),
    ],
)
def test_confirm_values_normalize_without_widening(raw, expected):
    assert project_d1.normalize_confirm(raw) == expected


# ── request-publish records the destructive change on the Action ────────────────


@pytest.fixture
def instinct_store(tmp_path, monkeypatch):
    store = InstinctStore(tmp_path / "destructive_instinct.db")
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: store)
    return store


async def _project_site_with_draft(source: dict[str, str]) -> None:
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content=source
    )
    oid = await sites_service._resolve_live_site_oid(WS, POCKET)
    await Site(
        id=oid, workspace=WS, pocket_id=POCKET, owner=USER, name="Notes", d1_database_id=DB
    ).insert()


@pytest.mark.asyncio
async def test_request_publish_records_the_destructive_migrations(beanie_test_db, instinct_store):
    cf, _ = await _db_with_rows()
    await _project_site_with_draft(
        {"migrations/0001_init.sql": CREATE, "migrations/0002_drop.sql": DROP}
    )

    action = await sites_service.request_publish_pocket(
        workspace_id=WS, user_id=USER, pocket_id=POCKET, _cloudflare=cf
    )

    blob = action.parameters["_artifact_change"]
    assert blob["destructive_check"] == "ok"
    assert blob["destructive_migrations"] == [
        {
            "migration": "0002_drop.sql",
            "sha256": _sha(DROP),
            "key": f"0002_drop.sql:{_sha(DROP)}",
            "table": "notes",
            "statement": "DROP TABLE notes",
        }
    ]
    # The Tray shows the card's text, so the warning is in it, not only in the blob.
    assert "notes" in action.description
    assert "delete" in action.description.lower()


@pytest.mark.asyncio
async def test_request_publish_without_destructive_changes_records_none(
    beanie_test_db, instinct_store
):
    cf, _ = await _db_with_rows()
    await _project_site_with_draft({"migrations/0001_init.sql": CREATE})

    action = await sites_service.request_publish_pocket(
        workspace_id=WS, user_id=USER, pocket_id=POCKET, _cloudflare=cf
    )

    blob = action.parameters["_artifact_change"]
    assert blob["destructive_migrations"] == []
    assert blob["destructive_check"] == "ok"


@pytest.mark.asyncio
async def test_request_publish_records_a_failed_check_without_a_confirm(
    beanie_test_db, instinct_store
):
    """Cloudflare unreachable at review time: nothing is recorded as approved, so the
    approve publishes without a confirm and a destructive migration is refused then."""

    class _Down:
        async def query_d1(self, **_kw):
            raise ValidationError("sites.cloudflare_error", "down")

        query_d1_batch = query_d1

    await _project_site_with_draft(
        {"migrations/0001_init.sql": CREATE, "migrations/0002_drop.sql": DROP}
    )

    action = await sites_service.request_publish_pocket(
        workspace_id=WS, user_id=USER, pocket_id=POCKET, _cloudflare=_Down()
    )

    blob = action.parameters["_artifact_change"]
    assert blob["destructive_migrations"] == []
    assert blob["destructive_check"] == "failed"


@pytest.mark.asyncio
async def test_request_publish_on_a_site_without_a_database_skips_the_check(
    beanie_test_db, instinct_store
):
    await versions.write_draft(
        scope_type="pocket", scope_id=POCKET, workspace_id=WS, content={"index.html": "<p>"}
    )

    class _Never:
        async def query_d1(self, **_kw):
            raise AssertionError("no database, no query")

    action = await sites_service.request_publish_pocket(
        workspace_id=WS, user_id=USER, pocket_id=POCKET, _cloudflare=_Never()
    )

    blob = action.parameters["_artifact_change"]
    assert blob["destructive_migrations"] == []
    assert blob["destructive_check"] == "none"


# ── the executor passes the confirm only for an individually approved Action ────


def _action(blob_extra: dict[str, Any]) -> Any:
    class _A:
        id = "act-1"
        parameters = {
            "_artifact_change": {
                "schema": 1,
                "scope_type": "pocket",
                "scope_id": POCKET,
                "branch": "main",
                "to_version_id": "v-2",
                "workspace": WS,
                "user_id": USER,
                **blob_extra,
            }
        }

    return _A()


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.marks: list[tuple[str, str]] = []

    async def publish_pocket(self, **kw: Any) -> None:
        self.calls.append(kw)

    async def mark_executed(self, action_id, outcome):
        self.marks.append(("executed", outcome))

    async def mark_failed(self, action_id, outcome):
        self.marks.append(("failed", outcome))


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    rec = _Recorder()

    async def _publish_version(**_kw: Any) -> None:
        return None

    monkeypatch.setattr(versions, "publish", _publish_version)
    monkeypatch.setattr(sites_service, "publish_pocket", rec.publish_pocket)
    monkeypatch.setattr("pocketpaw.stores.get_instinct_store", lambda *a, **k: rec)
    return rec


_FINDING = {
    "migration": "0002_drop.sql",
    "sha256": _sha(DROP),
    "key": f"0002_drop.sql:{_sha(DROP)}",
    "table": "notes",
    "statement": "DROP TABLE notes",
}


@pytest.mark.asyncio
async def test_an_individual_approve_confirms_exactly_the_reviewed_migrations(recorder):
    from pocketpaw_ee.versions import instinct_executor

    await instinct_executor.execute_approved_change(
        _action({"destructive_migrations": [_FINDING]}), confirm_destructive=True
    )

    (call,) = recorder.calls
    assert call["confirm_destructive_migrations"] == [_FINDING["key"]]
    assert recorder.marks[0][0] == "executed"


@pytest.mark.asyncio
async def test_a_bulk_approve_never_confirms(recorder):
    from pocketpaw_ee.versions import instinct_executor

    await instinct_executor.execute_approved_change(_action({"destructive_migrations": [_FINDING]}))

    (call,) = recorder.calls
    assert not call.get("confirm_destructive_migrations")


@pytest.mark.asyncio
async def test_an_action_with_nothing_destructive_confirms_nothing(recorder):
    from pocketpaw_ee.versions import instinct_executor

    await instinct_executor.execute_approved_change(
        _action({"destructive_migrations": []}), confirm_destructive=True
    )

    (call,) = recorder.calls
    assert not call.get("confirm_destructive_migrations")


@pytest.mark.asyncio
async def test_a_refused_migration_fails_the_action_with_its_reason(recorder, monkeypatch):
    from pocketpaw_ee.versions import instinct_executor

    async def _refuse(**_kw: Any) -> None:
        raise ValidationError("sites.migration_destructive", "Migration 0002_drop.sql would ...")

    monkeypatch.setattr(sites_service, "publish_pocket", _refuse)

    await instinct_executor.execute_approved_change(
        _action({"destructive_migrations": []}), confirm_destructive=True
    )

    status, outcome = recorder.marks[0]
    assert status == "failed"
    assert "sites.migration_destructive" in outcome


def test_an_approve_time_edit_cannot_widen_the_confirm():
    from pocketpaw_ee.instinct.router import _pin_gated_blobs

    stored = _action({"destructive_migrations": []}).parameters
    incoming = {
        "_artifact_change": {
            **stored["_artifact_change"],
            "destructive_migrations": [_FINDING],
        }
    }

    pinned = _pin_gated_blobs(stored, incoming)

    assert pinned["_artifact_change"]["destructive_migrations"] == []


# ── the instinct router: individual approve confirms, bulk approve does not ─────


class _FakeMembership:
    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.role = "admin"


class _FakeUser:
    def __init__(self) -> None:
        self.id = USER
        self.active_workspace = WS
        self.workspaces = [_FakeMembership(WS)]


def _instinct_client(monkeypatch) -> Any:
    from unittest.mock import AsyncMock

    import pocketpaw_ee.cloud.workspace.service as ws_svc
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.instinct.router import router as instinct_router

    monkeypatch.setattr(ws_svc, "get_workspace_plan", AsyncMock(return_value="enterprise"))
    user = _FakeUser()
    app = FastAPI()
    add_error_handler(app)
    app.include_router(instinct_router)
    app.dependency_overrides[require_license] = lambda: None
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: WS
    return TestClient(app)


async def _destructive_action(instinct_store) -> Any:
    cf, _ = await _db_with_rows()
    await _project_site_with_draft(
        {"migrations/0001_init.sql": CREATE, "migrations/0002_drop.sql": DROP}
    )
    return await sites_service.request_publish_pocket(
        workspace_id=WS, user_id=USER, pocket_id=POCKET, _cloudflare=cf
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bulk", [False, True])
async def test_the_router_confirms_only_on_an_individual_approve(
    beanie_test_db, instinct_store, monkeypatch, bulk
):
    from unittest.mock import patch

    action = await _destructive_action(instinct_store)
    calls: list[dict[str, Any]] = []

    async def _deploy(**kw: Any) -> None:
        calls.append(kw)

    monkeypatch.setattr(sites_service, "publish_pocket", _deploy)
    client = _instinct_client(monkeypatch)
    with patch("pocketpaw_ee.instinct.router._store", return_value=instinct_store):
        if bulk:
            resp = client.post("/instinct/actions/bulk-approve", json={"ids": [action.id]})
        else:
            resp = client.post(f"/instinct/actions/{action.id}/approve")
    assert resp.status_code == 200, resp.text

    (call,) = calls
    if bulk:
        assert not call.get("confirm_destructive_migrations")
    else:
        assert call["confirm_destructive_migrations"] == [f"0002_drop.sql:{_sha(DROP)}"]


# ── the agent's publish files the review instead of failing or confirming ──────


@pytest.mark.asyncio
async def test_agent_publish_files_an_approval_for_a_destructive_migration(monkeypatch):
    import json

    from pocketpaw_ee.agent.mcp_servers import sites as sites_mcp

    seen: dict[str, Any] = {}

    async def _publish(**kw: Any) -> None:
        seen.setdefault("publish", []).append(kw)
        raise ValidationError("sites.migration_destructive", "Migration 0002_drop.sql would ...")

    class _Action:
        id = "act-9"
        parameters = {"_artifact_change": {"destructive_migrations": [_FINDING]}}

    async def _request(**kw: Any) -> Any:
        seen["request"] = kw
        return _Action()

    monkeypatch.setattr(sites_mcp, "_identity", lambda: (WS, USER))
    monkeypatch.setattr(sites_mcp, "record_tool_call", lambda **_kw: None)
    monkeypatch.setattr(sites_service, "publish_pocket", _publish)
    monkeypatch.setattr(sites_service, "request_publish_pocket", _request)

    out = await sites_mcp._publish_handler({"pocket_id": POCKET})

    body = json.loads(out["content"][0]["text"])
    assert body["approval_required"] is True
    assert body["action_id"] == "act-9"
    assert body["destructive_migrations"] == [_FINDING]
    assert seen["request"] == {"workspace_id": WS, "user_id": USER, "pocket_id": POCKET}
    # Exactly one publish attempt, and it never confirmed anything itself.
    (attempt,) = seen["publish"]
    assert not attempt.get("confirm_destructive_migrations")
