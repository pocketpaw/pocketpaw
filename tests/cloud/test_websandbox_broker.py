# test_websandbox_broker.py — Code Mode private-repo broker-clone tests (CM-3c).
# Created 2026-07-16 (feat/code-mode).
#
# The broker clones a PRIVATE repo into the VM while keeping the credential
# entirely server-side. These tests pin the two guarantees that matter:
#   1. ROUTING — resolve_repo_token mints from the FIRST connection whose
#      installation can reach the repo, skips ones that can't, and returns None
#      (→ public fallback) when nothing can auth it or the repo isn't brokerable.
#   2. TOKEN ISOLATION — clone_into_vm ships the packed tree into the VM via
#      upload_bytes + tar, and the token NEVER appears in anything sent to the VM.
#
# The server-side clone+pack step (real git/tar) is behind the ``pack`` DI seam,
# so no test spawns a subprocess. The GitHub App client is a FAKE RepoAuthProvider
# (monkeypatched into the resolver), and the connection registry runs on real
# Beanie over mongomock-motor (the ``mongo_db`` fixture), so the tenant-scoped
# connection read is exercised for real.
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError
from pocketpaw_ee.cloud.codeconnect import service as codeconnect_service
from pocketpaw_ee.cloud.websandbox import broker
from pocketpaw_ee.cloud.websandbox.repoauth import ProviderId, ScopedRepoToken

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws-1"
_USER = "user-1"
_EXP = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
_CLEAN = "https://github.com/owner/repo.git"


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


class _FakeProvider:
    """A RepoAuthProvider whose mint succeeds only for whitelisted installations."""

    provider_id = ProviderId.GITHUB

    def __init__(self, *, mintable: set[str], token: str = "tok-SECRET") -> None:
        self.mintable = mintable
        self.token = token
        self.calls: list[tuple[str, str]] = []

    async def mint_repo_token(self, connection_id, repo, *, scopes=None, now=None):  # noqa: ANN001
        self.calls.append((connection_id, repo))
        if connection_id not in self.mintable:
            raise CloudError(502, "websandbox.installation_token_failed", "no access")
        return ScopedRepoToken(
            provider=ProviderId.GITHUB,
            token=self.token,
            expires_at=_EXP,
            repo=repo,
            scopes={},
        )

    async def list_repositories(self, connection_id, *, now=None):  # noqa: ANN001
        return []

    def upstream_clone_url(self, repo):  # noqa: ANN001
        return f"https://github.com/{repo}.git"


