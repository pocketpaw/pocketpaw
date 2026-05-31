from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pocketpaw.uploads.file_store import FileRecord, JSONLFileStore


def _record(file_id: str = "f1", **overrides) -> FileRecord:
    defaults = {
        "id": file_id,
        "storage_key": "chat/202604/abc.png",
        "filename": "cat.png",
        "mime": "image/png",
        "size": 1234,
        "owner_id": "user-1",
        "chat_id": "chat-1",
        "created": datetime(2026, 4, 16, 12, 0, tzinfo=UTC),
    }
    defaults.update(overrides)
    return FileRecord(**defaults)


class TestJSONLFileStore:
    def test_save_then_get_roundtrip(self, tmp_path: Path):
        store = JSONLFileStore(path=tmp_path / "idx.jsonl")
        store.save(_record())
        got = store.get("f1")
        assert got is not None
        assert got.filename == "cat.png"
        assert got.size == 1234

    def test_get_missing_returns_none(self, tmp_path: Path):
        store = JSONLFileStore(path=tmp_path / "idx.jsonl")
        assert store.get("nope") is None

    def test_soft_delete_hides_from_get(self, tmp_path: Path):
        store = JSONLFileStore(path=tmp_path / "idx.jsonl")
        store.save(_record())
        store.soft_delete("f1")
        assert store.get("f1") is None

    def test_cold_reload_preserves_state(self, tmp_path: Path):
        path = tmp_path / "idx.jsonl"
        s1 = JSONLFileStore(path=path)
        s1.save(_record("a"))
        s1.save(_record("b", filename="b.png"))
        s1.soft_delete("a")

        s2 = JSONLFileStore(path=path)  # reload
        assert s2.get("a") is None
        assert s2.get("b") is not None
        assert s2.get("b").filename == "b.png"

    def test_corrupt_line_is_skipped(self, tmp_path: Path):
        path = tmp_path / "idx.jsonl"
        path.write_text(
            '{"op": "save", "record": {"id": "a", "storage_key": "k", "filename": "a", '
            '"mime": "text/plain", "size": 1, "owner_id": "u", "chat_id": null, '
            '"created": "2026-04-16T12:00:00+00:00"}}\n'
            "THIS IS NOT JSON\n"
            '{"op": "save", "record": {"id": "b", "storage_key": "k2", "filename": "b", '
            '"mime": "text/plain", "size": 1, "owner_id": "u", "chat_id": null, '
            '"created": "2026-04-16T12:00:00+00:00"}}\n',
            encoding="utf-8",
        )
        store = JSONLFileStore(path=path)
        assert store.get("a") is not None
        assert store.get("b") is not None
