# Meetings — provider credential storage + Google Meet OAuth consent.
#
# Single-account model: one deployment-global MeetingProviderCredentials
# row per provider, set by a workspace admin via the Settings → Meetings
# connector page. Secret values are encrypted at rest (_core/crypto.py).
#
# Google Meet OAuth flow (manual-paste callback — needs no public URL):
#   1. Admin pastes client_id/client_secret -> store_google_meet (enabled=False).
#   2. Admin opens get_google_meet_auth_url -> Google consent screen.
#   3. Google redirects to http://localhost?code=...&state=... (page won't
#      load — that's fine). Admin copies the URL back into the panel.
#   4. complete_google_meet_oauth exchanges the code for a refresh token,
#      stores it encrypted, flips enabled=True.
#
# http://localhost is the redirect URI on purpose: it's what
# scripts/get_meet_refresh_token.py already uses, so an OAuth client set
# up for the CLI helper works here unchanged.

from __future__ import annotations

import logging
import secrets
import urllib.parse
from datetime import UTC, datetime

import httpx

from pocketpaw_ee.cloud._core import crypto
from pocketpaw_ee.cloud._core.errors import NotFound, ValidationError
from pocketpaw_ee.cloud.meetings.dto import (
    CompleteGoogleMeetOAuthRequest,
    CredentialsResponse,
    DisconnectResponse,
    GoogleMeetAuthUrlResponse,
    GoogleMeetRedirectUriResponse,
    StoreGoogleMeetCredentialsRequest,
    StoreZoomCredentialsRequest,
)
from pocketpaw_ee.cloud.meetings.providers.recall.clients.zoom import ZoomAPIError, ZoomClient
from pocketpaw_ee.cloud.models.meeting import MeetingProviderCredentials as _CredsDoc

logger = logging.getLogger(__name__)

_VALID_PROVIDERS = ("zoom", "google_meet")

# Redirect URI for the Google Meet OAuth flow. Bare http://localhost —
# the consent code is copied from the address bar (manual-paste flow).
_REDIRECT_URI = "http://localhost"

_GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Meet REST API v2 scopes — create/modify spaces + read conferenceRecords
# and transcripts. Matches scripts/get_meet_refresh_token.py.
GOOGLE_MEET_SCOPES = (
    "https://www.googleapis.com/auth/meetings.space.created",
    "https://www.googleapis.com/auth/meetings.space.readonly",
)

_REQUEST_TIMEOUT = 30


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_provider(provider: str) -> None:
    if provider not in _VALID_PROVIDERS:
        raise ValidationError(
            "meetings.unknown_provider",
            f"Unknown meetings provider '{provider}' — expected one of "
            f"{', '.join(_VALID_PROVIDERS)}.",
        )


def _to_response(doc: _CredsDoc) -> CredentialsResponse:
    return CredentialsResponse(
        provider=doc.provider,  # type: ignore[arg-type]
        enabled=doc.enabled,
        has_credentials=bool(doc.secret_enc),
        last_validated_at=doc.last_validated_at,
        last_error=doc.last_error,
    )


async def _get_doc(provider: str) -> _CredsDoc | None:
    # global-config: provider credentials are deployment-wide, not tenant-scoped.
    return await _CredsDoc.find_one(_CredsDoc.provider == provider)


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


async def list_credentials() -> list[CredentialsResponse]:
    """Status of every configured provider. Secret values never leave here."""
    # global-config: deployment-wide rows, not tenant-scoped.
    docs = await _CredsDoc.find_all().to_list()
    return [_to_response(d) for d in docs]


async def get_credentials(provider: str) -> CredentialsResponse:
    """One provider's status. Returns an empty 'not configured' shape if unset."""
    _validate_provider(provider)
    doc = await _get_doc(provider)
    if doc is None:
        return CredentialsResponse(
            provider=provider,  # type: ignore[arg-type]
            enabled=False,
            has_credentials=False,
            last_validated_at=None,
            last_error="",
        )
    return _to_response(doc)


