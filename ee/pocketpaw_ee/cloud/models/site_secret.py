# ee/pocketpaw_ee/cloud/models/site_secret.py: one named secret of one Paw Site.
#
# A row per (workspace, pocket_id, name). The pocket, not the Site doc, is the key:
# an agent asks for a secret while it builds the site, long before the first publish
# mints a Site, and the builder's by-pocket routes address it the same way.
#
# A row is in one of two states:
#   * pending: ``encrypted_value`` is None. An agent (or a user) asked for the
#     secret; the owner has not filled it in yet. ``description`` says what it is for.
#   * set:     ``encrypted_value`` holds a ``cloud._core.crypto`` Fernet token.
#
# ENCRYPTED AT REST and NEVER READ BACK OVER ANY API OR TOOL. The only reader that
# decrypts is ``sites.site_secrets.secrets_for_deploy``, which hands the values to the
# bundle deploy as ``secret_text`` bindings. Every other read answers from the
# name / status / timestamps columns. Registered in ``cloud.models.__init__`` so
# ``init_beanie`` wires the ``site_secrets`` collection.

from __future__ import annotations

from datetime import datetime

from beanie import Indexed
from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SiteSecret(TimestampedDocument):
    """A named secret (or a pending request for one) on one site pocket."""

    workspace: Indexed(str)  # type: ignore[valid-type]
    pocket_id: str
    # UPPER_SNAKE, at most 64 chars; validated by the service before any write.
    name: str
    # Fernet token from cloud._core.crypto.encrypt(). None while the request is
    # pending. Never serialized into an API response or a tool result.
    encrypted_value: str | None = None
    # What the value is for, in the requester's words. Shown on the input card.
    description: str = ""
    # "agent" or "user": who asked for it. None for a secret the owner set unasked.
    requested_by: str | None = None
    requested_by_user: str | None = None
    requested_at: datetime | None = None
    # Who last set the value, and when. Both None while pending.
    value_set_by: str | None = None
    value_set_at: datetime | None = None

    class Settings:
        name = "site_secrets"
        indexes = [
            IndexModel([("workspace", 1), ("pocket_id", 1), ("name", 1)], unique=True),
        ]
