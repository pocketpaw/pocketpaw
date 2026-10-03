# tests/cloud/ai_visibility/test_site_card.py — the Staff AI visibility card.
#
# /sites/{id}/ai-visibility over HTTP (GET, PUT questions, POST check, POST
# apply-fix) against a mongomock Beanie DB, with fake engines (no network), a
# recorded enqueue and a recorded republish: the empty card, question
# validation, the Staff plan gate, the 24-hour limit, the job writing a check
# the card maps per contract (engine labels, named/of/failed, competitor and
# source counts, fix; next_run_at only with the scheduler on), a timed-out job
# marked failed, apply-fix allowed only on Staff for the latest check's own fix,
# tenant scoping, and the monthly sweep picking only Staff sites with questions
# whose last check is 30+ days old.
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud.ai_visibility import service, service_admin, worker
from pocketpaw_ee.cloud.ai_visibility.domain import EngineAnswer, Location
from pocketpaw_ee.cloud.ai_visibility.engines import EngineError
from pocketpaw_ee.cloud.models.ai_visibility_check import AiVisibilityCheck
from pocketpaw_ee.cloud.models.ai_visibility_site import AiVisibilitySite
from pocketpaw_ee.cloud.models.site import Site

pytestmark = [pytest.mark.usefixtures("mongo_db"), pytest.mark.asyncio]

WS = "w1"


class FakeEngine:
    def __init__(self, name: str, text: str, fail_every: int = 0):
        self.name, self.text, self.fail_every, self.calls = name, text, fail_every, 0

    async def ask(self, question: str, location: Location) -> EngineAnswer:
        self.calls += 1
        if self.fail_every and self.calls % self.fail_every == 0:
            raise EngineError(f"{self.name} 500")
        return EngineAnswer(
            engine=self.name,
            model="m",
            text=self.text,
            consulted_urls=(
                "https://www.yelp.com/biz/joes-pizza-austin",
                "https://www.reddit.com/r/austin/pizza",
            ),
            cost_usd=0.01,
        )


async def _site(plan_tier: str | None = "staff", status: str = "active", ws: str = WS) -> str:
    doc = Site(
        workspace=ws,
        pocket_id="p1",
        owner="u1",
        name="Joe's Pizza",
        url="https://joes-pizza.pawsites.workers.dev",
        plan_tier=plan_tier,
        subscription_status=status,
    )
    await doc.insert()
    return str(doc.id)


@pytest.fixture
def enqueued(monkeypatch) -> list[str]:
    calls: list[str] = []

    async def _enqueue(site_id: str) -> None:
        calls.append(site_id)

    monkeypatch.setattr(service, "_enqueue", _enqueue)
    return calls


@pytest.fixture
def engines(monkeypatch) -> list[FakeEngine]:
    fakes = [
        FakeEngine("openai", "Try Joe's Pizza downtown."),
        FakeEngine("perplexity", "Home Slice is the pick.", fail_every=3),
        FakeEngine("claude", "Via 313 or Home Slice."),
    ]
    monkeypatch.setattr(service, "default_engines", lambda: fakes)
    monkeypatch.setattr(service_admin, "default_engines", lambda: fakes)
    monkeypatch.setattr(service, "default_decision_models", lambda: (None, None))
    return fakes


@pytest_asyncio.fixture
async def client(monkeypatch):
    from fastapi import FastAPI
    from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind, request_context
    from pocketpaw_ee.cloud._core.deps import current_workspace_id
    from pocketpaw_ee.cloud._core.http import add_error_handler
    from pocketpaw_ee.cloud.ai_visibility.router import site_router
    from pocketpaw_ee.cloud.auth import current_active_user
    from pocketpaw_ee.cloud.license import require_license
    from pocketpaw_ee.cloud.workspace import service as workspace_service

    from tests.ee.sites.test_delete_endpoint import _FakeUser

    async def _plan(workspace_id: str) -> str:
        return "enterprise"

    monkeypatch.setattr(workspace_service, "get_workspace_plan", _plan)
    user = _FakeUser(WS, "u1")

    async def _ctx() -> RequestContext:
        return RequestContext(
            user_id="u1",
            workspace_id=WS,
            request_id="test",
            scope=ScopeKind.WORKSPACE,
            started_at=datetime.now(UTC),
        )

    app = FastAPI()
    add_error_handler(app)
    app.include_router(site_router, prefix="/api/v1")
    app.dependency_overrides[request_context] = _ctx
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_workspace_id] = lambda: WS
    app.dependency_overrides[require_license] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _with_last_fix(site_id: str, fix_id: str, ws: str = WS) -> None:
    check = AiVisibilityCheck(
        workspace=ws, site_id=site_id, business={}, location={}, fix={"id": fix_id}
    )
    await check.insert()
    await AiVisibilitySite(
        workspace=ws, site_id=site_id, questions=["q"], status="done", last_check_id=str(check.id)
    ).insert()


