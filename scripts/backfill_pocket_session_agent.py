"""Backfill the default agent onto orphaned pocket ``Session`` rows.

New script. Companion to the OSS-path fix that stamps the workspace's default
``pocketpaw`` agent (and pocket id) onto sessions created via
``auto_create_pocket_session``. Existing sessions written before that fix have
``agent=None`` and ``context_type="pocket"``, so they never surface in the
PocketPaw DM room (``GET /sessions?agent_id=...`` -> ``list_by_agent`` keys on
``Session.agent``). This script finds those rows, resolves each one's
workspace's ``pocketpaw`` agent, and stamps it. ``pocket`` is left as-is when
unknown (it is not recoverable from a Session row alone).

Idempotent: only touches rows with ``agent == None`` and
``context_type == "pocket"``, and skips a row when its workspace has no
default agent. Safe to re-run.

Usage:
    uv run python scripts/backfill_pocket_session_agent.py            # apply
    uv run python scripts/backfill_pocket_session_agent.py --dry-run  # report only

    POCKETPAW_CLOUD_MONGO_URI=mongodb://localhost:27017/paw-cloud \\
        uv run python scripts/backfill_pocket_session_agent.py
"""

from __future__ import annotations

import argparse
import asyncio
import os


async def backfill(*, dry_run: bool) -> int:
    """Stamp the workspace default agent onto orphaned pocket sessions.

    Returns the number of rows updated (or that would be updated, in
    ``--dry-run`` mode).
    """
    from pocketpaw_ee.cloud.chat.agent_service import _get_default_workspace_agent_id
    from pocketpaw_ee.cloud.models.session import Session

    # Cache the per-workspace agent lookup so a backlog of sessions in one
    # workspace costs a single agent query.
    agent_cache: dict[str, str | None] = {}

    async def _agent_for(workspace_id: str) -> str | None:
        if workspace_id not in agent_cache:
            agent_cache[workspace_id] = await _get_default_workspace_agent_id(workspace_id)
        return agent_cache[workspace_id]

    orphans = await Session.find(
        Session.agent == None,  # noqa: E711
        Session.context_type == "pocket",
    ).to_list()

    print(f"Found {len(orphans)} pocket session(s) with no agent.")

    updated = 0
    skipped_no_agent = 0
    for doc in orphans:
        workspace_id = doc.workspace
        if not workspace_id:
            skipped_no_agent += 1
            continue
        agent_id = await _agent_for(workspace_id)
        if not agent_id:
            skipped_no_agent += 1
            continue
        if dry_run:
            print(f"  [dry-run] {doc.sessionId} ws={workspace_id} -> agent={agent_id}")
            updated += 1
            continue
        doc.agent = agent_id
        await doc.save()
        updated += 1

    verb = "Would update" if dry_run else "Updated"
    print(f"{verb} {updated} session(s); skipped {skipped_no_agent} (no default agent).")
    return updated


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing.",
    )
    args = parser.parse_args()

    uri = os.environ.get("POCKETPAW_CLOUD_MONGO_URI", "mongodb://localhost:27017/paw-cloud")
    print(f"Connecting to: {uri}")

    from pocketpaw_ee.cloud.shared.db import close_cloud_db, init_cloud_db

    await init_cloud_db(uri)
    try:
        await backfill(dry_run=args.dry_run)
    finally:
        await close_cloud_db()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
