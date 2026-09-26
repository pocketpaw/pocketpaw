# tests/cloud/test_paw_bar_public_route_gates.py — the anonymous paw-bar routes
# cannot be used to take a site's concierge offline or to write into the owner's
# systems without the site's embed key.
# Created 2026-09-26 (fix/pawbar-public-route-gates). Covers:
#   * POST /paw-bar/events/{id}: a concierge widget (agent bound) refuses a
#     key-less write, and every ingested event lands in its OWN rate bucket, so a
#     flood of them can no longer 429 the visitor chat. An unbound legacy widget
#     keeps its key-less path, in that separate bucket.
#   * a per-IP limit on the public routes (spec, events, decision, chat, gate).
#   * PawBarStore.admit_event: check-and-record is one step, so a concurrent burst
#     cannot slip past the cap.
#   * the decision GET format-checks customer_ref and needs the key for a
#     concierge widget; chat format-checks customer_ref and bounds message length.

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from pocketpaw.paw_bar.models import PawBarEvent
from pocketpaw.paw_bar.store import PawBarStore
from pocketpaw.security.rate_limiter import RateLimiter
from tests.cloud import test_paw_bar_reply_sources as _sources
from tests.cloud.test_paw_bar_reply_sources import (
    _ORIGIN,
    _VALID_KEY,
    _site,
    _spec,
    _sse_events,
    _stub_kb_search,
    _stub_run_machinery,
    _widget,
)

# The sibling suite's app + store fixture, re-exported so pytest finds it here.
concierge_client = _sources.concierge_client

_REF = "visitor-0001"


def _chat_body(widget_id: str, **ov) -> dict:
    body = {
        "widget_id": widget_id,
        "signed_key": _VALID_KEY,
        "customer_ref": _REF,
        "message": "What time do you open?",
    }
    body.update(ov)
    return body


def _event_body(ref: str, **ov) -> dict:
    body = {"type": "order_click", "payload": {"item": "oat_latte"}, "customer_ref": ref}
    body.update(ov)
    return body


def _tight_ip_limiter(monkeypatch, capacity: int) -> None:
    """Swap the router's per-IP limiter for one that refills ~never."""
    monkeypatch.setattr(
        "pocketpaw_ee.paw_bar.router._PUBLIC_IP_LIMITER",
        RateLimiter(rate=0.0001, capacity=capacity),
    )


# --------------------------------------------------------------------------- #
# #1 / #2 — the key-less event write
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_keyless_event_on_a_concierge_widget_is_rejected(concierge_client):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())

    res = await client.post(
        f"/paw-bar/events/{widget.id}",
        json=_event_body("visitor-flood-01"),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 401, res.text
    assert await store.recent_events(widget.id) == []


@pytest.mark.asyncio
async def test_keyed_event_on_a_concierge_widget_is_accepted(concierge_client):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())

    res = await client.post(
        f"/paw-bar/events/{widget.id}",
        json=_event_body(_REF, signed_key=_VALID_KEY),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 200, res.text
    assert res.json()["accepted"] is True


@pytest.mark.asyncio
async def test_keyed_event_with_a_bad_key_is_rejected(concierge_client):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())

    res = await client.post(
        f"/paw-bar/events/{widget.id}",
        json=_event_body(_REF, signed_key="site_key_" + "z" * 24),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 401, res.text


@pytest.mark.asyncio
async def test_a_flood_of_events_no_longer_429s_the_visitor_chat(concierge_client, monkeypatch):
    """The DoS: one caller fills the widget's per-minute bucket and every real
    visitor's chat 429s. Events now have their own bucket."""
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget(rate_limit_per_min=3))
    _stub_run_machinery(monkeypatch)
    _stub_kb_search(monkeypatch, [])

    statuses = []
    for i in range(6):
        res = await client.post(
            f"/paw-bar/events/{widget.id}",
            json=_event_body(f"visitor-flood-{i:02d}", signed_key=_VALID_KEY),
            headers={"Origin": _ORIGIN},
        )
        statuses.append(res.status_code)
    # The flood fills ITS OWN bucket (3/min) and is cut off there...
    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses[3:]

    # ...and the visitor's chat is untouched by it.
    res = await client.post(
        "/paw-bar/chat", json=_chat_body(widget.id), headers={"Origin": _ORIGIN}
    )
    assert res.status_code == 200, res.text
    assert [n for n, _ in _sse_events(res.text)][-1] == "stream_end"


