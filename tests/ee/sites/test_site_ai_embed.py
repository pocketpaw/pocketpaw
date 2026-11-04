# tests/ee/sites/test_site_ai_embed.py: /ai/embed, opt-in transcripts, and the site
# transfer / delete cascade for site AI rows.
#
# Embed: Worker and browser callers, ``model`` defaulting to ``embed``, input
# limits, the plan allowlist, budget refusal as a 402. Transcripts: off by default,
# capped and image-free when on, never the system prompt, deleted when turned off,
# readable with ``sites.ai_manage`` only. Lifecycle: a transfer re-keys settings,
# presets and budget, releases holds, drops transcripts and leaves usage rows; a
# delete removes every row. The LLM is faked.
from __future__ import annotations

import base64

import fakeredis.aioredis
import pytest
from cryptography.fernet import Fernet

from tests.ee.sites.site_ai_harness import (
    ORIGIN,
    OTHER_WS,
    SITE_KEY,
    WS,
    FakeLlm,
    client,
    gateway_token,
    make_pocket,
    make_site,
    parse_sse,
    text_events,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from pocketpaw_ee.cloud._core import redis_client
    from pocketpaw_ee.integrations import site_gate
    from pocketpaw_ee.site_ai import chat

    from pocketpaw.security.rate_limiter import RateLimiter

    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("POCKETPAW_SITE_AI_TICKET_SECRET", "t" * 40)
    monkeypatch.setenv("PAW_SITE_AI_ALLOW_NO_TURNSTILE", "1")
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
    monkeypatch.setattr(chat, "_ip_limiter", RateLimiter(rate=1000.0, capacity=1000))
    monkeypatch.setattr(chat, "_schedule_bill", lambda run_id: None)
    monkeypatch.setattr(site_gate, "turnstile_configured", lambda: False)

    async def _no_widget(site):
        return None

    monkeypatch.setattr(site_gate, "site_widget", _no_widget)


class _Embedder:
    def __init__(self, monkeypatch, tokens=7, fail=None):
        from pocketpaw_ee.site_ai import llm

        self.calls = []
        self.tokens, self.fail = tokens, fail
        monkeypatch.setattr(llm, "embed", self._embed)

    async def _embed(self, *, provider_model, inputs, workspace_id, tags):
        self.calls.append({"model": provider_model, "inputs": inputs, "user": workspace_id})
        if self.fail:
            raise self.fail
        return [[0.1, 0.2] for _ in inputs], self.tokens


async def _setup(plan="staff", **settings):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiSettingsDoc

    pid = await make_pocket()
    site = await make_site(pid, plan_tier=plan)
    if settings:
        await SiteAiSettingsDoc(workspace=WS, pocket_id=pid, **settings).insert()
    return site, await gateway_token(site)


async def _embed(c, site, token, body):
    return await c.post(
        f"/api/v1/sites/{site.id}/ai/embed",
        json=body,
        headers={"authorization": f"Bearer {token}"},
    )


@pytest.mark.asyncio
async def test_worker_embed(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCallDoc

    site, token = await _setup()
    fake = _Embedder(monkeypatch)
    async with client() as c:
        one = await _embed(c, site, token, {"input": "hello"})
        many = await _embed(c, site, token, {"input": ["a", "b"], "model": "embed"})
    assert one.status_code == 200, one.text
    assert one.json()["vectors"] == [[0.1, 0.2]]
    assert one.json()["usage"]["input_tokens"] == 7 and one.json()["usage"]["credits_micro"] > 0
    assert len(many.json()["vectors"]) == 2
    assert fake.calls[0]["model"] == "text-embedding-3-small"
    assert fake.calls[0]["user"] == WS
    assert "text-embedding" not in one.text
    rows = await SiteAiCallDoc.find(SiteAiCallDoc.state == "settled").to_list()
    assert {r.kind for r in rows} == {"embed"} and {r.model for r in rows} == {"embed"}


@pytest.mark.asyncio
async def test_embed_limits_and_plan(beanie_test_db, monkeypatch):
    _Embedder(monkeypatch)
    site, token = await _setup()
    free, free_token = await _setup(plan="free")
    async with client() as c:
        cases = {
            "fast": (await _embed(c, site, token, {"input": "x", "model": "fast"}), 400),
            "none": (await _embed(c, site, token, {}), 400),
            "empty": (await _embed(c, site, token, {"input": [""]}), 400),
            "many": (await _embed(c, site, token, {"input": ["x"] * 65}), 400),
            "long": (await _embed(c, site, token, {"input": "x" * 8001}), 400),
            "number": (await _embed(c, site, token, {"input": [1]}), 400),
            "free": (await _embed(c, free, free_token, {"input": "x"}), 403),
        }
    for name, (resp, status) in cases.items():
        assert resp.status_code == status, (name, resp.text)


@pytest.mark.asyncio
async def test_embed_budget_and_provider_failure(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiBudgetDoc, SiteAiCallDoc
    from pocketpaw_ee.site_ai.llm import LlmError

    site, token = await _setup()
    _Embedder(monkeypatch, fail=LlmError(True))
    async with client() as c:
        down = await _embed(c, site, token, {"input": "x"})
    assert (down.status_code, down.json()["detail"]) == (503, "provider_unavailable")
    # The request left, so its input is billed even though it failed.
    failed = await SiteAiCallDoc.find(SiteAiCallDoc.state == "settled").to_list()
    assert len(failed) == 1 and failed[0].input_tokens == 1

    await SiteAiBudgetDoc(workspace=WS, pocket_id=site.pocket_id, daily_cap_micro=0).insert()
    async with client() as c:
        capped = await _embed(c, site, token, {"input": "x"})
    assert (capped.status_code, capped.json()["detail"]) == (402, "budget_exhausted")


@pytest.mark.asyncio
async def test_browser_embed_defaults_the_model(beanie_test_db, monkeypatch):
    site, _token = await _setup(browser_access=True)
    _Embedder(monkeypatch)
    hdrs = {"x-paw-site-key": SITE_KEY, "origin": ORIGIN}
    async with client() as c:
        ticket = (await c.post(f"/api/v1/sites/{site.id}/ai/ticket", json={}, headers=hdrs)).json()[
            "ticket"
        ]
        no_ticket = await c.post(
            f"/api/v1/sites/{site.id}/ai/embed", json={"input": "x"}, headers=hdrs
        )
        ok = await c.post(
            f"/api/v1/sites/{site.id}/ai/embed",
            json={"input": "x"},
            headers={**hdrs, "x-paw-ai-ticket": ticket},
        )
    assert no_ticket.status_code == 401
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------- transcripts


@pytest.mark.asyncio
async def test_transcripts_are_off_by_default(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiTranscriptDoc

    site, token = await _setup()
    FakeLlm(monkeypatch, script=[text_events("secret answer")])
    async with client() as c:
        await c.post(
            f"/api/v1/sites/{site.id}/ai/chat",
            json={"model": "fast", "messages": [{"role": "user", "content": "my email is a@b"}]},
            headers={"authorization": f"Bearer {token}"},
        )
    assert await SiteAiTranscriptDoc.find_all().count() == 0


@pytest.mark.asyncio
async def test_opted_in_transcript_is_capped_image_free_and_admin_only(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiTranscriptDoc
    from pocketpaw_ee.site_ai.llm import LlmEvent

    site, token = await _setup(keep_transcripts=True, allowed_models=["fast", "vision"])
    FakeLlm(
        monkeypatch,
        script=[
            [
                LlmEvent("text", text="It is a cat."),
                LlmEvent("tool", index=0, id="c1", name="save", arguments='{"a":'),
                LlmEvent("tool", index=0, arguments="1}"),
                LlmEvent("usage", input_tokens=5, output_tokens=5),
            ]
        ],
    )
    img = "data:image/png;base64," + base64.b64encode(PNG).decode()
    body = {
        "model": "vision",
        "messages": [
            {"role": "system", "content": "SECRET SYSTEM PROMPT"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is " + "x" * 5000},
                    {"type": "image_url", "image_url": {"url": img}},
                ],
            },
        ],
    }
    async with client() as c:
        resp = await c.post(
            f"/api/v1/sites/{site.id}/ai/chat",
            json=body,
            headers={"authorization": f"Bearer {token}"},
        )
        assert parse_sse(resp.text)[-1][0] == "done"
        listed = await c.get(f"/api/v1/sites/by-pocket/{site.pocket_id}/ai/transcripts")
    async with client(role="member") as c:
        member = await c.get(f"/api/v1/sites/by-pocket/{site.pocket_id}/ai/transcripts")
    async with client(workspace_id=OTHER_WS) as c:
        other = await c.get(f"/api/v1/sites/by-pocket/{site.pocket_id}/ai/transcripts")
    doc = await SiteAiTranscriptDoc.find_one()
    assert doc is not None and doc.output == "It is a cat."
    assert doc.tool_calls == [{"name": "save", "arguments": '{"a":1}'}]
    assert [m["role"] for m in doc.messages] == ["user"]
    assert "[image]" in doc.messages[0]["content"] and "base64" not in str(doc.messages)
    assert len(doc.messages[0]["content"]) < 4_100
    assert "SECRET SYSTEM PROMPT" not in str(doc.model_dump())
    assert listed.status_code == 200 and listed.json()[0]["run_id"] == doc.run_id
    assert member.status_code == 403
    assert other.status_code == 404

    async with client() as c:
        off = await c.put(
            f"/api/v1/sites/by-pocket/{site.pocket_id}/ai/settings",
            json={"keep_transcripts": False},
        )
    assert off.status_code == 200
    assert await SiteAiTranscriptDoc.find_all().count() == 0


# ---------------------------------------------------------------- lifecycle


async def _seed(pid: str, workspace: str = WS):
    from pocketpaw_ee.cloud.models.site_ai import (
        SiteAiBudgetDoc,
        SiteAiPresetDoc,
        SiteAiSettingsDoc,
        SiteAiTranscriptDoc,
    )
    from pocketpaw_ee.site_ai import budget
    from pocketpaw_ee.site_ai.service import day_period

    await SiteAiSettingsDoc(workspace=workspace, pocket_id=pid, browser_access=True).insert()
    await SiteAiPresetDoc(
        workspace=workspace, pocket_id=pid, preset_id="pre_1", name="p", system_prompt="s"
    ).insert()
    await SiteAiBudgetDoc(workspace=workspace, pocket_id=pid, daily_cap_micro=123).insert()
    await SiteAiTranscriptDoc(
        workspace=workspace, pocket_id=pid, run_id="t1", caller="browser", model="fast"
    ).insert()
    settled = await budget.reserve(
        run_id=f"done-{pid}",
        workspace_id=workspace,
        pocket_id=pid,
        site_id="s",
        caller="worker",
        model="fast",
        amount=50,
        caps=[budget.Cap("day", day_period(), 10_000)],
    )
    await budget.settle(settled, 20, 1, 1)
    await budget.reserve(
        run_id=f"held-{pid}",
        workspace_id=workspace,
        pocket_id=pid,
        site_id="s",
        caller="worker",
        model="fast",
        amount=70,
        caps=[budget.Cap("day", day_period(), 10_000)],
    )


@pytest.mark.asyncio
async def test_transfer_moves_config_releases_holds_and_drops_transcripts(beanie_test_db):
    from pocketpaw_ee.cloud.models.site_ai import (
        SiteAiBudgetDoc,
        SiteAiCallDoc,
        SiteAiCounterDoc,
        SiteAiPresetDoc,
        SiteAiSettingsDoc,
        SiteAiTranscriptDoc,
    )
    from pocketpaw_ee.sites.service import _TransferDeps

    pid = await make_pocket()
    await _seed(pid)
    moved = await _TransferDeps.move_secrets(pocket_id=pid, destination_workspace_id=OTHER_WS)
    assert moved == 3
    for doc in (SiteAiSettingsDoc, SiteAiPresetDoc, SiteAiBudgetDoc):
        rows = await doc.find(doc.pocket_id == pid).to_list()
        assert [r.workspace for r in rows] == [OTHER_WS]
    assert (await SiteAiBudgetDoc.find_one(SiteAiBudgetDoc.pocket_id == pid)).daily_cap_micro == 123
    assert await SiteAiTranscriptDoc.find_all().count() == 0
    calls = {c.run_id: c for c in await SiteAiCallDoc.find_all().to_list()}
    assert calls[f"done-{pid}"].workspace == WS
    assert calls[f"done-{pid}"].state == "settled"
    assert calls[f"held-{pid}"].state == "released"
    counter = await SiteAiCounterDoc.find_one()
    # Keyed by the pocket: the spend (and so the caps) carries over to the new owner.
    assert counter.workspace == OTHER_WS and counter.held_micro == 0
    assert counter.spent_micro == 20
    # A resume is a no-op.
    assert await _TransferDeps.move_secrets(pocket_id=pid, destination_workspace_id=OTHER_WS) == 0


@pytest.mark.asyncio
async def test_delete_cascade_removes_every_site_ai_row(beanie_test_db):
    from pocketpaw_ee.cloud.models.site_ai import (
        SiteAiBudgetDoc,
        SiteAiCallDoc,
        SiteAiCounterDoc,
        SiteAiPresetDoc,
        SiteAiSettingsDoc,
        SiteAiTranscriptDoc,
    )
    from pocketpaw_ee.sites import delete_cascade

    pid = await make_pocket()
    keep = await make_pocket()
    await _seed(pid)
    await _seed(keep)
    site = await make_site(pid)
    await delete_cascade._purge_site_ai(site)
    for doc in (
        SiteAiSettingsDoc,
        SiteAiPresetDoc,
        SiteAiBudgetDoc,
        SiteAiCallDoc,
        SiteAiCounterDoc,
        SiteAiTranscriptDoc,
    ):
        assert await doc.find(doc.pocket_id == pid).count() == 0, doc.__name__
        assert await doc.find(doc.pocket_id == keep).count() > 0, doc.__name__


# ---------------------------------------------------------------- hardening review


@pytest.mark.asyncio
async def test_a_transfer_keeps_the_day_cap(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.cloud.models.site_ai import SiteAiBudgetDoc
    from pocketpaw_ee.site_ai import budget
    from pocketpaw_ee.site_ai.service import load_budget
    from pocketpaw_ee.sites.service import _TransferDeps

    site, token = await _setup()
    await SiteAiBudgetDoc(workspace=WS, pocket_id=site.pocket_id, daily_cap_micro=500).insert()
    hold = await budget.reserve(
        run_id="pre",
        workspace_id=WS,
        pocket_id=site.pocket_id,
        site_id=str(site.id),
        caller="worker",
        model="fast",
        amount=450,
        caps=budget.caps_for(
            caller="worker", budget=await load_budget(WS, site.pocket_id), allowance=10**12
        ),
    )
    await budget.settle(hold, 450, 1, 1)
    await _TransferDeps.move_secrets(pocket_id=site.pocket_id, destination_workspace_id=OTHER_WS)
    await Site.get_pymongo_collection().update_one(
        {"_id": site.id}, {"$set": {"workspace": OTHER_WS}}
    )
    moved = await load_budget(OTHER_WS, site.pocket_id)
    with pytest.raises(budget.BudgetRefused):
        await budget.reserve(
            run_id="post",
            workspace_id=OTHER_WS,
            pocket_id=site.pocket_id,
            site_id=str(site.id),
            caller="worker",
            model="fast",
            amount=100,
            caps=budget.caps_for(caller="worker", budget=moved, allowance=10**12),
        )


@pytest.mark.asyncio
async def test_transcripts_rechecked_at_write_time(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site import Site
    from pocketpaw_ee.cloud.models.site_ai import SiteAiSettingsDoc, SiteAiTranscriptDoc
    from pocketpaw_ee.site_ai.llm import LlmEvent

    site, token = await _setup(keep_transcripts=True)
    flipped = {"done": False}

    async def _flip_then_answer(payload):
        # The owner turns transcripts off while the call is in flight.
        if not flipped["done"]:
            await SiteAiSettingsDoc.get_pymongo_collection().update_one(
                {"pocket_id": site.pocket_id}, {"$set": {"keep_transcripts": False}}
            )
            flipped["done"] = True
        yield LlmEvent("text", text="hi")
        yield LlmEvent("usage", input_tokens=3, output_tokens=1)

    from pocketpaw_ee.site_ai import llm

    monkeypatch.setattr(llm, "stream", _flip_then_answer)
    body = {"model": "fast", "messages": [{"role": "user", "content": "x"}]}
    hdr = {"authorization": f"Bearer {token}"}
    async with client() as c:
        await c.post(f"/api/v1/sites/{site.id}/ai/chat", json=body, headers=hdr)
    assert await SiteAiTranscriptDoc.find_all().count() == 0

    # Back on, but the site moves to another workspace mid-call: not written either.
    await SiteAiSettingsDoc.get_pymongo_collection().update_one(
        {"pocket_id": site.pocket_id}, {"$set": {"keep_transcripts": True}}
    )

    async def _move_then_answer(payload):
        await Site.get_pymongo_collection().update_one(
            {"_id": site.id}, {"$set": {"workspace": OTHER_WS}}
        )
        yield LlmEvent("text", text="hi")
        yield LlmEvent("usage", input_tokens=3, output_tokens=1)

    monkeypatch.setattr(llm, "stream", _move_then_answer)
    async with client() as c:
        await c.post(f"/api/v1/sites/{site.id}/ai/chat", json=body, headers=hdr)
    assert await SiteAiTranscriptDoc.find_all().count() == 0


@pytest.mark.asyncio
async def test_a_site_delete_leaves_the_workspace_wallet_counter(beanie_test_db, monkeypatch):
    from pocketpaw_ee.cloud.models.site_ai import SiteAiCounterDoc
    from pocketpaw_ee.site_ai import budget
    from pocketpaw_ee.site_ai.service import purge_for_pocket

    pid = await make_pocket()
    await budget.reserve(
        run_id="w",
        workspace_id=WS,
        pocket_id=pid,
        site_id="s",
        caller="worker",
        model="fast",
        amount=10,
        caps=[budget.Cap("wallet", "inflight", 10**9, budget.wallet_key(WS))],
    )
    await purge_for_pocket(workspace_id=WS, pocket_id=pid)
    assert await SiteAiCounterDoc.find_one(SiteAiCounterDoc.key == budget.wallet_key(WS))
