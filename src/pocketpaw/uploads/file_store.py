"""OSS metadata store — append-only JSONL keyed by file_id."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


@dataclass
class FileRecord:
    id: str
    storage_key: str
    filename: str
    mime: str
    size: int
    owner_id: str
    chat_id: str | None
    created: datetime


class JSONLFileStore:
    """Append-only JSONL store with in-memory cache.

    Each line is either:
      - ``{"op": "save",   "record": {...FileRecord...}}``
      - ``{"op": "delete", "id": "<file_id>"}``

    Deleted records are hidden from ``get`` but the historical line stays in
    the file for audit.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._records: dict[str, FileRecord] = {}
        self._deleted: set[str] = set()
        self._lock = Lock()
        self._reload()

    def _reload(self) -> None:
        if not self._path.exists():
            return
        for line in self._path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping corrupt upload-index line: %r", line[:120])
                continue
            op = row.get("op")
            if op == "save":
                rec = row.get("record") or {}
                try:
                    rec["created"] = datetime.fromisoformat(rec["created"])
                    self._records[rec["id"]] = FileRecord(**rec)
                except (KeyError, TypeError, ValueError):
                    logger.warning("skipping malformed record line: %r", line[:120])
            elif op == "delete":
                fid = row.get("id")
                if isinstance(fid, str):
                    self._deleted.add(fid)

    def _append(self, line: dict) -> None:
        with self._lock:
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, default=_json_default) + "\n")

    def save(self, record: FileRecord) -> None:
        self._records[record.id] = record
        self._deleted.discard(record.id)
        self._append({"op": "save", "record": asdict(record)})

    def get(self, file_id: str) -> FileRecord | None:
        if file_id in self._deleted:
            return None
        return self._records.get(file_id)

    def soft_delete(self, file_id: str) -> None:
        self._deleted.add(file_id)
        self._append({"op": "delete", "id": file_id, "at": datetime.now(UTC).isoformat()})


def _json_default(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value)}")
