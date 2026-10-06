# tests/cloud/test_paw_bar_concierge_v2_degrade.py — when the v2 concierge can't answer.
#
# A v2 concierge that cannot answer a turn (the site is over its daily spend cap,
# the site's monthly conversation allowance is used up, or the model provider kept
# failing) sends the widget ONE structured ``unavailable`` frame,
# {"type": "unavailable", "reason": "temporary" | "limit"}, then ``stream_end``.
# No canned chunk text, no handoff, no owner notification per turn: the widget
# renders the state, and the visitor's own "Talk to a person" button stays the
# only way a conversation reaches the team. These tests pin:
#
#   * each trigger produces the frame with the right reason, and the cap and quota
#     arms make no model call (tests/mutations/concierge_v2_runtime.json guards
#     each arm);
#   * a transient provider failure (timeout, 429, 5xx, connection error) is retried
#     once when nothing has streamed yet; content-filter and config errors are not;
#   * no arm raises a handoff or flips the conversation to needs_human;
#   * the owner hears about the daily spend cap at most once per site per UTC day;
#   * the cap is per site per UTC day, compared with ``>=``, and 0 turns it off;
#   * the metered call carries the site and widget as LiteLLM request tags;
#   * a legacy site is untouched by the cap.
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

_END = ("stream_end", {"assistant_message_id": None, "cancelled": False})
_LIMIT = ("unavailable", {"type": "unavailable", "reason": "limit"})
_TEMPORARY = ("unavailable", {"type": "unavailable", "reason": "temporary"})


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    """The retry's backoff is real time; the tests don't wait for it."""
    from pocketpaw_ee.paw_bar import concierge_runtime

    monkeypatch.setattr(concierge_runtime, "_RETRY_BACKOFF_S", 0.0, raising=False)


def _spy_handoffs(monkeypatch) -> list[dict[str, Any]]:
    from pocketpaw_ee.paw_bar import handoff

    seen: list[dict[str, Any]] = []
    real = handoff.raise_handoff

    async def _spy(**kw: Any) -> Any:
        seen.append(kw)
        return await real(**kw)

    monkeypatch.setattr(handoff, "raise_handoff", _spy)
    return seen


def _spy_owner_notes(monkeypatch) -> list[dict[str, Any]]:
    """Every owner notification the turn asks for."""
    from pocketpaw_ee.paw_bar import notify

    seen: list[dict[str, Any]] = []

    async def _spy(**kw: Any) -> bool:
        seen.append(kw)
        return True

    monkeypatch.setattr(notify, "notify_workspace_owner", _spy)
    return seen


def _no_needs_human_note(notes: list[dict[str, Any]]) -> bool:
    from pocketpaw_ee.paw_bar.notify import NOTIFY_NEEDS_HUMAN

    return all(n.get("kind") != NOTIFY_NEEDS_HUMAN for n in notes)


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
async def test_a_site_at_its_daily_cap_is_unavailable_with_no_model_call(
    concierge_client, model, monkeypatch
):
    """Spend EXACTLY at the cap stops the concierge: the cap is the most a site may
    spend. The visitor gets the ``limit`` frame; nobody is handed the conversation."""
    client, store = concierge_client
    _seed_kb(monkeypatch, _HOURS_KB)
    _pin_cap(monkeypatch, 0.5)
    await _seed_spend(0.25)
    await _seed_spend(0.25)
    handoffs = _spy_handoffs(monkeypatch)
    notes = _spy_owner_notes(monkeypatch)
    await _site()
    widget = await store.create_widget(_widget())

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert _frames(res.text) == [_LIMIT, _END]
    assert model.calls == []
    # No run doc for a turn nobody answered; the two seeded runs are all there is.
    assert len(await _runs()) == 2
    assert handoffs == []
    assert _no_needs_human_note(notes)
    assert await _state(store, widget.id) != ConversationState.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_the_owner_hears_about_the_cap_once_per_site_per_day(
    concierge_client, model, monkeypatch
):
    """Every capped turn lands on the cap; the owner is told once a UTC day, not
    once a turn. The record outlives the process cache: a worker that finds
    today's notification already written does not send another."""
    from pocketpaw_ee.cloud.models.notification import Notification
    from pocketpaw_ee.paw_bar import notify

    async def _owner(_ws: str) -> str:
        return "owner-1"

    monkeypatch.setattr(notify, "resolve_workspace_owner", _owner)
    client, store = concierge_client
    _pin_cap(monkeypatch, 0.5)
    await _seed_spend(1.0)
    await _site()
    widget = await store.create_widget(_widget())

    for _ in range(3):
        res = await _chat(client, widget.id)
        assert _frames(res.text) == [_LIMIT, _END]
    getattr(notify, "_spend_cap_noted", set()).clear()
    await _chat(client, widget.id)

    notes = await Notification.find(
        Notification.type == getattr(notify, "NOTIFY_SPEND_CAP", "paw_bar_spend_cap")
    ).to_list()
    assert len(notes) == 1
    assert notes[0].recipient == "owner-1"
    assert notes[0].workspace == "ws-1"


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
async def test_v2_quota_hit_is_unavailable_limit_with_no_model_call(
    concierge_client, model, monkeypatch
):
    client, store = concierge_client
    _quota_used_up(monkeypatch)
    handoffs = _spy_handoffs(monkeypatch)
    notes = _spy_owner_notes(monkeypatch)
    await _site()
    widget = await store.create_widget(_widget())

    res = await _chat(client, widget.id)

    assert res.status_code == 200, res.text
    assert _frames(res.text) == [_LIMIT, _END]
    assert model.calls == []
    assert await _runs() == []
    assert handoffs == []
    assert _no_needs_human_note(notes)
    assert await _state(store, widget.id) != ConversationState.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_v2_quota_degrade_still_spends_a_rate_limit_slot(
    concierge_client, model, monkeypatch
):
    """An unavailable turn is still a served turn: it must be rate-limited."""
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
    """Streams ``reply`` then raises ``exc``, on every call."""

    def __init__(self, exc: BaseException, reply: list[str] | None = None) -> None:
        super().__init__(reply=reply or [])
        self.exc = exc

    def build(self, _settings: Any, _spec: str | None = None) -> Any:
        from pydantic_ai.models.function import FunctionModel

        async def _stream(messages, info):
            self.calls.append({"messages": messages, "info": info})
            for piece in self.reply:
                yield piece
            raise self.exc

        return FunctionModel(stream_function=_stream, model_name="fake-concierge")


