# File-based memory store implementation.
# Created: 2026-02-02
# Part of Nanobot Pattern Adoption - Memory System
#
# Stores memories as markdown files for human readability:
# - ~/.pocketclaw/memory/MEMORY.md     (long-term)
# - ~/.pocketclaw/memory/2026-02-02.md (daily)
# - ~/.pocketclaw/memory/sessions/     (session JSON files)

import json
import re
import uuid
import asyncio
from datetime import datetime, date
from pathlib import Path
from typing import Any

from pocketclaw.memory.protocol import MemoryStoreProtocol, MemoryEntry, MemoryType


class FileMemoryStore:
    """
    File-based memory store.

    Human-readable markdown for long-term and daily memories.
    JSON for session memories (machine-readable).
    """

    def __init__(self, base_path: Path | None = None):
        self.base_path = base_path or (Path.home() / ".pocketclaw" / "memory")
        self.base_path.mkdir(parents=True, exist_ok=True)

        # Sub-directories
        self.sessions_path = self.base_path / "sessions"
        self.sessions_path.mkdir(exist_ok=True)

        # File paths
        self.long_term_file = self.base_path / "MEMORY.md"

        # In-memory index for fast lookup
        self._index: dict[str, MemoryEntry] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._load_index()

    def _load_index(self) -> None:
        """Load existing memories into index."""
        # Load long-term memories
        if self.long_term_file.exists():
            self._parse_markdown_file(self.long_term_file, MemoryType.LONG_TERM)

        # Load today's daily notes
        today_file = self._get_daily_file(date.today())
        if today_file.exists():
            self._parse_markdown_file(today_file, MemoryType.DAILY)

    def _parse_markdown_file(self, path: Path, memory_type: MemoryType) -> None:
        """Parse a markdown file into memory entries."""
        content = path.read_text(encoding="utf-8")

        # Split by headers (## or ###)
        sections = re.split(r"\n(?=##+ )", content)

        for section in sections:
            if not section.strip():
                continue

            # Extract header and content
            lines = section.strip().split("\n")
            header = lines[0].lstrip("#").strip()
            body = "\n".join(lines[1:]).strip()

            if body:
                entry_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{path}:{header}"))
                self._index[entry_id] = MemoryEntry(
                    id=entry_id,
                    type=memory_type,
                    content=body,
                    tags=self._extract_tags(body),
                    metadata={"header": header, "source": str(path)},
                )

    def _extract_tags(self, content: str) -> list[str]:
        """Extract #tags from content."""
        return re.findall(r"#(\w+)", content)

    def _get_daily_file(self, d: date) -> Path:
        """Get the path for a daily notes file."""
        return self.base_path / f"{d.isoformat()}.md"

    def _get_session_file(self, session_key: str) -> Path:
        """Get the path for a session file."""
        safe_key = session_key.replace(":", "_").replace("/", "_")
        return self.sessions_path / f"{safe_key}.json"

    # =========================================================================
    # MemoryStoreProtocol Implementation
    # =========================================================================

    async def save(self, entry: MemoryEntry) -> str:
        """Save a memory entry."""
        if not entry.id:
            entry.id = str(uuid.uuid4())

        entry.updated_at = datetime.now()
        self._index[entry.id] = entry

        # Persist based on type
        if entry.type == MemoryType.LONG_TERM:
            await self._append_to_markdown(self.long_term_file, entry)
        elif entry.type == MemoryType.DAILY:
            daily_file = self._get_daily_file(date.today())
            await self._append_to_markdown(daily_file, entry)
        elif entry.type == MemoryType.SESSION:
            await self._save_session_entry(entry)

        return entry.id

    async def _append_to_markdown(self, path: Path, entry: MemoryEntry) -> None:
        """Append a memory entry to a markdown file."""
        header = entry.metadata.get("header", datetime.now().strftime("%H:%M"))
        tags_str = " ".join(f"#{t}" for t in entry.tags) if entry.tags else ""

        section = f"\n\n## {header}\n\n{entry.content}"
        if tags_str:
            section += f"\n\n{tags_str}"

        with open(path, "a", encoding="utf-8") as f:
            f.write(section)

    async def _save_session_entry(self, entry: MemoryEntry) -> None:
        """Save a session memory entry."""
        if not entry.session_key:
            return

        session_file = self._get_session_file(entry.session_key)

        if entry.session_key not in self._session_locks:
            self._session_locks[entry.session_key] = asyncio.Lock()

        async with self._session_locks[entry.session_key]:
            # Load existing session
            session_data = []
            if session_file.exists():
                try:
                    session_data = json.loads(session_file.read_text())
                except json.JSONDecodeError:
                    pass

            # Append new entry
            session_data.append(
                {
                    "id": entry.id,
                    "role": entry.role,
                    "content": entry.content,
                    "timestamp": entry.created_at.isoformat(),
                    "session_key": entry.session_key,  # Store key for recovery
                    "metadata": entry.metadata,
                }
            )

            # Compaction: Keep only last 100 messages
            if len(session_data) > 100:
                session_data = session_data[-100:]

            # Save back
            session_file.write_text(json.dumps(session_data, indent=2))

    async def list_sessions(self) -> list[str]:
        """List all available session keys."""
        sessions = set()
        if not self.sessions_path.exists():
            return []

        for session_file in self.sessions_path.glob("*.json"):
            try:
                data = json.loads(session_file.read_text())
                if data and isinstance(data, list) and len(data) > 0:
                    # Try to get key from first entry
                    first = data[0]
                    if "session_key" in first:
                        sessions.add(first["session_key"])
                    else:
                        # Fallback to filename (lossy)
                        sessions.add(session_file.stem)
            except (json.JSONDecodeError, KeyError, OSError):
                continue
                
        return list(sessions)

    async def get(self, entry_id: str) -> MemoryEntry | None:
        """Get a memory entry by ID."""
        return self._index.get(entry_id)

    async def delete(self, entry_id: str) -> bool:
        """Delete a memory entry."""
        if entry_id in self._index:
            del self._index[entry_id]
            # Note: Doesn't delete from files (append-only design)
            return True
        return False

    async def search(
        self,
        query: str | None = None,
        memory_type: MemoryType | None = None,
        tags: list[str] | None = None,
        limit: int = 10,
    ) -> list[MemoryEntry]:
        """Search memories."""
        results = []

        for entry in self._index.values():
            # Type filter
            if memory_type and entry.type != memory_type:
                continue

            # Tag filter
            if tags and not any(t in entry.tags for t in tags):
                continue

            # Query filter (simple substring match)
            if query and query.lower() not in entry.content.lower():
                continue

            results.append(entry)

            if len(results) >= limit:
                break

        return results

    async def get_by_type(self, memory_type: MemoryType, limit: int = 100) -> list[MemoryEntry]:
        """Get all memories of a specific type."""
        return [e for e in self._index.values() if e.type == memory_type][:limit]

    async def get_session(self, session_key: str) -> list[MemoryEntry]:
        """Get session history."""
        session_file = self._get_session_file(session_key)

        if not session_file.exists():
            return []

        try:
            data = json.loads(session_file.read_text())
            return [
                MemoryEntry(
                    id=item["id"],
                    type=MemoryType.SESSION,
                    content=item["content"],
                    role=item.get("role"),
                    session_key=session_key,
                    created_at=datetime.fromisoformat(item["timestamp"]),
                    metadata=item.get("metadata", {}),
                )
                for item in data
            ]
        except (json.JSONDecodeError, KeyError):
            return []

    async def clear_session(self, session_key: str) -> int:
        """Clear session history."""
        session_file = self._get_session_file(session_key)

        if session_key not in self._session_locks:
            self._session_locks[session_key] = asyncio.Lock()

        async with self._session_locks[session_key]:
            if session_file.exists():
                try:
                    data = json.loads(session_file.read_text())
                    count = len(data)
                    session_file.unlink()
                    return count
                except json.JSONDecodeError:
                    session_file.unlink()
                    return 0
            return 0
