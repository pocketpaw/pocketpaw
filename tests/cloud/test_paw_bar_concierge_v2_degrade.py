# tests/cloud/test_paw_bar_concierge_v2_degrade.py — spend cap and graceful degrade (CR-5).
#
# Created: 2026-09-28 (feat/concierge-spend-cap) — a v2 concierge that cannot answer
# (the site is over its daily spend cap, the site's monthly conversation allowance
# is used up, or the model provider timed out or failed) sends the visitor ONE
# fixed leave-a-message reply as ordinary ``chunk`` + ``stream_end`` frames and
# hands the conversation to the owner through ``handoff.raise_handoff``, instead of
# an error. These tests pin:
#
#   * each trigger produces the degrade reply, and the cap and quota arms make no
#     model call (tests/mutations/concierge_v2_runtime.json guards each arm);
#   * the cap is per site per UTC day, compared with ``>=``, and 0 turns it off;
#   * the metered call carries the site and widget as LiteLLM request tags;
#   * a legacy site is untouched by the cap;
#   * a conversation already waiting on a person is not handed off again, and the
#     handoff respects the site's transcript-retention switch.
#
# Fixtures come from CR-1's test module, as test_paw_bar_concierge_v2_output does;
# naming one as a test parameter is how pytest injects it, hence the F811 waiver.
# ruff: noqa: F811

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from pocketpaw.paw_bar.models import ConversationState
from tests.cloud.test_paw_bar_concierge_v2 import (  # noqa: F401 — fixtures
    _HOURS_KB,
    _MODEL_REPLY,
    _chat,
    _frames,
    _mock_legacy_machinery,
    _RecordingModel,
    _seed_kb,
    _site,
    _widget,
    concierge_client,
    model,
)


def _pin_cap(monkeypatch, cap: float) -> None:
    """Pin the daily spend cap on top of the ``model`` fixture's settings."""
    from pocketpaw_ee.paw_bar import concierge_runtime

    pinned = concierge_runtime._settings().model_copy(
        update={"pawbar_concierge_daily_spend_cap": cap}
    )
    monkeypatch.setattr(concierge_runtime, "_settings", lambda: pinned)


async def _seed_spend(
    cost_usd: float,
    *,
    pocket_id: str = "pocket-1",
    workspace: str = "ws-1",
    at: datetime | None = None,
    context_type: str = "concierge",
) -> None:
    """One finished run carrying a reported cost, as the meter reads it."""
    import uuid

    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    await ChatRunDoc(
        run_id=uuid.uuid4().hex,
        workspace=workspace,
        context_type=context_type,
        scope_id=pocket_id,
        session_key="s",
        user_id="u",
        agent_id="",
        client_message_id=uuid.uuid4().hex,
        user_message_id="",
        status="completed",
        usage={"model": "fake-concierge", "total_cost_usd": cost_usd},
        createdAt=at or datetime.now(UTC),
    ).insert()


