# ee/pocketpaw_ee/cloud/models/social_postiz_org.py — the Postiz organisation that
# holds one Growth › Social profile's connected accounts: one row per (workspace,
# profile), unique, created on the profile's first connect. ``api_key_enc`` is the
# org's Postiz API key as a ``_core.crypto`` Fernet token, never plaintext.
#
# Only ``ee.cloud.growth.social.service`` may import this doc class (import-linter
# "Growth" contract).

from __future__ import annotations

from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class SocialPostizOrg(TimestampedDocument):
    """A social profile's Postiz organisation and its encrypted API key."""

    workspace: str
    profile: str
    postiz_org_id: str
    api_key_enc: str

    class Settings(TimestampedDocument.Settings):
        name = "growth_social_postiz_orgs"
        indexes = [IndexModel([("workspace", 1), ("profile", 1)], unique=True)]
