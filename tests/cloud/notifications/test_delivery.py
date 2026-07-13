# tests/cloud/notifications/test_delivery.py
# Created: 2026-07-08 (feat/external-alerting-delivery) — proves the external
# fan-out (Slack + generic webhook) that ``notifications.service.create`` now
# performs. Tests drive the REAL ``create`` path with a stubbed httpx transport
# (spy on the POST, don't mock the seam under test) so the whole seam is
# exercised end-to-end: config load -> routing -> httpx POST -> per-sink payload.
# Fire-and-forget is proven directly: a sink whose transport RAISES does not
# propagate out of ``create`` and the notification still inserts + still emits.

from __future__ import annotations

import json

import httpx
import pytest
from pocketpaw_ee.cloud._core.errors import Forbidden
from pocketpaw_ee.cloud._core.realtime.events import NotificationNew
from pocketpaw_ee.cloud.models.notification_delivery import NotificationDeliveryConfig
from pocketpaw_ee.cloud.notifications import delivery as delivery_mod
from pocketpaw_ee.cloud.notifications import service as notifications_service

pytestmark = pytest.mark.usefixtures("mongo_db")

SLACK_URL = "https://hooks.slack.com/services/T000/B000/xxx"
WEBHOOK_URL = "https://alerts.example.com/ingest"


class _Recorder:
    """Records every httpx request routed through the stubbed transport and
    returns a caller-supplied response (or raises to simulate a dead sink)."""

    def __init__(self, responder=None) -> None:
        self.requests: list[tuple[str, dict | None]] = []
        # Default responder: 200 OK.
        self._responder = responder or (lambda req: httpx.Response(200))

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((str(request.url), body))
        return self._responder(request)

    def urls(self) -> list[str]:
        return [u for u, _ in self.requests]


