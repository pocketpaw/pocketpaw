# tests/cloud/test_paw_bar_concierge_v2_contact.py — contact requests and the output cap.
#
# A visitor asking for a person ("I want to talk to a person", "can someone from
# your team call me back?") must always leave the turn with a way to reach the
# team, and a reply that hits the output-token cap must never fail the turn.
# Prod showed why: a reasoning model behind the LiteLLM gateway spent the whole
# 600-token budget thinking before it wrote the lead card the prompt asks for,
# pydantic_ai raised ``UnexpectedModelBehavior`` on the empty ``length`` response,
# and the visitor got ``unavailable`` (temporary) on 4 of 4 contact requests.
# Pinned here:
#
#   * ``is_contact_request``: a small, conservative check for a first-person
#     request to reach a person, with ordinary questions as negatives;
#   * a reply cut off at the cap keeps the text that streamed and ends normally;
#     a card fence left open is dropped, as before;
#   * a contact request whose model output is empty, truncated or failed still
#     gets the server-built send_to_team form (lead capture on) or the line that
#     points to "Talk to a person" (lead capture off), never ``unavailable``;
#   * a contact request the model answered with a valid lead card gets no second
#     card;
#   * a normal question with an empty, capped reply is still unavailable(temporary);
#   * the reply budget defaults to 2000 tokens and reasoning effort is opt-in.
#
# The cap is simulated the way a provider reports it: a FunctionModel whose
# streamed response ends with ``finish_reason="length"``.
# ruff: noqa: F811

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest

from tests.cloud.test_paw_bar_concierge_v2 import (  # noqa: F401 — fixtures
    _HOURS_KB,
    _chat,
    _frames,
    _RecordingModel,
    _seed_kb,
    _site,
    _widget,
    concierge_client,
    model,
)

_END = ("stream_end", {"assistant_message_id": None, "cancelled": False})
_TEMPORARY = ("unavailable", {"type": "unavailable", "reason": "temporary"})
_PERSON = "I want to talk to a person"
_CALLBACK = "can someone from your team call me back about an order?"
_LEAD_CARD = json.dumps(
    {
        "ui": {
            "type": "form",
            "props": {
                "verb": "send_to_team",
                "submit_label": "Send",
                "fields": [{"name": "email", "label": "Email", "type": "email"}],
            },
        }
    }
)


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch):
    from pocketpaw_ee.paw_bar import concierge_runtime

    monkeypatch.setattr(concierge_runtime, "_RETRY_BACKOFF_S", 0.0, raising=False)


class _CappedModel(_RecordingModel):
    """Streams ``reply`` (or only ``thinking``) and stops the way a provider does
    at max_tokens: the response's finish_reason is "length". Thinking alone is a
    reasoning model that spent the whole budget before writing any text."""

    def __init__(self, reply: list[str] | None = None, *, thinking: str = "") -> None:
        super().__init__(reply=reply or [])
        self.thinking = thinking

    def build(self, _settings: Any, _spec: str | None = None) -> Any:
        from pydantic_ai.models.function import DeltaThinkingPart, FunctionModel

        rec = self

        async def _stream(messages, info):
            rec.calls.append({"messages": messages, "info": info})
            if rec.thinking:
                yield {0: DeltaThinkingPart(content=rec.thinking)}
                return
            for piece in rec.reply:
                yield piece

        class _Capped(FunctionModel):
            @asynccontextmanager
            async def request_stream(self, *args: Any, **kwargs: Any):
                async with super().request_stream(*args, **kwargs) as response:
                    response.finish_reason = "length"
                    yield response

        return _Capped(stream_function=_stream, model_name="fake-concierge")


class _BrokenModel(_RecordingModel):
    """Raises a non-transient provider error before any text, every call."""

    def build(self, _settings: Any, _spec: str | None = None) -> Any:
        from pydantic_ai.models.function import FunctionModel

        async def _stream(messages, info):
            self.calls.append({"messages": messages, "info": info})
            raise RuntimeError("upstream model exploded")
            yield ""  # pragma: no cover — makes this an async generator

        return FunctionModel(stream_function=_stream, model_name="fake-concierge")


async def _turn(monkeypatch, client, store, rec, message: str, **site_kw: Any):
    from pocketpaw_ee.paw_bar import concierge_runtime

    monkeypatch.setattr(concierge_runtime, "_build_model", rec.build)
    _seed_kb(monkeypatch, _HOURS_KB)
    await _site(**site_kw)
    widget = await store.create_widget(_widget())
    res = await _chat(client, widget.id, message=message)
    assert res.status_code == 200, res.text
    return _frames(res.text)


def _text(frames: list[tuple[str, dict[str, Any]]]) -> str:
    return "".join(d["content"] for e, d in frames if e == "chunk")


def _cards(text: str) -> list[dict[str, Any]]:
    out = []
    for part in text.split("```pawbar-card\n")[1:]:
        out.append(json.loads(part.split("\n```", 1)[0]))
    return out


