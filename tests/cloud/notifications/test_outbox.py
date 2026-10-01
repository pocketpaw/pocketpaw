# tests/cloud/notifications/test_outbox.py
# The notification outbox: atomic claim with a lease, the 1m/5m/30m/2h/6h
# backoff then dead, no double send with two concurrent claimers, the email
# sink's retry-then-sent path, the signed webhook (verified with the receiver
# helper), auto-disable after 10 consecutive dead webhook deliveries, and the
# SSRF refusal both at save and at send. Runs on the shared mongo_db fixture
# with an httpx MockTransport standing in for the network.

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud.models.notification_delivery import NotificationDeliveryConfig
from pocketpaw_ee.cloud.models.notification_outbox import NotificationOutboxItem
from pocketpaw_ee.cloud.notifications import email as email_mod
from pocketpaw_ee.cloud.notifications import outbox, webhook_signing
from pocketpaw_ee.cloud.notifications import service as notifications_service

pytestmark = pytest.mark.usefixtures("mongo_db", "public_dns")

WEBHOOK_URL = "https://hooks.example.com/paw"
SLACK_URL = "https://hooks.slack.com/services/T/B/x"


@pytest.fixture
def public_dns(monkeypatch):
    from pocketpaw_ee.cloud.audit import webhooks as audit_webhooks

    async def _resolve(_hostname):
        return ["93.184.216.34"]

    monkeypatch.setattr(audit_webhooks, "_resolve_addresses", _resolve)


class _Net:
    """httpx MockTransport spy: records requests, answers from a script."""

    def __init__(self, *responses) -> None:
        self.requests: list[httpx.Request] = []
        self._responses = list(responses) or [httpx.Response(200)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        nxt = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@pytest.fixture
def net(monkeypatch):
    holder: dict[str, _Net] = {"net": _Net()}
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(lambda r: holder["net"].handler(r))
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)

    def install(*responses) -> _Net:
        holder["net"] = _Net(*responses)
        return holder["net"]

    return install


@pytest.fixture
def email_on(monkeypatch):
    config = email_mod.EmailConfig("acct", "tok", "notify@paw.example", "Paw")
    monkeypatch.setattr(email_mod, "load_config", lambda: config)
    return config


def _slack_row(**over) -> dict:
    row = {
        "workspace": "w1",
        "kind": "mention",
        "sink": "slack",
        "target": SLACK_URL,
        "payload": {"text": "hi"},
    }
    row.update(over)
    return row


def _t0() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Claim / lease / backoff
# ---------------------------------------------------------------------------


async def test_claim_takes_a_lease_and_hides_the_row_until_it_lapses() -> None:
    await outbox.enqueue(**_slack_row())
    now = _t0()
    first = await outbox.claim_one(now=now)
    assert first is not None and first.status == "sending" and first.attempts == 1
    # Leased: nobody else can claim it now.
    assert await outbox.claim_one(now=now) is None
    # A claimant that died leaves the lease to lapse; then it is claimable again.
    again = await outbox.claim_one(now=now + timedelta(seconds=outbox.LEASE_SECONDS + 1))
    assert again is not None and again.id == first.id and again.attempts == 2
    assert again.claim_id != first.claim_id


def test_backoff_schedule() -> None:
    delays = [outbox.backoff_after(n) for n in range(1, 7)]
    assert delays[:5] == [
        timedelta(minutes=1),
        timedelta(minutes=5),
        timedelta(minutes=30),
        timedelta(hours=2),
        timedelta(hours=6),
    ]
    assert delays[5] is None  # sixth failure: dead
    assert outbox.MAX_ATTEMPTS == 6


async def test_failures_back_off_then_die(net) -> None:
    network = net(httpx.Response(500))
    item = await outbox.enqueue(**_slack_row())
    now = _t0()
    expected = [60, 300, 1800, 7200, 21600]
    for attempt, delay in enumerate(expected, start=1):
        assert await outbox.process_due(now=now) == 1
        row = await NotificationOutboxItem.get(item.id)
        assert row.status == "pending" and row.attempts == attempt
        assert row.last_error == "http 500"
        # Not due before the backoff elapses...
        assert await outbox.process_due(now=now + timedelta(seconds=delay - 1)) == 0
        now = now + timedelta(seconds=delay)
    assert await outbox.process_due(now=now) == 1
    row = await NotificationOutboxItem.get(item.id)
    assert row.status == "dead" and row.attempts == 6 and row.finished_at is not None
    assert len(network.requests) == 6
    # Dead rows are never claimed again.
    assert await outbox.process_due(now=now + timedelta(days=1)) == 0


