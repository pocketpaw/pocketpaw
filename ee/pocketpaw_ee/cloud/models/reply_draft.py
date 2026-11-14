# ee/pocketpaw_ee/cloud/models/reply_draft.py — one Growth › Social reply card:
# a drafted reply to someone else's X post, for one workspace and profile.
#
# A card is never posted by the server. ``intent_url`` is an x.com intent link a
# person opens and posts by hand; ``status`` records what they did with it
# (``new`` / ``posted`` / ``skipped``). ``source_text`` is internal: the exact
# text the draft was checked against (the target post plus the product's recent
# changes), so a later hand edit is re-checked against the same source.
#
# The unique (workspace, profile, target_post_id) index means a post never gets
# two cards; a racing second insert is a DuplicateKeyError the drafter skips.
#
# Only ``ee.cloud.growth.social.service`` may import this doc class
# (import-linter "Growth" contract).

from __future__ import annotations

from pymongo import IndexModel

from pocketpaw_ee.cloud.models.base import TimestampedDocument


class ReplyDraft(TimestampedDocument):
    """A drafted reply to one X post, posted (or not) by a person."""

    workspace: str
    profile: str
    platform: str = "x"
    target_post_id: str
    target_url: str = ""
    author: str = ""
    context: str = ""
    draft_text: str = ""
    intent_url: str = ""
    why: str = ""
    status: str = "new"
    source_text: str = ""

    class Settings(TimestampedDocument.Settings):
        name = "growth_social_reply_drafts"
        indexes = [
            IndexModel([("workspace", 1), ("profile", 1), ("target_post_id", 1)], unique=True),
            IndexModel([("workspace", 1), ("profile", 1), ("createdAt", -1)]),
        ]