class _FakeVM:
    """Records the file uploads + commands the broker sends into the sandbox."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes, str]] = []
        self.execs: list[str] = []

    async def upload_bytes(self, sandbox_id, data, remote_path):  # noqa: ANN001
        self.uploads.append((sandbox_id, data, remote_path))

    async def execute_command(self, sandbox_id, command, **kwargs):  # noqa: ANN001
        self.execs.append(command)
        return None


def _token(tok: str = "tok-SECRET", repo: str = "owner/repo") -> ScopedRepoToken:
    return ScopedRepoToken(
        provider=ProviderId.GITHUB, token=tok, expires_at=_EXP, repo=repo, scopes={}
    )


# ---------------------------------------------------------------------------
# repo_full_name.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/owner/repo.git", "owner/repo"),
        ("https://github.com/owner/repo", "owner/repo"),
        ("https://github.com/owner/repo/", "owner/repo"),
        ("http://github.com/acme/api.git", "acme/api"),
        ("git@github.com:acme/api.git", None),  # non-http
        ("https://x-access-token:t@github.com/o/r.git", None),  # embedded creds
        ("https://github.com/onlyowner", None),  # too few segments
        ("not a url", None),
    ],
)
def test_repo_full_name(url: str, expected: str | None) -> None:
    assert broker.repo_full_name(url) == expected


# ---------------------------------------------------------------------------
# resolve_repo_token — routing.
# ---------------------------------------------------------------------------


async def test_resolve_none_when_repo_not_brokerable(monkeypatch) -> None:
    # A non-github-shaped URL never even resolves a provider.
    called = False

    def _spy(_p):  # noqa: ANN001
        nonlocal called
        called = True
        return _FakeProvider(mintable=set())

    monkeypatch.setattr(broker, "get_repo_auth_provider", _spy)
    assert await broker.resolve_repo_token(_WS, _USER, "git@github.com:o/r.git") is None
    assert called is False


async def test_resolve_none_when_provider_unconfigured(monkeypatch) -> None:
    monkeypatch.setattr(broker, "get_repo_auth_provider", lambda _p: None)
    assert await broker.resolve_repo_token(_WS, _USER, _CLEAN) is None


async def test_resolve_none_when_no_connections(monkeypatch) -> None:
    monkeypatch.setattr(
        broker, "get_repo_auth_provider", lambda _p: _FakeProvider(mintable={"any"})
    )
    assert await broker.resolve_repo_token(_WS, _USER, _CLEAN) is None


async def test_resolve_mints_from_the_connection_that_can_reach_the_repo(monkeypatch) -> None:
    # Two connections, only ``inst-can`` can reach the repo. The resolver returns
    # that connection's token — it doesn't give up on the one that can't mint.
    await codeconnect_service.save_connection(_WS, _USER, "inst-can")
    await codeconnect_service.save_connection(_WS, _USER, "inst-cannot")
    provider = _FakeProvider(mintable={"inst-can"})
    monkeypatch.setattr(broker, "get_repo_auth_provider", lambda _p: provider)

    scoped = await broker.resolve_repo_token(_WS, _USER, _CLEAN)

    assert scoped is not None
    assert scoped.token == "tok-SECRET"
    assert scoped.repo == "owner/repo"
    # The minting connection was among those tried, always for the right repo.
    assert ("inst-can", "owner/repo") in provider.calls
    assert all(repo == "owner/repo" for _cid, repo in provider.calls)


async def test_resolve_tries_every_connection_before_giving_up(monkeypatch) -> None:
    # Nothing can mint → the resolver tries ALL of the caller's connections
    # (skipping each failed mint) and only then returns None. Order-independent.
    await codeconnect_service.save_connection(_WS, _USER, "inst-1")
    await codeconnect_service.save_connection(_WS, _USER, "inst-2")
    provider = _FakeProvider(mintable=set())  # nothing mints
    monkeypatch.setattr(broker, "get_repo_auth_provider", lambda _p: provider)

    assert await broker.resolve_repo_token(_WS, _USER, _CLEAN) is None
    assert {cid for cid, _repo in provider.calls} == {"inst-1", "inst-2"}


async def test_resolve_skips_other_provider_connections(monkeypatch) -> None:
    # A non-github connection is skipped by the provider filter — never minted.
    await codeconnect_service.save_connection(_WS, _USER, "goog-1", provider="google")
    provider = _FakeProvider(mintable={"goog-1"})
    monkeypatch.setattr(broker, "get_repo_auth_provider", lambda _p: provider)
    assert await broker.resolve_repo_token(_WS, _USER, _CLEAN) is None
    assert provider.calls == []


async def test_resolve_scopes_to_the_caller(monkeypatch) -> None:
    # A connection owned by another user in the same workspace is never used.
    await codeconnect_service.save_connection(_WS, "other-user", "inst-x")
    provider = _FakeProvider(mintable={"inst-x"})
    monkeypatch.setattr(broker, "get_repo_auth_provider", lambda _p: provider)
    assert await broker.resolve_repo_token(_WS, _USER, _CLEAN) is None
    assert provider.calls == []


# ---------------------------------------------------------------------------
# clone_into_vm — token isolation.
# ---------------------------------------------------------------------------


async def test_clone_into_vm_ships_tar_and_never_leaks_the_token() -> None:
    seen: dict = {}

    async def fake_pack(token, clean_url, branch):  # noqa: ANN001
        seen["clean_url"] = clean_url
        seen["branch"] = branch
        return b"TARBYTES"

    vm = _FakeVM()
    await broker.clone_into_vm(
        vm,
        "dtn-1",
        _token(),
        "/home/daytona",
        clean_url=_CLEAN,
        branch="main",
        pack=fake_pack,
    )

    # The packer got the CLEAN url (it tokenizes internally, server-side).
    assert seen == {"clean_url": _CLEAN, "branch": "main"}
    # The exact tar bytes were uploaded to the staging path, then extracted.
    assert vm.uploads == [("dtn-1", b"TARBYTES", "/tmp/ws-broker.tgz")]
    assert any("tar -xzf /tmp/ws-broker.tgz" in c for c in vm.execs)

    # THE INVARIANT: the token appears in NOTHING sent to the VM.
    blob = repr(vm.uploads) + repr(vm.execs)
    assert "tok-SECRET" not in blob


async def test_clone_into_vm_rejects_oversized_repo(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_WEBSANDBOX_BROKER_MAX_MB", "0.000001")  # ~0 bytes

    async def fake_pack(token, clean_url, branch):  # noqa: ANN001
        return b"x" * 4096

    vm = _FakeVM()
    with pytest.raises(CloudError) as exc:
        await broker.clone_into_vm(
            vm, "dtn-1", _token(), "/home/daytona", clean_url=_CLEAN, pack=fake_pack
        )
    assert exc.value.code == "websandbox.broker_repo_too_large"
    # Nothing was shipped to the VM.
    assert vm.uploads == []
    assert vm.execs == []


async def test_clone_into_vm_wraps_pack_failure() -> None:
    async def boom_pack(token, clean_url, branch):  # noqa: ANN001
        raise RuntimeError("git exploded")

    vm = _FakeVM()
    with pytest.raises(CloudError) as exc:
        await broker.clone_into_vm(
            vm, "dtn-1", _token(), "/home/daytona", clean_url=_CLEAN, pack=boom_pack
        )
    assert exc.value.code == "websandbox.broker_clone_failed"
    assert vm.uploads == []


async def test_clone_into_vm_surfaces_cloud_error_from_pack() -> None:
    async def cloud_boom(token, clean_url, branch):  # noqa: ANN001
        raise CloudError(503, "websandbox.broker_git_unavailable", "no git")

    vm = _FakeVM()
    with pytest.raises(CloudError) as exc:
        await broker.clone_into_vm(
            vm, "dtn-1", _token(), "/home/daytona", clean_url=_CLEAN, pack=cloud_boom
        )
    # A CloudError from the packer is preserved verbatim (not re-wrapped).
    assert exc.value.code == "websandbox.broker_git_unavailable"


# ---------------------------------------------------------------------------
# _authenticated_url + _redact — the token-handling primitives.
# ---------------------------------------------------------------------------


def test_authenticated_url_injects_github_scheme() -> None:
    url = broker._authenticated_url(ProviderId.GITHUB, _CLEAN, "tok123")
    assert url == "https://x-access-token:tok123@github.com/owner/repo.git"


def test_authenticated_url_rejects_unsupported_provider() -> None:
    with pytest.raises(CloudError) as exc:
        broker._authenticated_url(ProviderId.GOOGLE, _CLEAN, "tok123")
    assert exc.value.code == "websandbox.broker_provider_unsupported"


def test_redact_scrubs_the_secret() -> None:
    assert broker._redact("clone https://x-access-token:abc@h failed", "abc") == (
        "clone https://x-access-token:***@h failed"
    )
    assert broker._redact("no secret here", "") == "no secret here"
