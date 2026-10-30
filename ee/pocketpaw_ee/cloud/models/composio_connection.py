# ComposioConnection Beanie document — records the external identity
# a paw user authorized for each Composio toolkit. Keyed on
# ``(workspace, paw_user_id, toolkit)``. The stored ``external_identity``
# is the toolkit's native id (GitHub login, Gmail address, Slack user
# id) returned by the per-toolkit probe at
# ``pocketpaw_ee.cloud.composio.identity``.
#
# Purpose: tripwire. When a user re-authorizes a toolkit, the next
# probe may return a different ``external_identity`` than the stored
# one. The service layer surfaces this mismatch ("Connected as X — this
# differs from previously verified Y") rather than silently overwriting,
# so wrong-account binds are caught the first time.

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pydantic import Field

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class ComposioConnection(TimestampedDocument):
    """Verified external identity for one (workspace, user, toolkit).

    Tenancy: ``workspace`` is required and indexed; every read in
    ``service.py`` filters on it. ``paw_user_id`` + ``toolkit`` together
    with ``workspace`` form the natural unique key — enforced at the
    service layer via find-or-create rather than a Mongo unique index
    (keeps re-verification idempotent under race).

    ``external_identity`` is the verbatim string returned by the
    per-toolkit probe (e.g. ``"octocat"`` for GitHub,
    ``"user@example.com"`` for Gmail). ``None`` means a verification
    was attempted but the probe couldn't resolve the identity (toolkit
    has no probe registered, or the call failed) — distinct from no
    record at all.

    ``last_verified_at`` updates on every successful probe; ``mismatch_count``
    increments when a fresh probe returns a different identity than the
    stored one (without overwriting — the user must confirm the change
    explicitly).
    """

    workspace: Indexed(str)  # type: ignore[valid-type]
    paw_user_id: str
    toolkit: str
    external_identity: str | None = None
    last_verified_at: datetime | None = None
    mismatch_count: int = Field(default=0, ge=0)
    last_mismatch_identity: str | None = None
    last_mismatch_at: datetime | None = None

    class Settings(TimestampedDocument.Settings):
        name = "composio_connections"
