# connect.py — Code Mode GitHub connect orchestration (CM-3).
# Created 2026-07-16 (feat/code-mode): the GitHub-touching layer ABOVE
# ``codeconnect/service.py`` (the registry). It builds the App install URL, handles
# the post-install callback, and lists the caller's repos through the App client —
# so the service stays a pure Beanie repository that never imports an HTTP client.
#
# Three flows:
#   1. ``build_install_url`` — sign the caller's (workspace, user) into a state
#      token → return ``https://github.com/apps/<slug>/installations/new?state=…``.
#      The install screen sends the user back to our callback with an installation
#      id + that state.
#   2. ``handle_callback`` — verify the state (recovers who is connecting, since the
#      browser redirect carries no bearer token), then persist the connection.
#   3. ``list_repositories`` — for each of the caller's connections, list the repos
#      that installation can reach (via the neutral RepoAuthProvider), merged +
#      de-duplicated for the picker.
#
# ``provider=None`` on ``list_repositories`` resolves the live GitHub App client;
# tests inject a fake so no test hits real GitHub.
#
# Updated 2026-07-16 (connect-UX): the callback now best-effort enriches the
# connection with the account login + avatar (``get_installation_account``), and
# ``list_connections`` lazily self-heals any row still missing that display info —
# so the connected-account chip renders a profile image + username without a
# reinstall. Enrichment failures never break the connection itself.

from __future__ import annotations

import logging
from datetime import datetime

from pocketpaw_ee.cloud._core.errors import BadRequest, CloudError
from pocketpaw_ee.cloud.codeconnect import service as codeconnect_service
from pocketpaw_ee.cloud.codeconnect.domain import CodeConnectionView
from pocketpaw_ee.cloud.codeconnect.state import sign_state, verify_state
from pocketpaw_ee.cloud.websandbox.githubapp import (
    get_github_app_client,
    github_app_slug,
)
from pocketpaw_ee.cloud.websandbox.repoauth import (
    ProviderId,
    RepoAuthProvider,
    get_repo_auth_provider,
)

logger = logging.getLogger(__name__)

_GITHUB_APP_BASE = "https://github.com"


def build_install_url(workspace_id: str, user_id: str) -> str:
    """Build the GitHub App install URL, carrying a signed state for the callback.

    Raises a clean 503 CloudError when the App slug isn't configured (an operational
    condition, not a bug) so the frontend can show "GitHub connect isn't set up"
    instead of opening a broken ``/apps//installations/new``.
    """
    slug = github_app_slug()
    if not slug:
        raise CloudError(
            503,
            "codeconnect.github_not_configured",
            "GitHub connect is not configured on this server",
        )
    state = sign_state(workspace_id, user_id)
    return f"{_GITHUB_APP_BASE}/apps/{slug}/installations/new?state={state}"


async def handle_callback(installation_id: str, state: str) -> tuple[str, str]:
    """Handle the post-install redirect: verify state, persist the connection.

    Returns the recovered ``(workspace_id, user_id)`` so the router can redirect the
    browser back to the right place. Raises ``BadRequest`` on a missing installation
    id or an unverifiable state — the browser redirect authenticates ENTIRELY via
    the signed state (it carries no bearer token), so a bad state must fail closed.
    """
    installation_id = (installation_id or "").strip()
    if not installation_id:
        raise BadRequest("codeconnect.missing_installation", "No installation id in callback")

    verified = verify_state(state)
    if verified is None:
        raise BadRequest("codeconnect.invalid_state", "The connect request could not be verified")
    workspace_id, user_id = verified

    login, avatar_url = await _fetch_account_info(installation_id)
    await codeconnect_service.save_connection(
        workspace_id,
        user_id,
        installation_id,
        account_login=login,
        avatar_url=avatar_url,
    )
    logger.info(
        "codeconnect.callback: bound installation=%s to ws=%s user=%s",
        installation_id,
        workspace_id,
        user_id,
    )
    return workspace_id, user_id


