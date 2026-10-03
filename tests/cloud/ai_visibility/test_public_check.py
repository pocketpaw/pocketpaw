# tests/cloud/ai_visibility/test_public_check.py — POST /api/v1/tools/ai-check.
#
# The public free check end to end over HTTP with a fake OpenAI engine (no
# network) and an httpx MockTransport standing in for Cloudflare Turnstile:
# the happy path and the exact public allow-list, "mentioned" decided only by
# the questions that don't name the business, Turnstile failure, the per-IP
# limit, the daily spend cap, engine failure / no engine, and the request DTO's
# forbid-extra and website handling.
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core import rate_limit
from pocketpaw_ee.cloud.ai_visibility import public_check
from pocketpaw_ee.cloud.ai_visibility.domain import EngineAnswer, Location
from pocketpaw_ee.cloud.ai_visibility.dto import AiCheckRequest
from pocketpaw_ee.cloud.ai_visibility.engines import EngineError
from pocketpaw_ee.cloud.models.ai_visibility_check import AiVisibilityCheck

pytestmark = [pytest.mark.usefixtures("mongo_db"), pytest.mark.asyncio]

URL = "/api/v1/tools/ai-check"
BODY = {"name": "Joe's Pizza", "city": "Austin", "turnstile_token": "tok"}
PUBLIC_KEYS = {"mentioned", "engine", "answers_checked", "competitors", "sources", "fix"}


class FakeOpenAI:
    name = "openai"

    def __init__(self, unprompted: str, named: str | None = None, fail: bool = False):
        self.unprompted, self.named, self.fail = unprompted, named, fail
        self.questions: list[str] = []

    async def ask(self, question: str, location: Location) -> EngineAnswer:
        self.questions.append(question)
        if self.fail:
            raise EngineError("openai 500")
        text = self.named if (self.named and "good choice" in question) else self.unprompted
        return EngineAnswer(
            engine="openai",
            model="gpt-6-luna",
            text=text,
            consulted_urls=(
                "https://www.yelp.com/biz/joes-pizza-austin",
                "http://insecure.example.com/list",
                "https://joespizzaatx.com/menu",
            ),
            cost_usd=0.03,
        )


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    rate_limit._ai_check_public_limiter._buckets.clear()
    settings = SimpleNamespace(turnstile_secret="", ai_check_daily_usd=5.0)
    monkeypatch.setattr("pocketpaw.config.get_settings", lambda: settings)
    monkeypatch.setattr(public_check, "default_decision_models", lambda: (None, None))
    yield settings
    rate_limit._ai_check_public_limiter._buckets.clear()


