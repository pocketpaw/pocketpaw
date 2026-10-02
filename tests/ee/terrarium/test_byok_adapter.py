# tests/ee/terrarium/test_byok_adapter.py — a universe pays with its workspace's
# own key when one is stored, through the product's one decrypting reader.
#
# Terrarium keeps no second copy of anyone's key. It asks cloud.byok who pays
# for this tick and hands the answer to the transport. These pin that seam: the
# right transport is picked, the key rides as x-api-key, and it is in no body.

from __future__ import annotations

import json

import httpx
import pytest
from pocketpaw_ee.cloud.byok import service as byok
from pocketpaw_ee.terrarium import llm as citizen_llm
from pocketpaw_ee.terrarium import service as svc
from pocketpaw_ee.terrarium.llm import HttpLlm, MockLlm, model_for_tier, resolve_llm

from .conftest import WS, create_universe  # noqa: E402

_KEY = "sk-ant-" + "a" * 40


@pytest.fixture(autouse=True)
def _encryption_key(monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())


def test_resolve_llm_prefers_http_when_a_key_is_present(monkeypatch):
    monkeypatch.delenv("POCKETPAW_TERRARIUM_LLM", raising=False)
    assert isinstance(resolve_llm(api_key=None, tier="premium"), MockLlm)
    picked = resolve_llm(api_key=_KEY, tier="tail")
    assert isinstance(picked, HttpLlm)
    assert picked.model == model_for_tier("tail")


@pytest.mark.asyncio
async def test_http_llm_sends_the_key_as_x_api_key_and_never_logs_it(caplog):
    seen: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        text = '{"thought": "ok", "acts": []}'
        return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    llm = HttpLlm(_KEY, "claude-sonnet-4-6", client=client)
    out = await llm.decide(prompt="hello", physics=None, citizen=None, digest=None)  # type: ignore[arg-type]
    assert '"thought"' in out
    assert seen["headers"]["x-api-key"] == _KEY
    assert seen["body"]["model"] == "claude-sonnet-4-6"
    assert _KEY not in caplog.text


@pytest.mark.asyncio
async def test_tick_resolves_the_workspace_key_and_no_body_carries_it(client, monkeypatch):
    # Store a key for the test workspace without the network round-trip.
    async def _no_network(_key: str) -> None:
        return None

    monkeypatch.setattr(byok, "validate_key", _no_network)
    await byok.set_key(WS, _KEY)

    built: list[tuple[str | None, str | None]] = []

    def spy(*, api_key=None, tier=None):
        built.append((api_key, tier))
        return MockLlm()

    monkeypatch.setattr(citizen_llm, "resolve_llm", spy)
    monkeypatch.setattr(svc.citizen_llm, "resolve_llm", spy)

    uni = create_universe(client, founders=2)
    res = client.post(f"/terrarium/universes/{uni['id']}/tick?n=1")
    assert res.status_code == 200, res.text

    assert built, "tick must resolve a transport"
    api_key, tier = built[-1]
    assert api_key == _KEY, "the workspace key must reach the transport"
    assert tier == "premium"

    for path in (
        f"/terrarium/universes/{uni['id']}",
        f"/terrarium/universes/{uni['id']}/citizens",
        f"/terrarium/universes/{uni['id']}/events",
    ):
        body = client.get(path).text
        assert _KEY not in body
        assert "encrypted_key" not in body


@pytest.mark.asyncio
async def test_without_a_stored_key_the_tick_uses_platform_credentials(client, monkeypatch):
    built: list[tuple[str | None, str | None]] = []

    def spy(*, api_key=None, tier=None):
        built.append((api_key, tier))
        return MockLlm()

    monkeypatch.setattr(citizen_llm, "resolve_llm", spy)
    monkeypatch.setattr(svc.citizen_llm, "resolve_llm", spy)

    uni = create_universe(client, founders=1)
    assert client.post(f"/terrarium/universes/{uni['id']}/tick?n=1").status_code == 200
    assert built and built[-1][0] is None
