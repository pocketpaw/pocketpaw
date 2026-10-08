# ee/pocketpaw_ee/cloud/models/site_draft_worker.py: the draft Worker registry row of
# one Paw Site pocket.
#
# A ``project`` pocket whose build has server code previews through an account-level
# draft Worker (``sites.draft_worker``). This row is the only record of that Worker
# and of the draft-only D1 / KV / R2 it binds, so cleanup, the proxy and the sweeper
# all read it. One row per pocket (the POCKET is the key: a draft exists before any
# Site doc does). Registered in ``cloud.models.__init__``; only ``sites.draft_worker``
# reads or writes it.
#
# ``state`` is ``live`` (a draft may be deployed; ``deployed_hash`` names the content
# it runs, "" when none), ``deleting`` (a purge is pending; nothing proxies to it) or
# ``purged`` (nothing left on Cloudflare; the row keeps ``published_hash`` /
# ``published_url`` so the builder shows the live site instead of rebuilding a draft).
# ``auth_secret_enc`` is a ``cloud._core.crypto`` Fernet token, never logged.

from __future__ import annotations

from datetime import datetime

from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SiteDraftWorker(TimestampedDocument):
    """The draft Worker and draft-only resources of one site pocket."""

    pocket_id: str
    workspace: str = ""
    script: str = ""
    host: str = ""
    deployed_hash: str = ""
    deployed_at: datetime | None = None
    d1_database_id: str = ""
    kv_namespaces: dict[str, str] = Field(default_factory=dict)
    r2_buckets: dict[str, str] = Field(default_factory=dict)
    seeded: bool = False
    auth_secret_enc: str = ""
    state: str = "live"
    attempts: int = 0
    last_error: str = ""
    retry_after: datetime | None = None
    published_hash: str = ""
    published_url: str = ""

    class Settings:
        name = "site_draft_workers"
        indexes = [
            IndexModel([("pocket_id", 1)], unique=True),
            IndexModel([("state", 1)]),
        ]
