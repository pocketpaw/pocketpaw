"""One-off migration — promote legacy ``type="private"`` groups to ``"channel"``.

Before the channel/group split was enforced on the create path, the frontend
"New Group" button created rooms with ``type="private"``, which are only
visible to their explicit members. Operators who expected these to behave as
workspace channels can run this script to flip the type for selected groups.

Usage:

    # Dry-run across a workspace (default — no writes)
    uv run python backend/scripts/migrate_private_groups_to_channels.py \\
        --workspace <workspace_id>

    # Commit the changes
    uv run python backend/scripts/migrate_private_groups_to_channels.py \\
        --workspace <workspace_id> --apply

    # Target specific group IDs
    uv run python backend/scripts/migrate_private_groups_to_channels.py \\
        --group <id1> --group <id2> --apply

``dm`` groups are never touched. ``public`` and ``channel`` groups are already
workspace-visible and are left alone.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from beanie import PydanticObjectId


async def _run(workspace_id: str | None, group_ids: list[str], apply: bool) -> int:
    from ee.cloud.models.group import Group
    from ee.cloud.shared.db import close_cloud_db, init_cloud_db

    mongo_uri = os.environ.get("MONGO_URI", "mongodb://localhost:27017/paw-enterprise")
    await init_cloud_db(mongo_uri)

    try:
        query: dict = {"type": "private", "archived": False}
        if workspace_id:
            query["workspace"] = workspace_id
        if group_ids:
            query["_id"] = {"$in": [PydanticObjectId(gid) for gid in group_ids]}

        candidates = await Group.find(query).to_list()
        if not candidates:
            print("No private groups matched the filter.")
            return 0

        print(f"Found {len(candidates)} private group(s):")
        for g in candidates:
            print(f"  - {g.id}  name={g.name!r}  workspace={g.workspace}  members={len(g.members)}")

        if not apply:
            print("\nDry run — pass --apply to promote these to type='channel'.")
            return 0

        for g in candidates:
            g.type = "channel"
            await g.save()
        print(f"\nUpdated {len(candidates)} group(s) to type='channel'.")
        return 0
    finally:
        await close_cloud_db()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", help="Workspace ID to scope the migration to.")
    parser.add_argument(
        "--group",
        action="append",
        default=[],
        help="Specific group ID(s) to migrate. Can be repeated. Overrides --workspace filter.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit the changes. Default is a dry run.",
    )
    args = parser.parse_args()

    if not args.workspace and not args.group:
        parser.error("Provide --workspace or at least one --group.")

    return asyncio.run(_run(args.workspace, args.group, args.apply))


if __name__ == "__main__":
    sys.exit(main())