@pytest.mark.asyncio
async def test_keyless_events_on_a_legacy_widget_keep_working_in_their_own_bucket(
    concierge_client,
):
    client, store = concierge_client
    widget = await store.create_widget(_widget(agent_id="", rate_limit_per_min=3))

    for i in range(3):
        res = await client.post(
            f"/paw-bar/events/{widget.id}",
            json=_event_body(f"legacy-visitor-{i:02d}"),
            headers={"Origin": _ORIGIN},
        )
        assert res.status_code == 200, res.text
        assert res.json()["accepted"] is True

    # None of them count against the shared (chat) budget.
    assert await store.within_rate_limit(
        widget.id, overall_per_min=3, per_customer_per_min=10, customer_ref=_REF
    )


@pytest.mark.asyncio
async def test_legacy_event_with_a_malformed_customer_ref_is_400(concierge_client):
    client, store = concierge_client
    widget = await store.create_widget(_widget(agent_id=""))

    res = await client.post(
        f"/paw-bar/events/{widget.id}",
        json=_event_body("bad ref!"),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "invalid_customer_ref"


# --------------------------------------------------------------------------- #
# Store: separate bucket + atomic admit
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_events_bucket_does_not_count_against_the_shared_bucket(tmp_path: Path):
    store = PawBarStore(tmp_path / "bucket.db")
    for i in range(5):
        await store.record_event(
            PawBarEvent(widget_id="w1", type="concierge_message", customer_ref=f"r-{i:08d}"),
            bucket="events",
        )
    assert await store.within_rate_limit(
        "w1", overall_per_min=5, per_customer_per_min=5, customer_ref="r-00000000"
    )
    assert not await store.within_rate_limit(
        "w1", overall_per_min=5, per_customer_per_min=5, customer_ref="r-00000000", bucket="events"
    )
    # The owner's event list still shows every row.
    assert len(await store.recent_events("w1")) == 5


@pytest.mark.asyncio
async def test_concurrent_admits_never_exceed_the_cap(tmp_path: Path):
    store = PawBarStore(tmp_path / "admit.db")
    cap = 5

    async def one(i: int) -> bool:
        return await store.admit_event(
            PawBarEvent(widget_id="w1", type="concierge_message", customer_ref=f"r-{i:08d}"),
            overall_per_min=cap,
            per_customer_per_min=cap,
        )

    results = await asyncio.gather(*(one(i) for i in range(30)))

    assert sum(results) == cap
    assert await store.count_events_since("w1", datetime.fromtimestamp(0)) == cap


@pytest.mark.asyncio
async def test_concurrent_admits_for_one_visitor_respect_the_per_customer_cap(tmp_path: Path):
    store = PawBarStore(tmp_path / "admit_one.db")

    async def one() -> bool:
        return await store.admit_event(
            PawBarEvent(widget_id="w1", type="concierge_message", customer_ref="r-00000001"),
            overall_per_min=100,
            per_customer_per_min=3,
        )

    results = await asyncio.gather(*(one() for _ in range(20)))
    assert sum(results) == 3


@pytest.mark.asyncio
async def test_concurrent_chats_do_not_exceed_the_per_visitor_cap(concierge_client, monkeypatch):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget(per_customer_limit_per_min=2))
    _stub_run_machinery(monkeypatch)
    _stub_kb_search(monkeypatch, [])

    responses = await asyncio.gather(
        *(
            client.post("/paw-bar/chat", json=_chat_body(widget.id), headers={"Origin": _ORIGIN})
            for _ in range(6)
        )
    )
    codes = sorted(r.status_code for r in responses)
    assert codes.count(200) == 2, codes
    assert codes.count(429) == 4, codes


# --------------------------------------------------------------------------- #
# Per-IP limit
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_per_ip_limit_trips_on_event_writes(concierge_client, monkeypatch):
    client, store = concierge_client
    widget = await store.create_widget(_widget(agent_id="", rate_limit_per_min=1000))
    _tight_ip_limiter(monkeypatch, capacity=2)

    codes = []
    for i in range(3):
        res = await client.post(
            f"/paw-bar/events/{widget.id}",
            json=_event_body(f"rotating-ref-{i:02d}"),
            headers={"Origin": _ORIGIN},
        )
        codes.append(res.status_code)
    assert codes == [200, 200, 429]


@pytest.mark.asyncio
async def test_per_ip_limit_keys_on_the_proxy_observed_address(concierge_client, monkeypatch):
    """The rightmost X-Forwarded-For hop is the one our proxy appended; a caller
    rotating the leftmost value does not get a fresh bucket."""
    client, store = concierge_client
    widget = await store.create_widget(_widget(agent_id=""))
    _tight_ip_limiter(monkeypatch, capacity=1)

    first = await client.get(
        f"/paw-bar/spec/{widget.id}",
        headers={"Origin": _ORIGIN, "X-Forwarded-For": "1.1.1.1, 203.0.113.9"},
    )
    spoofed = await client.get(
        f"/paw-bar/spec/{widget.id}",
        headers={"Origin": _ORIGIN, "X-Forwarded-For": "9.9.9.9, 203.0.113.9"},
    )
    other = await client.get(
        f"/paw-bar/spec/{widget.id}",
        headers={"Origin": _ORIGIN, "X-Forwarded-For": "203.0.113.10"},
    )
    assert (first.status_code, spoofed.status_code, other.status_code) == (200, 429, 200)


