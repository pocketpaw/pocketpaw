# Domain value objects for the Web Push entity (pocketpaw#1391).
# Created: 2026-06-09 (feat/push-subscription-store) — pure-Python frozen
# dataclasses with no Beanie / FastAPI imports. Tenancy fields are required
# (no defaults) so a domain object can't be constructed without workspace +
# user, per ee/cloud rule §3. The service converts between these and the
# Beanie ``PushSubscription`` document.
# Updated: 2026-06-09 (review nits) — ``created_at`` now carries the
# inherited ``TimestampedDocument.createdAt`` value (the dead per-doc
# ``created_at`` field was removed from the model).

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class PushKeys:
    """ECDH keys from the browser's PushSubscription (base64url)."""

    p256dh: str
    auth: str


@dataclass(frozen=True)
class PushSubscription:
    """A stored browser Web Push subscription, scoped to workspace + user."""

    id: str
    workspace_id: str
    user_id: str
    endpoint: str
    keys: PushKeys
    expiration_time: int | None = None
    user_agent: str = ""
    created_at: datetime | None = None


__all__ = ["PushKeys", "PushSubscription"]