class _FlakyModel(_RecordingModel):
    """Raises ``exc`` before streaming anything on the first ``failures`` calls,
    then answers with ``_MODEL_REPLY``."""

    def __init__(self, exc: BaseException, failures: int = 1) -> None:
        super().__init__()
        self.exc = exc
        self.failures = failures

    def build(self, _settings: Any, _spec: str | None = None) -> Any:
        from pydantic_ai.models.function import FunctionModel

        async def _stream(messages, info):
            self.calls.append({"messages": messages, "info": info})
            if len(self.calls) <= self.failures:
                raise self.exc
            for piece in self.reply:
                yield piece

        return FunctionModel(stream_function=_stream, model_name="fake-concierge")


async def _turn_with(monkeypatch, client, store, rec: _RecordingModel, **site_kw: Any):
    from pocketpaw_ee.paw_bar import concierge_runtime

    monkeypatch.setattr(concierge_runtime, "_build_model", rec.build)
    _seed_kb(monkeypatch, _HOURS_KB)
    await _site(**site_kw)
    widget = await store.create_widget(_widget())
    res = await _chat(client, widget.id)
    assert res.status_code == 200, res.text
    return res, widget


def _caused_by(cause: BaseException) -> BaseException:
    err = RuntimeError("model request failed")
    err.__cause__ = cause
    return err


class APIConnectionError(Exception):
    """Named like the openai SDK's, which subclasses nothing builtin."""


def _transient_errors() -> list[BaseException]:
    from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

    return [
        TimeoutError("read timed out"),
        ModelHTTPError(429, "fake-concierge", body="rate limited"),
        ModelHTTPError(503, "fake-concierge", body="overloaded"),
        ModelAPIError("fake-concierge", "Connection error."),
        ConnectionResetError("connection reset by peer"),
        _caused_by(APIConnectionError("Connection error.")),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    _transient_errors(),
    ids=["timeout", "http429", "http503", "model_api", "conn_reset", "sdk_conn_cause"],
)
async def test_a_transient_failure_is_retried_once_and_answered(
    concierge_client, model, monkeypatch, exc
):
    """A provider blip before any text is the same question asked again, not a
    handoff: the retry answers it and the visitor never sees the failure."""
    client, store = concierge_client
    handoffs = _spy_handoffs(monkeypatch)
    notes = _spy_owner_notes(monkeypatch)
    rec = _FlakyModel(exc)

    res, widget = await _turn_with(monkeypatch, client, store, rec)

    frames = _frames(res.text)
    assert _text(frames) == "".join(_MODEL_REPLY)
    assert "unavailable" not in [e for e, _d in frames]
    assert frames[-1] == _END
    assert len(rec.calls) == 2
    assert handoffs == []
    assert _no_needs_human_note(notes)
    (run,) = await _runs()
    assert run.status == "completed"
    assert await _state(store, widget.id) != ConversationState.NEEDS_HUMAN


