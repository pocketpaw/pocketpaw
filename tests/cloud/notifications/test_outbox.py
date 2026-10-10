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


@pytest.fixture
async def slack_config():
    """Slack rows are re-checked against the workspace config at send time."""
    await notifications_service.set_delivery_config("w1", slack_webhook_url=SLACK_URL, enabled=True)


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


async def test_failures_back_off_then_die(net, slack_config) -> None:
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


async def test_two_concurrent_claimers_never_double_send(net, slack_config) -> None:
    network = net(httpx.Response(200))
    for i in range(20):
        await outbox.enqueue(**_slack_row(payload={"text": f"n{i}"}))
    await asyncio.gather(outbox.process_due(), outbox.process_due())
    texts = [json.loads(r.content)["text"] for r in network.requests]
    assert sorted(texts) == sorted(f"n{i}" for i in range(20))  # each exactly once
    assert await NotificationOutboxItem.find({"status": "sent"}).count() == 20


async def test_a_stale_claimant_cannot_overwrite_the_new_claim(net, slack_config) -> None:
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
    # Envelope plus the deprecated flat notification fields (back-compat).
    assert {"id", "type", "created_at", "data"} <= set(event)
    assert event["kind"] == "mention" and event["id"] == event["data"]["id"]


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


# ---------------------------------------------------------------------------
# Security-review fixes
# ---------------------------------------------------------------------------


def _logical_url(request: httpx.Request) -> str:
    return f"{request.url.scheme}://{request.headers['host']}{request.url.raw_path.decode()}"


async def test_b1_pre_signing_webhook_still_delivers_unsigned_flat_body(net) -> None:
    """A workspace webhook saved before signing existed has no secret. It must
    keep receiving the flat body, unsigned, not go dead."""
    network = net(httpx.Response(200))
    await NotificationDeliveryConfig(workspace="w1", webhook_url=WEBHOOK_URL, enabled=True).insert()
    config = await notifications_service.get_delivery_config("w1")
    assert config["signed"] is False and config["has_webhook_secret"] is False

    out = await notifications_service.create(
        workspace_id="w1", recipient="u2", kind="mention", title="Hi", body="b"
    )
    await outbox.process_due()

    row = await NotificationOutboxItem.find_one({})
    assert row.status == "sent"
    req = network.requests[0]
    assert webhook_signing.SIGNATURE_HEADER not in req.headers
    body = json.loads(req.content)
    assert body["id"] == out.id and body["kind"] == "mention" and body["title"] == "Hi"
    # Saving it again signs it from then on.
    saved = await notifications_service.set_delivery_config(
        "w1", webhook_url=WEBHOOK_URL, enabled=True
    )
    assert saved["webhook_secret"] and saved["signed"] is True


async def test_b2_a_hung_endpoint_is_cut_off_and_other_rows_still_go(monkeypatch, email_on) -> None:
    hang_url = "https://slow.example.com/hook"
    seen: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        url = _logical_url(request)
        if url == hang_url:
            await asyncio.sleep(60)  # a drip server: never finishes
        seen.append(url)
        if "api.cloudflare.com" in url:
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "errors": [],
                    "result": {"delivered": ["a@b.co"], "queued": [], "permanent_bounces": []},
                },
            )
        return httpx.Response(200)

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(outbox, "SEND_DEADLINE_SECONDS", 0.3)

    await notifications_service.set_delivery_config(
        "w1", webhook_url=hang_url, slack_webhook_url=SLACK_URL, enabled=True
    )
    for i in range(3):
        await outbox.enqueue(
            workspace="w1",
            kind="k",
            sink="webhook",
            target=hang_url,
            payload={"event_id": f"e{i}", "data": {}},
            webhook_ref="workspace:w1",
        )
    await outbox.enqueue(**_slack_row())
    await outbox.enqueue(
        workspace="w1",
        kind="t",
        sink="email",
        target="a@b.co",
        payload={"template": "test", "site_name": "Acme", "footer_url": "https://app/x"},
    )

    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await outbox.process_due() == 5
    assert loop.time() - started < 5  # cut off, and the three hangs ran in parallel

    rows = {(r.sink, r.status) for r in await NotificationOutboxItem.find_all().to_list()}
    assert ("email", "sent") in rows and ("slack", "sent") in rows
    hung = await NotificationOutboxItem.find({"sink": "webhook"}).to_list()
    assert {r.status for r in hung} == {"pending"}  # an ordinary failure: backoff
    assert all(r.last_error.startswith("deadline") for r in hung)


