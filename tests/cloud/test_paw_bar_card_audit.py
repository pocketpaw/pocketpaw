# tests/cloud/test_paw_bar_card_audit.py: POST /paw-bar/cards/{run_id}/{card_id}/audit.
#
# The browser posts one chat card's pure-audit counts after the card lands. The
# route takes the site's embed key, a body of at most 1 KB holding integers and
# widget type names only, spends its own rate bucket (never the chat's), and
# writes the row onto the visitor's own concierge run doc. Anything else is
# refused before a write: no key, a big body, a non-integer count, an unknown
# field, someone else's run.

from __future__ import annotations

import uuid

import pytest

from pocketpaw.security.rate_limiter import RateLimiter
from tests.cloud import test_paw_bar_reply_sources as _sources
from tests.cloud.test_paw_bar_reply_sources import _ORIGIN, _VALID_KEY, _site, _widget

concierge_client = _sources.concierge_client

_REF = "visitor-0001"
_COUNTS = {"schema": 0, "catalog": 1, "leftover": 2, "futureDate": 0}


async def _run(customer_ref: str = _REF, **ov) -> str:
    from pocketpaw_ee.cloud.chat.runs import service as run_service
    from pocketpaw_ee.cloud.chat.runs.domain import RunSpec

    fields = dict(
        run_id=uuid.uuid4().hex,
        workspace_id="ws-1",
        context_type="concierge",
        scope_id="pocket-1",
        session_key="cloud:concierge:pocket-1:conv:agent-xyz",
        group=None,
        user_id=customer_ref,
        agent_id="agent-xyz",
        client_message_id=uuid.uuid4().hex,
        user_message_id="",
        content="hi",
        history=[],
        intent=None,
        surface="concierge",
    )
    fields.update(ov)
    return (await run_service.create_run(RunSpec(**fields))).run_id


def _body(widget_id: str, **ov) -> dict:
    body = {
        "widget_id": widget_id,
        "signed_key": _VALID_KEY,
        "customer_ref": _REF,
        "counts": dict(_COUNTS),
        "waived": 0,
        "widget_types": ["card", "text"],
        "ms": 12,
        "seen_ms": 340,
    }
    body.update(ov)
    return body


async def _audits(run_id: str) -> dict:
    from pocketpaw_ee.cloud.chat.runs import service as run_service

    return (await run_service.get_run(run_id)).card_audits


async def _post(client, run_id: str, body: dict, card_id: str = "c1"):
    return await client.post(
        f"/paw-bar/cards/{run_id}/{card_id}/audit", json=body, headers={"Origin": _ORIGIN}
    )


@pytest.mark.asyncio
async def test_counts_land_on_the_visitors_run(concierge_client):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    run_id = await _run()

    res = await _post(client, run_id, _body(widget.id))

    assert res.status_code == 204, res.text
    row = (await _audits(run_id))["c1"]
    assert row["counts"] == _COUNTS
    assert (row["waived"], row["widget_types"], row["ms"], row["seen_ms"]) == (
        0,
        ["card", "text"],
        12,
        340,
    )
    assert "at" in row


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["", "site_key_" + "z" * 24])
async def test_an_unsigned_post_is_rejected(concierge_client, key):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    run_id = await _run()

    res = await _post(client, run_id, _body(widget.id, signed_key=key))

    assert res.status_code == 401, res.text
    assert await _audits(run_id) == {}


@pytest.mark.asyncio
async def test_a_body_over_1kb_is_rejected(concierge_client):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    run_id = await _run()
    # Valid in every field, just big: 30 type names of 38 chars.
    big = _body(widget.id, widget_types=[f"w{i:02d}" + "x" * 35 for i in range(30)])

    res = await _post(client, run_id, big)

    assert res.status_code == 413, res.text
    assert await _audits(run_id) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [2.5, "3", True, -1, None])
async def test_a_non_integer_count_is_rejected(concierge_client, bad):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    run_id = await _run()

    res = await _post(client, run_id, _body(widget.id, counts={"schema": bad}))

    assert res.status_code == 422, res.text
    assert await _audits(run_id) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "extra",
    [{"spec": {"ui": {"type": "text"}}}, {"text": "hello"}, {"counts": {"some check": 1}}],
)
async def test_no_spec_and_no_text_ride_along(concierge_client, extra):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    run_id = await _run()

    res = await _post(client, run_id, _body(widget.id, **extra))

    assert res.status_code == 422, res.text


@pytest.mark.asyncio
async def test_another_visitors_run_is_not_found(concierge_client):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    theirs = await _run(customer_ref="visitor-0002")

    res = await _post(client, theirs, _body(widget.id))

    assert res.status_code == 404, res.text
    assert await _audits(theirs) == {}


@pytest.mark.asyncio
async def test_a_card_id_outside_the_stream_shape_is_refused(concierge_client):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    run_id = await _run()

    res = await _post(client, run_id, _body(widget.id), card_id="c1.counts")

    assert res.status_code == 422, res.text
    assert await _audits(run_id) == {}


@pytest.mark.asyncio
async def test_audits_spend_their_own_bucket_not_the_chats(concierge_client, monkeypatch):
    client, store = concierge_client
    # A roomy IP limiter, so only the store's buckets are under test.
    monkeypatch.setattr(
        "pocketpaw_ee.paw_bar.router._PUBLIC_IP_LIMITER", RateLimiter(rate=100.0, capacity=1000)
    )
    await _site()
    widget = await store.create_widget(_widget(per_customer_limit_per_min=3))
    run_id = await _run()

    statuses = [
        (await _post(client, run_id, _body(widget.id), card_id=f"c{i}")).status_code
        for i in range(1, 6)
    ]

    assert statuses == [204, 204, 204, 429, 429]
    assert await store.within_rate_limit(
        widget.id, overall_per_min=60, per_customer_per_min=3, customer_ref=_REF
    )
