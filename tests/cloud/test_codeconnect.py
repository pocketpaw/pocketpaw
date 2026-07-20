# test_codeconnect.py — state + service + connect tests for the Code Mode GitHub
# connect flow (CM-3, feat/code-mode).
#
# The registry runs on real Beanie over mongomock-motor (the ``mongo_db`` fixture)
# so the tenant-filtered query paths are exercised for real; the GitHub App client
# is an injected FAKE RepoAuthProvider, so no test touches real GitHub.
#
# Covers:
#   * state sign/verify roundtrip; a tampered or expired state is rejected.
#   * save_connection is idempotent per (workspace, user, provider, installation)
#     and refreshes a newly-known account_login / avatar_url without a duplicate.
#   * list_connections never crosses the workspace OR user boundary.
#   * build_install_url embeds the slug + a verifiable state; 503 when unconfigured.
#   * handle_callback persists the connection + recovers (ws, user) from state,
#     backfills account login + avatar, still persists when that fetch fails, and
#     fails closed on a missing installation id or an unverifiable state.
#   * connect.list_connections lazily enriches a row missing its display info,
#     stops calling GitHub once complete, and falls back when the App is off.
#   * list_repositories merges + de-dupes across a caller's connections, is empty
#     with no connections, and 503s when connections exist but the provider is off.
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pocketpaw_ee.cloud._core.errors import BadRequest, CloudError
from pocketpaw_ee.cloud.codeconnect import connect, service, state
from pocketpaw_ee.cloud.websandbox.repoauth import ProviderId, RemoteRepo

pytestmark = pytest.mark.usefixtures("mongo_db")

_WS = "ws-1"
_USER = "user-1"


# ---------------------------------------------------------------------------
# Fakes.
# ---------------------------------------------------------------------------


class _FakeProvider:
    """A RepoAuthProvider that serves canned repos per installation id."""

    def __init__(self, repos_by_installation: dict[str, list[RemoteRepo]]) -> None:
        self._repos = repos_by_installation
        self.calls: list[str] = []

    async def list_repositories(self, connection_id, *, now=None):  # noqa: ANN001
        self.calls.append(connection_id)
        return list(self._repos.get(connection_id, []))


class _FakeAppClient:
    """A GitHub App client stub that serves canned account info per installation.

    Stands in for ``get_github_app_client()`` so the callback-backfill and
    lazy-enrich paths are exercised without touching real GitHub. Counts calls so a
    test can assert enrichment stops firing once the row is complete.
    """

    def __init__(self, accounts: dict[str, dict | None]) -> None:
        self._accounts = accounts
        self.calls: list[str] = []

    async def get_installation_account(self, installation_id, *, now=None):  # noqa: ANN001
        self.calls.append(installation_id)
        return self._accounts.get(installation_id)


def _repo(full_name: str, *, private: bool = True, branch: str = "main") -> RemoteRepo:
    return RemoteRepo(
        full_name=full_name,
        private=private,
        default_branch=branch,
        clone_url=f"https://github.com/{full_name}.git",
    )


# ---------------------------------------------------------------------------
# state sign / verify.
# ---------------------------------------------------------------------------


def test_state_roundtrips() -> None:
    token = state.sign_state(_WS, _USER)
    assert state.verify_state(token) == (_WS, _USER)


def test_state_rejects_tampered_or_garbage() -> None:
    token = state.sign_state(_WS, _USER)
    assert state.verify_state(token + "x") is None  # tampered signature
    assert state.verify_state("not-a-jwt") is None
    assert state.verify_state("") is None


def test_state_rejects_expired() -> None:
    past = datetime.now(UTC) - timedelta(seconds=1000)  # older than the 900s lifetime
    token = state.sign_state(_WS, _USER, now=past)
    assert state.verify_state(token) is None


# ---------------------------------------------------------------------------
# service — persistence + tenancy.
# ---------------------------------------------------------------------------


async def test_save_connection_is_idempotent_per_installation() -> None:
    first = await service.save_connection(_WS, _USER, "inst-1")
    second = await service.save_connection(_WS, _USER, "inst-1")
    assert second.id == first.id  # same row, not a duplicate
    listing = await service.list_connections(_WS, _USER)
    assert len(listing) == 1


async def test_save_connection_refreshes_account_login() -> None:
    await service.save_connection(_WS, _USER, "inst-1")
    refreshed = await service.save_connection(_WS, _USER, "inst-1", account_login="acme")
    assert refreshed.account_login == "acme"
    assert len(await service.list_connections(_WS, _USER)) == 1


async def test_save_connection_persists_and_refreshes_avatar() -> None:
    created = await service.save_connection(
        _WS, _USER, "inst-1", account_login="acme", avatar_url="https://av/1.png"
    )
    assert created.avatar_url == "https://av/1.png"
    # A later callback with a rotated avatar refreshes the same row in place.
    refreshed = await service.save_connection(
        _WS, _USER, "inst-1", account_login="acme", avatar_url="https://av/2.png"
    )
    assert refreshed.avatar_url == "https://av/2.png"
    assert len(await service.list_connections(_WS, _USER)) == 1


async def test_list_connections_never_crosses_tenant() -> None:
    await service.save_connection(_WS, _USER, "inst-1")
    await service.save_connection(_WS, "user-2", "inst-2")
    await service.save_connection("ws-2", _USER, "inst-3")
    mine = await service.list_connections(_WS, _USER)
    assert [c.installation_id for c in mine] == ["inst-1"]


# ---------------------------------------------------------------------------
# connect — install url.
# ---------------------------------------------------------------------------


