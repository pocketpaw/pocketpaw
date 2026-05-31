# test_skills_router_from_url.py — HTTP + service tests for the
# URL-fetch variant of the API-doc skill install (POST
# /api/v1/skills/api-doc-from-url).
# 2026-05-23 — covers the cloud-safe URL fetch path: a successful
# fetch installs the skill, http:// is rejected, a private / loopback
# host is rejected via the SSRF guard, an oversized response is
# rejected, an upstream error returns 422, and the auth seams hold.
# The httpx call is monkeypatched throughout so no real network I/O
# fires during the test run.

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import current_active_user
from pocketpaw_ee.cloud.license import require_license
from pocketpaw_ee.cloud.skills.router import router as skills_router


def _minimal_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "Remote API"},
        "servers": [{"url": "https://remote.example.com"}],
        "paths": {
            "/things": {
                "get": {
                    "tags": ["Things"],
                    "summary": "List things",
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }


def _fake_user(user_id: str = "u1", workspace_id: str | None = "w1") -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        active_workspace=workspace_id,
        workspaces=[SimpleNamespace(workspace=workspace_id, role="admin")] if workspace_id else [],
    )


def _build_app(*, workspace_id: str | None = "w1", monkeypatch=None) -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(skills_router)
    app.dependency_overrides[require_license] = lambda: None

    user = _fake_user(workspace_id=workspace_id)

    async def _fake_user_dep():
        return user

    app.dependency_overrides[current_active_user] = _fake_user_dep

    if monkeypatch is not None:
        from pocketpaw_ee.cloud._core import deps as core_deps

        monkeypatch.setattr(core_deps, "check_workspace_action", lambda *a, **k: None)

    return app


def _patch_ssrf_allow(monkeypatch) -> None:
    """Bypass the SSRF guard so tests don't need real DNS resolution.

    The guard's behavior is covered in
    ``test_install_api_doc_from_url_rejects_private_host`` below,
    where we make it raise to assert the rejection path.
    """
    from pocketpaw_ee.cloud.pockets import _http_guard

    async def _allow(_hostname: str) -> None:
        return None

    monkeypatch.setattr(_http_guard, "_assert_host_external", _allow)


def _patch_httpx_get(monkeypatch, *, body: bytes, status: int = 200) -> None:
    """Stub httpx.AsyncClient.get so the service receives a known response."""

    class _StubResponse:
        def __init__(self) -> None:
            self.status_code = status
            self.content = body

    class _StubClient:
        def __init__(self, *_a, **_k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a) -> None:
            return None

        async def get(self, _url: str) -> _StubResponse:
            return _StubResponse()

    monkeypatch.setattr(httpx, "AsyncClient", _StubClient)


@pytest_asyncio.fixture
async def client(monkeypatch, tmp_path) -> AsyncClient:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = _build_app(monkeypatch=monkeypatch)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


async def test_install_from_url_installs_skill(client: AsyncClient, monkeypatch, tmp_path) -> None:
    """A valid https URL returning a real OpenAPI spec installs the skill."""
    _patch_ssrf_allow(monkeypatch)
    _patch_httpx_get(monkeypatch, body=json.dumps(_minimal_spec()).encode("utf-8"))

    r = await client.post(
        "/skills/api-doc-from-url",
        json={"url": "https://remote.example.com/openapi.json"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["slug"] == "api-remote-example-com"

    skill_md = tmp_path / ".pocketpaw" / "skills" / "api-remote-example-com" / "SKILL.md"
    assert skill_md.is_file()
    assert "`GET /things`" in skill_md.read_text(encoding="utf-8")


async def test_install_from_url_accepts_yaml_response(client: AsyncClient, monkeypatch) -> None:
    """YAML responses are parsed and installed too."""
    import yaml

    _patch_ssrf_allow(monkeypatch)
    _patch_httpx_get(monkeypatch, body=yaml.safe_dump(_minimal_spec()).encode("utf-8"))

    r = await client.post(
        "/skills/api-doc-from-url",
        json={"url": "https://remote.example.com/openapi.yaml"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["slug"] == "api-remote-example-com"


# ---------------------------------------------------------------------------
# URL / scheme rejections
# ---------------------------------------------------------------------------


async def test_install_from_url_rejects_http_scheme(client: AsyncClient, monkeypatch) -> None:
    """http:// URLs are rejected before any DNS / fetch — https-only."""
    _patch_ssrf_allow(monkeypatch)  # ensure SSRF wouldn't be the first reject

    r = await client.post(
        "/skills/api-doc-from-url",
        json={"url": "http://remote.example.com/openapi.json"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "skills.api_doc.bad_scheme"


async def test_install_from_url_rejects_malformed_url(client: AsyncClient) -> None:
    """Pydantic's HttpUrl catches obviously-malformed URLs at parse time."""
    r = await client.post(
        "/skills/api-doc-from-url",
        json={"url": "not-a-url"},
    )
    assert r.status_code == 422  # FastAPI body-validation 422


# ---------------------------------------------------------------------------
# SSRF guard
# ---------------------------------------------------------------------------


async def test_install_from_url_rejects_private_host(client: AsyncClient, monkeypatch) -> None:
    """A hostname that resolves to a private / loopback IP is rejected
    before any fetch fires — the SSRF guard is the trust boundary."""
    from pocketpaw_ee.cloud.pockets import _http_guard

    async def _reject(_hostname: str) -> None:
        raise _http_guard._GuardError(
            "backend host resolves to an internal address",
            code="bad_host",
        )

    monkeypatch.setattr(_http_guard, "_assert_host_external", _reject)

    r = await client.post(
        "/skills/api-doc-from-url",
        json={"url": "https://internal.corp/openapi.json"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "skills.api_doc.bad_host"


# ---------------------------------------------------------------------------
# Response-handling rejections
# ---------------------------------------------------------------------------


async def test_install_from_url_rejects_redirect(client: AsyncClient, monkeypatch) -> None:
    """A 3xx response is rejected — redirects are off, so the caller
    must pass the final URL directly."""
    _patch_ssrf_allow(monkeypatch)
    _patch_httpx_get(monkeypatch, body=b"", status=302)

    r = await client.post(
        "/skills/api-doc-from-url",
        json={"url": "https://remote.example.com/openapi.json"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "skills.api_doc.fetch_redirect"


async def test_install_from_url_rejects_non_2xx(client: AsyncClient, monkeypatch) -> None:
    """A 4xx / 5xx response surfaces as fetch_failed with the status."""
    _patch_ssrf_allow(monkeypatch)
    _patch_httpx_get(monkeypatch, body=b"not found", status=404)

    r = await client.post(
        "/skills/api-doc-from-url",
        json={"url": "https://remote.example.com/openapi.json"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "skills.api_doc.fetch_failed"


async def test_install_from_url_rejects_oversized_response(
    client: AsyncClient, monkeypatch
) -> None:
    """A response body larger than the 2 MB cap is rejected."""
    _patch_ssrf_allow(monkeypatch)
    huge = b'{"_pad": "' + (b"x" * (2 * 1024 * 1024 + 1)) + b'"}'
    _patch_httpx_get(monkeypatch, body=huge)

    r = await client.post(
        "/skills/api-doc-from-url",
        json={"url": "https://remote.example.com/openapi.json"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "skills.api_doc.too_large"


async def test_install_from_url_rejects_unparseable(client: AsyncClient, monkeypatch) -> None:
    """A response that is neither JSON nor YAML is rejected."""
    _patch_ssrf_allow(monkeypatch)
    _patch_httpx_get(monkeypatch, body=b"{not valid: [json")

    r = await client.post(
        "/skills/api-doc-from-url",
        json={"url": "https://remote.example.com/openapi.json"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "skills.api_doc.unparseable"


async def test_install_from_url_rejects_spec_with_no_paths(
    client: AsyncClient, monkeypatch
) -> None:
    """A fetched spec with no ``paths`` is rejected by the installer."""
    _patch_ssrf_allow(monkeypatch)
    _patch_httpx_get(monkeypatch, body=json.dumps({"openapi": "3.0.0"}).encode("utf-8"))

    r = await client.post(
        "/skills/api-doc-from-url",
        json={"url": "https://remote.example.com/openapi.json"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "skills.api_doc.invalid_spec"


async def test_install_from_url_rejects_network_error(client: AsyncClient, monkeypatch) -> None:
    """httpx errors (DNS, TCP, TLS, timeout) all map to fetch_failed."""
    _patch_ssrf_allow(monkeypatch)

    class _ErrorClient:
        def __init__(self, *_a, **_k) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a) -> None:
            return None

        async def get(self, _url: str):
            raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "AsyncClient", _ErrorClient)

    r = await client.post(
        "/skills/api-doc-from-url",
        json={"url": "https://remote.example.com/openapi.json"},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "skills.api_doc.fetch_failed"


# ---------------------------------------------------------------------------
# Auth seam — share with the multipart endpoint
# ---------------------------------------------------------------------------


async def test_install_from_url_unauthenticated_returns_401(tmp_path, monkeypatch) -> None:
    """Without a current_active_user override the auth chain 401s."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = FastAPI()
    add_error_handler(app)
    app.include_router(skills_router)
    app.dependency_overrides[require_license] = lambda: None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post(
            "/skills/api-doc-from-url",
            json={"url": "https://remote.example.com/openapi.json"},
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------------
# Service direct
# ---------------------------------------------------------------------------


async def test_service_install_from_url_direct(monkeypatch, tmp_path) -> None:
    """The service installs a skill when called directly (bus/job path)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _patch_ssrf_allow(monkeypatch)
    _patch_httpx_get(monkeypatch, body=json.dumps(_minimal_spec()).encode("utf-8"))

    from pocketpaw_ee.cloud.skills import service as skills_service
    from pocketpaw_ee.cloud.skills.domain import ApiDocFromUrlInstall

    body = ApiDocFromUrlInstall(
        workspace_id="w1",
        user_id="u1",
        url="https://remote.example.com/openapi.json",
    )
    out = await skills_service.install_api_doc_from_url("w1", "u1", body)
    assert out.ok is True
    assert out.slug == "api-remote-example-com"


def test_strip_url_query_drops_userinfo_and_fragment() -> None:
    """Regression for S2 (PR #1195 review): the audit-log URL must drop
    userinfo credentials AND fragment, not just the query string —
    upstreams that ship API keys in ``https://user:token@host/...``
    were previously leaking the token into ``audit.jsonl``."""
    from pocketpaw_ee.cloud.skills.service import _strip_url_query

    assert (
        _strip_url_query("https://user:apikey@vendor.com/openapi.json?k=v#frag")
        == "https://vendor.com/openapi.json"
    )
    assert _strip_url_query("https://vendor.com:8443/api") == "https://vendor.com:8443/api"


# Silence ruff unused-import nudge.
_ = pytest