def _permanent_errors() -> list[BaseException]:
    from pydantic_ai.exceptions import ContentFilterError, ModelHTTPError, UserError

    return [
        ContentFilterError("content filter triggered"),
        ModelHTTPError(400, "fake-concierge", body="bad request"),
        ModelHTTPError(401, "fake-concierge", body="bad key"),
        UserError("unknown model"),
        RuntimeError("upstream model exploded"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc",
    _permanent_errors(),
    ids=["content_filter", "http400", "http401", "user_error", "runtime"],
)
async def test_a_permanent_failure_is_not_retried(concierge_client, model, monkeypatch, exc):
    client, store = concierge_client
    rec = _FlakyModel(exc)

    res, _w = await _turn_with(monkeypatch, client, store, rec)

    assert len(rec.calls) == 1
    assert _frames(res.text)[-2:] == [_TEMPORARY, _END]


@pytest.mark.asyncio
async def test_a_provider_that_keeps_failing_is_unavailable_with_no_handoff(
    concierge_client, model, monkeypatch
):
    """Retried once, failed twice: the ``temporary`` frame, no canned text, no
    handoff, no owner notification, and the conversation stays with the bot."""
    client, store = concierge_client
    handoffs = _spy_handoffs(monkeypatch)
    notes = _spy_owner_notes(monkeypatch)
    rec = _FlakyModel(TimeoutError("read timed out"), failures=99)

    res, widget = await _turn_with(monkeypatch, client, store, rec)

    frames = _frames(res.text)
    assert frames[0][0] == "message.persisted"
    assert frames[1:] == [_TEMPORARY, _END]
    assert _text(frames) == ""
    assert "can't answer" not in res.text
    assert len(rec.calls) == 2
    assert handoffs == []
    assert _no_needs_human_note(notes)
    (run,) = await _runs()
    assert run.status == "failed"
    assert run.error == "concierge_v2_provider_timeout"
    assert await _state(store, widget.id) != ConversationState.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_a_timeout_named_by_the_sdk_is_a_timeout(concierge_client, model, monkeypatch):
    """``openai.APITimeoutError`` is not a TimeoutError; its name says what it is."""

    class APITimeoutError(Exception):
        pass

    client, store = concierge_client
    await _turn_with(
        monkeypatch, client, store, _FailingModel(APITimeoutError("Request timed out."))
    )

    (run,) = await _runs()
    assert run.error == "concierge_v2_provider_timeout"


@pytest.mark.asyncio
async def test_a_failure_after_text_streamed_keeps_the_text_and_is_not_retried(
    concierge_client, model, monkeypatch
):
    """A retry would repeat what the visitor already read: what streamed stays,
    the ``temporary`` frame follows it, and nothing canned is added."""
    client, store = concierge_client
    handoffs = _spy_handoffs(monkeypatch)
    rec = _FailingModel(
        RuntimeError("upstream model exploded: sk-secret-internal-detail"),
        reply=["We open at "],
    )

    res, widget = await _turn_with(monkeypatch, client, store, rec)

    frames = _frames(res.text)
    assert frames[-2:] == [_TEMPORARY, _END]
    assert "error" not in [e for e, _d in frames]
    assert _text(frames) == "We open at "
    assert "sk-secret" not in res.text
    assert len(rec.calls) == 1
    assert handoffs == []
    (run,) = await _runs()
    assert run.status == "failed"
    assert run.error == "concierge_v2_provider_error"
    # The transcript keeps what the model said, never a canned line.
    assert run.partial_text == "We open at "
    assert await _state(store, widget.id) != ConversationState.NEEDS_HUMAN


@pytest.mark.asyncio
async def test_a_transient_failure_mid_stream_is_not_retried(concierge_client, model, monkeypatch):
    client, store = concierge_client
    rec = _FailingModel(TimeoutError("read timed out"), reply=["We open at "])

    res, _w = await _turn_with(monkeypatch, client, store, rec)

    assert len(rec.calls) == 1
    frames = _frames(res.text)
    assert _text(frames) == "We open at "
    assert frames[-2:] == [_TEMPORARY, _END]


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
        settings, "anthropic:x", "ws-1", tags=["pawbar_site:s", "pawbar_widget:w"]
    )
    assert "extra_body" not in out
    assert "openai_user" not in out


# --------------------------------------------------------------------------- #
# 5. degrade_reply itself
# --------------------------------------------------------------------------- #


async def _collect(gen) -> list[tuple[str, dict[str, Any]]]:
    body = b"".join([frame async for frame in gen]).decode()
    return _frames(body)


@pytest.mark.asyncio
async def test_degrade_reply_is_the_unavailable_frame_and_never_a_handoff(monkeypatch):
    """Cap and quota read as ``limit``, a failed provider as ``temporary``; the
    internal reason itself never reaches the visitor."""
    from pocketpaw_ee.paw_bar import concierge_runtime

    handoffs = _spy_handoffs(monkeypatch)
    expected = {
        "spend_cap": _LIMIT,
        "quota": _LIMIT,
        "provider_timeout": _TEMPORARY,
        "provider_error": _TEMPORARY,
    }
    assert set(expected) == set(concierge_runtime.DEGRADE_REASONS)
    for reason, frame in expected.items():
        frames = await _collect(concierge_runtime.degrade_reply(object(), reason))
        assert frames == [frame, _END]
    assert handoffs == []


def test_the_canned_degrade_lines_are_gone():
    from pocketpaw_ee.paw_bar import concierge_runtime

    assert not hasattr(concierge_runtime, "DEGRADE_HANDED_OFF")
    assert not hasattr(concierge_runtime, "DEGRADE_LEAVE_MESSAGE")