@pytest.mark.asyncio
async def test_spec_get_is_rate_limited(concierge_client, monkeypatch):
    client, store = concierge_client
    widget = await store.create_widget(_widget(agent_id=""))
    _tight_ip_limiter(monkeypatch, capacity=2)

    codes = [
        (await client.get(f"/paw-bar/spec/{widget.id}", headers={"Origin": _ORIGIN})).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]


@pytest.mark.asyncio
async def test_decision_get_is_rate_limited(concierge_client, monkeypatch):
    client, store = concierge_client
    widget = await store.create_widget(_widget(agent_id=""))
    _tight_ip_limiter(monkeypatch, capacity=2)

    codes = [
        (
            await client.get(
                f"/paw-bar/events/{widget.id}/decision/{_REF}", headers={"Origin": _ORIGIN}
            )
        ).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]


@pytest.mark.asyncio
async def test_chat_is_rate_limited_per_ip(concierge_client, monkeypatch):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    _tight_ip_limiter(monkeypatch, capacity=0)

    res = await client.post(
        "/paw-bar/chat", json=_chat_body(widget.id), headers={"Origin": _ORIGIN}
    )
    assert res.status_code == 429


# --------------------------------------------------------------------------- #
# #3 — the decision GET
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_decision_get_rejects_a_malformed_customer_ref(concierge_client):
    client, store = concierge_client
    widget = await store.create_widget(_widget(agent_id=""))

    res = await client.get(f"/paw-bar/events/{widget.id}/decision/abc", headers={"Origin": _ORIGIN})

    assert res.status_code == 400
    assert res.json()["detail"] == "invalid_customer_ref"


@pytest.mark.asyncio
async def test_decision_get_needs_the_key_for_a_concierge_widget(concierge_client):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())

    keyless = await client.get(
        f"/paw-bar/events/{widget.id}/decision/{_REF}", headers={"Origin": _ORIGIN}
    )
    keyed = await client.get(
        f"/paw-bar/events/{widget.id}/decision/{_REF}",
        params={"signed_key": _VALID_KEY},
        headers={"Origin": _ORIGIN},
    )

    assert keyless.status_code == 401
    assert keyed.status_code == 200, keyed.text
    assert keyed.json()["found"] is False


