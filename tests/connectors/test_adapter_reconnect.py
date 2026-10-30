# test_adapter_reconnect.py — connector-store-unification CS-5 — reconnect audit.
# Created: 2026-06-12 — Locks the reconnect-safety fixes from the native
#   adapter audit: DatabaseAdapter disposes its previous engine on a second
#   connect() (no leaked pool) and resets _connected on a failed connect;
#   MongoDBAdapter closes its previous motor client on a second connect()
#   (no leaked socket pool) and resets _connected on a failed connect. These
#   are the double-connect shapes ensure_connected's restart-recovery path
#   makes more likely (config change → disconnect+reconnect; racing executes).

from __future__ import annotations

from typing import Any

import pytest

from pocketpaw.connectors.db_adapter import DatabaseAdapter
from pocketpaw.connectors.mongo_adapter import MongoDBAdapter

# ---------------------------------------------------------------------------
# DatabaseAdapter (sqlalchemy engine)
# ---------------------------------------------------------------------------


class _FakeConn:
    async def execute(self, _stmt: Any) -> None:
        return None

    async def __aenter__(self) -> _FakeConn:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class _FakeEngine:
    fail_connect = False

    def __init__(self) -> None:
        self.disposed = False

    def connect(self) -> Any:
        if self.fail_connect:
            raise RuntimeError("db unreachable")
        return _FakeConn()

    async def dispose(self) -> None:
        self.disposed = True


@pytest.fixture
def fake_engines(monkeypatch) -> list[_FakeEngine]:
    """Route create_async_engine to fakes; returns the created instances.

    Per-engine failure is staged by monkeypatching ``_FakeEngine.fail_connect``
    (a class attribute) before the connect under test.
    """
    created: list[_FakeEngine] = []

    def _fake_create(*_args: Any, **_kwargs: Any) -> _FakeEngine:
        engine = _FakeEngine()
        created.append(engine)
        return engine

    import sqlalchemy.ext.asyncio as sa_asyncio

    monkeypatch.setattr(sa_asyncio, "create_async_engine", _fake_create)
    return created


_DB_CONFIG = {"DB_HOST": "h", "DB_NAME": "d", "DB_USER": "u", "DB_PASSWORD": "p"}


@pytest.mark.asyncio
async def test_db_double_connect_disposes_previous_engine(fake_engines) -> None:
    adapter = DatabaseAdapter("postgresql")
    assert (await adapter.connect("p1", _DB_CONFIG)).success is True
    assert (await adapter.connect("p1", {**_DB_CONFIG, "DB_PASSWORD": "p2"})).success is True

    assert len(fake_engines) == 2
    assert fake_engines[0].disposed is True  # the old pool must not leak
    assert fake_engines[1].disposed is False
    assert adapter._engine is fake_engines[1]
    assert adapter._connected is True


@pytest.mark.asyncio
async def test_db_failed_reconnect_resets_connected_flag(fake_engines, monkeypatch) -> None:
    adapter = DatabaseAdapter("postgresql")
    assert (await adapter.connect("p1", _DB_CONFIG)).success is True
    assert adapter._connected is True

    # Next engine refuses to connect (service down / bad creds).
    monkeypatch.setattr(_FakeEngine, "fail_connect", True)
    result = await adapter.connect("p1", {**_DB_CONFIG, "DB_PASSWORD": "bad"})
    assert result.success is False
    # The adapter must not report a connection it no longer has.
    assert adapter._connected is False
    assert adapter._engine is None
    # And the original engine was still disposed, not leaked.
    assert fake_engines[0].disposed is True


# ---------------------------------------------------------------------------
# MongoDBAdapter (motor client)
# ---------------------------------------------------------------------------


class _FakeAdmin:
    def __init__(self, fail_ping: bool) -> None:
        self._fail_ping = fail_ping

    async def command(self, _name: str) -> None:
        if self._fail_ping:
            raise RuntimeError("mongo unreachable")
        return None


class _FakeMotorClient:
    fail_ping = False

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        self.closed = False
        self.admin = _FakeAdmin(self.fail_ping)

    def close(self) -> None:
        self.closed = True

    def __getitem__(self, _name: str) -> Any:
        return object()


@pytest.fixture
def fake_motor_clients(monkeypatch) -> list[_FakeMotorClient]:
    created: list[_FakeMotorClient] = []

    class _Recording(_FakeMotorClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

    import motor.motor_asyncio as motor_asyncio

    monkeypatch.setattr(motor_asyncio, "AsyncIOMotorClient", _Recording)
    return created


_MONGO_CONFIG = {"MONGO_URI": "mongodb://h:27017", "MONGO_DATABASE": "d"}


@pytest.mark.asyncio
async def test_mongo_double_connect_closes_previous_client(fake_motor_clients) -> None:
    adapter = MongoDBAdapter()
    assert (await adapter.connect("p1", _MONGO_CONFIG)).success is True
    assert (await adapter.connect("p1", _MONGO_CONFIG)).success is True

    assert len(fake_motor_clients) == 2
    assert fake_motor_clients[0].closed is True  # the old socket pool must not leak
    assert fake_motor_clients[1].closed is False
    assert adapter._client is fake_motor_clients[1]
    assert adapter._connected is True


@pytest.mark.asyncio
async def test_mongo_failed_reconnect_resets_connected_flag(
    fake_motor_clients, monkeypatch
) -> None:
    adapter = MongoDBAdapter()
    assert (await adapter.connect("p1", _MONGO_CONFIG)).success is True
    assert adapter._connected is True

    monkeypatch.setattr(_FakeMotorClient, "fail_ping", True)
    result = await adapter.connect("p1", _MONGO_CONFIG)
    assert result.success is False
    assert adapter._connected is False
    assert adapter._client is None
    assert fake_motor_clients[0].closed is True