def _assert_server_lead_card(text: str) -> None:
    (card,) = _cards(text)
    ui = card["ui"]
    assert ui["type"] == "form"
    props = ui["props"]
    assert props["verb"] == "send_to_team"
    names = [f["name"] for f in props["fields"]]
    assert {"email", "phone"} & set(names)
    assert set(names) <= {"name", "email", "phone", "message"}
    # Prefilled with nothing: the server knows nothing the visitor didn't type there.
    assert all("value" not in f for f in props["fields"])


async def _runs() -> list[Any]:
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    return await ChatRunDoc.find(ChatRunDoc.context_type == "concierge").to_list()


# --------------------------------------------------------------------------- #
# 1. The intent check
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "message",
    [
        _PERSON,
        _CALLBACK,
        "Can I speak to a human?",
        "talk to someone please",
        "I'd like to speak with your team",
        "Please call me",
        "can you contact me about this",
        "I need a callback",
        "Can I get a call back tomorrow?",
        "let me talk to the manager",
        "Is there a real person I can chat with?",
        "connect me to an agent",
        "How do I contact you?",
        "I want to get in touch with sales",
        "can I reach a human",
        "Representative",
    ],
)
def test_a_request_for_a_person_is_a_contact_request(message):
    from pocketpaw_ee.paw_bar.contact_route import is_contact_request

    assert is_contact_request(message)


@pytest.mark.parametrize(
    "message",
    [
        "When do you open on Sunday?",
        "Do you sell contact lenses?",
        "Is this jacket fine for a person over 6ft?",
        "Does the agent API support webhooks?",
        "What does a property manager plan cost?",
        "Can someone use this on two laptops?",
        "How human-like is the voice?",
        "Are your reviews from real people?",
        "Can you email me the receipt?",
        "How do I reach you from the station?",
        "Do I need a person to sign for delivery?",
        "does it support a live agent / talk to a human handoff?",
        "Does the API support a callback URL?",
        "",
    ],
)
def test_a_normal_question_is_not_a_contact_request(message):
    from pocketpaw_ee.paw_bar.contact_route import is_contact_request

    assert not is_contact_request(message)


# --------------------------------------------------------------------------- #
# 2. The output cap never fails the turn
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_reply_cut_off_at_the_cap_keeps_its_text_and_ends_normally(
    concierge_client, model, monkeypatch
):
    client, store = concierge_client
    rec = _CappedModel(["We open at ", "7:30am on Sun"])

    frames = await _turn(monkeypatch, client, store, rec, "When do you open on Sunday?")

    assert _text(frames) == "We open at 7:30am on Sun"
    assert _TEMPORARY not in frames
    assert frames[-1] == _END
    (run,) = await _runs()
    assert run.status == "completed"


@pytest.mark.asyncio
async def test_a_card_cut_off_at_the_cap_is_dropped_and_the_text_kept(
    concierge_client, model, monkeypatch
):
    client, store = concierge_client
    rec = _CappedModel(["Here is our espresso. ", '```pawbar-card\n{"ui": {"type": "pro'])

    frames = await _turn(monkeypatch, client, store, rec, "what espresso do you have?")

    assert _text(frames) == "Here is our espresso. "
    assert _TEMPORARY not in frames
    assert frames[-1] == _END


@pytest.mark.asyncio
async def test_a_normal_question_with_an_empty_capped_reply_is_still_unavailable(
    concierge_client, model, monkeypatch
):
    """Nothing usable came out and the visitor asked a question only the model can
    answer: the honest frame is still ``unavailable``."""
    client, store = concierge_client
    rec = _CappedModel(thinking="Let me think about the hours very carefully...")

    frames = await _turn(monkeypatch, client, store, rec, "When do you open on Sunday?")

    assert frames[-2:] == [_TEMPORARY, _END]
    assert _text(frames) == ""


@pytest.mark.asyncio
async def test_a_question_that_only_mentions_a_person_still_fails_as_unavailable(
    concierge_client, model, monkeypatch
):
    """The provider failed and the visitor didn't ask for a person: no server card,
    the turn ends as unavailable(temporary)."""
    client, store = concierge_client

    frames = await _turn(
        monkeypatch, client, store, _BrokenModel(), "Can you email me the receipt?"
    )

    assert frames[-2:] == [_TEMPORARY, _END]
    assert "pawbar-card" not in _text(frames)
    (run,) = await _runs()
    assert run.status == "failed"


# --------------------------------------------------------------------------- #
# 3. A contact request always gets a route to the team
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
@pytest.mark.parametrize("message", [_PERSON, _CALLBACK])
async def test_a_contact_request_the_model_spent_on_thinking_gets_the_lead_card(
    concierge_client, model, monkeypatch, message
):
    """The prod failure: the reasoning model used the whole budget thinking."""
    client, store = concierge_client
    rec = _CappedModel(thinking="The visitor wants a person, so a send_to_team form...")

    frames = await _turn(monkeypatch, client, store, rec, message)

    assert _TEMPORARY not in frames
    assert frames[-1] == _END
    text = _text(frames)
    assert text.startswith("I can pass this to the team.")
    _assert_server_lead_card(text)
    (run,) = await _runs()
    assert run.status == "completed"
    assert "send_to_team" in (run.partial_text or "")


