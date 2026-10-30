# Regression: the queries that grow with the database are indexed and projected.
#
# Created 2026-09-04 (backend-perf H4, M2, M3). Three reads got slower as the
# database filled rather than as traffic rose, which is the kind of problem that
# passes every test and every staging soak and then arrives all at once.
#
#   H4  find_active_run_scopes filters on `status` alone. No index led on
#       status, so it was a COLLSCAN of `chat_runs` — a collection with no TTL
#       that grows forever, where every row carries the model's full answer in
#       `partial_text`. It is the jail GC's guard and runs every five minutes.
#       It also hydrated a full document per active run to read three strings.
#   M3  get_by_type(SESSION) filters on `context_type` alone and sorts
#       -createdAt. The available index had `group` unbounded in the middle, so
#       createdAt could supply neither bound nor sort: Mongo fetched every
#       matching document and sorted in memory, which HARD-FAILS past 32 MB.
#   M2  get_session was unbounded — covered in tests/test_session_history_bounds.
#
# These assert on the declared index set and the projection rather than on a
# live explain plan, because the suite runs on mongomock, which does not model
# index selection. That is a real limit of this file: it proves the index is
# DECLARED, not that the planner picks it. The reasoning for why each index
# shape serves each query is written next to the index in the model.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.chat.runs import service as run_service
from pocketpaw_ee.cloud.models.chat_run import ChatRunDoc
from pocketpaw_ee.cloud.models.message import Message


def _index_keys(doc_cls) -> list[list[tuple]]:
    """Normalise Beanie's mixed index declarations to lists of key tuples.

    Settings.indexes accepts both bare lists and IndexModel instances, and this
    model uses both.
    """
    out = []
    for idx in doc_cls.Settings.indexes:
        if hasattr(idx, "document"):
            out.append([tuple(k) for k in idx.document["key"].items()])
        else:
            out.append([tuple(k) for k in idx])
    return out


class TestChatRunIndexes:
    def test_an_index_leads_on_status(self):
        """Without one, the jail GC's every-five-minutes guard is a full scan
        of a collection that grows forever."""
        leading = {keys[0][0] for keys in _index_keys(ChatRunDoc)}
        assert "status" in leading, (
            "no chat_runs index leads on `status`, so find_active_run_scopes is a COLLSCAN"
        )

    def test_the_status_index_also_carries_created_at(self):
        """So the sweeper's age-ordered variants are an index walk too."""
        status_indexes = [k for k in _index_keys(ChatRunDoc) if k[0][0] == "status"]
        assert any(len(k) > 1 and k[1][0] == "createdAt" for k in status_indexes)

    def test_the_existing_workspace_indexes_are_untouched(self):
        """This PR adds an index; it must not have moved the tenant-scoped
        ones the activity board depends on."""
        leading = [keys[0][0] for keys in _index_keys(ChatRunDoc)]
        assert leading.count("workspace") >= 2
        assert "run_id" in leading


class TestMessageIndexes:
    def test_context_type_plus_created_at_exists(self):
        """get_by_type(SESSION) filters context_type and sorts -createdAt. The
        older three-key index has `group` unbounded in the middle, so it can
        serve neither the bound nor the sort."""
        keys = _index_keys(Message)
        assert [("context_type", 1), ("createdAt", -1)] in keys, (
            "the in-memory sort this avoids does not degrade, it hard-fails "
            "past Mongo's 32 MB sort limit"
        )

    def test_the_sort_direction_matches_the_query(self):
        """A -createdAt sort served by an ascending index still walks, but the
        pair must exist as declared; an ascending second key here would be a
        different index and a silent no-op for this query."""
        keys = _index_keys(Message)
        pair = [k for k in keys if len(k) == 2 and k[0][0] == "context_type"]
        assert pair, "no two-key context_type index at all"
        assert pair[0][1] == ("createdAt", -1)

    def test_the_session_key_indexes_survive(self):
        keys = _index_keys(Message)
        assert [("session_key", 1), ("createdAt", 1)] in keys


class _FakeCursor:
    def __init__(self, rows, recorder):
        self._rows = rows
        self._recorder = recorder

    def __aiter__(self):
        async def _gen():
            for r in self._rows:
                yield r

        return _gen()


class _FakeCollection:
    def __init__(self, rows):
        self.rows = rows
        self.calls: list[tuple] = []

    def find(self, query, projection=None):
        self.calls.append((query, projection))
        return _FakeCursor(self.rows, self)


class TestActiveRunScopesProjection:
    @pytest.fixture
    def collection(self, monkeypatch):
        rows = [
            {"workspace": "w1", "context_type": "session", "scope_id": "s1"},
            {"workspace": "w1", "context_type": "dm", "scope_id": "s2"},
            {"workspace": "w2", "context_type": "session", "scope_id": "s1"},
        ]
        fake = _FakeCollection(rows)
        monkeypatch.setattr(ChatRunDoc, "get_pymongo_collection", classmethod(lambda cls: fake))
        return fake

    async def test_returns_the_scope_tuples(self, collection):
        scopes = await run_service.find_active_run_scopes()
        assert scopes == {
            ("w1", "session", "s1"),
            ("w1", "dm", "s2"),
            ("w2", "session", "s1"),
        }

    async def test_only_the_three_needed_fields_are_requested(self, collection):
        """A run document carries the model's whole answer in `partial_text`
        and, on the concierge surface, the visitor's own text in `user_text`.
        An unprojected read pulls both across the wire, for every active run,
        every five minutes, to read three short strings."""
        await run_service.find_active_run_scopes()

        _query, projection = collection.calls[0]
        assert projection is not None, "the read is unprojected"
        assert set(projection) == {"workspace", "context_type", "scope_id"}
        assert "partial_text" not in projection
        assert "user_text" not in projection

    async def test_the_active_status_filter_is_unchanged(self, collection):
        """The jail GC must keep protecting exactly queued + running: an
        interrupted run the user retries spawns a NEW queued run, which
        re-protects the jail."""
        await run_service.find_active_run_scopes()

        query, _projection = collection.calls[0]
        assert set(query["status"]["$in"]) == set(run_service.ACTIVE_RUN_STATUSES)

    async def test_a_row_missing_a_field_does_not_raise(self, monkeypatch):
        """Projection returns what the document has. A legacy row written
        before a field existed must not crash the GC guard, because the GC
        failing open would evict a jail that is in use."""
        fake = _FakeCollection([{"workspace": "w1"}])
        monkeypatch.setattr(ChatRunDoc, "get_pymongo_collection", classmethod(lambda cls: fake))

        assert await run_service.find_active_run_scopes() == {("w1", "", "")}