# --------------------------------------------------------------------------- #
# #4 — chat input bounds
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_chat_rejects_a_malformed_customer_ref(concierge_client, monkeypatch):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    fake_exec = _stub_run_machinery(monkeypatch)

    res = await client.post(
        "/paw-bar/chat",
        json=_chat_body(widget.id, customer_ref="x" * 200),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "invalid_customer_ref"
    assert fake_exec.submitted == []


@pytest.mark.asyncio
async def test_chat_rejects_an_oversize_message(concierge_client, monkeypatch):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    fake_exec = _stub_run_machinery(monkeypatch)

    res = await client.post(
        "/paw-bar/chat",
        json=_chat_body(widget.id, message="a" * 8001),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 400
    assert res.json()["detail"] == "message_too_long"
    assert fake_exec.submitted == []


@pytest.mark.asyncio
async def test_chat_accepts_a_message_at_the_limit(concierge_client, monkeypatch):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    _stub_run_machinery(monkeypatch)
    _stub_kb_search(monkeypatch, [])

    res = await client.post(
        "/paw-bar/chat",
        json=_chat_body(widget.id, message="a" * 8000),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 200, res.text


# --------------------------------------------------------------------------- #
# Review follow-ups (2026-09-26)
# --------------------------------------------------------------------------- #


def _locked_admit(store: PawBarStore) -> None:
    """Make admit_event behave like a store whose write lock is held elsewhere."""

    async def _locked(*_a, **_kw):
        raise sqlite3.OperationalError("database is locked")

    store.admit_event = _locked  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_chat_fails_open_when_the_store_is_locked(concierge_client, monkeypatch):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget())
    _stub_run_machinery(monkeypatch)
    _stub_kb_search(monkeypatch, [])
    _locked_admit(store)

    res = await client.post(
        "/paw-bar/chat", json=_chat_body(widget.id), headers={"Origin": _ORIGIN}
    )

    assert res.status_code == 200, res.text
    assert [n for n, _ in _sse_events(res.text)][-1] == "stream_end"
    # The marker still lands through the best-effort fallback.
    assert [e.type for e in await store.recent_events(widget.id)] == ["concierge_message"]


@pytest.mark.asyncio
async def test_ingest_fails_closed_when_the_store_is_locked(concierge_client):
    client, store = concierge_client
    widget = await store.create_widget(_widget(agent_id=""))
    _locked_admit(store)

    res = await client.post(
        f"/paw-bar/events/{widget.id}",
        json=_event_body("legacy-visitor-01"),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 503
    assert await store.recent_events(widget.id) == []


@pytest.mark.asyncio
async def test_keyed_event_for_a_widget_in_another_pocket_is_403(concierge_client):
    client, store = concierge_client
    await _site()  # the key resolves to pocket-1
    widget = await store.create_widget(_widget(pocket_id="pocket-2", spec=_spec("pocket-2")))

    res = await client.post(
        f"/paw-bar/events/{widget.id}",
        json=_event_body(_REF, signed_key=_VALID_KEY),
        headers={"Origin": _ORIGIN},
    )

    assert res.status_code == 403
    assert await store.recent_events(widget.id) == []


@pytest.mark.asyncio
async def test_keyless_decision_get_still_works_for_a_legacy_widget(concierge_client):
    client, store = concierge_client
    widget = await store.create_widget(_widget(agent_id=""))

    res = await client.get(
        f"/paw-bar/events/{widget.id}/decision/{_REF}", headers={"Origin": _ORIGIN}
    )

    assert res.status_code == 200, res.text
    assert res.json()["found"] is False


@pytest.mark.asyncio
async def test_keyless_ingest_does_not_accept_the_frame_origin(concierge_client):
    """The key-less path keeps the old allowed_domains check exactly; our frame's
    origin is not on it and must not be waved through."""
    client, store = concierge_client
    widget = await store.create_widget(_widget(agent_id=""))

    res = await client.post(
        f"/paw-bar/events/{widget.id}",
        json=_event_body("legacy-visitor-01"),
        headers={"Origin": "http://t"},  # the test app's own origin = frame origin
    )

    assert res.status_code == 403


@pytest.mark.asyncio
async def test_per_ip_budget_is_per_widget(concierge_client, monkeypatch):
    """A shared NAT address spending one site's budget leaves another site's alone."""
    client, store = concierge_client
    first = await store.create_widget(_widget(agent_id=""))
    second = await store.create_widget(_widget(agent_id=""))
    _tight_ip_limiter(monkeypatch, capacity=1)

    a1 = await client.get(f"/paw-bar/spec/{first.id}", headers={"Origin": _ORIGIN})
    a2 = await client.get(f"/paw-bar/spec/{first.id}", headers={"Origin": _ORIGIN})
    b1 = await client.get(f"/paw-bar/spec/{second.id}", headers={"Origin": _ORIGIN})

    assert (a1.status_code, a2.status_code, b1.status_code) == (200, 429, 200)


@pytest.mark.asyncio
async def test_a_refused_chat_turn_spends_no_rate_slot(concierge_client, monkeypatch):
    client, store = concierge_client
    await _site()
    widget = await store.create_widget(_widget(per_customer_limit_per_min=1))
    _stub_run_machinery(monkeypatch)
    _stub_kb_search(monkeypatch, [])

    async def _has_connector(workspace_id, pocket_id, **_):
        return [object()]

    async def _no_connectors(workspace_id, pocket_id, **_):
        return []

    target = "pocketpaw_ee.cloud.connectors.service.list_pocket_connectors"
    monkeypatch.setattr(target, _has_connector)
    refused = await client.post(
        "/paw-bar/chat", json=_chat_body(widget.id), headers={"Origin": _ORIGIN}
    )
    monkeypatch.setattr(target, _no_connectors)
    served = await client.post(
        "/paw-bar/chat", json=_chat_body(widget.id), headers={"Origin": _ORIGIN}
    )

    assert refused.status_code == 409
    assert served.status_code == 200, served.text


def test_the_per_ip_limiter_sweeps_idle_buckets(monkeypatch):
    from pocketpaw_ee.paw_bar import router as ppr

    calls: list[float] = []
    limiter = RateLimiter(rate=10.0, capacity=300)
    monkeypatch.setattr(limiter, "cleanup", lambda max_age: calls.append(max_age) or 0)
    monkeypatch.setattr(ppr, "_PUBLIC_IP_LIMITER", limiter)
    monkeypatch.setattr(ppr, "_public_ip_last_sweep", 0.0)

    class _Req:
        client = type("C", (), {"host": "203.0.113.5"})()
        headers: dict = {}

    ppr._public_ip_gate(_Req(), "w1")
    ppr._public_ip_gate(_Req(), "w1")

    assert calls == [ppr._PUBLIC_IP_BUCKET_MAX_AGE_S]