# ---------------------------------------------------------------------------
# Zoom — store + validate
# ---------------------------------------------------------------------------


async def store_zoom(body: StoreZoomCredentialsRequest) -> CredentialsResponse:
    """Persist Zoom S2S credentials, validating them via a live token grant.

    If Zoom rejects the credentials nothing is written — the admin sees
    the provider error inline rather than discovering it at meeting time.
    """
    body = StoreZoomCredentialsRequest.model_validate(body)

    # Validate before persisting: the S2S account_credentials grant is the
    # cheapest call that proves all three values are correct.
    try:
        await ZoomClient(body.account_id, body.client_id, body.client_secret)._get_token()  # noqa: SLF001
    except ZoomAPIError as exc:
        raise ValidationError(
            "meetings.zoom_credentials_invalid",
            f"Zoom rejected these credentials: {exc}",
        ) from exc

    secret_enc = crypto.encrypt_json({"client_secret": body.client_secret})
    now = datetime.now(UTC)
    doc = await _get_doc("zoom")
    if doc is None:
        doc = _CredsDoc(
            provider="zoom",
            enabled=True,
            public_config={"account_id": body.account_id, "client_id": body.client_id},
            secret_enc=secret_enc,
            last_validated_at=now,
            last_error="",
        )
        await doc.insert()
    else:
        doc.enabled = True
        doc.public_config = {"account_id": body.account_id, "client_id": body.client_id}
        doc.secret_enc = secret_enc
        doc.last_validated_at = now
        doc.last_error = ""
        await doc.save()

    # no-event: deployment-global credential config, no downstream consumers.
    logger.info("Stored + validated Zoom credentials")
    return _to_response(doc)


# ---------------------------------------------------------------------------
# Google Meet — store app creds, then OAuth consent
# ---------------------------------------------------------------------------


async def store_google_meet(body: StoreGoogleMeetCredentialsRequest) -> CredentialsResponse:
    """Persist Google Meet app credentials (pre-consent).

    Stores ``client_id`` / ``client_secret`` and leaves the row disabled
    until the OAuth consent callback supplies a refresh token.
    """
    body = StoreGoogleMeetCredentialsRequest.model_validate(body)

    secret_enc = crypto.encrypt_json({"client_secret": body.client_secret})
    doc = await _get_doc("google_meet")
    if doc is None:
        doc = _CredsDoc(
            provider="google_meet",
            enabled=False,
            public_config={"client_id": body.client_id},
            secret_enc=secret_enc,
            pending_state=None,
            last_validated_at=None,
            last_error="awaiting_oauth_consent",
        )
        await doc.insert()
    else:
        doc.enabled = False
        doc.public_config = {"client_id": body.client_id}
        doc.secret_enc = secret_enc
        doc.pending_state = None
        doc.last_error = "awaiting_oauth_consent"
        await doc.save()

    # no-event: deployment-global credential config, no downstream consumers.
    logger.info("Stored Google Meet app credentials (consent pending)")
    return _to_response(doc)


def get_google_meet_redirect_uri() -> GoogleMeetRedirectUriResponse:
    """The redirect URI to register on the Google OAuth client."""
    return GoogleMeetRedirectUriResponse(redirect_uri=_REDIRECT_URI)


async def get_google_meet_auth_url() -> GoogleMeetAuthUrlResponse:
    """Build the Google consent URL and arm a one-time state nonce.

    Requires Meet app credentials to have been stored first.
    """
    doc = await _get_doc("google_meet")
    client_id = (doc.public_config.get("client_id") if doc else "") or ""
    if doc is None or not client_id:
        raise ValidationError(
            "meetings.google_meet_not_initialized",
            "Save the Google Meet client ID and secret before connecting.",
        )

    nonce = secrets.token_urlsafe(24)
    doc.pending_state = nonce
    await doc.save()

    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": _REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(GOOGLE_MEET_SCOPES),
            "access_type": "offline",  # ask for a refresh token
            "prompt": "consent",  # force consent so a refresh token is issued
            "state": nonce,
        }
    )
    return GoogleMeetAuthUrlResponse(
        auth_url=f"{_GOOGLE_AUTH_URL}?{query}",
        redirect_uri=_REDIRECT_URI,
    )


