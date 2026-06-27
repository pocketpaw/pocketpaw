# test_state_store.py — connector-store-unification CS-1 — FileConnectorStateStore.
# Created: 2026-06-12 — Locks the durable connector-state contract: JSON rows
#   keyed by (name, scope_key), sanitized + hash-suffixed filenames that can't
#   path-traverse or collide, 0600 file perms, and tolerant reads (corrupt
#   files degrade to "no state", never crash).

from __future__ import annotations

import json
import os
import stat

import pytest

from pocketpaw.connectors.state_store import (
    ConnectorStateStore,
    FileConnectorStateStore,
)


@pytest.fixture
def store(tmp_path) -> FileConnectorStateStore:
    return FileConnectorStateStore(base_dir=tmp_path / "state")


class TestRoundTrip:
    def test_set_then_get(self, store: FileConnectorStateStore) -> None:
        store.set("stripe", "pocket-1", {"api_key": "sk_test_123"})
        assert store.get("stripe", "pocket-1") == {"api_key": "sk_test_123"}

    def test_get_missing_returns_none(self, store: FileConnectorStateStore) -> None:
        assert store.get("stripe", "pocket-1") is None

    def test_set_overwrites(self, store: FileConnectorStateStore) -> None:
        store.set("stripe", "pocket-1", {"api_key": "old"})
        store.set("stripe", "pocket-1", {"api_key": "new"})
        assert store.get("stripe", "pocket-1") == {"api_key": "new"}

    def test_rows_are_scope_isolated(self, store: FileConnectorStateStore) -> None:
        store.set("stripe", "pocket-1", {"api_key": "one"})
        store.set("stripe", "pocket-2", {"api_key": "two"})
        assert store.get("stripe", "pocket-1") == {"api_key": "one"}
        assert store.get("stripe", "pocket-2") == {"api_key": "two"}

    def test_delete(self, store: FileConnectorStateStore) -> None:
        store.set("stripe", "pocket-1", {"api_key": "k"})
        store.delete("stripe", "pocket-1")
        assert store.get("stripe", "pocket-1") is None

    def test_delete_missing_is_noop(self, store: FileConnectorStateStore) -> None:
        store.delete("stripe", "pocket-1")  # must not raise

    def test_list_returns_original_keys(self, store: FileConnectorStateStore) -> None:
        store.set("stripe", "pocket-1", {"api_key": "k"})
        store.set("github", "alice@example.com", {"token": "t"})
        assert sorted(store.list()) == [
            ("github", "alice@example.com"),
            ("stripe", "pocket-1"),
        ]

    def test_list_empty_dir_absent(self, tmp_path) -> None:
        store = FileConnectorStateStore(base_dir=tmp_path / "never-created")
        assert store.list() == []


class TestSanitization:
    def test_hostile_keys_stay_inside_base_dir(self, tmp_path) -> None:
        base = tmp_path / "state"
        store = FileConnectorStateStore(base_dir=base)
        store.set("../../evil", "../escape", {"k": "v"})
        # Everything written must live directly under the base dir.
        written = list(base.glob("*.json"))
        assert len(written) == 1
        assert written[0].parent == base
        assert store.get("../../evil", "../escape") == {"k": "v"}

    def test_distinct_keys_with_same_sanitized_prefix_do_not_collide(
        self, store: FileConnectorStateStore
    ) -> None:
        # Both scope keys sanitize to "a-x.com"; the raw-value hash keeps
        # them in separate files (token_store convention).
        store.set("svc", "a@x.com", {"who": "at"})
        store.set("svc", "a/x.com", {"who": "slash"})
        assert store.get("svc", "a@x.com") == {"who": "at"}
        assert store.get("svc", "a/x.com") == {"who": "slash"}

    def test_separator_inside_name_does_not_cross_segments(
        self, store: FileConnectorStateStore
    ) -> None:
        store.set("a__b", "c", {"k": "1"})
        store.set("a", "b__c", {"k": "2"})
        assert store.get("a__b", "c") == {"k": "1"}
        assert store.get("a", "b__c") == {"k": "2"}


@pytest.mark.skipif(os.name != "posix", reason="POSIX file permissions")
class TestPermissions:
    def test_state_files_are_0600(self, tmp_path) -> None:
        base = tmp_path / "state"
        store = FileConnectorStateStore(base_dir=base)
        store.set("stripe", "pocket-1", {"api_key": "secret"})
        path = next(base.glob("*.json"))
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode == 0o600


class TestTolerantReads:
    def test_corrupt_file_reads_as_missing(self, tmp_path) -> None:
        base = tmp_path / "state"
        store = FileConnectorStateStore(base_dir=base)
        store.set("stripe", "pocket-1", {"api_key": "k"})
        path = next(base.glob("*.json"))
        path.write_text("{not json")
        assert store.get("stripe", "pocket-1") is None

    def test_list_skips_corrupt_files(self, tmp_path) -> None:
        base = tmp_path / "state"
        store = FileConnectorStateStore(base_dir=base)
        store.set("stripe", "pocket-1", {"api_key": "k"})
        (base / "garbage__row.json").write_text("{not json")
        assert store.list() == [("stripe", "pocket-1")]

    def test_payload_without_config_dict_reads_as_missing(self, tmp_path) -> None:
        base = tmp_path / "state"
        store = FileConnectorStateStore(base_dir=base)
        store.set("stripe", "pocket-1", {"api_key": "k"})
        path = next(base.glob("*.json"))
        path.write_text(json.dumps({"name": "stripe", "scope_key": "pocket-1", "config": "nope"}))
        assert store.get("stripe", "pocket-1") is None


def test_file_store_satisfies_protocol() -> None:
    assert isinstance(FileConnectorStateStore(), ConnectorStateStore)
