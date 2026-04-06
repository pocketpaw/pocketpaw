# CLI sessions command - list, search, and delete chat sessions.

from __future__ import annotations

import asyncio

from pocketpaw.cli.utils import (
    BOLD,
    DIM,
    GREEN,
    RESET,
    output_json,
    print_fail,
    print_header,
)


def run_sessions_cmd(
    action: str | None = None,
    query: str | None = None,
    limit: int = 20,
    as_json: bool = False,
) -> int:
    """Manage chat sessions.

    - No action: list recent sessions
    - delete <key>: delete a session
    - search <query>: search session content
    """
    if action == "delete":
        if not query:
            print_fail("Usage: pocketpaw sessions delete <session-key>")
            return 1
        return _run(_delete_session(query))

    if action == "search":
        if not query:
            print_fail("Usage: pocketpaw sessions search <query>")
            return 1
        return _run(_search_sessions(query, limit, as_json))

    return _run(_list_sessions(limit, as_json))


def _run(coro) -> int:
    """Run an async function."""
    return asyncio.run(coro)


async def _list_sessions(limit: int, as_json: bool) -> int:
    from pocketpaw.memory.manager import get_memory_manager

    mm = get_memory_manager()

    # list_sessions_for_chat uses a default session_key; we list all
    sessions = await mm.list_sessions_for_chat("default")
    sessions = sessions[:limit]

    if as_json:
        output_json(sessions)
        return 0

    print_header("Sessions", f"{len(sessions)} session(s)")

    if not sessions:
        print(f"  {DIM}No sessions found.{RESET}\n")
        return 0

    print(f"  {'TITLE':<30} {'MESSAGES':<10} {'LAST ACTIVITY'}")
    print(f"  {'─' * 60}")
    for s in sessions:
        title = (s.get("title") or s.get("session_key", "untitled"))[:28]
        count = s.get("message_count", 0)
        activity = s.get("last_activity", "")
        if isinstance(activity, str) and len(activity) > 16:
            activity = activity[:16]
        is_active = s.get("is_active", False)
        marker = f" {GREEN}*{RESET}" if is_active else ""
        print(f"  {title:<30} {count:<10} {DIM}{activity}{RESET}{marker}")

    print(f"\n  {DIM}* = active session{RESET}\n")
    return 0


async def _delete_session(session_key: str) -> int:
    from pocketpaw.memory.manager import get_memory_manager

    mm = get_memory_manager()
    deleted = await mm.delete_session(session_key)
    if deleted:
        print(f"  {GREEN}Deleted{RESET} session: {BOLD}{session_key}{RESET}")
        return 0
    print_fail(f"Session '{session_key}' not found.")
    return 1


async def _search_sessions(query: str, limit: int, as_json: bool) -> int:
    from pocketpaw.memory.manager import get_memory_manager

    mm = get_memory_manager()
    results = await mm.search_sessions(query, limit=limit)

    if as_json:
        output_json(results)
        return 0

    print_header("Session Search", f"query: '{query}'")

    if not results:
        print(f"  {DIM}No matches found.{RESET}\n")
        return 0

    for r in results:
        session = r.get("session_key", "unknown")
        matches = r.get("matches", [])
        print(f"  {BOLD}{session}{RESET} ({len(matches)} match(es))")
        for m in matches[:3]:
            role = m.get("role", "")
            content = m.get("content", "")[:80]
            print(f"    {DIM}{role}: {content}{RESET}")
        print()

    return 0
