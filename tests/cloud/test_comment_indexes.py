# tests/cloud/test_comment_indexes.py — the two comment stores index real fields.
#
# Created 2026-10-01 (CN-7, F5): Comment and TaskEvent both indexed
# ``created_at`` while the TimestampedDocument field is ``createdAt`` (no alias),
# so the sort index covered a field no document has. This pins every index key
# to a declared model field.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.models.comment import Comment
from pocketpaw_ee.cloud.models.task_event import TaskEvent


@pytest.mark.parametrize("model", [Comment, TaskEvent])
def test_every_index_key_is_a_real_field(model) -> None:
    for index in model.Settings.indexes:
        for key, _direction in index:
            assert key.split(".", 1)[0] in model.model_fields, (model.__name__, key)


@pytest.mark.parametrize("model", [Comment, TaskEvent])
def test_the_sort_index_is_on_created_at(model) -> None:
    keys = [key for index in model.Settings.indexes for key, _ in index]
    assert "createdAt" in keys
    assert "created_at" not in keys
