# Regression: the Mongo and Redis clients are built with explicit ceilings.
#
# Created 2026-09-04 (backend-perf H6). Both were constructed with the URL and
# nothing else, so both ran entirely on library defaults, and the defaults are
# the wrong shape for a request-serving process:
#
#   Mongo   serverSelectionTimeoutMS defaults to 30s, so a brief blip becomes
#           30-second request hangs rather than fast failures, and every hung
#           request holds a worker slot. socketTimeoutMS and waitQueueTimeoutMS
#           both default to None, i.e. wait forever.
#   Redis   max_connections is effectively unbounded on the async pool. That is
#           worse here than it would be elsewhere, because every open SSE run
#           stream parks a connection in XREAD BLOCK 15000 for as long as the
#           client stays subscribed — so connection count tracked open browser
#           tabs, with no ceiling and no early signal.
#
# Two of these tests exist to stop a plausible-looking FIX rather than a
# regression, which is why they assert on absence:
#   - Redis must NOT get socket_timeout. It bounds every read, and the stream
#     reader blocks by design, so it would sever healthy streams on a 15s cycle.
#   - Mongo kwargs must NOT override options already in the URI. PyMongo's own
#     precedence is the reverse, and the URI is the only knob a deploy exposes.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core import redis_client
from pocketpaw_ee.cloud.shared.db import _MONGO_DEFAULTS, _client_options


class TestMongoClientOptions:
    def test_timeouts_are_set(self):
        opts = _client_options("mongodb://localhost:27017/paw")
        for key in (
            "serverSelectionTimeoutMS",
            "connectTimeoutMS",
            "socketTimeoutMS",
            "waitQueueTimeoutMS",
        ):
            assert key in opts, f"{key} is unset, so it falls back to the library default"
            assert opts[key] > 0

    def test_server_selection_fails_faster_than_the_30s_default(self):
        """The whole point. 30s of hanging is 30s of a held worker slot."""
        assert _client_options("mongodb://h/db")["serverSelectionTimeoutMS"] < 30_000

    def test_socket_timeout_is_generous_enough_for_a_real_query(self):
        """Set too tight this severs legitimate work. The app opens no change
        streams or tailable cursors, so nothing here is meant to be long-lived,
        but an aggregation can still take seconds."""
        assert _client_options("mongodb://h/db")["socketTimeoutMS"] >= 15_000

    def test_a_uri_option_wins_over_our_default(self):
        opts = _client_options("mongodb://h/db?serverSelectionTimeoutMS=1234")
        assert "serverSelectionTimeoutMS" not in opts, (
            "passing this as a kwarg silently overrules the operator's URI, "
            "and the URI is the only knob a deploy actually exposes"
        )
        assert "socketTimeoutMS" in opts, "unrelated defaults must still apply"

    @pytest.mark.parametrize("written_as", ["maxpoolsize", "MAXPOOLSIZE", "MaxPoolSize"])
    def test_uri_option_matching_is_case_insensitive(self, written_as):
        """MongoDB URI option names are case-insensitive, so however the
        operator spelled it, their value must still win.

        Both spellings matter for the mutation, not just the lowercase one: a
        lowercase URI matches even if the comparison folds only one side, so a
        test using `maxpoolsize=` alone passes with the folding half removed.
        `MAXPOOLSIZE` fails unless BOTH sides are folded.
        """
        opts = _client_options(f"mongodb://h/db?{written_as}=25")
        assert "maxPoolSize" not in opts

    def test_multiple_uri_options_are_all_honoured(self):
        opts = _client_options("mongodb://h/db?maxPoolSize=25&socketTimeoutMS=1000&tls=true")
        assert "maxPoolSize" not in opts
        assert "socketTimeoutMS" not in opts
        assert "serverSelectionTimeoutMS" in opts

    def test_a_uri_with_no_query_string_gets_every_default(self):
        assert _client_options("mongodb://h/db") == _MONGO_DEFAULTS

    def test_max_pool_size_matches_pymongos_own_default(self):
        """Set explicitly for visibility, not to change behaviour. If this
        stops matching PyMongo's default it is a real deploy change and should
        be a deliberate one."""
        assert _MONGO_DEFAULTS["maxPoolSize"] == 100


class TestRedisClientOptions:
    @pytest.fixture(autouse=True)
    def _reset(self, monkeypatch):
        monkeypatch.setenv("POCKETPAW_REDIS_URL", "redis://localhost:6379/0")
        redis_client._reset_for_tests()
        yield
        redis_client._reset_for_tests()

    def _pool_kwargs(self):
        return redis_client.get_redis().connection_pool.connection_kwargs

    def test_the_pool_is_bounded(self):
        pool = redis_client.get_redis().connection_pool
        assert pool.max_connections is not None
        assert pool.max_connections <= 512, (
            f"max_connections={pool.max_connections} is not a ceiling in any "
            "useful sense; every live SSE stream parks one connection"
        )

    def test_connect_timeout_is_set(self):
        assert self._pool_kwargs().get("socket_connect_timeout") is not None

    def test_no_read_timeout_is_set(self):
        """socket_timeout would look like the obvious companion to
        socket_connect_timeout and would be wrong. The run-stream reader parks
        in XREAD BLOCK 15000 by design; a read timeout severs healthy streams
        on that cycle, and the reconnect looks like a backend fault."""
        assert self._pool_kwargs().get("socket_timeout") is None, (
            "socket_timeout bounds every read, including the deliberate block "
            "in the SSE stream reader"
        )

    def test_idle_connections_are_health_checked(self):
        """A NAT or proxy that drops an idle flow otherwise hands back a dead
        connection on the next checkout, failing the caller instead."""
        assert self._pool_kwargs().get("health_check_interval", 0) > 0

    def test_decode_responses_is_preserved(self):
        """Pre-existing behaviour every caller depends on — the stream reader
        indexes str keys, not bytes."""
        assert self._pool_kwargs().get("decode_responses") is True

    def test_max_connections_is_env_tunable(self, monkeypatch):
        monkeypatch.setenv("POCKETPAW_REDIS_MAX_CONNECTIONS", "7")
        redis_client._reset_for_tests()
        assert redis_client.get_redis().connection_pool.max_connections == 7

    @pytest.mark.parametrize("bad", ["nope", "0", "-3"])
    def test_a_bad_env_value_falls_back_rather_than_unbounding_the_pool(self, monkeypatch, bad):
        monkeypatch.setenv("POCKETPAW_REDIS_MAX_CONNECTIONS", bad)
        redis_client._reset_for_tests()
        pool = redis_client.get_redis().connection_pool
        assert pool.max_connections == redis_client._DEFAULT_MAX_CONNECTIONS
