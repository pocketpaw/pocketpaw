# Meetings — Google Meet OAuth callback flow.
# Created: 2026-05-19 — phase 1.6 of the meetings integration.
#
# Flow:
#   1. Admin pastes Meet client_id/client_secret → POST /meetings/credentials/google_meet
#      (handled in credentials.store_google_meet_init; persists creds in
#       the token blob's ``extra`` with ``access_token=""``).
#   2. User clicks "Connect Google Meet" → frontend hits GET /meetings/credentials/
#      google_meet/auth-url to get the authorization URL.
#   3. User consents on Google's screen → Google redirects with ?code=...&state=...
#      The state encodes the workspace_id so we know whose creds to write.
#   4. Frontend POSTs the code + state to POST /meetings/credentials/google_meet/
#      callback → we exchange code for tokens, persist them in the same blob,
#      enable the MeetingProviderCredentials row.

from __future__ import annotations

import base64
import json
import logging
import secrets
from datetime import UTC, datetime
from typing import Any

from ee.cloud._core.errors import NotFound, ValidationError
from ee.cloud.meetings.dto import CompleteGoogleMeetOAuthRequest
from ee.cloud.models.meeting import MeetingProviderCredentials as _CredsDoc
from pocketpaw.clients.oauth import OAuthManager
from pocketpaw.clients.token_store import TokenStore

logger = logging.getLogger(__name__)

# Scopes requested at consent time.
#
# The Google Meet REST API v2 exposes exactly three OAuth scopes:
#   - meetings.space.created   — create / modify / delete spaces you own
#   - meetings.space.readonly  — read spaces, conferenceRecords, and transcripts
#   - meetings.space.settings  — edit Meet conference settings in your domain
#
# We request the first two — that covers lifecycle (create_space) and the
# full read surface (list_meetings, get_meeting, list_recordings,
# transcript_get). There is NO separate ``meetings.conferences.readonly``
# scope; ``meetings.space.readonly`` already grants conferenceRecords +
# transcripts access. ``meetings.space.settings`` is admin-only and not
# needed for our user-scoped operations.
#
# If we later want to auto-download Meet recordings from Drive, we'd add
# ``https://www.googleapis.com/auth/drive.readonly`` here (Phase 2.5).
GOOGLE_MEET_SCOPES = [
    "https://www.googleapis.com/auth/meetings.space.created",
    "https://www.googleapis.com/auth/meetings.space.readonly",
]


def _service_name(workspace_id: str) -> str:
    return f"workspace-{workspace_id}-google_meet"


def _encode_state(workspace_id: str, nonce: str) -> str:
    """Encode workspace_id + nonce into the OAuth state parameter.

    Google echoes ``state`` back unmodified; we use it to identify
    which workspace's creds to write after consent. The nonce prevents
    a cross-session callback from being applied to the wrong workspace
    if multiple OAuth flows are in flight.
    """
    payload = json.dumps({"workspace_id": workspace_id, "nonce": nonce}).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_state(state: str) -> dict[str, Any]:
    padded = state + "=" * (-len(state) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(padded.encode()).decode())
    except Exception as exc:  # noqa: BLE001
        raise ValidationError(
            "meeting.oauth_invalid_state", "state parameter is malformed"
        ) from exc


async def get_auth_url(
    workspace_id: str,
    *,
    redirect_uri: str,
    oauth_manager: OAuthManager | None = None,
) -> str:
    """Build the Google consent URL for this workspace.

    Reads ``client_id`` from the token blob (already populated by
    ``credentials.store_google_meet_init``). Raises ``NotFound`` if the
    admin hasn't pasted client creds yet.
    """
    service = _service_name(workspace_id)
    store = TokenStore()
    tokens = store.load(service)
    if tokens is None or not tokens.extra.get("client_id"):
        raise NotFound("meeting_credentials", "google_meet")

    nonce = secrets.token_urlsafe(16)
    # Persist the nonce so we can validate it on callback. Reuse the
    # token blob's extra dict — we're about to overwrite this whole
    # blob with real tokens anyway.
    tokens.extra["pending_nonce"] = nonce
    store.save(tokens)

    manager = oauth_manager or OAuthManager(store)
    return manager.get_auth_url(
        provider="google_meet",
        client_id=tokens.extra["client_id"],
        redirect_uri=redirect_uri,
        scopes=GOOGLE_MEET_SCOPES,
        state=_encode_state(workspace_id, nonce),
    )


async def complete_callback(
    body: CompleteGoogleMeetOAuthRequest,
    *,
    redirect_uri: str,
    oauth_manager: OAuthManager | None = None,
) -> str:
    """Exchange ``code`` for tokens and enable the credentials row.

    Returns the ``workspace_id`` that was authorized so the caller (the
    desktop client) can confirm it matches the one it expected. Raises
    ``ValidationError`` if the state nonce doesn't match (cross-session
    callback attempt).
    """
    body = CompleteGoogleMeetOAuthRequest.model_validate(body)
    decoded = _decode_state(body.state)
    workspace_id = decoded.get("workspace_id")
    callback_nonce = decoded.get("nonce")
    if not workspace_id or not callback_nonce:
        raise ValidationError("meeting.oauth_invalid_state", "state payload incomplete")

    service = _service_name(workspace_id)
    store = TokenStore()
    pending = store.load(service)
    if pending is None or pending.extra.get("pending_nonce") != callback_nonce:
        raise ValidationError(
            "meeting.oauth_nonce_mismatch",
            "OAuth state nonce did not match — start the connect flow again.",
        )

    client_id = pending.extra.get("client_id")
    client_secret = pending.extra.get("client_secret")
    if not client_id or not client_secret:
        raise ValidationError(
            "meeting.credentials_incomplete",
            "Client credentials missing from token blob — re-paste them in Settings.",
        )

    manager = oauth_manager or OAuthManager(store)
    tokens = await manager.exchange_code(
        provider="google_meet",
        service=service,
        code=body.code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=GOOGLE_MEET_SCOPES,
    )
    # Persist client_id/client_secret in the new blob so the adapter
    # factory can reconstruct ``GoogleMeetClient`` without a separate
    # lookup. ``exchange_code`` wrote a fresh blob that wiped the
    # pre-consent ``extra``, so we re-add them now.
    tokens.extra["client_id"] = client_id
    tokens.extra["client_secret"] = client_secret
    store.save(tokens)

    # Enable the Mongo row + record validation.
    doc = await _CredsDoc.find_one(
        _CredsDoc.workspace == workspace_id,
        _CredsDoc.provider == "google_meet",
    )
    if doc is None:
        # Defensive — the row should exist from store_google_meet_init.
        raise NotFound("meeting_credentials", "google_meet")
    doc.enabled = True
    doc.last_validated_at = datetime.now(UTC)
    doc.last_error = ""
    await doc.save()

    logger.info("Google Meet OAuth completed for workspace=%s", workspace_id)
    return workspace_id