async def test_b2_lanes_claim_only_their_own_sinks() -> None:
    await outbox.enqueue(**_slack_row())
    assert await outbox.claim_one(sinks=("email",)) is None
    assert (await outbox.claim_one(sinks=("webhook", "slack"))).sink == "slack"


async def test_n4_malformed_row_is_marked_dead_and_the_sweep_continues(net, slack_config) -> None:
    network = net(httpx.Response(200))
    coll = NotificationOutboxItem.get_pymongo_collection()
    bad = await coll.insert_one(
        {
            "status": "pending",
            "next_at": _t0() - timedelta(minutes=1),
            "sink": "slack",
            "payload": "not-a-dict",
        }
    )
    await outbox.enqueue(**_slack_row())
    assert await outbox.process_due() == 1
    raw = await coll.find_one({"_id": bad.inserted_id})
    assert raw["status"] == "dead" and raw["last_error"].startswith("malformed row")
    assert len(network.requests) == 1


async def test_s5_connection_is_pinned_to_the_checked_ip(net, slack_config) -> None:
    network = net(httpx.Response(200))
    await outbox.enqueue(**_slack_row())
    await outbox.process_due()
    req = network.requests[0]
    assert req.url.host == "93.184.216.34"  # connects to the address that was checked
    assert req.headers["host"] == "hooks.slack.com"
    assert req.extensions["sni_hostname"] == "hooks.slack.com"


async def test_s5_dns_failure_at_send_fails_closed(net, slack_config, monkeypatch) -> None:
    from pocketpaw_ee.cloud.audit import webhooks as audit_webhooks

    network = net(httpx.Response(200))
    await outbox.enqueue(**_slack_row())

    async def _no_dns(_hostname):
        return None

    monkeypatch.setattr(audit_webhooks, "_resolve_addresses", _no_dns)
    await outbox.process_due()
    row = await NotificationOutboxItem.find_one({})
    assert row.status == "pending" and "dns" in row.last_error
    assert network.requests == []


@pytest.mark.parametrize("addr", ["100.100.100.200", "100.64.0.1", "10.1.2.3", "fd00::1"])
async def test_s5_non_global_addresses_are_refused(net, slack_config, monkeypatch, addr) -> None:
    from pocketpaw_ee.cloud.audit import webhooks as audit_webhooks

    network = net(httpx.Response(200))
    await outbox.enqueue(**_slack_row())

    async def _resolve(_hostname):
        return [addr]

    monkeypatch.setattr(audit_webhooks, "_resolve_addresses", _resolve)
    await outbox.process_due()
    row = await NotificationOutboxItem.find_one({})
    assert row.status == "dead" and row.last_error.startswith("unsafe url")
    assert network.requests == []
    with pytest.raises(Forbidden):
        await notifications_service.set_delivery_config(
            "w1", webhook_url="https://cgnat.example.com/x", enabled=True
        )


def test_s5_audit_helper_uses_is_global() -> None:
    import ipaddress

    from pocketpaw_ee.cloud.audit.webhooks import _ip_is_unsafe

    assert _ip_is_unsafe(ipaddress.ip_address("100.100.100.200"))
    assert _ip_is_unsafe(ipaddress.ip_address("224.0.0.1"))
    assert not _ip_is_unsafe(ipaddress.ip_address("93.184.216.34"))


async def test_n2_slack_is_rechecked_at_send(net, slack_config) -> None:
    network = net(httpx.Response(200))
    await outbox.enqueue(**_slack_row())
    await notifications_service.set_delivery_config("w1", slack_webhook_url="", enabled=True)
    await outbox.process_due()
    row = await NotificationOutboxItem.find_one({})
    assert row.status == "dead" and network.requests == []


