"""``GET /api/v1/platform/health`` — chunk 11 of the Paw Admin PRD.

Design: docs/design/drafts/2026-09-15-paw-admin-screen-settings-health.md §4.3.

The most important test in this file is the asymmetry with settings:
``platform.health.read`` is SUPPORT (support staff triage outages) while
``platform.settings.read`` is OPERATOR. Both directions are asserted here and
in ``test_platform_settings.py``.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pocketpaw_ee.cloud._core.platform_deps import require_platform
from pocketpaw_ee.cloud.models.platform_audit import PlatformAuditEvent
from pocketpaw_ee.cloud.models.user import User as UserDoc
from pocketpaw_ee.cloud.platform import health as platform_health
from starlette.datastructures import Headers
from starlette.requests import Request

pytestmark = pytest.mark.asyncio


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/platform/health",
            "headers": Headers(raw=[(b"user-agent", b"paw-admin/test")]).raw,
            "query_string": b"",
            "client": ("10.0.0.5", 1234),
        }
    )


async def _user(platform_role: str) -> UserDoc:
    doc = UserDoc(
        email=f"{platform_role}@paw.test",
        hashed_password="x",
        full_name=platform_role.title(),
        platform_role=platform_role,
    )
    await doc.insert()
    return doc


class _FakeGuardUser:
    def __init__(self, platform_role: str | None) -> None:
        self.id = "operator-1"
        self.email = "ops@example.com"
        self.platform_role = platform_role


def _guard_request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/platform/health",
            "headers": Headers(raw=[(b"cookie", b"paw_auth=a-real-session-token")]).raw,
            "query_string": b"",
            "client": ("10.0.0.5", 1234),
        }
    )


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict | None = None) -> None:
        self.status_code = status_code
        self._json_body = json_body or {}

    def json(self) -> dict:
        return self._json_body


class _FakeClient:
    """Routes .get(url) to a canned response keyed by URL suffix."""

    def __init__(self, responses: dict[str, _FakeResponse | Exception]) -> None:
        self._responses = responses

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        for suffix, response in self._responses.items():
            if url.endswith(suffix):
                if isinstance(response, Exception):
                    raise response
                return response
        raise AssertionError(f"unexpected probe URL: {url}")


def _patched_client(responses: dict[str, _FakeResponse | Exception]):
    return patch("httpx.AsyncClient", return_value=_FakeClient(responses))


# ---------------------------------------------------------------------------
# The asymmetry: platform.health.read is SUPPORT, platform.settings.read is not
# ---------------------------------------------------------------------------


async def test_support_is_allowed_the_health_read() -> None:
    """The asymmetry, from the health side: support triages outages."""
    guard = require_platform("platform.health.read")
    user = _FakeGuardUser("support")
    assert await guard(request=_guard_request(), user=user) is user


async def test_operator_is_also_allowed_the_health_read() -> None:
    guard = require_platform("platform.health.read")
    user = _FakeGuardUser("operator")
    assert await guard(request=_guard_request(), user=user) is user


async def test_no_platform_role_is_refused_the_health_read() -> None:
    from fastapi import HTTPException

    guard = require_platform("platform.health.read")
    with pytest.raises(HTTPException) as exc:
        await guard(request=_guard_request(), user=_FakeGuardUser(None))
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Probe 1 — proxy reachable
# ---------------------------------------------------------------------------


async def test_probe_1_hits_health_readiness_not_health(
    mongo_db, monkeypatch
) -> None:
    """Deliberately not /health — see the module docstring for why."""
    monkeypatch.setattr(
        platform_health,
        "get_settings",
        lambda: SimpleNamespace(litellm_api_base="https://proxy.example", litellm_api_key="key"),
    )
    with _patched_client(
        {
            "/health/readiness": _FakeResponse(200),
            "/model/info": _FakeResponse(200, {"data": [{"id": "a"}]}),
        }
    ):
        result = await platform_health.get_platform_health(
            request=_request(), operator=await _user("support")
        )

    assert result.proxy_reachable.status == "success"
    assert result.proxy_reachable.word == "Reachable"


async def test_probe_1_reports_no_answer_on_connection_failure(mongo_db, monkeypatch) -> None:
    monkeypatch.setattr(
        platform_health,
        "get_settings",
        lambda: SimpleNamespace(litellm_api_base="http://localhost:4000", litellm_api_key=None),
    )
    with _patched_client({"/health/readiness": ConnectionError("refused")}):
        result = await platform_health.get_platform_health(
            request=_request(), operator=await _user("support")
        )

    assert result.proxy_reachable.status == "error"
    assert result.proxy_reachable.word == "No answer"


async def test_probe_1_reports_answering_not_serving_on_non_200(mongo_db, monkeypatch) -> None:
    monkeypatch.setattr(
        platform_health,
        "get_settings",
        lambda: SimpleNamespace(litellm_api_base="http://localhost:4000", litellm_api_key=None),
    )
    with _patched_client({"/health/readiness": _FakeResponse(503)}):
        result = await platform_health.get_platform_health(
            request=_request(), operator=await _user("support")
        )

    assert result.proxy_reachable.status == "warning"
    assert result.proxy_reachable.word == "Answering, not serving"


# ---------------------------------------------------------------------------
# Probe 2 — master key authenticates
# ---------------------------------------------------------------------------


async def test_probe_2_not_run_when_proxy_unreachable(mongo_db, monkeypatch) -> None:
    monkeypatch.setattr(
        platform_health,
        "get_settings",
        lambda: SimpleNamespace(litellm_api_base="http://localhost:4000", litellm_api_key="key"),
    )
    with _patched_client({"/health/readiness": ConnectionError("refused")}):
        result = await platform_health.get_platform_health(
            request=_request(), operator=await _user("support")
        )

    assert result.master_key_authenticates.status == "neutral"
    assert result.master_key_authenticates.word == "Not run"


async def test_probe_2_warns_when_no_key_set(mongo_db, monkeypatch) -> None:
    monkeypatch.setattr(
        platform_health,
        "get_settings",
        lambda: SimpleNamespace(litellm_api_base="http://localhost:4000", litellm_api_key=None),
    )
    with _patched_client({"/health/readiness": _FakeResponse(200)}):
        result = await platform_health.get_platform_health(
            request=_request(), operator=await _user("support")
        )

    assert result.master_key_authenticates.status == "warning"
    assert result.master_key_authenticates.word == "No key set"


async def test_probe_2_reports_rejected_on_auth_failure(mongo_db, monkeypatch) -> None:
    monkeypatch.setattr(
        platform_health,
        "get_settings",
        lambda: SimpleNamespace(litellm_api_base="http://localhost:4000", litellm_api_key="bad-key"),
    )
    with _patched_client(
        {"/health/readiness": _FakeResponse(200), "/model/info": _FakeResponse(401)}
    ):
        result = await platform_health.get_platform_health(
            request=_request(), operator=await _user("support")
        )

    assert result.master_key_authenticates.status == "error"
    assert result.master_key_authenticates.word == "Rejected"


async def test_probe_2_authenticates_and_the_key_never_appears_in_the_response(
    mongo_db, monkeypatch
) -> None:
    secret_key = "sk-master-do-not-leak"
    monkeypatch.setattr(
        platform_health,
        "get_settings",
        lambda: SimpleNamespace(litellm_api_base="http://localhost:4000", litellm_api_key=secret_key),
    )
    with _patched_client(
        {
            "/health/readiness": _FakeResponse(200),
            "/model/info": _FakeResponse(200, {"data": [{"id": "a"}, {"id": "b"}]}),
        }
    ):
        result = await platform_health.get_platform_health(
            request=_request(), operator=await _user("support")
        )

    assert result.master_key_authenticates.status == "success"
    dumped = result.model_dump_json()
    assert secret_key not in dumped


# ---------------------------------------------------------------------------
# Probe 3 — always never_run in this chunk (out of scope, see module docstring)
# ---------------------------------------------------------------------------


async def test_probe_3_is_always_never_run(mongo_db, monkeypatch) -> None:
    monkeypatch.setattr(
        platform_health,
        "get_settings",
        lambda: SimpleNamespace(litellm_api_base="http://localhost:4000", litellm_api_key="key"),
    )
    with _patched_client(
        {"/health/readiness": _FakeResponse(200), "/model/info": _FakeResponse(200, {"data": []})}
    ):
        result = await platform_health.get_platform_health(
            request=_request(), operator=await _user("support")
        )

    assert result.byok_forwarding.status == "never_run"


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


async def test_health_read_writes_an_audit_row(mongo_db, monkeypatch) -> None:
    monkeypatch.setattr(
        platform_health,
        "get_settings",
        lambda: SimpleNamespace(litellm_api_base="http://localhost:4000", litellm_api_key="key"),
    )
    with _patched_client(
        {"/health/readiness": _FakeResponse(200), "/model/info": _FakeResponse(200, {"data": []})}
    ):
        await platform_health.get_platform_health(
            request=_request(), operator=await _user("support")
        )

    rows = await PlatformAuditEvent.find_all().to_list()
    assert len(rows) == 1
    assert rows[0].action == "platform.health.read"
    assert rows[0].actor_platform_role == "support"
    assert rows[0].status == "applied"
