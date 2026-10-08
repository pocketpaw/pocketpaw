# tests/cloud/test_paw_bar_card_streaming.py — progressive card frames for "ripple" sites.
#
# A v2 concierge on the "ripple" UI profile streams each ```pawbar-card fence as it
# is written instead of holding it until it closes: ``card.start{card_id}`` when the
# fence opens, ``card.delta{card_id, text}`` with the raw body in order,
# then ``card.final{card_id, card}`` (the validated, hydrated object) or
# ``card.rejected{card_id, reason}`` ("invalid", or "truncated" for a fence the reply
# never closed). No ``chunk`` carries the fence, and the run's transcript keeps the
# validated card fence exactly as before. A "pawbar" site's byte stream is pinned
# unchanged. The realistic card is ripple's recorded explainer scenario
# (tests/fixtures/ripple_explainer_card.json).
#
# Mutations: tests/mutations/concierge_v2_runtime.json ("card streaming" entries).
#
# ruff: noqa: F811 — pytest fixtures imported by name

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests.cloud.test_paw_bar_concierge_v2 import (  # noqa: F401 — fixtures
    _chat,
    _frames,
    _seed_kb,
    _site,
    _widget,
    concierge_client,
    model,
)
from tests.cloud.test_paw_bar_concierge_v2_degrade import _FailingModel
from tests.cloud.test_paw_bar_concierge_v2_output import _splits

_FIXTURE = Path(__file__).parents[1] / "fixtures" / "ripple_explainer_card.json"
_CODE_LINE = "I can't share code here."


def _explainer() -> dict:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _fence(body: str) -> str:
    return f"```pawbar-card\n{body}```"


def _ripple_filter(**kw: Any):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE
    from pocketpaw_ee.paw_bar.concierge_runtime import FenceFilter

    return FenceFilter(profile=RIPPLE_PROFILE, stream_cards=True, **kw)


def _events(chunks: list[str], **kw: Any) -> list[tuple[str, Any]]:
    """("chunk", text) and (event, data) pairs, with adjacent text merged."""
    from pocketpaw_ee.paw_bar.concierge_runtime import CardEvent

    f = _ripple_filter(**kw)
    pieces = [p for c in chunks for p in f.feed(c)] + f.close()
    out: list[tuple[str, Any]] = []
    for p in pieces:
        if isinstance(p, CardEvent):
            out.append((p.event, p.data))
        elif out and out[-1][0] == "chunk":
            out[-1] = ("chunk", out[-1][1] + p)
        else:
            out.append(("chunk", p))
    return out


