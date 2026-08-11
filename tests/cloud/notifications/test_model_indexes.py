# Index-shape contract for the Notification document.
# Created: 2026-08-11 (fix/notif-liveness-dispatch) — the inbox index was
# declared on ``created_at``, which this document does not have: timestamps
# come from TimestampedDocument as camelCase ``createdAt`` with no alias, so
# the sort key named a nonexistent field and the bell's newest-first query got
# no index for its sort. The mismatch was invisible — Mongo happily builds an
# index on a missing field and the query just scans. These assertions pin the
# field name against the actual model attribute (not a hardcoded string on both
# sides) and pin the TTL that stops the collection growing forever.

from __future__ import annotations

from pocketpaw_ee.cloud.models.notification import Notification
from pymongo import IndexModel

TTL_SECONDS = 86400 * 90


def _index_keys(index: IndexModel) -> list[tuple[str, int]]:
    return list(index.document["key"].items())


def _indexes() -> list[IndexModel]:
    return list(Notification.Settings.indexes)


def test_timestamp_field_is_camel_case_on_the_document() -> None:
    # The premise of the whole fix: the field is createdAt, not created_at.
    # If a future refactor adds a snake_case alias, this fails first and the
    # index assertions below become the thing to revisit.
    assert "createdAt" in Notification.model_fields
    assert "created_at" not in Notification.model_fields


def test_inbox_index_sorts_on_the_real_timestamp_field() -> None:
    inbox = [
        idx
        for idx in _indexes()
        if [k for k, _ in _index_keys(idx)] == ["recipient", "read", "createdAt"]
    ]
    assert len(inbox) == 1, "the recipient/read/createdAt inbox index is missing"

    keys = _index_keys(inbox[0])
    assert keys[-1] == ("createdAt", -1), "newest-first sort must be descending"


def test_default_list_query_has_a_recipient_only_index() -> None:
    # list_for_user's DEFAULT query (unread=False) filters recipient alone and
    # sorts createdAt. The three-key index cannot serve it — skipping the
    # middle `read` key leaves a gap, so the sort can't ride the index and
    # Mongo falls back to an in-memory sort. The bell's most common query is
    # exactly this one, so it needs its own index.
    default_query = [
        idx for idx in _indexes() if [k for k, _ in _index_keys(idx)] == ["recipient", "createdAt"]
    ]
    assert len(default_query) == 1, "the recipient/createdAt index is missing"
    assert _index_keys(default_query[0])[-1] == ("createdAt", -1)


def test_no_index_names_a_nonexistent_field() -> None:
    # Catches the original bug shape generally: every indexed key must be a
    # real field on the document.
    fields = set(Notification.model_fields)
    for idx in _indexes():
        for key, _direction in _index_keys(idx):
            root = key.split(".")[0]
            assert root in fields, f"index key {key!r} is not a field on Notification"


def test_ttl_index_expires_rows_at_ninety_days() -> None:
    ttl = [idx for idx in _indexes() if "expireAfterSeconds" in idx.document]
    assert len(ttl) == 1, "notifications must have exactly one TTL index"

    doc = ttl[0].document
    assert doc["expireAfterSeconds"] == TTL_SECONDS
    # TTL must hang off createdAt: expires_at is a dead field nothing writes,
    # so a TTL on it would never expire anything.
    assert _index_keys(ttl[0]) == [("createdAt", 1)]


def test_expires_at_is_still_declared_but_unwritten() -> None:
    # Documented dead field — kept so existing rows deserialize. If something
    # starts writing it, revisit whether the TTL should move onto it.
    assert Notification.model_fields["expires_at"].default is None
