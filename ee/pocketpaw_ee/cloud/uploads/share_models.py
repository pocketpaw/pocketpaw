# share_models.py — FL-12b "Public share links".
#
# Beanie document for a public, token-gated download link over one FileUpload.
# A file owner mints a ShareLink; the unguessable ``token`` (secrets.token_urlsafe)
# lets an unauthenticated recipient download that ONE file via the public
# GET /api/v1/share/{token} route. Security posture:
#   - ``token`` is the sole capability; it is unique-indexed and unguessable.
#   - The link is single-file scoped (``file_id`` + ``workspace_id``), so a
#     token for file A can never resolve file B — the public route only ever
#     mints a presigned URL for the exact ``file_id`` on this row.
#   - ``owner_id`` + ``workspace_id`` are captured at creation so the public
#     route can replay the owner's read identity into
#     ``EEUploadService.presigned_get`` (which forces attachment for
#     non-inline mimes) WITHOUT weakening any check — the token stands in for
#     the owner's authorization, and the forced-attachment gate is inherited.
#   - ``expires_at`` defaults to 7 days out; ``revoked`` is an owner kill-switch.
#     The public route treats expired/revoked as gone (410).
# Independent of FileUpload.hide_from_ai — sharing and AI-visibility are
# separate gates and are never coupled here.

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta

from beanie import Document, Indexed
from pydantic import Field

# Default lifetime for a freshly minted share link.
SHARE_LINK_TTL_DAYS = 7


def _new_token() -> str:
    """Unguessable URL-safe token (~256 bits of entropy)."""
    return secrets.token_urlsafe(32)


def _default_expiry() -> datetime:
    return datetime.now(UTC) + timedelta(days=SHARE_LINK_TTL_DAYS)


class ShareLink(Document):
    """A public, token-gated download link for a single workspace file.

    Workspace-scoped like the other upload stores. The ``token`` is the only
    secret a recipient needs; everything else (owner, workspace, expiry) is
    server-side state the public route consults but never leaks.
    """

    token: Indexed(str, unique=True) = Field(default_factory=_new_token)  # type: ignore[valid-type]
    file_id: Indexed(str)  # type: ignore[valid-type]
    workspace_id: Indexed(str)  # type: ignore[valid-type]
    owner_id: str
    created: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime = Field(default_factory=_default_expiry)
    revoked: bool = False

    def is_active(self, *, now: datetime | None = None) -> bool:
        """True when the link may still resolve to a download.

        Not revoked AND not past ``expires_at``. ``expires_at`` may be a naive
        datetime when a legacy/mongomock round-trip drops tzinfo, so compare
        defensively in UTC.
        """
        if self.revoked:
            return False
        moment = now or datetime.now(UTC)
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=UTC)
        return moment < exp

    class Settings:
        name = "file_share_links"
        indexes = [
            # Owner-side listing/revocation, and the public token lookup.
            [("workspace_id", 1), ("file_id", 1)],
            [("workspace_id", 1), ("owner_id", 1)],
        ]