def _text(frames: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(d["content"] for e, d in frames if e == "chunk")


async def _runs() -> list[Any]:
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    return await ChatRunDoc.find(ChatRunDoc.context_type == "concierge").to_list()


async def _state(store, widget_id: str) -> Any:
    from tests.cloud.test_paw_bar_concierge_v2 import _REF

    conv = await store.get_conversation(widget_id, _REF, workspace_id="ws-1")
    return conv.state if conv is not None else None


# --------------------------------------------------------------------------- #
# 1. The spend cap
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_site_at_its_daily_cap_gets_the_degrade_reply_and_no_model_call(
    concierge_client, model, monkeypatch
):
    """Spend EXACTLY at the cap degrades: the cap is the most a site may spend."""
    from pocketpaw_ee.paw_bar.concierge_runtime import DEGRADE_HANDED_OFF

    client, store = concierge_client
    _seed_kb(monkeypatch, _HOURS_KB)
    _pin_cap(monkeypatch, 0.5)
    await _seed_spend(0.25)
    await _seed_spend(0.25)
    await _site()
    widget = await store.create_widget(_widget())

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    frames = _frames(res.text)
    assert [e for e, _d in frames] == ["chunk", "stream_end"]
    assert _text(frames) == DEGRADE_HANDED_OFF
    assert frames[-1][1] == {"assistant_message_id": None, "cancelled": False}
    assert model.calls == []
    # No run doc for a turn nobody answered; the two seeded runs are all there is.
    assert len(await _runs()) == 2
    # The owner has it: the conversation waits on a person.
    assert await _state(store, widget.id) == ConversationState.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_a_site_under_its_daily_cap_is_answered(concierge_client, model, monkeypatch):
    client, store = concierge_client
    _seed_kb(monkeypatch, _HOURS_KB)
    _pin_cap(monkeypatch, 0.5)
    await _seed_spend(0.49)
    await _site()
    widget = await store.create_widget(_widget())

    res = await _chat(client, widget.id)

    assert _text(_frames(res.text)) == "".join(_MODEL_REPLY)
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_a_zero_cap_is_no_cap(concierge_client, model, monkeypatch):
    client, store = concierge_client
    _seed_kb(monkeypatch, _HOURS_KB)
    _pin_cap(monkeypatch, 0)
    await _seed_spend(1_000.0)
    await _site()
    widget = await store.create_widget(_widget())

    res = await _chat(client, widget.id)

    assert _text(_frames(res.text)) == "".join(_MODEL_REPLY)


@pytest.mark.asyncio
async def test_spend_is_counted_per_site_per_utc_day(mongo_db):
    """Only this site's (workspace, pocket) concierge runs since UTC midnight count."""
    from pocketpaw_ee.paw_bar.concierge_runtime import site_spend_today_usd

    now = datetime(2026, 9, 28, 0, 30, tzinfo=UTC)
    midnight = datetime(2026, 9, 28, tzinfo=UTC)
    await _seed_spend(0.10, at=midnight)  # counts: at midnight exactly
    await _seed_spend(0.20, at=now - timedelta(minutes=1))  # counts
    await _seed_spend(5.00, at=midnight - timedelta(seconds=1))  # yesterday
    await _seed_spend(7.00, pocket_id="pocket-2", at=now)  # another site
    await _seed_spend(9.00, workspace="ws-2", at=now)  # another tenant
    await _seed_spend(11.0, context_type="pocket", at=now)  # not the concierge

    spent = await site_spend_today_usd("ws-1", "pocket-1", now=now)

    assert spent == pytest.approx(0.30)


@pytest.mark.asyncio
async def test_a_failed_spend_read_serves_the_visitor(concierge_client, model, monkeypatch):
    """Fail open, like the quota: a lost read must not silence a paying site."""
    from pocketpaw_ee.cloud.chat.runs import service as run_service

    client, store = concierge_client
    _seed_kb(monkeypatch, _HOURS_KB)
    _pin_cap(monkeypatch, 0.5)

    async def _boom(**_kw: Any) -> Any:
        raise RuntimeError("mongo hiccup")

    monkeypatch.setattr(run_service, "find_run_usage_since", _boom)
    await _site()
    widget = await store.create_widget(_widget())

    res = await _chat(client, widget.id)

    assert _text(_frames(res.text)) == "".join(_MODEL_REPLY)


@pytest.mark.asyncio
async def test_a_legacy_site_is_untouched_by_the_cap(concierge_client, model, monkeypatch):
    client, store = concierge_client
    fake_exec = _mock_legacy_machinery(monkeypatch)
    _pin_cap(monkeypatch, 0.5)
    await _seed_spend(100.0)
    await _site(concierge_runtime="legacy")
    widget = await store.create_widget(_widget())

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert _text(_frames(res.text)) == "We open at 8am!"
    assert len(fake_exec.submitted) == 1
    assert model.calls == []


# --------------------------------------------------------------------------- #
# 2. The monthly conversation allowance
# --------------------------------------------------------------------------- #


def _quota_used_up(monkeypatch) -> None:
    async def _exceeded(*_a: Any, **_kw: Any) -> bool:
        return True

    monkeypatch.setattr(
        "pocketpaw_ee.cloud.billing.enforcement.concierge_conversation_quota_exceeded",
        _exceeded,
    )


@pytest.mark.asyncio
async def test_v2_quota_hit_is_the_degrade_reply_with_no_model_call(
    concierge_client, model, monkeypatch
):
    from pocketpaw_ee.paw_bar.concierge_runtime import DEGRADE_HANDED_OFF

    client, store = concierge_client
    _quota_used_up(monkeypatch)
    await _site()
    widget = await store.create_widget(_widget())

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    frames = _frames(res.text)
    assert [e for e, _d in frames] == ["chunk", "stream_end"]
    assert _text(frames) == DEGRADE_HANDED_OFF
    assert model.calls == []
    assert await _runs() == []
    assert await _state(store, widget.id) == ConversationState.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_v2_quota_degrade_still_spends_a_rate_limit_slot(
    concierge_client, model, monkeypatch
):
    """A degrade is a served turn that writes a handoff: it must be rate-limited."""
    from pocketpaw.paw_bar.models import PawBarEvent

    client, store = concierge_client
    _quota_used_up(monkeypatch)
    await _site()
    widget = await store.create_widget(_widget(per_customer_limit_per_min=2))
    for _ in range(2):
        await store.record_event(
            PawBarEvent(widget_id=widget.id, type="concierge_message", customer_ref="cust-0001")
        )

    res = await _chat(client, widget.id)

    assert res.status_code == 429
    assert model.calls == []


@pytest.mark.asyncio
async def test_legacy_quota_hit_is_still_403(concierge_client, model, monkeypatch):
    client, store = concierge_client
    _mock_legacy_machinery(monkeypatch)
    _quota_used_up(monkeypatch)
    await _site(concierge_runtime="legacy")
    widget = await store.create_widget(_widget())

    res = await _chat(client, widget.id)

    assert res.status_code == 403
    assert res.json()["detail"] == "concierge_quota_exceeded"


# --------------------------------------------------------------------------- #
# 3. Provider timeout and provider error
# --------------------------------------------------------------------------- #


class _FailingModel(_RecordingModel):
    def __init__(self, exc: BaseException, reply: list[str] | None = None) -> None:
        super().__init__(reply=reply or [])
        self.exc = exc

    def build(self, _settings: Any) -> Any:
        from pydantic_ai.models.function import FunctionModel

        async def _stream(messages, info):
            self.calls.append({"messages": messages, "info": info})
            for piece in self.reply:
                yield piece
            raise self.exc

        return FunctionModel(stream_function=_stream, model_name="fake-concierge")


async def _degraded_by(monkeypatch, client, store, exc: BaseException, reply=None):
    from pocketpaw_ee.paw_bar import concierge_runtime

    rec = _FailingModel(exc, reply=reply)
    monkeypatch.setattr(concierge_runtime, "_build_model", rec.build)
    _seed_kb(monkeypatch, _HOURS_KB)
    await _site()
    widget = await store.create_widget(_widget())
    res = await _chat(client, widget.id)
    assert res.status_code == 200, res.text
    return res, widget


@pytest.mark.asyncio
async def test_a_provider_timeout_is_the_degrade_reply(concierge_client, model, monkeypatch):
    from pocketpaw_ee.paw_bar.concierge_runtime import DEGRADE_HANDED_OFF

    client, store = concierge_client
    res, widget = await _degraded_by(monkeypatch, client, store, TimeoutError("read timed out"))

    frames = _frames(res.text)
    assert frames[0][0] == "message.persisted"
    assert [e for e, _d in frames[1:]] == ["chunk", "stream_end"]
    assert _text(frames) == DEGRADE_HANDED_OFF
    (run,) = await _runs()
    assert run.status == "failed"
    assert run.error == "concierge_v2_provider_timeout"
    assert await _state(store, widget.id) == ConversationState.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_a_timeout_named_by_the_sdk_is_a_timeout(concierge_client, model, monkeypatch):
    """``openai.APITimeoutError`` is not a TimeoutError; its name says what it is."""

    class APITimeoutError(Exception):
        pass

    client, store = concierge_client
    await _degraded_by(monkeypatch, client, store, APITimeoutError("Request timed out."))

    (run,) = await _runs()
    assert run.error == "concierge_v2_provider_timeout"


@pytest.mark.asyncio
async def test_a_provider_error_is_the_degrade_reply(concierge_client, model, monkeypatch):
    from pocketpaw_ee.paw_bar.concierge_runtime import DEGRADE_HANDED_OFF

    client, store = concierge_client
    res, widget = await _degraded_by(
        monkeypatch,
        client,
        store,
        RuntimeError("upstream model exploded: sk-secret-internal-detail"),
        reply=["We open at "],
    )

    frames = _frames(res.text)
    assert frames[-1] == ("stream_end", {"assistant_message_id": None, "cancelled": False})
    assert "error" not in [e for e, _d in frames]
    # What streamed before the failure stays; the degrade line follows it.
    assert _text(frames).startswith("We open at ")
    assert _text(frames).endswith(DEGRADE_HANDED_OFF)
    assert "sk-secret" not in res.text
    (run,) = await _runs()
    assert run.status == "failed"
    assert run.error == "concierge_v2_provider_error"
    assert run.partial_text == "We open at "
    assert await _state(store, widget.id) == ConversationState.NEEDS_HUMAN


# --------------------------------------------------------------------------- #
# 4. Spend tags on the metered call
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_metered_call_is_tagged_with_the_site_and_widget(
    concierge_client, model, monkeypatch
):
    client, store = concierge_client
    _seed_kb(monkeypatch, _HOURS_KB)
    site = await _site()
    widget = await store.create_widget(_widget())

    await _chat(client, widget.id)

    settings = model.last["info"].model_settings
    tags = settings["extra_body"]["metadata"]["tags"]
    assert f"pawbar_site:{site.id}" in tags
    assert f"pawbar_widget:{widget.id}" in tags
    assert settings["openai_user"] == "ws-1"
    assert settings["timeout"] > 0
    # The usage the meter prices names both too.
    (run,) = await _runs()
    assert run.usage["site_id"] == str(site.id)
    assert run.usage["widget_id"] == widget.id
    assert run.usage["backend"] == "pawbar_concierge_v2"


@pytest.mark.asyncio
async def test_tags_are_not_sent_to_a_provider_that_is_not_our_proxy(monkeypatch):
    """A direct provider rejects LiteLLM's metadata body; only the proxy gets tags."""
    from pocketpaw_ee.paw_bar import concierge_runtime

    from pocketpaw.config import get_settings

    settings = get_settings().model_copy(update={"pawbar_concierge_model": "anthropic:x"})
    out = concierge_runtime._model_settings(
        settings, "ws-1", tags=["pawbar_site:s", "pawbar_widget:w"]
    )
    assert "extra_body" not in out
    assert "openai_user" not in out


# --------------------------------------------------------------------------- #
# 5. degrade_reply itself
# --------------------------------------------------------------------------- #


class _Conv:
    def __init__(self, state: Any) -> None:
        self.state = state


async def _collect(gen) -> list[tuple[str, dict[str, Any]]]:
    body = b"".join([frame async for frame in gen]).decode()
    return _frames(body)


@pytest.mark.asyncio
async def test_a_conversation_already_waiting_on_a_person_is_not_handed_off_again(
    monkeypatch,
):
    """No second owner notification per visitor turn while the cap holds."""
    from pocketpaw_ee.paw_bar import concierge_runtime, handoff

    raised: list[dict[str, Any]] = []

    async def _raise(**kw: Any) -> Any:
        raised.append(kw)
        return handoff.HandoffOutcome(ok=True)

    monkeypatch.setattr(handoff, "raise_handoff", _raise)
    frames = await _collect(
        concierge_runtime.degrade_reply(
            object(),
            "spend_cap",
            workspace_id="ws-1",
            customer_ref="cust-0001",
            question="hello",
            conversation=_Conv(ConversationState.NEEDS_HUMAN),
        )
    )
    assert raised == []
    assert _text(frames) == concierge_runtime.DEGRADE_HANDED_OFF


@pytest.mark.asyncio
async def test_a_handoff_that_did_not_land_says_leave_a_message(monkeypatch):
    """The reply never claims the team has the message when nothing recorded it."""
    from pocketpaw_ee.paw_bar import concierge_runtime, handoff

    async def _raise(**_kw: Any) -> Any:
        return handoff.HandoffOutcome(ok=False, error="handoff_unavailable", http_status=503)

    monkeypatch.setattr(handoff, "raise_handoff", _raise)
    frames = await _collect(
        concierge_runtime.degrade_reply(
            object(), "provider_error", workspace_id="ws-1", customer_ref="cust-0001"
        )
    )
    assert _text(frames) == concierge_runtime.DEGRADE_LEAVE_MESSAGE
    assert [e for e, _d in frames] == ["chunk", "stream_end"]


@pytest.mark.asyncio
async def test_the_degrade_reply_never_names_its_reason(monkeypatch):
    """Cap, quota and provider state are the owner's business, not the visitor's."""
    from pocketpaw_ee.paw_bar import concierge_runtime, handoff

    async def _raise(**_kw: Any) -> Any:
        return handoff.HandoffOutcome(ok=True)

    monkeypatch.setattr(handoff, "raise_handoff", _raise)
    texts = set()
    for reason in concierge_runtime.DEGRADE_REASONS:
        frames = await _collect(
            concierge_runtime.degrade_reply(
                object(), reason, workspace_id="ws-1", customer_ref="cust-0001"
            )
        )
        texts.add(_text(frames))
    assert texts == {concierge_runtime.DEGRADE_HANDED_OFF}


@pytest.mark.asyncio
async def test_retention_off_keeps_the_visitor_line_off_the_handoff(
    concierge_client, model, monkeypatch
):
    from pocketpaw_ee.paw_bar import handoff

    client, store = concierge_client
    _pin_cap(monkeypatch, 0.5)
    await _seed_spend(1.0)
    seen: list[dict[str, Any]] = []
    real = handoff.raise_handoff

    async def _spy(**kw: Any) -> Any:
        seen.append(kw)
        return await real(**kw)

    monkeypatch.setattr(handoff, "raise_handoff", _spy)
    await _site(concierge_store_transcripts=False)
    widget = await store.create_widget(_widget())

    await _chat(client, widget.id)

    assert len(seen) == 1
    assert seen[0]["question"] == ""
    assert seen[0]["store"] is store