async def test_two_concurrent_claimers_never_double_send(net) -> None:
    network = net(httpx.Response(200))
    for i in range(20):
        await outbox.enqueue(**_slack_row(payload={"text": f"n{i}"}))
    await asyncio.gather(outbox.process_due(), outbox.process_due())
    texts = [json.loads(r.content)["text"] for r in network.requests]
    assert sorted(texts) == sorted(f"n{i}" for i in range(20))  # each exactly once
    assert await NotificationOutboxItem.find({"status": "sent"}).count() == 20


async def test_a_stale_claimant_cannot_overwrite_the_new_claim(net) -> None:
    net(httpx.Response(200))
    await outbox.enqueue(**_slack_row())
    now = _t0()
    stale = await outbox.claim_one(now=now)
    fresh = await outbox.claim_one(now=now + timedelta(seconds=outbox.LEASE_SECONDS + 1))
    assert await outbox._finish(stale, outbox.Outcome("dead", "late"), now) == "lost"
    assert await outbox._finish(fresh, outbox.Outcome("sent"), now) == "sent"


# ---------------------------------------------------------------------------
# Email sink
# ---------------------------------------------------------------------------


def _cf_ok(**result) -> httpx.Response:
    base = {"delivered": [], "queued": [], "permanent_bounces": []}
    base.update(result)
    return httpx.Response(200, json={"success": True, "errors": [], "result": base})


async def _email_row() -> NotificationOutboxItem:
    return await outbox.enqueue(
        workspace="w1",
        kind="t",
        sink="email",
        target="owner@example.com",
        payload={"template": "test", "site_name": "Acme", "footer_url": "https://app/x"},
    )


async def test_email_429_then_retry_succeeds(net, email_on) -> None:
    network = net(
        httpx.Response(429, json={"success": False, "errors": []}),
        _cf_ok(delivered=["owner@example.com"]),
    )
    item = await _email_row()
    now = _t0()
    await outbox.process_due(now=now)
    row = await NotificationOutboxItem.get(item.id)
    assert row.status == "pending" and "429" in row.last_error
    await outbox.process_due(now=now + timedelta(minutes=1))
    row = await NotificationOutboxItem.get(item.id)
    assert row.status == "sent"
    assert len(network.requests) == 2
    body = json.loads(network.requests[-1].content)
    assert body["to"] == ["owner@example.com"] and body["html"] and body["text"]


async def test_email_400_is_dead_without_retry(net, email_on) -> None:
    network = net(httpx.Response(400, json={"success": False, "errors": [{"code": 10001}]}))
    item = await _email_row()
    await outbox.process_due()
    row = await NotificationOutboxItem.get(item.id)
    assert row.status == "dead" and row.attempts == 1
    assert len(network.requests) == 1


async def test_email_permanent_bounce_is_dead(net, email_on) -> None:
    net(_cf_ok(permanent_bounces=["owner@example.com"]))
    item = await _email_row()
    await outbox.process_due()
    row = await NotificationOutboxItem.get(item.id)
    assert row.status == "dead" and row.last_error == "permanent bounce"


async def test_email_with_sink_off_is_dead_and_never_calls_out(net, monkeypatch) -> None:
    network = net(httpx.Response(200))
    monkeypatch.setattr(email_mod, "load_config", lambda: None)
    item = await _email_row()
    await outbox.process_due()
    assert (await NotificationOutboxItem.get(item.id)).status == "dead"
    assert network.requests == []


# ---------------------------------------------------------------------------
# Signed workspace webhook
# ---------------------------------------------------------------------------


async def test_workspace_webhook_secret_shown_once_and_signature_verifies(net) -> None:
    network = net(httpx.Response(200))
    saved = await notifications_service.set_delivery_config(
        "w1", webhook_url=WEBHOOK_URL, enabled=True
    )
    secret = saved["webhook_secret"]
    assert secret and saved["has_webhook_secret"]
    # Stored encrypted, never returned again.
    doc = await NotificationDeliveryConfig.find_one({"workspace": "w1"})
    assert secret not in doc.webhook_secret_enc
    assert (await notifications_service.get_delivery_config("w1"))["webhook_secret"] is None
    # Re-saving the same URL keeps the secret (receivers keep verifying).
    again = await notifications_service.set_delivery_config(
        "w1", webhook_url=WEBHOOK_URL, enabled=True, routes={"x": ["webhook"]}
    )
    assert again["webhook_secret"] is None

    await notifications_service.create(
        workspace_id="w1", recipient="u2", kind="mention", title="Hi"
    )
    await outbox.process_due()
    req = network.requests[0]
    ts = req.headers[webhook_signing.TIMESTAMP_HEADER]
    sig = req.headers[webhook_signing.SIGNATURE_HEADER]
    assert sig.startswith("v1=")
    assert webhook_signing.verify_signature(secret, ts, req.content, sig)
    event = json.loads(req.content)
    assert set(event) == {"id", "type", "created_at", "data"}