async def complete_google_meet_oauth(
    body: CompleteGoogleMeetOAuthRequest,
) -> CredentialsResponse:
    """Exchange the consent ``code`` for a refresh token and enable the row."""
    body = CompleteGoogleMeetOAuthRequest.model_validate(body)

    doc = await _get_doc("google_meet")
    if doc is None:
        raise NotFound("meeting_credentials", "google_meet")
    if not doc.pending_state or not secrets.compare_digest(doc.pending_state, body.state):
        raise ValidationError(
            "meetings.oauth_state_mismatch",
            "The OAuth state did not match — open 'Connect Google Meet' again "
            "and retry the consent flow.",
        )

    client_id = doc.public_config.get("client_id", "")
    client_secret = crypto.decrypt_json(doc.secret_enc).get("client_secret", "")
    if not client_id or not client_secret:
        raise ValidationError(
            "meetings.credentials_incomplete",
            "Google Meet client credentials are missing — re-enter them in Settings.",
        )

    async with httpx.AsyncClient(timeout=_REQUEST_TIMEOUT) as client:
        resp = await client.post(
            _GOOGLE_TOKEN_URL,
            data={
                "code": body.code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": _REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
    if resp.status_code != 200:
        doc.last_error = f"oauth_exchange_failed: {resp.text[:200]}"
        await doc.save()
        raise ValidationError(
            "meetings.oauth_exchange_failed",
            f"Google rejected the authorization code: {resp.text[:200]}",
        )

    refresh_token = resp.json().get("refresh_token")
    if not refresh_token:
        raise ValidationError(
            "meetings.oauth_no_refresh_token",
            "Google did not return a refresh token. Revoke the app's access at "
            "https://myaccount.google.com/permissions and connect again.",
        )

    doc.secret_enc = crypto.encrypt_json(
        {"client_secret": client_secret, "refresh_token": refresh_token}
    )
    doc.enabled = True
    doc.pending_state = None
    doc.last_validated_at = datetime.now(UTC)
    doc.last_error = ""
    await doc.save()

    # no-event: deployment-global credential config, no downstream consumers.
    logger.info("Google Meet OAuth consent completed — refresh token stored")
    return _to_response(doc)


# ---------------------------------------------------------------------------
# Disconnect
# ---------------------------------------------------------------------------


async def disconnect(provider: str) -> DisconnectResponse:
    """Remove a provider's stored credentials. Idempotent-ish (404 if absent)."""
    _validate_provider(provider)
    doc = await _get_doc(provider)
    if doc is None:
        raise NotFound("meeting_credentials", provider)
    await doc.delete()
    # no-event: deployment-global credential config, no downstream consumers.
    logger.info("Disconnected meetings provider %s", provider)
    return DisconnectResponse(provider=provider, disconnected=True)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Resolution — used by the adapter factory in service.py
# ---------------------------------------------------------------------------


async def resolve(provider: str) -> dict[str, str] | None:
    """Return decrypted, ready-to-use credentials for one provider, or None.

    ``None`` means "no usable stored credentials" — the caller falls back
    to the ``ZOOM_*`` / ``GOOGLE_MEET_*`` environment variables. A row
    that exists but isn't ``enabled`` (or is missing required fields)
    also resolves to ``None``.
    """
    doc = await _get_doc(provider)
    if doc is None or not doc.enabled:
        return None

    merged = {**doc.public_config, **crypto.decrypt_json(doc.secret_enc)}
    required = (
        ("account_id", "client_id", "client_secret")
        if provider == "zoom"
        else ("client_id", "client_secret", "refresh_token")
    )
    if any(not merged.get(k) for k in required):
        return None
    return {k: merged[k] for k in required}