async def test_n3_rotation_signs_with_both_secrets_during_grace(net) -> None:
    from pocketpaw_ee.cloud.notifications import service as svc

    network = net(httpx.Response(200))
    first = await svc.set_delivery_config("w1", webhook_url=WEBHOOK_URL, enabled=True)
    rotated = await svc.rotate_webhook_secret("w1")
    old, new = first["webhook_secret"], rotated["webhook_secret"]
    assert old != new

    await svc.create(workspace_id="w1", recipient="u2", kind="m", title="x")
    await outbox.process_due()
    req = network.requests[0]
    ts, sig = req.headers["X-Paw-Timestamp"], req.headers["X-Paw-Signature"]
    assert sig.count("v1=") == 2 and sig.startswith(
        webhook_signing.signature_header(new, ts, req.content)
    )
    assert webhook_signing.verify_signature(new, ts, req.content, sig)
    assert webhook_signing.verify_signature(old, ts, req.content, sig)

    # After the grace window only the new secret signs.
    doc = await NotificationDeliveryConfig.find_one({"workspace": "w1"})
    doc.webhook_secret_rotated_at = _t0() - svc.WEBHOOK_SECRET_GRACE - timedelta(minutes=1)
    await doc.save()
    target = await svc.webhook_target("w1")
    assert target[1] == [new]


async def test_s1_concurrent_failures_are_all_counted_and_disable_once() -> None:
    await notifications_service.set_delivery_config("w1", webhook_url=WEBHOOK_URL, enabled=True)
    await asyncio.gather(
        *(notifications_service.record_webhook_result("w1", ok=False) for _ in range(12))
    )
    doc = await NotificationDeliveryConfig.find_one({"workspace": "w1"})
    assert doc.webhook_failure_count == 12 and doc.webhook_disabled_at is not None


async def test_s7_saving_the_same_url_rearms_the_workspace_webhook() -> None:
    first = await notifications_service.set_delivery_config(
        "w1", webhook_url=WEBHOOK_URL, enabled=True
    )
    for _ in range(10):
        await notifications_service.record_webhook_result("w1", ok=False)
    assert await notifications_service.webhook_target("w1") is None
    again = await notifications_service.set_delivery_config(
        "w1", webhook_url=WEBHOOK_URL, enabled=True
    )
    assert again["webhook_secret"] is None  # same URL keeps its secret
    assert again["webhook_disabled_at"] is None and again["webhook_failure_count"] == 0
    target = await notifications_service.webhook_target("w1")
    assert target is not None and first["webhook_secret"] in target[1]


async def test_s8_cloudflare_auth_failure_retries_and_rings_admins_once(net, email_on) -> None:
    import uuid

    from pocketpaw_ee.cloud.models.notification import Notification as _NotificationDoc
    from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership

    admin = User(
        email=f"a{uuid.uuid4().hex[:6]}@x.io",
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="A",
        workspaces=[WorkspaceMembership(workspace="w1", role="owner")],
    )
    await admin.insert()
    net(httpx.Response(401, json={"success": False, "errors": [{"code": 10101}]}))
    for _ in range(2):
        await _email_row()
    await outbox.process_due()

    rows = await NotificationOutboxItem.find_all().to_list()
    assert {r.status for r in rows} == {"pending"}  # retried, not dropped
    notes = await _NotificationDoc.find({"type": outbox.EMAIL_FAILING_KIND}).to_list()
    assert [n.recipient for n in notes] == [str(admin.id)]  # once, deduped


# ---------------------------------------------------------------------------
# Re-review fixes
# ---------------------------------------------------------------------------


async def test_two_hosts_on_one_ip_never_share_a_connection(monkeypatch) -> None:
    """Pinned requests go to the same IP; a shared pool would reuse host A's
    TLS session for host B. Each host gets its own client (pool), and every
    request carries its own SNI name."""
    used: list[tuple[int, str, str]] = []

    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        transport_box: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            used.append(
                (
                    id(transport_box["t"]),
                    request.headers["host"],
                    request.extensions["sni_hostname"],
                )
            )
            return httpx.Response(200)

        transport_box["t"] = httpx.MockTransport(handler)
        kwargs["transport"] = transport_box["t"]
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    a, b = "https://hooks.a.example.com/x", "https://hooks.b.example.com/x"
    for _ in range(2):
        for url in (a, b):
            await outbox.enqueue(
                workspace="w1",
                kind="k",
                sink="webhook",
                target=url,
                payload={"data": {}},
                webhook_ref=f"test:{url}",
            )

    async def _target(item):
        return outbox._Target(url=item.target, secrets=["s3cret"])

    monkeypatch.setattr(outbox, "_webhook_target", _target)
    await outbox.process_due()

    assert len(used) == 4
    assert all(host == sni for _t, host, sni in used)
    pools_for = {host: {t for t, h, _s in used if h == host} for _t, host, _s in used}
    assert pools_for["hooks.a.example.com"].isdisjoint(pools_for["hooks.b.example.com"])


