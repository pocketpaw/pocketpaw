"""``get_stream_transport`` selection — Redis when URL set, memory fallback."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat.runs import transport as transport_mod
from pocketpaw_ee.cloud.chat.runs.memory_stream import InMemoryStreamTransport


def test_memory_fallback_when_redis_url_unset(monkeypatch, caplog):
    monkeypatch.delenv("POCKETPAW_REDIS_URL", raising=False)
    monkeypatch.delenv("POCKETPAW_CLOUD_STREAM_TRANSPORT", raising=False)
    transport_mod._reset_for_tests()

    with caplog.at_level("WARNING", logger="pocketpaw_ee.cloud.chat.runs.transport"):
        t = transport_mod.get_stream_transport()

    assert isinstance(t, InMemoryStreamTransport)
    assert any("in-memory stream transport" in r.message for r in caplog.records)
    transport_mod._reset_for_tests()


def test_redis_selected_when_url_set(monkeypatch):
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://test:6379/0")
    monkeypatch.delenv("POCKETPAW_CLOUD_STREAM_TRANSPORT", raising=False)
    transport_mod._reset_for_tests()

    from pocketpaw_ee.cloud._core import redis_client as redis_client_mod
    from pocketpaw_ee.cloud.chat.runs.redis_stream import RedisStreamTransport

    redis_client_mod._reset_for_tests()
    t = transport_mod.get_stream_transport()

    assert isinstance(t, RedisStreamTransport)
    transport_mod._reset_for_tests()
    redis_client_mod._reset_for_tests()


def test_explicit_memory_override(monkeypatch):
    monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://test:6379/0")
    monkeypatch.setenv("POCKETPAW_CLOUD_STREAM_TRANSPORT", "memory")
    transport_mod._reset_for_tests()

    t = transport_mod.get_stream_transport()
    assert isinstance(t, InMemoryStreamTransport)
    transport_mod._reset_for_tests()


def test_unknown_backend_raises(monkeypatch):
    monkeypatch.setenv("POCKETPAW_CLOUD_STREAM_TRANSPORT", "nats")
    transport_mod._reset_for_tests()

    with pytest.raises(RuntimeError, match="unknown POCKETPAW_CLOUD_STREAM_TRANSPORT"):
        transport_mod.get_stream_transport()
    transport_mod._reset_for_tests()