def _collapse(events: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """Deltas of one card merged, so a chunking-independent shape can be compared."""
    out: list[tuple[str, Any]] = []
    for name, data in events:
        if name == "card.delta" and out and out[-1][0] == "card.delta":
            prev = out[-1][1]
            if prev["card_id"] == data["card_id"]:
                out[-1] = (name, {**prev, "text": prev["text"] + data["text"]})
                continue
        out.append((name, data))
    return out


# --------------------------------------------------------------------------- #
# 1. FenceFilter(stream_cards=True)
# --------------------------------------------------------------------------- #


def test_a_card_split_mid_key_and_mid_string_streams_start_deltas_final():
    body = json.dumps(_explainer()) + "\n"
    reply = f"Here is how gears work.\n{_fence(body)}\nTap any part."
    expected = [
        ("chunk", "Here is how gears work.\n"),
        ("card.start", {"card_id": "c1"}),
        ("card.delta", {"card_id": "c1", "text": body}),
        ("card.final", {"card_id": "c1", "card": _explainer()}),
        ("chunk", "\nTap any part."),
    ]
    # Per character, in 7-char slices (mid-key, mid-string, mid-marker), whole.
    for chunks in (list(reply), [reply[i : i + 7] for i in range(0, len(reply), 7)], [reply]):
        events = _events(chunks)
        deltas = [d["text"] for e, d in events if e == "card.delta"]
        assert "".join(deltas) == body
        assert all(deltas)
        assert len(deltas) > 1 or len(chunks) == 1
        assert _collapse(events) == expected


def test_card_frames_do_not_depend_on_where_the_chunks_split():
    body = '{"ui":{"type":"text","props":{"text":"a`b``c"}}}\n'
    reply = f"Hi `x` {_fence(body)} bye"
    results = {json.dumps(_collapse(_events(c))) for c in _splits(reply)}
    assert len(results) == 1, results
    events = json.loads(results.pop())
    assert [e for e, _ in events] == ["chunk", "card.start", "card.delta", "card.final", "chunk"]
    assert events[2][1]["text"] == body
    assert events[0][1] == "Hi `x` "


def test_an_invalid_card_is_rejected_and_never_reaches_the_text():
    body = '{"ui":{"type":"no-such-widget"}}\n'
    events = _events(["Look: ", _fence(body), " ok"])
    assert events[-2:] == [
        ("card.rejected", {"card_id": "c1", "reason": "invalid"}),
        ("chunk", " ok"),
    ]
    assert not any(e == "card.final" for e, _ in events)


@pytest.mark.parametrize("body", ["not json\n", "[1, 2]\n", '"a string"\n'])
def test_a_card_that_is_not_a_json_object_is_rejected_on_a_streaming_filter(body):
    # Legacy passthrough lets these through as text; a card.final must be an object.
    events = _events([_fence(body)])
    assert events[-1] == ("card.rejected", {"card_id": "c1", "reason": "invalid"})


def test_a_reply_that_ends_mid_card_is_truncated():
    events = _events(["Sure.\n```pawbar-card\n", '{"ui":{"type":"te'])
    assert events == [
        ("chunk", "Sure.\n"),
        ("card.start", {"card_id": "c1"}),
        ("card.delta", {"card_id": "c1", "text": '{"ui":{"type":"te'}),
        ("card.rejected", {"card_id": "c1", "reason": "truncated"}),
    ]


def test_two_cards_in_one_reply_get_distinct_ids():
    card = '{"ui":{"type":"text","props":{"text":"x"}}}\n'
    events = _events([f"A {_fence(card)} B {_fence(card)} C"])
    starts = [d["card_id"] for e, d in events if e == "card.start"]
    finals = [d["card_id"] for e, d in events if e == "card.final"]
    assert starts == finals == ["c1", "c2"]


def test_an_emit_outside_the_host_events_is_rejected_at_close():
    good = {"ui": {"type": "button", "on_click": {"action": "emit", "target": "checkout"}}}
    bad = {"ui": {"type": "button", "on_click": {"action": "emit", "target": "pay"}}}
    events = _events([_fence(json.dumps(good)), _fence(json.dumps(bad))], verbs=["checkout"])
    assert ("card.final", {"card_id": "c1", "card": good}) in events
    assert events[-1] == ("card.rejected", {"card_id": "c2", "reason": "invalid"})


def _deep(depth: int) -> dict:
    node: dict = {"type": "text", "props": {"text": "x"}}
    for _ in range(depth - 1):
        node = {"type": "flex", "children": [node]}
    return {"ui": node}


@pytest.mark.parametrize(
    "spec",
    [
        # A node kept in a prop is checked as a node (embed is deferred).
        {"ui": {"type": "popover", "props": {"trigger": "o", "content": {"type": "embed"}}}},
        # An action sitting in state is checked like one in ui.
        {"ui": {"type": "text"}, "state": {"go": {"action": "navigate", "url": "/x"}}},
        # Far past the depth bound: dropped, never raised.
        _deep(200),
    ],
)
def test_the_hardened_ripple_refusals_are_card_rejected_invalid(spec):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    body = json.dumps(spec) + "\n"
    assert render_card(body, [], profile=RIPPLE_PROFILE) is None
    events = _events([_fence(body)])
    assert events[-1] == ("card.rejected", {"card_id": "c1", "reason": "invalid"})


def test_an_expression_built_javascript_url_streams_then_is_rejected_invalid():
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    spec = {"ui": {"type": "cta", "props": {"label": "Go", "href": "{'java'+'script:alert(1)'}"}}}
    body = json.dumps(spec) + "\n"
    assert render_card(body, [], profile=RIPPLE_PROFILE) is None
    fence = _fence(body)
    events = _events([fence[i : i + 5] for i in range(0, len(fence), 5)])
    # The raw body still streams (deltas are untransformed); the close refuses it.
    assert "".join(d["text"] for e, d in events if e == "card.delta") == body
    assert events[-1] == ("card.rejected", {"card_id": "c1", "reason": "invalid"})
    assert "card.final" not in [e for e, _ in events]


def test_only_card_fences_stream_other_fences_keep_todays_rules():
    events = _events(["x ```py\nprint(1)\n``` y"])
    assert events == [("chunk", f"x {_CODE_LINE} y")]


def test_a_failed_catalog_lookup_rejects_the_streamed_card():
    from pocketpaw_ee.paw_bar.concierge_runtime import CardEvent

    async def lookup(ids):
        raise RuntimeError("store down")

    async def go():
        f = _ripple_filter(lookup=lookup)
        body = '{"ui":{"type":"product-card","props":{"ids":["a"]}}}\n'
        return await f.afeed(_fence(body))

    import asyncio

    out = asyncio.run(go())
    assert out[-1] == CardEvent("card.rejected", {"card_id": "c1", "reason": "invalid"})


# --------------------------------------------------------------------------- #
# 2. The runner, end to end
# --------------------------------------------------------------------------- #


async def _ripple_turn(client, store, monkeypatch, **site_kw: Any):
    _seed_kb(monkeypatch, {})
    await _site(concierge_ui_profile="ripple", **site_kw)
    widget = await store.create_widget(_widget())
    res = await _chat(client, widget.id, message="how do gears work?")
    assert res.status_code == 200, res.text
    return _frames(res.text)


async def _transcript() -> str:
    from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc

    runs = await ChatRunDoc.find(ChatRunDoc.context_type == "concierge").to_list()
    return runs[0].partial_text


@pytest.mark.asyncio
async def test_a_ripple_site_streams_the_card_and_keeps_it_in_the_transcript(
    concierge_client, model, monkeypatch
):
    from pocketpaw_ee.paw_bar.card_spec import RIPPLE_PROFILE, render_card

    client, store = concierge_client
    body = json.dumps(_explainer()) + "\n"
    reply = f"Gears, briefly.\n{_fence(body)}\nAsk me more."
    model.reply = [reply[i : i + 11] for i in range(0, len(reply), 11)]

    frames = await _ripple_turn(client, store, monkeypatch)

    names = [e for e, _ in frames]
    assert names[0] == "message.persisted" and names[-1] == "stream_end"
    start = names.index("card.start")
    final = names.index("card.final")
    before = "".join(d["content"] for e, d in frames[:start] if e == "chunk")
    assert before == "Gears, briefly.\n"
    assert all(n == "card.delta" for n in names[start + 1 : final])
    assert "".join(d["text"] for e, d in frames if e == "card.delta") == body
    assert frames[final][1] == {"card_id": "c1", "card": _explainer()}
    after = "".join(d["content"] for e, d in frames[final:] if e == "chunk")
    assert after == "\nAsk me more."
    assert not any("```" in d["content"] for e, d in frames if e == "chunk")
    # The transcript keeps the validated fence, as it did before streaming.
    fence = render_card(body, [], profile=RIPPLE_PROFILE)
    assert await _transcript() == f"Gears, briefly.\n{fence}\nAsk me more."


@pytest.mark.asyncio
async def test_a_ripple_sites_invalid_card_is_rejected_and_left_out_of_the_transcript(
    concierge_client, model, monkeypatch
):
    client, store = concierge_client
    model.reply = ["Here:\n```pawbar-card\n", '{"ui":{"type":"nope"}}\n', "```\nDone."]

    frames = await _ripple_turn(client, store, monkeypatch)

    assert ("card.rejected", {"card_id": "c1", "reason": "invalid"}) in frames
    assert "card.final" not in [e for e, _ in frames]
    assert await _transcript() == "Here:\n\nDone."


@pytest.mark.asyncio
async def test_a_ripple_reply_cut_off_mid_card_ends_with_a_truncated_rejection(
    concierge_client, model, monkeypatch
):
    client, store = concierge_client
    # The model stopped at its output cap: the stream simply ends inside the fence.
    model.reply = ["Here:\n```pawbar-card\n", '{"ui":{"type":"flex","children":[']

    frames = await _ripple_turn(client, store, monkeypatch)

    assert frames[-2:] == [
        ("card.rejected", {"card_id": "c1", "reason": "truncated"}),
        ("stream_end", {"assistant_message_id": None, "cancelled": False}),
    ]
    assert await _transcript() == "Here:\n"


@pytest.mark.asyncio
async def test_a_ripple_turn_that_fails_mid_card_rejects_it_before_unavailable(
    concierge_client, monkeypatch
):
    from pocketpaw_ee.paw_bar import concierge_runtime

    client, store = concierge_client
    rec = _FailingModel(TimeoutError("read timed out"), reply=['```pawbar-card\n{"ui"'])
    monkeypatch.setattr(concierge_runtime, "_build_model", rec.build)
    monkeypatch.setattr(concierge_runtime, "_RETRY_BACKOFF_S", 0.0, raising=False)

    frames = await _ripple_turn(client, store, monkeypatch)

    # A card frame is something the visitor saw: no retry, so no second c1.
    assert len(rec.calls) == 1
    assert [e for e, _ in frames].count("card.start") == 1
    assert frames[-3:] == [
        ("card.rejected", {"card_id": "c1", "reason": "truncated"}),
        ("unavailable", {"type": "unavailable", "reason": "temporary"}),
        ("stream_end", {"assistant_message_id": None, "cancelled": False}),
    ]


@pytest.mark.asyncio
async def test_a_pawbar_sites_event_stream_is_byte_for_byte_unchanged(
    concierge_client, model, monkeypatch
):
    from pocketpaw_ee.paw_bar.router import _sse

    client, store = concierge_client
    _seed_kb(monkeypatch, {})
    await _site()  # no concierge_ui_profile: "pawbar"
    widget = await store.create_widget(_widget())
    card = '{"ui":{"type":"text","props":{"text":"Open daily"}}}'
    model.reply = [
        "Sure. ``",
        f"`pawbar-card\n{card[:9]}",
        f"{card[9:]}\n```",
        " And ```py\nx = 1\n``` done ``",
    ]

    res = await _chat(client, widget.id)
    assert res.status_code == 200, res.text

    head = _frames(res.text)[0][1]
    pieces = [
        "Sure. ",
        f"```pawbar-card\n{card}\n```",
        " And ",
        _CODE_LINE,
        " done ",
        "``",
    ]
    expected = b"".join(
        [_sse("message.persisted", head)]
        + [_sse("chunk", {"content": p, "type": "text"}) for p in pieces]
        + [_sse("stream_end", {"assistant_message_id": None, "cancelled": False})]
    )
    assert res.content == expected
