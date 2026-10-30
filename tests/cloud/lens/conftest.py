# tests/cloud/lens/conftest.py — shared paw-lens upstream fixtures.
#
# ``upstream`` swaps the lens service's LensClient for one on an
# httpx.MockTransport (returns a setter for the response handler); ``seen``
# records every upstream request, so tests run router -> service -> client with
# no network.

from __future__ import annotations

import httpx
import pytest
from pocketpaw_ee.cloud.lens import service as lens_service
from pocketpaw_ee.cloud.lens.client import LensClient


@pytest.fixture
def seen() -> list[httpx.Request]:
    return []


@pytest.fixture
def upstream(monkeypatch, seen):
    """Install a MockTransport-backed client; returns a setter for the handler."""
    state = {"handler": lambda req: httpx.Response(200, json={"ok": True})}

    def _transport_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return state["handler"](request)

    monkeypatch.setattr(
        lens_service, "_client", LensClient(_transport=httpx.MockTransport(_transport_handler))
    )

    def _set(handler) -> None:
        state["handler"] = handler

    return _set