async def _fetch_account_info(installation_id: str) -> tuple[str | None, str | None]:
    """Best-effort ``(login, avatar_url)`` for display; never fails the caller.

    The connection is the load-bearing artifact; the account login + avatar are
    only for the connected-account chip. A missing App client, a GitHub hiccup, or
    a revoked installation all resolve to ``(None, None)`` so the connect flow (and
    the token invariant) is never held hostage to a display lookup.
    """
    client = get_github_app_client()
    if client is None:
        return None, None
    try:
        info = await client.get_installation_account(installation_id)
    except Exception:  # noqa: BLE001 — display enrichment is strictly best-effort
        logger.warning(
            "codeconnect: account fetch failed for installation=%s",
            installation_id,
            exc_info=True,
        )
        return None, None
    if not info:
        return None, None
    return info.get("login"), info.get("avatar_url")


async def list_connections(workspace_id: str, user_id: str) -> list[CodeConnectionView]:
    """List the caller's connections, lazily backfilling display info (login+avatar).

    The stored row can predate its account enrichment — the callback's fetch is
    best-effort and older rows predate the ``avatar_url`` field entirely. Any
    connection still missing its login OR avatar is enriched here and persisted
    (an idempotent refresh). Once filled, no further GitHub calls fire, so the cost
    is bounded to the window before the data is complete. Enrichment failures fall
    back to the un-enriched view rather than dropping the connection.
    """
    views = await codeconnect_service.list_connections(workspace_id, user_id)
    client = get_github_app_client()
    if client is None:
        return views

    enriched: list[CodeConnectionView] = []
    for view in views:
        if view.account_login and view.avatar_url:
            enriched.append(view)
            continue
        try:
            info = await client.get_installation_account(view.installation_id)
        except Exception:  # noqa: BLE001 — display enrichment is strictly best-effort
            logger.warning(
                "codeconnect: lazy account enrich failed for installation=%s",
                view.installation_id,
                exc_info=True,
            )
            enriched.append(view)
            continue
        if not info:
            enriched.append(view)
            continue
        refreshed = await codeconnect_service.save_connection(
            workspace_id,
            user_id,
            view.installation_id,
            account_login=info.get("login"),
            avatar_url=info.get("avatar_url"),
        )
        enriched.append(refreshed)
    return enriched


async def list_repositories(
    workspace_id: str,
    user_id: str,
    *,
    provider: RepoAuthProvider | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """List the repos the caller's connections can reach, merged for the picker.

    Resolves the GitHub ``RepoAuthProvider`` (or takes an injected fake), then for
    each of the caller's connections lists the installation's repos and merges them,
    de-duplicated by ``full_name``. No connections → ``[]`` (a clean empty picker,
    not an error). Connections exist but the provider is unconfigured → 503, so the
    misconfig is visible rather than silently empty.
    """
    connections = await codeconnect_service.list_connections(workspace_id, user_id)
    if not connections:
        return []

    resolved = provider if provider is not None else get_repo_auth_provider(ProviderId.GITHUB)
    if resolved is None:
        raise CloudError(
            503,
            "codeconnect.github_not_configured",
            "GitHub connect is not configured on this server",
        )

    seen: set[str] = set()
    repos: list[dict] = []
    for conn in connections:
        try:
            found = await resolved.list_repositories(conn.installation_id, now=now)
        except CloudError:
            # One bad installation (revoked, rate-limited) must not sink the rest.
            logger.warning(
                "codeconnect.repos: listing failed for installation=%s",
                conn.installation_id,
                exc_info=True,
            )
            continue
        for repo in found:
            if repo.full_name in seen:
                continue
            seen.add(repo.full_name)
            repos.append(
                {
                    "full_name": repo.full_name,
                    "private": repo.private,
                    "default_branch": repo.default_branch,
                    "clone_url": repo.clone_url,
                }
            )
    return repos


__all__ = [
    "build_install_url",
    "handle_callback",
    "list_connections",
    "list_repositories",
]
