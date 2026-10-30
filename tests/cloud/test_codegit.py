# test_codegit.py — Code Mode git smart-HTTP proxy tests (CM-3d).
# Created 2026-07-16 (feat/code-mode).
#
# The proxy lets the VM push/fetch WITHOUT the GitHub token entering the VM. These
# tests pin the guarantees:
#   * TICKET — sign/verify roundtrips; a tampered/expired/garbage ticket is a clean
#     reject (the VM→broker credential, distinct from the GitHub token).
#   * PROXY — a request is forwarded to the right github.com URL with the minted
#     token injected (replacing the incoming basic-auth ticket), only the two git
#     services are allowed, the ticket's repo is enforced, and — the load-bearing
#     invariant — the GitHub token appears in NOTHING returned to the VM.
#   * WIRE — the VM's origin is repointed at the proxy only when a public backend
#     URL is reachable; a localhost/unset URL skips wiring (clone still usable).
#
# The GitHub token mint is monkeypatched (no real GitHub) and the httpx client is a
# fake (no network), so nothing here touches an external service.
from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from pocketpaw_ee.cloud._core.errors import BadRequest, Forbidden
from pocketpaw_ee.cloud.codegit import proxy, wire
from pocketpaw_ee.cloud.codegit.router import _ticket_from_basic_auth
from pocketpaw_ee.cloud.codegit.ticket import TicketClaims, sign_ticket, verify_ticket
from pocketpaw_ee.cloud.websandbox.repoauth import ProviderId, ScopedRepoToken

_WS = "ws-1"
_USER = "user-1"
_SBX = "sbx-1"
_REPO = "owner/repo"
_GH_TOKEN = "gh-SECRET-TOKEN"


# ---------------------------------------------------------------------------
# ticket.
# ---------------------------------------------------------------------------


def test_ticket_roundtrips() -> None:
    token = sign_ticket(_WS, _USER, _SBX, _REPO)
    claims = verify_ticket(token)
    assert claims == TicketClaims(workspace_id=_WS, user_id=_USER, sandbox_id=_SBX, repo=_REPO)


def test_ticket_rejects_tampered_or_garbage() -> None:
    token = sign_ticket(_WS, _USER, _SBX, _REPO)
    assert verify_ticket(token + "x") is None
    assert verify_ticket("not-a-jwt") is None
    assert verify_ticket("") is None


def test_ticket_rejects_expired() -> None:
    past = datetime.now(UTC) - timedelta(days=2)  # older than the 24h lifetime
    token = sign_ticket(_WS, _USER, _SBX, _REPO, now=past)
    assert verify_ticket(token) is None


# ---------------------------------------------------------------------------
# proxy — fakes.
# ---------------------------------------------------------------------------


class _FakeUpstream:
    def __init__(self, status: int, headers: dict[str, str], chunks: list[bytes]) -> None:
        self.status_code = status
        self.headers = headers
        self._chunks = chunks
        self.closed = False

    async def aiter_raw(self):
        for c in self._chunks:
            yield c

    async def aclose(self) -> None:
        self.closed = True


class _FakeHttpClient:
    def __init__(self, upstream: _FakeUpstream) -> None:
        self.upstream = upstream
        self.built: dict | None = None
        self.closed = False

    def build_request(self, method, url, headers=None, content=None):  # noqa: ANN001
        self.built = {"method": method, "url": url, "headers": headers, "content": content}
        return self.built

    async def send(self, request, stream=False):  # noqa: ANN001
        return self.upstream

    async def aclose(self) -> None:
        self.closed = True


def _claims(repo: str = _REPO) -> TicketClaims:
    return TicketClaims(workspace_id=_WS, user_id=_USER, sandbox_id=_SBX, repo=repo)


def _mint_ok(monkeypatch) -> None:
    async def _fake(ws, user, repo_url, **kw):  # noqa: ANN001
        return ScopedRepoToken(
            provider=ProviderId.GITHUB,
            token=_GH_TOKEN,
            expires_at=datetime(2026, 7, 16, tzinfo=UTC),
            repo=_REPO,
            scopes={},
        )

    monkeypatch.setattr(proxy.broker, "resolve_repo_token", _fake)


async def _drain(resp) -> bytes:  # noqa: ANN001
    return b"".join([c async for c in resp.body_iterator])


# ---------------------------------------------------------------------------
# proxy — happy paths + the token-isolation invariant.
# ---------------------------------------------------------------------------