def _url(site_id: str, tail: str = "") -> str:
    return f"/api/v1/sites/{site_id}/ai-visibility{tail}"


async def test_empty_card(client) -> None:
    staff, free = await _site(), await _site(plan_tier="free", status="none")
    data = (await client.get(_url(staff))).json()
    assert data == {
        "ai_training_allowed": False,
        "plan_allows_check": True,
        "questions": [],
        "check": None,
    }
    assert (await client.get(_url(free))).json()["plan_allows_check"] is False
    # A lapsed Staff subscription does not include checks.
    lapsed = await _site(status="cancelled")
    assert (await client.get(_url(lapsed))).json()["plan_allows_check"] is False


async def test_other_workspace_site_is_404(client) -> None:
    other = await _site(ws="w2")
    assert (await client.get(_url(other))).status_code == 404
    assert (await client.get(_url("not-an-id"))).status_code == 404


async def test_put_questions_validates_and_saves(client) -> None:
    site_id = await _site()
    url = _url(site_id, "/questions")
    assert (await client.put(url, json={"questions": []})).status_code == 422
    assert (await client.put(url, json={"questions": ["q" * 5] * 11})).status_code == 422
    assert (await client.put(url, json={"questions": ["hi"]})).status_code == 422
    bad = await client.put(url, json={"questions": ["best pizza in Austin"], "x": 1})
    assert bad.status_code == 422
    resp = await client.put(url, json={"questions": ["  best pizza in Austin  "]})
    assert resp.status_code == 200
    assert resp.json()["questions"] == ["best pizza in Austin"]
    assert resp.json()["check"] is None


async def test_check_needs_staff_questions_and_a_day_between(client, enqueued) -> None:
    free = await _site(plan_tier="free", status="none")
    await client.put(_url(free, "/questions"), json={"questions": ["best pizza in Austin"]})
    denied = await client.post(_url(free, "/check"))
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "ai_visibility.plan_required"

    staff = await _site()
    empty = await client.post(_url(staff, "/check"))
    assert empty.status_code == 422
    assert empty.json()["error"]["code"] == "ai_visibility.questions"

    await client.put(_url(staff, "/questions"), json={"questions": ["best pizza in Austin"]})
    ok = await client.post(_url(staff, "/check"))
    assert ok.status_code == 202 and ok.json() == {"status": "pending"}
    assert enqueued == [staff]
    assert (await client.get(_url(staff))).json()["check"]["status"] == "pending"

    again = await client.post(_url(staff, "/check"))
    assert again.status_code == 429
    assert again.json()["error"]["code"] == "ai_visibility.too_soon"
    assert enqueued == [staff]

    later = datetime.now(UTC) + timedelta(hours=25)
    assert await service.request_check(WS, staff, now=later) == {"status": "pending"}
    assert enqueued == [staff, staff]


async def test_enqueue_failure_marks_the_check_failed(client, monkeypatch) -> None:
    staff = await _site()
    await client.put(_url(staff, "/questions"), json={"questions": ["best pizza in Austin"]})

    async def _down(site_id: str) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(service, "_enqueue", _down)
    with pytest.raises(ConnectionError):
        await service.request_check(WS, staff)
    state = await AiVisibilitySite.find_one({"site_id": staff})
    assert state.status == "failed"