@pytest.mark.asyncio
async def test_a_contact_request_whose_card_was_cut_off_gets_the_lead_card(
    concierge_client, model, monkeypatch
):
    client, store = concierge_client
    rec = _CappedModel(
        ["Happy to get the team on this. ", '```pawbar-card\n{"ui": {"type": "form", "pr']
    )

    frames = await _turn(monkeypatch, client, store, rec, _CALLBACK)

    assert _TEMPORARY not in frames
    text = _text(frames)
    assert text.startswith("Happy to get the team on this. ")
    _assert_server_lead_card(text)


@pytest.mark.asyncio
async def test_a_contact_request_with_an_empty_reply_gets_the_lead_card(
    concierge_client, model, monkeypatch
):
    client, store = concierge_client
    rec = _CappedModel([])

    frames = await _turn(monkeypatch, client, store, rec, _PERSON)

    assert _TEMPORARY not in frames
    _assert_server_lead_card(_text(frames))


@pytest.mark.asyncio
async def test_a_contact_request_the_provider_failed_gets_the_lead_card(
    concierge_client, model, monkeypatch
):
    """The route to the team doesn't depend on the model at all."""
    client, store = concierge_client

    frames = await _turn(monkeypatch, client, store, _BrokenModel(), _PERSON)

    assert _TEMPORARY not in frames
    assert frames[-1] == _END
    _assert_server_lead_card(_text(frames))


@pytest.mark.asyncio
async def test_a_contact_request_with_lead_capture_off_points_to_talk_to_a_person(
    concierge_client, model, monkeypatch
):
    """No lead card on this site: the route is the bar's own "Talk to a person"."""
    client, store = concierge_client
    rec = _CappedModel(thinking="The visitor wants a person...")

    frames = await _turn(monkeypatch, client, store, rec, _PERSON, concierge_lead_capture=False)

    assert _TEMPORARY not in frames
    assert frames[-1] == _END
    text = _text(frames)
    assert "I can pass this to the team." in text
    assert "Talk to a person" in text
    assert "pawbar-card" not in text


@pytest.mark.asyncio
async def test_a_contact_request_answered_with_a_lead_card_gets_no_second_card(
    concierge_client, model, monkeypatch
):
    client, store = concierge_client
    rec = _RecordingModel(["Sure, leave your email. ", f"```pawbar-card\n{_LEAD_CARD}\n```"])

    frames = await _turn(monkeypatch, client, store, rec, _PERSON)

    text = _text(frames)
    assert text.startswith("Sure, leave your email. ")
    assert len(_cards(text)) == 1
    assert "I can pass this to the team." not in text


@pytest.mark.asyncio
async def test_a_contact_request_answered_in_text_still_gets_the_lead_card(
    concierge_client, model, monkeypatch
):
    """The model wrote words but no form: the visitor still needs the form."""
    client, store = concierge_client
    rec = _RecordingModel(["Of course, our team is happy to help."])

    frames = await _turn(monkeypatch, client, store, rec, _CALLBACK)

    text = _text(frames)
    assert text.startswith("Of course, our team is happy to help.")
    _assert_server_lead_card(text)


# --------------------------------------------------------------------------- #
# 4. The building blocks
# --------------------------------------------------------------------------- #


def test_the_server_lead_card_passes_the_same_validation_as_a_model_card():
    from pocketpaw_ee.paw_bar.card_spec import render_card
    from pocketpaw_ee.paw_bar.contact_route import lead_card_fence

    fence = lead_card_fence()
    assert fence is not None
    body = fence.split("```pawbar-card\n", 1)[1].rsplit("\n```", 1)[0]
    assert render_card(body, [], lead_capture=True) == fence
    assert render_card(body, [], lead_capture=False) is None


def test_the_fence_filter_notices_a_lead_card():
    from pocketpaw_ee.paw_bar.concierge_runtime import FenceFilter

    on = FenceFilter(lead_capture=True)
    on.feed(f"Leave your email. ```pawbar-card\n{_LEAD_CARD}\n```")
    assert on.lead_card is True

    off = FenceFilter(lead_capture=False)
    off.feed(f"```pawbar-card\n{_LEAD_CARD}\n```")
    assert off.lead_card is False  # dropped, so not shown

    plain = FenceFilter(lead_capture=True)
    plain.feed("We open at 7:30am.")
    assert plain.lead_card is False


def test_the_reply_budget_fits_a_reasoning_model_and_a_card():
    from pocketpaw.config import Settings

    assert Settings.model_fields["pawbar_concierge_max_tokens"].default == 2000


def test_reasoning_effort_is_sent_only_when_configured():
    from pocketpaw_ee.paw_bar import concierge_runtime

    from pocketpaw.config import get_settings

    base = get_settings().model_copy(update={"pawbar_concierge_model": "litellm:fake"})
    assert "openai_reasoning_effort" not in concierge_runtime._model_settings(
        base, "litellm:fake", "ws-1"
    )

    low = base.model_copy(update={"pawbar_concierge_reasoning_effort": "low"})
    assert (
        concierge_runtime._model_settings(low, "litellm:fake", "ws-1")["openai_reasoning_effort"]
        == "low"
    )