async def test_proxy_upload_pack_injects_token_and_streams_back(monkeypatch) -> None:
    _mint_ok(monkeypatch)
    up = _FakeUpstream(
        200,
        {
            "Content-Type": "application/x-git-upload-pack-result",
            "Transfer-Encoding": "chunked",  # hop-by-hop — must be dropped
            "Content-Length": "8",  # must be dropped (we re-stream)
        },
        [b"PACK", b"DATA"],
    )
    client = _FakeHttpClient(up)

    resp = await proxy.proxy_git(
        claims=_claims(),
        owner="owner",
        repo="repo",
        git_path="git-upload-pack",
        service="git-upload-pack",
        method="POST",
        request_headers={
            "content-type": "application/x-git-upload-pack-request",
            "git-protocol": "version=2",
            # The incoming ticket — must NOT be forwarded upstream.
            "authorization": "Basic dXNlcjp0aWNrZXQ=",
            "host": "backend.example",
        },
        request_body=b"REQ",
        http_client=client,
    )

    # Forwarded to the right github URL with the MINTED token, not the ticket.
    assert client.built["url"] == "https://github.com/owner/repo.git/git-upload-pack"
    assert client.built["headers"]["Authorization"] == f"token {_GH_TOKEN}"
    assert all(not v.startswith("Basic ") for v in client.built["headers"].values())
    # Only whitelisted request headers forwarded (no host).
    assert "host" not in {k.lower() for k in client.built["headers"]}
    assert client.built["headers"]["git-protocol"] == "version=2"

    # Response streams back; hop-by-hop/length headers dropped, content-type kept.
    body = await _drain(resp)
    assert body == b"PACKDATA"
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/x-git-upload-pack-result"
    lowered = {k.lower() for k in resp.headers}
    assert "transfer-encoding" not in lowered
    assert "content-length" not in lowered

    # THE INVARIANT: the GitHub token is in NOTHING returned to the VM.
    assert _GH_TOKEN not in body.decode()
    assert all(_GH_TOKEN not in v for v in resp.headers.values())
    assert up.closed  # upstream response was closed after streaming


async def test_proxy_info_refs_carries_service_query(monkeypatch) -> None:
    _mint_ok(monkeypatch)
    up = _FakeUpstream(
        200, {"Content-Type": "application/x-git-upload-pack-advertisement"}, [b"refs"]
    )
    client = _FakeHttpClient(up)

    resp = await proxy.proxy_git(
        claims=_claims(),
        owner="owner",
        repo="repo",
        git_path="info/refs",
        service="git-upload-pack",
        method="GET",
        request_headers={"git-protocol": "version=2"},
        request_body=b"",
        query="service=git-upload-pack",
        http_client=client,
    )

    assert client.built["url"] == (
        "https://github.com/owner/repo.git/info/refs?service=git-upload-pack"
    )
    assert await _drain(resp) == b"refs"


async def test_proxy_allows_receive_pack(monkeypatch) -> None:
    _mint_ok(monkeypatch)
    client = _FakeHttpClient(_FakeUpstream(200, {"Content-Type": "x"}, [b"ok"]))
    resp = await proxy.proxy_git(
        claims=_claims(),
        owner="owner",
        repo="repo",
        git_path="git-receive-pack",
        service="git-receive-pack",
        method="POST",
        request_headers={},
        request_body=b"PACK",
        http_client=client,
    )
    assert resp.status_code == 200
    assert client.built["url"] == "https://github.com/owner/repo.git/git-receive-pack"


# ---------------------------------------------------------------------------
# proxy — rejections.
# ---------------------------------------------------------------------------


async def test_proxy_rejects_unknown_service(monkeypatch) -> None:
    _mint_ok(monkeypatch)
    with pytest.raises(BadRequest) as exc:
        await proxy.proxy_git(
            claims=_claims(),
            owner="owner",
            repo="repo",
            git_path="info/refs",
            service="git-secret-service",
            method="GET",
            request_headers={},
            request_body=b"",
            http_client=_FakeHttpClient(_FakeUpstream(200, {}, [])),
        )
    assert exc.value.code == "codegit.bad_service"


async def test_proxy_rejects_repo_the_ticket_does_not_cover(monkeypatch) -> None:
    _mint_ok(monkeypatch)
    with pytest.raises(Forbidden) as exc:
        await proxy.proxy_git(
            claims=_claims("owner/OTHER"),  # ticket is for a different repo
            owner="owner",
            repo="repo",
            git_path="git-upload-pack",
            service="git-upload-pack",
            method="POST",
            request_headers={},
            request_body=b"",
            http_client=_FakeHttpClient(_FakeUpstream(200, {}, [])),
        )
    assert exc.value.code == "codegit.repo_mismatch"