async def test_webhook_auto_disables_after_ten_dead_deliveries(net) -> None:
    net(httpx.Response(500))
    await notifications_service.set_delivery_config("w1", webhook_url=WEBHOOK_URL, enabled=True)
    for _ in range(10):
        await outbox.enqueue(
            workspace="w1",
            kind="k",
            sink="webhook",
            target=WEBHOOK_URL,
            payload={"event_id": "e", "event_type": "t", "data": {}},
            webhook_ref="workspace:w1",
            attempts=outbox.MAX_ATTEMPTS - 1,
        )
    await outbox.process_due()
    doc = await NotificationDeliveryConfig.find_one({"workspace": "w1"})
    assert doc.webhook_failure_count == 10
    assert doc.webhook_disabled_at is not None
    assert await notifications_service.webhook_target("w1") is None
    # Saving a new URL re-arms it.
    await notifications_service.set_delivery_config(
        "w1", webhook_url="https://hooks.example.com/v2", enabled=True
    )
    doc = await NotificationDeliveryConfig.find_one({"workspace": "w1"})
    assert doc.webhook_disabled_at is None and doc.webhook_failure_count == 0


async def test_a_sent_delivery_resets_the_failure_counter(net) -> None:
    net(httpx.Response(200))
    await notifications_service.set_delivery_config("w1", webhook_url=WEBHOOK_URL, enabled=True)
    doc = await NotificationDeliveryConfig.find_one({"workspace": "w1"})
    doc.webhook_failure_count = 7
    await doc.save()
    await outbox.enqueue(
        workspace="w1",
        kind="k",
        sink="webhook",
        target=WEBHOOK_URL,
        payload={"data": {}},
        webhook_ref="workspace:w1",
    )
    await outbox.process_due()
    doc = await NotificationDeliveryConfig.find_one({"workspace": "w1"})
    assert doc.webhook_failure_count == 0


# ---------------------------------------------------------------------------
# SSRF
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.example.com/x",
        "https://169.254.169.254/latest",
        "https://2852039166/",
        "https://localhost/x",
    ],
)
async def test_unsafe_urls_are_refused_at_save(url) -> None:
    with pytest.raises(Forbidden):
        await notifications_service.set_delivery_config("w1", webhook_url=url, enabled=True)
    assert await notifications_service.get_delivery_config("w1") is None


async def test_hostname_resolving_to_private_ip_is_refused(monkeypatch) -> None:
    from pocketpaw_ee.cloud.audit import webhooks as audit_webhooks

    async def _private(_hostname):
        return ["10.0.0.7"]

    monkeypatch.setattr(audit_webhooks, "_resolve_addresses", _private)
    with pytest.raises(Forbidden):
        await notifications_service.set_delivery_config(
            "w1", webhook_url="https://internal.example.com/x", enabled=True
        )


async def test_dns_rebinding_after_save_kills_the_delivery_without_posting(
    net, monkeypatch
) -> None:
    from pocketpaw_ee.cloud.audit import webhooks as audit_webhooks

    network = net(httpx.Response(200))
    await notifications_service.set_delivery_config("w1", slack_webhook_url=SLACK_URL, enabled=True)
    await notifications_service.create(workspace_id="w1", recipient="u2", kind="m", title="x")

    async def _private(_hostname):
        return ["127.0.0.1"]

    monkeypatch.setattr(audit_webhooks, "_resolve_addresses", _private)
    await outbox.process_due()
    row = await NotificationOutboxItem.find_one({})
    assert row.status == "dead" and row.last_error.startswith("unsafe url")
    assert network.requests == []


async def test_rows_for_a_removed_webhook_die_without_counting_against_it(net) -> None:
    network = net(httpx.Response(200))
    await notifications_service.set_delivery_config("w1", webhook_url=WEBHOOK_URL, enabled=True)
    await outbox.enqueue(
        workspace="w1",
        kind="k",
        sink="webhook",
        target="https://hooks.example.com/old",
        payload={"data": {}},
        webhook_ref="workspace:w1",
    )
    await outbox.process_due()
    row = await NotificationOutboxItem.find_one({})
    assert row.status == "dead" and network.requests == []
    doc = await NotificationDeliveryConfig.find_one({"workspace": "w1"})
    assert doc.webhook_failure_count == 0
