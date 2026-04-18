# Drive OAuth helpers — bearer-token resolution for the connector layer.
# Created: 2026-04-16 (Workstream C2 of the Org Architecture RFC).
#
# The retrieval router hands the adapter a ``Credential`` from the broker at
# dispatch time. In production that credential's ``token`` is the bearer
# string we send upstream. For local development and tool-path invocations
# (the non-zero-copy builtin tools) we still need a way to materialise a
# token: the fall-backs live here so ``source.py`` stays focused.
#
# Precedence order (first non-empty wins):
#   1. ``Credential.token`` from the broker (zero-copy federation).
#   2. ``GOOGLE_OAUTH_TOKEN`` env var (local dev / CI).
#   3. ``pocketpaw.integrations.oauth.OAuthManager`` (existing token store
#      populated by the OAuth flow in the dashboard).
#
# TODO(hardening): we inherit ``OAuthManager``'s current contract, which
# refreshes lazily on access. For long-running dispatches we may want a
# warm-up phase so the first parallel worker doesn't pay the refresh
# latency for the whole batch. Deferred until the second concrete adapter
# (Salesforce) confirms the pattern.

from __future__ import annotations

import logging
import os

from soul_protocol.engine.retrieval import Credential

from .errors import DriveAuthError

logger = logging.getLogger(__name__)

_ENV_TOKEN = "GOOGLE_OAUTH_TOKEN"


def resolve_bearer_token(
    credential: Credential | None,
    *,
    env: dict[str, str] | None = None,
) -> str:
    """Pick the best bearer token for a Drive call.

    ``env`` is injectable for tests; defaults to ``os.environ``. Raises
    :class:`DriveAuthError` when no source yields a usable token — the
    adapter surfaces this as a ``sources_failed`` entry rather than crashing
    the whole router.
    """
    if credential is not None and credential.token:
        return credential.token

    env_map = env if env is not None else os.environ
    env_token = env_map.get(_ENV_TOKEN, "").strip()
    if env_token:
        return env_token

    # Fall-through: the OAuth token store. Imported lazily because the
    # ``integrations`` package loads config state that we don't want to
    # pull in for pure-library imports.
    try:
        import asyncio

        from pocketpaw.config import get_settings
        from pocketpaw.integrations.oauth import OAuthManager
        from pocketpaw.integrations.token_store import TokenStore
    except Exception as e:  # pragma: no cover - defensive
        raise DriveAuthError(
            "no Drive bearer token available (credential empty, env unset, OAuth store unavailable)"
        ) from e

    store = TokenStore()
    # Short-circuit: if the OAuth store has nothing saved for Drive, skip the
    # async roundtrip entirely. This matters for tests (avoids closing the
    # default event loop with ``asyncio.run``) and for fresh installs where
    # the user hasn't completed the OAuth flow yet — we fail fast with a
    # clear error message instead of spinning up a loop just to get None.
    try:
        tokens = store.load("google_drive")
    except Exception:
        tokens = None
    if tokens is None:
        raise DriveAuthError(
            "Drive OAuth token not found. Complete the OAuth flow in "
            "Settings > Google OAuth > Authorize Drive, or set "
            f"{_ENV_TOKEN} for headless usage."
        )

    manager = OAuthManager(store)
    try:
        settings = get_settings()
    except Exception as e:  # pragma: no cover - config may not be loaded in tests
        raise DriveAuthError(f"cannot read OAuth settings: {e}") from e

    client_id = getattr(settings, "google_oauth_client_id", "") or ""
    client_secret = getattr(settings, "google_oauth_client_secret", "") or ""

    async def _fetch() -> str | None:
        return await manager.get_valid_token(
            service="google_drive",
            client_id=client_id,
            client_secret=client_secret,
        )

    # Prefer an isolated loop so we never close the caller's default loop —
    # ``asyncio.run`` would swap out the main-thread loop on teardown, which
    # breaks any downstream code that called ``asyncio.get_event_loop()``.
    loop = asyncio.new_event_loop()
    try:
        token = loop.run_until_complete(_fetch())
    finally:
        loop.close()

    if not token:
        raise DriveAuthError(
            "Drive OAuth token not found. Complete the OAuth flow in "
            "Settings > Google OAuth > Authorize Drive, or set "
            f"{_ENV_TOKEN} for headless usage."
        )
    return token