async def test_proxy_rejects_malformed_segments(monkeypatch) -> None:
    _mint_ok(monkeypatch)
    with pytest.raises(BadRequest) as exc:
        await proxy.proxy_git(
            claims=_claims("../evil/repo"),
            owner="../evil",
            repo="repo",
            git_path="git-upload-pack",
            service="git-upload-pack",
            method="POST",
            request_headers={},
            request_body=b"",
            http_client=_FakeHttpClient(_FakeUpstream(200, {}, [])),
        )
    assert exc.value.code == "codegit.bad_repo"


async def test_proxy_forbidden_when_no_connection_can_mint(monkeypatch) -> None:
    async def _none(ws, user, repo_url, **kw):  # noqa: ANN001
        return None

    monkeypatch.setattr(proxy.broker, "resolve_repo_token", _none)
    with pytest.raises(Forbidden) as exc:
        await proxy.proxy_git(
            claims=_claims(),
            owner="owner",
            repo="repo",
            git_path="git-upload-pack",
            service="git-upload-pack",
            method="POST",
            request_headers={},
            request_body=b"",
            http_client=_FakeHttpClient(_FakeUpstream(200, {}, [])),
        )
    assert exc.value.code == "codegit.no_repo_access"


# ---------------------------------------------------------------------------
# router — basic-auth ticket extraction.
# ---------------------------------------------------------------------------


def _basic(user: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


def test_ticket_from_basic_auth_recovers_claims() -> None:
    ticket = sign_ticket(_WS, _USER, _SBX, _REPO)
    req = SimpleNamespace(headers={"authorization": _basic("x-paw-git", ticket)})
    assert _ticket_from_basic_auth(req) == _claims()


def test_ticket_from_basic_auth_rejects_missing_or_bad() -> None:
    assert _ticket_from_basic_auth(SimpleNamespace(headers={})) is None
    assert _ticket_from_basic_auth(SimpleNamespace(headers={"authorization": "Bearer xyz"})) is None
    assert (
        _ticket_from_basic_auth(
            SimpleNamespace(headers={"authorization": _basic("x", "not-a-ticket")})
        )
        is None
    )


# ---------------------------------------------------------------------------
# wire — remote repointing, gated on a reachable public URL.
# ---------------------------------------------------------------------------


class _FakeDaytona:
    def __init__(self) -> None:
        self.execs: list[dict] = []

    async def execute_command(self, sandbox_id, command, **kwargs):  # noqa: ANN001
        self.execs.append({"id": sandbox_id, "command": command, "cwd": kwargs.get("cwd")})


async def test_wire_skips_without_public_url(monkeypatch) -> None:
    monkeypatch.delenv("POCKETPAW_PUBLIC_BASE_URL", raising=False)
    dt = _FakeDaytona()
    wired = await wire.wire_push_remote(dt, _SBX, _WS, _USER, _REPO, "/home/daytona")
    assert wired is False
    assert dt.execs == []


async def test_wire_skips_on_localhost(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_PUBLIC_BASE_URL", "http://localhost:8888")
    dt = _FakeDaytona()
    assert await wire.wire_push_remote(dt, _SBX, _WS, _USER, _REPO, "/home/daytona") is False
    assert dt.execs == []


async def test_wire_repoints_origin_at_proxy_with_a_valid_ticket(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_PUBLIC_BASE_URL", "https://app.pocketpaw.com")
    dt = _FakeDaytona()

    wired = await wire.wire_push_remote(dt, _SBX, _WS, _USER, _REPO, "/home/daytona")

    assert wired is True
    assert len(dt.execs) == 1
    cmd = dt.execs[0]["command"]
    assert dt.execs[0]["cwd"] == "/home/daytona"
    assert cmd.startswith("git remote set-url origin ")
    # The remote points at our proxy path for the repo, on the public host.
    assert "https://x-paw-git:" in cmd
    assert "@app.pocketpaw.com/api/v1/codegit/owner/repo" in cmd
    # The embedded credential is a VALID ticket for this (sandbox, repo).
    ticket = cmd.split("x-paw-git:", 1)[1].split("@", 1)[0]
    assert verify_ticket(ticket) == _claims()
