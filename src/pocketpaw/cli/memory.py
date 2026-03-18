# CLI memory command - search and inspect long-term memories.

from __future__ import annotations

import asyncio

from pocketpaw.cli.utils import BOLD, DIM, RESET, output_json, print_fail, print_header


def run_memory_cmd(
    action: str | None = None,
    query: str | None = None,
    limit: int = 10,
    as_json: bool = False,
) -> int:
    """Manage long-term memories.

    - No action: show memory stats
    - search <query>: search memories
    """
    if action == "search":
        if not query:
            print_fail("Usage: pocketpaw memory search <query>")
            return 1
        return asyncio.run(_search_memories(query, limit, as_json))

    return asyncio.run(_memory_stats(as_json))


async def _memory_stats(as_json: bool) -> int:
    from pocketpaw.config import get_config_dir

    memory_dir = get_config_dir() / "memory"

    stats: dict = {
        "memory_dir": str(memory_dir),
        "long_term_file_exists": False,
        "daily_notes": 0,
        "sessions": 0,
    }

    if memory_dir.exists():
        # Long-term memory
        mem_file = memory_dir / "MEMORY.md"
        stats["long_term_file_exists"] = mem_file.exists()
        if mem_file.exists():
            content = mem_file.read_text(encoding="utf-8", errors="replace")
            # Count ## headers as memory entries
            stats["long_term_entries"] = content.count("\n## ")

        # Daily notes (YYYY-MM-DD.md files)
        daily_files = list(memory_dir.glob("????-??-??.md"))
        stats["daily_notes"] = len(daily_files)

        # Sessions
        sessions_dir = memory_dir / "sessions"
        if sessions_dir.exists():
            session_files = list(sessions_dir.glob("*.json"))
            stats["sessions"] = len(session_files)

    if as_json:
        output_json(stats)
        return 0

    print_header("Memory")
    print(f"  {'Directory:':<24} {DIM}{stats['memory_dir']}{RESET}")
    print(f"  {'Long-term memories:':<24} {stats.get('long_term_entries', 0)}")
    print(f"  {'Daily notes:':<24} {stats['daily_notes']}")
    print(f"  {'Sessions:':<24} {stats['sessions']}")
    print()
    return 0


async def _search_memories(query: str, limit: int, as_json: bool) -> int:
    from pocketpaw.memory.manager import get_memory_manager

    mm = get_memory_manager()
    results = await mm.search(query, limit=limit)

    if as_json:
        output_json(
            [
                {
                    "id": e.id,
                    "content": e.content,
                    "tags": e.tags,
                    "type": e.type.value if hasattr(e.type, "value") else str(e.type),
                    "header": e.metadata.get("header", ""),
                }
                for e in results
            ]
        )
        return 0

    print_header("Memory Search", f"query: '{query}'")

    if not results:
        print(f"  {DIM}No matches found.{RESET}\n")
        return 0

    for e in results:
        header = e.metadata.get("header", "Memory")
        tags = " ".join(f"#{t}" for t in e.tags) if e.tags else ""
        print(f"  {BOLD}{header}{RESET} {DIM}{tags}{RESET}")
        # Truncate long content
        content = e.content
        if len(content) > 120:
            content = content[:117] + "..."
        print(f"    {content}")
        print()

    return 0
