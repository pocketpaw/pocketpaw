"""Give pre-brand Growth outreach rows a brand (``profile_id``).

Prospects, ICPs (hunts) and outreach drafts gained a nullable ``profile_id``
(a Growth › Social ``SocialProfile`` id). Rows written before that carry none.
For every workspace holding such rows, this script assigns the workspace's
OLDEST profile (by ``createdAt``, ties on ``_id``), or creates one named after
the workspace when it has none (``active`` stays off, so nothing starts
drafting posts).

Safe by construction: it only ever matches rows whose ``profile_id`` is null
or missing, so a branded row is never touched and a re-run changes nothing.
Every write is scoped to one workspace; the cross-workspace reads are the
discovery scans, marked ``global-read``.

Usage:
    uv run python scripts/backfill_growth_profile_id.py --dry-run  # report only
    uv run python scripts/backfill_growth_profile_id.py            # apply

    POCKETPAW_CLOUD_MONGO_URI=mongodb://localhost:27017/paw-cloud \\
        uv run python scripts/backfill_growth_profile_id.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import os
from typing import Any

# Matches a missing field as well as an explicit null.
_UNBRANDED: dict[str, Any] = {"profile_id": None}


def _collections() -> dict[str, Any]:
    from pocketpaw_ee.cloud.models.draft import Draft
    from pocketpaw_ee.cloud.models.icp import Icp
    from pocketpaw_ee.cloud.models.prospect import Prospect

    return {"prospects": Prospect, "icps": Icp, "drafts": Draft}


async def _resolve(result: Any) -> Any:
    # The async PyMongo driver returns coroutines; mongomock-motor sometimes
    # returns plain values. Same idiom as growth.service's facet aggregate.
    return await result if inspect.isawaitable(result) else result


async def _oldest_profile_id(workspace_id: str) -> str | None:
    from pocketpaw_ee.cloud.models.social_profile import SocialProfile

    docs = (
        await SocialProfile.find({"workspace": workspace_id})
        .sort([("createdAt", 1), ("_id", 1)])
        .limit(1)
        .to_list()
    )
    return str(docs[0].id) if docs else None


async def _workspace_name(workspace_id: str) -> str:
    from beanie import PydanticObjectId
    from pocketpaw_ee.cloud.models.workspace import Workspace

    try:
        ws = await Workspace.get(PydanticObjectId(workspace_id))
    except Exception:  # noqa: BLE001 — a non-ObjectId workspace key has no doc
        ws = None
    return ws.name if ws is not None and ws.name else workspace_id


async def backfill(*, dry_run: bool) -> dict[str, Any]:
    """Brand every unbranded outreach row. Returns a per-workspace report:
    ``{workspace_id: {"profile_id", "created", "counts": {collection: n}}}``.
    In dry-run ``profile_id`` is None when a brand WOULD be created, and
    nothing is written."""
    from pocketpaw_ee.cloud.models.social_profile import SocialProfile

    cols = _collections()
    workspaces: set[str] = set()
    for model in cols.values():
        # global-read: the backfill discovers which workspaces hold unbranded
        # rows; every write below is scoped back to one workspace.
        found = await _resolve(model.get_pymongo_collection().distinct("workspace", _UNBRANDED))
        workspaces.update(w for w in found if w)

    report: dict[str, Any] = {}
    for workspace_id in sorted(workspaces):
        scoped = {"workspace": workspace_id, **_UNBRANDED}
        counts = {
            name: await _resolve(model.get_pymongo_collection().count_documents(scoped))
            for name, model in cols.items()
        }
        profile_id = await _oldest_profile_id(workspace_id)
        created = profile_id is None
        name = await _workspace_name(workspace_id) if created else None

        if dry_run:
            target = f"new brand '{name}'" if created else f"brand {profile_id}"
            print(f"[dry-run] workspace={workspace_id} {counts} -> {target}")
            report[workspace_id] = {"profile_id": profile_id, "created": created, "counts": counts}
            continue

        if created:
            doc = SocialProfile(workspace=workspace_id, company_name=name or workspace_id)
            await doc.insert()
            profile_id = str(doc.id)
        for model in cols.values():
            await _resolve(
                model.get_pymongo_collection().update_many(
                    scoped, {"$set": {"profile_id": profile_id}}
                )
            )
        print(f"workspace={workspace_id} {counts} -> brand {profile_id} (created={created})")
        report[workspace_id] = {"profile_id": profile_id, "created": created, "counts": counts}

    if not report:
        print("No unbranded growth rows.")
    return report


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true", help="Report what would change without writing."
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