async def test_job_writes_a_check_the_card_maps(client, enqueued, engines, monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_CLOUD_SCHEDULER_ENABLED", "true")
    staff = await _site()
    questions = ["best pizza in Austin", "pizza near Zilker"]
    await client.put(_url(staff, "/questions"), json={"questions": questions})
    await client.post(_url(staff, "/check"))
    await service.run_site_check(staff)

    check = (await client.get(_url(staff))).json()["check"]
    assert check["status"] == "done"
    assert check["questions"] == questions
    by_label = {e["label"]: e for e in check["engines"]}
    assert set(by_label) == {"ChatGPT", "Perplexity (search)", "Claude"}
    # 2 questions x 3 runs = 6 calls each; perplexity fails every third call.
    assert by_label["ChatGPT"] == {"label": "ChatGPT", "named": 6, "of": 6, "failed": 0}
    assert by_label["Perplexity (search)"]["failed"] == 2
    assert by_label["Perplexity (search)"]["of"] == 4
    assert by_label["Claude"]["named"] == 0
    assert {s["type"]: s["count"] for s in check["sources"]} == {"yelp": 1, "reddit": 1}
    # The site has no competitor list yet, so nothing is counted.
    assert check["competitors"] == []
    assert set(check["fix"]) == {"id", "text", "we_can_apply"}
    ran_at = datetime.fromisoformat(check["ran_at"])
    assert datetime.fromisoformat(check["next_run_at"]) - ran_at == timedelta(days=30)

    # No scheduler, no monthly check: the card must not promise one.
    monkeypatch.delenv("POCKETPAW_CLOUD_SCHEDULER_ENABLED", raising=False)
    assert (await client.get(_url(staff))).json()["check"]["next_run_at"] is None


async def test_timed_out_job_is_marked_failed(client, enqueued, engines, monkeypatch) -> None:
    staff = await _site()
    await client.put(_url(staff, "/questions"), json={"questions": ["best pizza in Austin"]})
    await client.post(_url(staff, "/check"))

    async def _cancelled(*a, **kw):
        raise asyncio.CancelledError

    monkeypatch.setattr(service, "run_check", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        await service.run_site_check(staff)
    check = (await client.get(_url(staff))).json()["check"]
    assert check["status"] == "failed"
    state = await AiVisibilitySite.find_one({"site_id": staff})
    assert state.error == "the check timed out"


async def test_first_check_in_flight_shows_empty_numbers(client, enqueued) -> None:
    staff = await _site()
    await client.put(_url(staff, "/questions"), json={"questions": ["best pizza in Austin"]})
    await client.post(_url(staff, "/check"))
    check = (await client.get(_url(staff))).json()["check"]
    assert check["status"] == "pending"
    assert check["engines"] == [] and check["sources"] == [] and check["fix"] is None
    assert check["ran_at"] is None and check["next_run_at"] is None


async def test_job_without_engines_fails(client, enqueued, monkeypatch) -> None:
    monkeypatch.setattr(service, "default_engines", lambda: [])
    staff = await _site()
    await client.put(_url(staff, "/questions"), json={"questions": ["best pizza in Austin"]})
    await client.post(_url(staff, "/check"))
    await service.run_site_check(staff)
    assert (await client.get(_url(staff))).json()["check"]["status"] == "failed"


async def test_apply_fix(client, monkeypatch) -> None:
    from pocketpaw_ee.sites import service as sites_service

    calls: list[dict] = []

    async def _publish(**kw):
        calls.append(kw)

    monkeypatch.setattr(sites_service, "publish_pocket", _publish)
    staff = await _site()
    # No check yet: nothing to apply.
    none_yet = await client.post(_url(staff, "/apply-fix"), json={"fix_id": "ai_access"})
    assert none_yet.status_code == 400
    assert none_yet.json()["error"]["code"] == "ai_visibility.fix_not_applicable"
    await _with_last_fix(staff, "ai_access")
    resp = await client.post(_url(staff, "/apply-fix"), json={"fix_id": "ai_access"})
    assert resp.status_code == 202 and resp.json() == {"republish": "started"}
    await asyncio.sleep(0)
    # The normal republish, with no plan key so the plan never changes.
    assert calls == [{"workspace_id": WS, "user_id": "u1", "pocket_id": "p1"}]

    refused = await client.post(_url(staff, "/apply-fix"), json={"fix_id": "gbp"})
    assert refused.status_code == 400
    assert refused.json()["error"]["code"] == "ai_visibility.fix_not_applicable"
    # site_content is owner guidance; a republish changes no words.
    content = await client.post(_url(staff, "/apply-fix"), json={"fix_id": "site_content"})
    assert content.status_code == 400
    assert len(calls) == 1


async def test_apply_fix_needs_the_latest_checks_fix(client, monkeypatch) -> None:
    from pocketpaw_ee.sites import service as sites_service

    calls: list[dict] = []

    async def _publish(**kw):
        calls.append(kw)

    monkeypatch.setattr(sites_service, "publish_pocket", _publish)
    staff = await _site()
    await _with_last_fix(staff, "gbp")
    stale = await client.post(_url(staff, "/apply-fix"), json={"fix_id": "ai_access"})
    assert stale.status_code == 400
    # The latest fix, but one the owner takes on another site.
    theirs = await client.post(_url(staff, "/apply-fix"), json={"fix_id": "gbp"})
    assert theirs.status_code == 400
    assert stale.json()["error"]["code"] == "ai_visibility.fix_not_applicable"
    # A check id from another workspace does not count.
    foreign = AiVisibilityCheck(
        workspace="w2", site_id=staff, business={}, location={}, fix={"id": "ai_access"}
    )
    await foreign.insert()
    state = await AiVisibilitySite.find_one({"site_id": staff})
    await state.set({"last_check_id": str(foreign.id)})
    crossed = await client.post(_url(staff, "/apply-fix"), json={"fix_id": "ai_access"})
    assert crossed.status_code == 400
    assert calls == []


async def test_apply_fix_needs_staff(client, monkeypatch) -> None:
    from pocketpaw_ee.sites import service as sites_service

    calls: list[dict] = []

    async def _publish(**kw):
        calls.append(kw)

    monkeypatch.setattr(sites_service, "publish_pocket", _publish)
    lapsed = await _site(status="cancelled")
    await _with_last_fix(lapsed, "ai_access")
    denied = await client.post(_url(lapsed, "/apply-fix"), json={"fix_id": "ai_access"})
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "ai_visibility.plan_required"
    assert calls == []


async def test_sweep_queues_only_due_staff_sites(enqueued, engines, monkeypatch) -> None:
    now = datetime.now(UTC)

    async def with_state(site_id: str, questions: list[str], requested_days_ago: int | None):
        requested = now - timedelta(days=requested_days_ago) if requested_days_ago else None
        await AiVisibilitySite(
            workspace=WS, site_id=site_id, questions=questions, requested_at=requested
        ).insert()

    never = await _site()
    await with_state(never, ["best pizza in Austin"], None)
    old = await _site()
    await with_state(old, ["best pizza in Austin"], 31)
    recent = await _site()
    await with_state(recent, ["best pizza in Austin"], 10)
    no_questions = await _site()
    await with_state(no_questions, [], None)
    free = await _site(plan_tier="free", status="none")
    await with_state(free, ["best pizza in Austin"], None)
    # A row naming a site that lives in another workspace is never queued.
    foreign = await _site(ws="w2")
    await with_state(foreign, ["best pizza in Austin"], None)

    assert await service_admin.sweep_due_checks(now) == 2
    assert sorted(enqueued) == sorted([never, old])
    # Stamped on queue, so the next tick queues nothing.
    assert await service_admin.sweep_due_checks(now + timedelta(days=1)) == 0

    monkeypatch.setattr(service_admin, "default_engines", lambda: [])
    assert await service_admin.sweep_due_checks(now + timedelta(days=40)) == 0


async def test_cron_tick_is_opt_in(monkeypatch, enqueued, engines) -> None:
    site_id = await _site()
    await AiVisibilitySite(workspace=WS, site_id=site_id, questions=["best pizza"]).insert()
    monkeypatch.delenv("POCKETPAW_CLOUD_SCHEDULER_ENABLED", raising=False)
    assert await worker.monthly_sweep({}) == 0
    monkeypatch.setenv("POCKETPAW_CLOUD_SCHEDULER_ENABLED", "true")
    assert await worker.monthly_sweep({}) == 1