def test_build_install_url_embeds_slug_and_verifiable_state(monkeypatch) -> None:
    monkeypatch.setenv("POCKETPAW_GITHUB_APP_SLUG", "my-app")
    url = connect.build_install_url(_WS, _USER)
    assert url.startswith("https://github.com/apps/my-app/installations/new?state=")
    token = url.split("state=", 1)[1]
    assert state.verify_state(token) == (_WS, _USER)


def test_build_install_url_503_when_slug_unset(monkeypatch) -> None:
    monkeypatch.delenv("POCKETPAW_GITHUB_APP_SLUG", raising=False)
    with pytest.raises(CloudError) as exc:
        connect.build_install_url(_WS, _USER)
    assert exc.value.code == "codeconnect.github_not_configured"


# ---------------------------------------------------------------------------
# connect — callback.
# ---------------------------------------------------------------------------


async def test_handle_callback_persists_and_recovers_identity() -> None:
    token = state.sign_state(_WS, _USER)
    ws, user = await connect.handle_callback("inst-9", token)
    assert (ws, user) == (_WS, _USER)
    # The connection is now persisted for that identity.
    listing = await service.list_connections(_WS, _USER)
    assert [c.installation_id for c in listing] == ["inst-9"]


async def test_handle_callback_rejects_bad_state() -> None:
    with pytest.raises(BadRequest):
        await connect.handle_callback("inst-9", "forged-state")
    # Nothing was persisted.
    assert await service.list_connections(_WS, _USER) == []


async def test_handle_callback_rejects_missing_installation() -> None:
    token = state.sign_state(_WS, _USER)
    with pytest.raises(BadRequest):
        await connect.handle_callback("", token)


async def test_handle_callback_backfills_account_login_and_avatar(monkeypatch) -> None:
    client = _FakeAppClient({"inst-9": {"login": "octo", "avatar_url": "https://av/o.png"}})
    monkeypatch.setattr(connect, "get_github_app_client", lambda: client)
    token = state.sign_state(_WS, _USER)

    await connect.handle_callback("inst-9", token)

    [conn] = await service.list_connections(_WS, _USER)
    assert conn.account_login == "octo"
    assert conn.avatar_url == "https://av/o.png"


async def test_handle_callback_persists_even_when_account_fetch_fails(monkeypatch) -> None:
    class _Boom:
        async def get_installation_account(self, _id, *, now=None):  # noqa: ANN001
            raise RuntimeError("github down")

    monkeypatch.setattr(connect, "get_github_app_client", lambda: _Boom())
    token = state.sign_state(_WS, _USER)

    # The connection is the load-bearing artifact — a failed display lookup must
    # not sink it (nor leak the error to the browser redirect).
    ws, user = await connect.handle_callback("inst-9", token)
    assert (ws, user) == (_WS, _USER)
    [conn] = await service.list_connections(_WS, _USER)
    assert conn.installation_id == "inst-9"
    assert conn.account_login is None and conn.avatar_url is None


# ---------------------------------------------------------------------------
# connect — lazy display enrichment on list.
# ---------------------------------------------------------------------------


async def test_list_connections_lazily_enriches_missing_display_info(monkeypatch) -> None:
    # A row saved with no display info (e.g. the callback fetch failed earlier).
    await service.save_connection(_WS, _USER, "inst-1")
    client = _FakeAppClient({"inst-1": {"login": "octo", "avatar_url": "https://av/o.png"}})
    monkeypatch.setattr(connect, "get_github_app_client", lambda: client)

    first = await connect.list_connections(_WS, _USER)
    assert first[0].account_login == "octo"
    assert first[0].avatar_url == "https://av/o.png"
    assert client.calls == ["inst-1"]  # enriched once

    # Second read is fully populated → no further GitHub calls.
    second = await connect.list_connections(_WS, _USER)
    assert second[0].avatar_url == "https://av/o.png"
    assert client.calls == ["inst-1"]  # unchanged — bounded to the missing window


async def test_list_connections_falls_back_when_client_absent(monkeypatch) -> None:
    await service.save_connection(_WS, _USER, "inst-1")
    monkeypatch.setattr(connect, "get_github_app_client", lambda: None)
    # No App client configured → return the un-enriched view, not an error.
    listing = await connect.list_connections(_WS, _USER)
    assert [c.installation_id for c in listing] == ["inst-1"]
    assert listing[0].avatar_url is None


# ---------------------------------------------------------------------------
# connect — repo listing.
# ---------------------------------------------------------------------------


async def test_list_repositories_merges_and_dedupes_across_connections() -> None:
    await service.save_connection(_WS, _USER, "inst-1")
    await service.save_connection(_WS, _USER, "inst-2")
    provider = _FakeProvider(
        {
            "inst-1": [_repo("acme/api"), _repo("acme/web")],
            "inst-2": [_repo("acme/web"), _repo("acme/infra")],  # acme/web overlaps
        }
    )

    repos = await connect.list_repositories(_WS, _USER, provider=provider)

    names = sorted(r["full_name"] for r in repos)
    assert names == ["acme/api", "acme/infra", "acme/web"]  # deduped
    assert set(provider.calls) == {"inst-1", "inst-2"}


async def test_list_repositories_empty_without_connections() -> None:
    provider = _FakeProvider({})
    assert await connect.list_repositories(_WS, _USER, provider=provider) == []


async def test_list_repositories_503_when_connected_but_provider_off(monkeypatch) -> None:
    await service.save_connection(_WS, _USER, "inst-1")
    # No injected provider AND the real resolver returns None (App unconfigured).
    monkeypatch.setattr(connect, "get_repo_auth_provider", lambda _p: None)
    with pytest.raises(CloudError) as exc:
        await connect.list_repositories(_WS, _USER)
    assert exc.value.code == "codeconnect.github_not_configured"


def test_provider_id_github_is_the_default() -> None:
    # Guard the string the connect layer resolves against the neutral provider enum.
    assert ProviderId.GITHUB.value == "github"