@pytest_asyncio.fixture
async def client(monkeypatch):
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.ai_visibility.router import router
    from pocketpaw_ee.cloud.license import require_license

    state = SimpleNamespace(engine=FakeOpenAI("Try Home Slice Pizza or Via 313."))
    monkeypatch.setattr(public_check, "default_engines", lambda: [state.engine])

    app = FastAPI()
    add_error_handler(app)
    app.dependency_overrides[require_license] = lambda: None
    app.include_router(router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        c.state = state
        yield c


async def test_happy_path_returns_only_the_public_allow_list(client) -> None:
    resp = await client.post(URL, json={**BODY, "website": "joespizzaatx.com"})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert set(data) == PUBLIC_KEYS
    assert data["engine"] == "ChatGPT"
    assert data["mentioned"] is False
    assert data["competitors"] == []
    # 3 questions x 2 runs; the named question is excluded from "answers_checked".
    assert len(client.state.engine.questions) == 6
    assert data["answers_checked"] == 4
    # https only, deduped, typed; own site recognised from ``website``.
    assert data["sources"] == [
        {"type": "yelp", "url": "https://www.yelp.com/biz/joes-pizza-austin"},
        {"type": "own_site", "url": "https://joespizzaatx.com/menu"},
    ]
    assert set(data["fix"]) == {"id", "text"}
    # Stored as an anonymous check; the questions name the business once.
    doc = await AiVisibilityCheck.find_one({})
    assert doc.workspace is None and doc.site_id is None
    assert doc.questions[0] == "Is Joe's Pizza in Austin a good choice?"
    assert doc.questions[1] == "best pizza restaurant in Austin"


async def test_named_question_echo_does_not_count_as_mentioned(client) -> None:
    client.state.engine = FakeOpenAI(
        "Home Slice Pizza is the local favourite.", named="Joe's Pizza in Austin is fine."
    )
    data = (await client.post(URL, json=BODY)).json()
    assert data["mentioned"] is False


async def test_mentioned_when_an_unprompted_answer_names_it(client) -> None:
    client.state.engine = FakeOpenAI("Locals love Joe's Pizza on 6th street.")
    data = (await client.post(URL, json=BODY)).json()
    assert data["mentioned"] is True


async def test_turnstile_failure_is_400_and_costs_nothing(client, _env) -> None:
    _env.turnstile_secret = "s3cret"
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content.decode()
        return httpx.Response(200, json={"success": False})

    real = public_check.verify_turnstile

    async def verify(token, ip, *, transport=None):
        await real(token, ip, transport=httpx.MockTransport(handler))

    public_check.verify_turnstile = verify
    try:
        resp = await client.post(URL, json=BODY)
    finally:
        public_check.verify_turnstile = real
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "tools.ai_check.turnstile_failed"
    assert "secret=s3cret" in seen["body"] and "response=tok" in seen["body"]
    assert client.state.engine.questions == []


async def test_turnstile_success_and_network_error(_env) -> None:
    _env.turnstile_secret = "s3cret"
    ok = httpx.MockTransport(lambda r: httpx.Response(200, json={"success": True}))
    await public_check.verify_turnstile("tok", "1.2.3.4", transport=ok)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    with pytest.raises(Exception) as err:
        await public_check.verify_turnstile("tok", None, transport=httpx.MockTransport(boom))
    assert err.value.code == "tools.ai_check.turnstile_failed"


async def test_per_ip_rate_limit(client) -> None:
    for _ in range(rate_limit._ai_check_public_limiter.capacity):
        assert (await client.post(URL, json=BODY)).status_code == 200
    blocked = await client.post(URL, json=BODY)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "tools.ai_check.rate_limited"


async def test_daily_spend_cap(client, _env) -> None:
    _env.ai_check_daily_usd = 0.2
    assert (await client.post(URL, json=BODY)).status_code == 200  # spends 6 x 0.03
    assert (await client.post(URL, json=BODY)).status_code == 200  # 0.18 < 0.2
    resp = await client.post(URL, json=BODY)
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "tools.ai_check.daily_limit"


async def test_spend_cap_ignores_workspace_checks() -> None:
    from pocketpaw_ee.cloud.ai_visibility.service import anonymous_spend_today

    base = {"business": {}, "location": {}}
    await AiVisibilityCheck(**base, total_cost_usd=1.0).insert()
    await AiVisibilityCheck(**base, workspace="w1", site_id="s1", total_cost_usd=9.0).insert()
    assert await anonymous_spend_today() == 1.0


async def test_engine_failure_is_502(client) -> None:
    client.state.engine = FakeOpenAI("", fail=True)
    resp = await client.post(URL, json=BODY)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "tools.ai_check.engine_failed"


async def test_no_engine_configured_is_502(client, monkeypatch) -> None:
    monkeypatch.setattr(public_check, "default_engines", lambda: [])
    resp = await client.post(URL, json=BODY)
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "tools.ai_check.engine_failed"


async def test_request_dto_forbids_extra_fields_and_normalises_website(client) -> None:
    resp = await client.post(URL, json={**BODY, "workspace_id": "w1"})
    assert resp.status_code == 422
    assert (await client.post(URL, json={**BODY, "name": "J"})).status_code == 422
    assert (await client.post(URL, json={**BODY, "website": "ftp://x.com"})).status_code == 422
    req = AiCheckRequest(**BODY, website="https://WWW.JoesPizzaATX.com/menu?x=1")
    assert req.website == "www.joespizzaatx.com"
    assert AiCheckRequest(**BODY, website="").website is None


async def test_business_type_guess() -> None:
    assert public_check.guess_business_type("Smile Dental Care") == "dentist"
    assert public_check.guess_business_type("Acme Holdings") == "business"