def test_safe_fetcher_pools_are_per_host_and_posts_never_keep_alive() -> None:
    from pocketpaw_ee.sites.safe_fetch import SafeFetcher

    fetcher = SafeFetcher(total_byte_cap=1)
    a = fetcher._client_for("a.example.com")
    assert fetcher._client_for("A.example.com") is a
    assert fetcher._client_for("b.example.com") is not a
    oneshot = fetcher._client_for("a.example.com", keepalive=False)
    assert oneshot is not a
    assert oneshot._transport._pool._max_keepalive_connections == 0


async def test_webhook_on_a_non_standard_port_delivers(net) -> None:
    network = net(httpx.Response(200))
    url = "https://x.example.com:8443/hook"
    saved = await notifications_service.set_delivery_config("w1", webhook_url=url, enabled=True)
    assert saved["webhook_url"] == url
    await notifications_service.create(workspace_id="w1", recipient="u2", kind="m", title="x")
    await outbox.process_due()
    row = await NotificationOutboxItem.find_one({})
    assert row.status == "sent"
    req = network.requests[0]
    assert (req.url.host, req.url.port) == ("93.184.216.34", 8443)
    assert req.headers["host"] == "x.example.com:8443"


@pytest.mark.parametrize("url", ["https://x.example.com:0/h", "https://x.example.com:70000/h"])
async def test_invalid_ports_are_refused_at_save(url) -> None:
    with pytest.raises(Forbidden):
        await notifications_service.set_delivery_config("w1", webhook_url=url, enabled=True)


async def test_a_reply_over_the_cap_is_still_a_success(net, slack_config, monkeypatch) -> None:
    monkeypatch.setattr(outbox, "_WEBHOOK_RESPONSE_CAP", 1024)
    net(httpx.Response(200, content=b"x" * 5000))
    await outbox.enqueue(**_slack_row())
    await outbox.process_due()
    assert (await NotificationOutboxItem.find_one({})).status == "sent"


async def test_email_failing_notice_is_once_even_with_concurrent_workers(email_on) -> None:
    import uuid

    from pocketpaw_ee.cloud.models.notification import Notification as _NotificationDoc
    from pocketpaw_ee.cloud.models.user import User, WorkspaceMembership

    admin = User(
        email=f"a{uuid.uuid4().hex[:6]}@x.io",
        hashed_password="x",
        is_active=True,
        is_verified=True,
        full_name="A",
        workspaces=[WorkspaceMembership(workspace="w1", role="admin")],
    )
    await admin.insert()
    await asyncio.gather(*(outbox._warn_email_failing("w1", "http 401") for _ in range(8)))
    notes = await _NotificationDoc.find({"type": outbox.EMAIL_FAILING_KIND}).to_list()
    assert len(notes) == 1
    assert "platform team has been alerted" in notes[0].body


async def test_claim_marker_gates_once_per_interval() -> None:
    hour = timedelta(hours=1)
    now = _t0()
    assert await outbox.claim_marker("k", hour, now=now) is True
    assert await outbox.claim_marker("k", hour, now=now + timedelta(minutes=5)) is False
    assert await outbox.claim_marker("k", hour, now=now + timedelta(hours=2)) is True


async def test_saving_the_config_does_not_overwrite_a_concurrent_rotation(monkeypatch) -> None:
    from pocketpaw_ee.cloud.notifications import service as svc

    await svc.set_delivery_config("w1", webhook_url=WEBHOOK_URL, enabled=True)
    stale = await svc._find_config("w1")  # read before the rotation lands
    rotated = await svc.rotate_webhook_secret("w1")

    real_find = svc._find_config
    calls = {"n": 0}

    async def stale_first(workspace_id):
        calls["n"] += 1
        return stale if calls["n"] == 1 else await real_find(workspace_id)

    monkeypatch.setattr(svc, "_find_config", stale_first)
    await svc.set_delivery_config(
        "w1", webhook_url=WEBHOOK_URL, enabled=True, routes={"m": ["webhook"]}
    )
    monkeypatch.setattr(svc, "_find_config", real_find)
    doc = await real_find("w1")
    assert doc.routes == {"m": ["webhook"]}
    assert doc.webhook_secret_rotated_at is not None and doc.webhook_secret_prev_enc
    _url, secrets_now = await svc.webhook_target("w1")
    assert secrets_now[0] == rotated["webhook_secret"]
