# Regression: the OpenCode client bounds the phases that cannot legitimately
# take minutes, and leaves the one that can.
#
# Created 2026-09-04 (backend-perf L1). The client was built with
# `timeout=None`, which applies to every phase including CONNECT — so an
# OpenCode server that was down or unreachable hung the caller forever. That
# included `_check_health`, whose entire job is to answer that question fast.
#
# The fix is not "add a timeout". A read deadline here truncates a long agent
# generation, which is a worse bug than the one being fixed and would look like
# the model stopping mid-sentence. Both directions are asserted, because either
# one alone permits the other failure.

from __future__ import annotations

import httpx

from pocketpaw.agents.opencode import OpenCodeBackend


def _timeout() -> httpx.Timeout:
    t = OpenCodeBackend._TIMEOUT
    assert isinstance(t, httpx.Timeout), f"expected an httpx.Timeout, got {t!r}"
    return t


def test_connect_is_bounded():
    """An unreachable server must fail, not hang."""
    assert _timeout().connect is not None
    assert _timeout().connect <= 30.0


def test_read_stays_unbounded():
    """An agent generation legitimately runs for minutes. A read deadline here
    truncates the answer, and the user sees the model stop mid-sentence."""
    assert _timeout().read is None, "a read timeout on this client cuts off long generations"


def test_write_and_pool_are_bounded():
    assert _timeout().write is not None
    assert _timeout().pool is not None


def test_the_client_is_actually_built_with_it():
    """The constant existing proves nothing on its own — it has to reach the
    client. No request is made; only the configured timeout is read."""
    backend = OpenCodeBackend.__new__(OpenCodeBackend)
    backend._client = None
    backend._base_url = "http://localhost:9999"

    client = backend._get_client()

    assert client.timeout.connect == _timeout().connect
    assert client.timeout.read is None