def _install_transport(monkeypatch: pytest.MonkeyPatch, recorder: _Recorder) -> None:
    """Patch ``httpx.AsyncClient`` so delivery's real client code runs but the
    network is served by an in-memory ``MockTransport``. This is a SPY, not a
    mock of the seam under test: real request encoding + real ``.post`` execute.
    """
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(recorder.handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


async def _set_config(**kwargs) -> None:
    """Persist a delivery config for workspace ``w1`` via the sole writer."""
    defaults: dict = {
        "slack_webhook_url": SLACK_URL,
        "webhook_url": WEBHOOK_URL,
        "enabled": True,
        "routes": {},
    }
    defaults.update(kwargs)
    await notifications_service.set_delivery_config("w1", **defaults)


# ---------------------------------------------------------------------------
# End-to-end through service.create — the real seam.
# ---------------------------------------------------------------------------


async def test_create_fans_out_to_both_sinks(monkeypatch, recording_bus) -> None:
    rec = _Recorder()
    _install_transport(monkeypatch, rec)
    await _set_config()

    out = await notifications_service.create(
        workspace_id="w1", recipient="u2", kind="mention", title="Hi", body="you were mentioned"
    )

    # Both sinks POSTed.
    assert set(rec.urls()) == {SLACK_URL, WEBHOOK_URL}
    payloads = {u: b for u, b in rec.requests}
    # Slack incoming-webhook shape: {"text": ...} carrying title + body.
    assert payloads[SLACK_URL] == {"text": "Hi\nyou were mentioned"}
    # Generic webhook: full notification payload.
    generic = payloads[WEBHOOK_URL]
    assert generic["kind"] == "mention"
    assert generic["workspace_id"] == "w1"
    assert generic["recipient_id"] == "u2"
    assert generic["title"] == "Hi"

    # The insert + realtime emit still happened.
    assert out.id
    assert len(await notifications_service.list_for_user("u2")) == 1
    assert any(isinstance(e, NotificationNew) for e in recording_bus.events)


async def test_dead_sink_never_breaks_create(monkeypatch, recording_bus) -> None:
    """A sink whose transport RAISES must not propagate out of create; the
    notification still inserts and still emits (fire-and-forget)."""

    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom", request=request)

    rec = _Recorder(responder=responder)
    _install_transport(monkeypatch, rec)
    await _set_config()

    # Must not raise even though every sink's POST blows up.
    out = await notifications_service.create(
        workspace_id="w1", recipient="u2", kind="mention", title="Hi"
    )

    # Delivery was attempted (both sinks tried) ...
    assert set(rec.urls()) == {SLACK_URL, WEBHOOK_URL}
    # ... but the insert + emit survived the dead sinks.
    assert out.id
    assert len(await notifications_service.list_for_user("u2")) == 1
    assert any(isinstance(e, NotificationNew) for e in recording_bus.events)


async def test_no_config_means_no_delivery(monkeypatch) -> None:
    rec = _Recorder()
    _install_transport(monkeypatch, rec)
    # No config saved for w1.
    out = await notifications_service.create(
        workspace_id="w1", recipient="u2", kind="mention", title="Hi"
    )
    assert out.id
    assert rec.requests == []


async def test_disabled_config_means_no_delivery(monkeypatch) -> None:
    rec = _Recorder()
    _install_transport(monkeypatch, rec)
    await _set_config(enabled=False)
    await notifications_service.create(
        workspace_id="w1", recipient="u2", kind="mention", title="Hi"
    )
    assert rec.requests == []


async def test_routes_narrow_kind_to_named_sink(monkeypatch) -> None:
    rec = _Recorder()
    _install_transport(monkeypatch, rec)
    # 'mention' routes to slack only; other kinds still deliver-all.
    await _set_config(routes={"mention": ["slack"]})

    await notifications_service.create(workspace_id="w1", recipient="u2", kind="mention", title="M")
    assert rec.urls() == [SLACK_URL]

    rec.requests.clear()
    await notifications_service.create(
        workspace_id="w1", recipient="u2", kind="task_assigned", title="T"
    )
    assert set(rec.urls()) == {SLACK_URL, WEBHOOK_URL}


async def test_delivery_scoped_per_workspace(monkeypatch) -> None:
    """A notification in a workspace with no config does not use another
    workspace's sinks."""
    rec = _Recorder()
    _install_transport(monkeypatch, rec)
    await _set_config()  # config for w1 only

    await notifications_service.create(
        workspace_id="w2", recipient="u2", kind="mention", title="Hi"
    )
    assert rec.requests == []


# ---------------------------------------------------------------------------
# set_delivery_config — the sole write path.
# ---------------------------------------------------------------------------


async def test_set_config_upserts_one_row_per_workspace() -> None:
    await notifications_service.set_delivery_config("w1", slack_webhook_url=SLACK_URL, enabled=True)
    await notifications_service.set_delivery_config("w1", webhook_url=WEBHOOK_URL, enabled=False)
    # Upsert, not duplicate insert: exactly one row for the workspace.
    count = await NotificationDeliveryConfig.find(
        NotificationDeliveryConfig.workspace == "w1"
    ).count()
    assert count == 1

    current = await notifications_service.get_delivery_config("w1")
    assert current is not None
    assert current["webhook_url"] == WEBHOOK_URL
    assert current["slack_webhook_url"] is None  # replaced by the second upsert
    assert current["enabled"] is False


async def test_set_config_rejects_unsafe_url_and_stores_nothing() -> None:
    with pytest.raises(Forbidden):
        await notifications_service.set_delivery_config(
            "w1", webhook_url="http://169.254.169.254/latest/meta-data", enabled=True
        )
    # Rejected before any write.
    assert await notifications_service.get_delivery_config("w1") is None


async def test_set_config_empty_string_clears_sink() -> None:
    await notifications_service.set_delivery_config(
        "w1", slack_webhook_url=SLACK_URL, webhook_url=WEBHOOK_URL, enabled=True
    )
    await notifications_service.set_delivery_config(
        "w1", slack_webhook_url="", webhook_url=WEBHOOK_URL, enabled=True
    )
    current = await notifications_service.get_delivery_config("w1")
    assert current["slack_webhook_url"] is None
    assert current["webhook_url"] == WEBHOOK_URL


# ---------------------------------------------------------------------------
# is_safe_webhook_url — SSRF baseline.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,safe",
    [
        ("https://hooks.slack.com/services/x", True),
        ("https://alerts.example.com/ingest", True),
        ("http://alerts.example.com/ingest", False),  # not https
        ("https://localhost/x", False),  # forbidden hostname
        ("https://127.0.0.1/x", False),  # loopback literal IP
        ("https://169.254.169.254/x", False),  # link-local (cloud metadata)
        ("https://10.0.0.5/x", False),  # private literal IP
        ("https://metadata.google.internal/x", False),  # forbidden hostname
        ("", False),
        (None, False),
    ],
)
def test_is_safe_webhook_url(url, safe) -> None:
    assert delivery_mod.is_safe_webhook_url(url) is safe


# ---------------------------------------------------------------------------
# SSRF encoding-bypass regression — the old guard only ran
# ``ipaddress.ip_address(hostname)`` and treated its ValueError as "not an IP",
# so alternate-encoding hosts (decimal / hex / octal integer, short-dotted)
# sailed through as safe while getaddrinfo / httpx resolve them to metadata /
# loopback. Each of these MUST now be rejected; a normal https host stays safe.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://2852039166/",  # decimal int == 169.254.169.254 (cloud metadata)
        "https://2130706433/",  # decimal int == 127.0.0.1
        "https://0x7f000001/",  # hex int == 127.0.0.1
        "https://127.1/",  # short-dotted == 127.0.0.1
        "https://017700000001/",  # leading-zero octal int == 127.0.0.1
    ],
)
def test_is_safe_webhook_url_rejects_encoded_ssrf(url) -> None:
    assert delivery_mod.is_safe_webhook_url(url) is False


def test_is_safe_webhook_url_allows_normal_https_host() -> None:
    assert delivery_mod.is_safe_webhook_url("https://hooks.slack.com/services/T0/B0/xxx") is True
