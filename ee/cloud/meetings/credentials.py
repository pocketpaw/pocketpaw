# Meetings — BYO provider credentials management.
# Created: 2026-05-19. Workspace admins paste their own Zoom S2S OAuth /
# Google Cloud OAuth credentials; we persist a Mongo row (state +
# webhook secret + validation timestamps) plus a token blob on disk
# under ~/.pocketpaw/oauth/. Secret bytes never live in Mongo.

from __future__ import annotations

import logging
from datetime import UTC, datetime

from ee.cloud._core.errors import NotFound, ValidationError
from ee.cloud.meetings.domain import MeetingProvider, ProviderCredentialsSnapshot
from ee.cloud.meetings.dto import (
    StoreGoogleMeetCredentialsRequest,
    StoreZoomCredentialsRequest,
)
from ee.cloud.models.meeting import MeetingProviderCredentials as _CredsDoc
from pocketpaw.clients.oauth import OAuthManager
from pocketpaw.clients.token_store import TokenStore

logger = logging.getLogger(__name__)


def _service_name(workspace_id: str, provider: MeetingProvider) -> str:
    """Token-store service key for one workspace's provider creds.

    Format: ``workspace-{workspace_id}-{provider}``. Keeps the on-disk
    file naming consistent and inspectable.
    """
    return f"workspace-{workspace_id}-{provider}"


async def get_snapshot(
    workspace_id: str,
    provider: MeetingProvider,
) -> ProviderCredentialsSnapshot | None:
    """Return the sanitized view of one provider's creds, or None if not set."""
    doc = await _CredsDoc.find_one(
        _CredsDoc.workspace == workspace_id,
        _CredsDoc.provider == provider,
    )
    if doc is None:
        return None
    store = TokenStore()
    tokens = store.load(_service_name(workspace_id, provider))
    return ProviderCredentialsSnapshot(
        workspace_id=workspace_id,
        provider=provider,
        enabled=doc.enabled,
        last_validated_at=doc.last_validated_at,
        last_error=doc.last_error,
        has_credentials=tokens is not None,
    )


async def list_snapshots(
    workspace_id: str,
) -> list[ProviderCredentialsSnapshot]:
    """Snapshot of every provider configured for this workspace."""
    docs = await _CredsDoc.find(_CredsDoc.workspace == workspace_id).to_list()
    out: list[ProviderCredentialsSnapshot] = []
    store = TokenStore()
    for doc in docs:
        tokens = store.load(_service_name(workspace_id, doc.provider))
        out.append(
            ProviderCredentialsSnapshot(
                workspace_id=workspace_id,
                provider=doc.provider,
                enabled=doc.enabled,
                last_validated_at=doc.last_validated_at,
                last_error=doc.last_error,
                has_credentials=tokens is not None,
            )
        )
    return out


async def store_zoom(
    workspace_id: str,
    body: StoreZoomCredentialsRequest,
    *,
    oauth_manager: OAuthManager | None = None,
) -> ProviderCredentialsSnapshot:
    """Persist Zoom S2S OAuth creds and validate via a token exchange.

    Side effects, in order:
      1. Exchange ``account_id`` + ``client_id`` + ``client_secret`` for an
         access token. If Zoom rejects the request we surface the error
         and write nothing.
      2. Save the token blob to ``~/.pocketpaw/oauth/workspace-{id}-zoom.json``.
      3. Upsert the ``MeetingProviderCredentials`` row with a freshly
         generated webhook secret and ``last_validated_at = now``.
    """
    body = StoreZoomCredentialsRequest.model_validate(body)
    manager = oauth_manager or OAuthManager()
    service = _service_name(workspace_id, "zoom")

    try:
        await manager.exchange_account_credentials(
            provider="zoom",
            service=service,
            client_id=body.client_id,
            client_secret=body.client_secret,
            account_id=body.account_id,
        )
    except Exception as e:  # noqa: BLE001 — surface provider error to admin
        raise ValidationError("meetings.zoom_credentials_invalid", str(e)) from e

    doc = await _CredsDoc.find_one(
        _CredsDoc.workspace == workspace_id,
        _CredsDoc.provider == "zoom",
    )
    if doc is None:
        doc = _CredsDoc(
            workspace=workspace_id,
            provider="zoom",
            credentials_ref=f"{service}.json",
            enabled=True,
            last_validated_at=datetime.now(UTC),
            last_error="",
        )
        await doc.insert()
    else:
        doc.enabled = True
        doc.last_validated_at = datetime.now(UTC)
        doc.last_error = ""
        await doc.save()

    logger.info("Stored Zoom credentials for workspace=%s", workspace_id)
    snapshot = await get_snapshot(workspace_id, "zoom")
    if snapshot is None:
        # Practically unreachable — we just wrote the doc.
        raise NotFound("meeting_credentials", "zoom")
    return snapshot


async def store_google_meet_init(
    workspace_id: str,
    body: StoreGoogleMeetCredentialsRequest,
) -> ProviderCredentialsSnapshot:
    """Persist Google Meet client_id/client_secret without consent yet.

    The user still has to complete the OAuth consent flow via
    ``get_auth_url`` → callback. This stores the app credentials so the
    callback handler can complete the exchange.
    """
    body = StoreGoogleMeetCredentialsRequest.model_validate(body)
    # We stash client_id / client_secret in tokens.extra until the user
    # completes consent; once exchange happens they're replaced with the
    # full token set.
    store = TokenStore()
    service = _service_name(workspace_id, "google_meet")
    from pocketpaw.clients.token_store import OAuthTokens

    store.save(
        OAuthTokens(
            service=service,
            access_token="",
            refresh_token=None,
            expires_at=None,
            extra={"client_id": body.client_id, "client_secret": body.client_secret},
        )
    )

    doc = await _CredsDoc.find_one(
        _CredsDoc.workspace == workspace_id,
        _CredsDoc.provider == "google_meet",
    )
    if doc is None:
        doc = _CredsDoc(
            workspace=workspace_id,
            provider="google_meet",
            credentials_ref=f"{service}.json",
            enabled=False,  # not enabled until consent completes
            last_validated_at=None,
            last_error="awaiting_oauth_consent",
        )
        await doc.insert()
    else:
        doc.enabled = False
        doc.last_error = "awaiting_oauth_consent"
        await doc.save()

    logger.info("Stored Google Meet client creds (consent pending) ws=%s", workspace_id)
    snapshot = await get_snapshot(workspace_id, "google_meet")
    if snapshot is None:
        raise NotFound("meeting_credentials", "google_meet")
    return snapshot


async def disconnect(
    workspace_id: str,
    provider: MeetingProvider,
) -> None:
    """Disable creds for one provider. Removes both Mongo row and token blob."""
    doc = await _CredsDoc.find_one(
        _CredsDoc.workspace == workspace_id,
        _CredsDoc.provider == provider,
    )
    if doc is None:
        raise NotFound("meeting_credentials", provider)
    TokenStore().delete(_service_name(workspace_id, provider))
    await doc.delete()
    logger.info("Disconnected %s for workspace=%s", provider, workspace_id)
