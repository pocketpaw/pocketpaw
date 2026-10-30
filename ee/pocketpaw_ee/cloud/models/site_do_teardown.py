# ee/pocketpaw_ee/cloud/models/site_do_teardown.py: a Durable Object teardown that a
# site delete could not finish.
#
# The delete cascade's ``do`` step writes one when ``durable_objects.teardown_script``
# reports failure; the Site doc is gone once the cascade ends, so this row is the only
# record of the script and classes still to clean up. ``sites.do_metering``'s
# ``sweep_do_teardowns`` retries it with backoff (force delete + namespace check, never
# a stub upload, since the script step already deleted the script) and removes the row
# on success. Past ``PAW_SITES_DO_TEARDOWN_MAX_ATTEMPTS`` the state becomes
# ``operator`` and the sweep stops touching it. Registered in ``cloud.models``; only
# ``sites.do_metering`` reads or writes it.

from __future__ import annotations

from datetime import datetime

from pydantic import Field
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SiteDoTeardown(TimestampedDocument):
    """One unfinished Durable Object teardown of a deleted site's script."""

    site_id: str
    workspace: str = ""
    script: str
    target: str  # durable_objects target: "dispatch" | "account"
    classes: list[str] = Field(default_factory=list)
    migration_tag: str | None = None
    state: str = "pending"  # pending | operator
    attempts: int = 1
    last_error: str = ""
    retry_after: datetime | None = None

    class Settings:
        name = "site_do_teardowns"
        indexes = [
            IndexModel([("script", 1), ("target", 1)], unique=True),
            IndexModel([("state", 1), ("retry_after", 1)]),
        ]
