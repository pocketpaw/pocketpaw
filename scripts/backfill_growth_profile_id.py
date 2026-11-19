"""Give pre-brand Growth outreach rows a brand (``profile_id``).

Prospects, ICPs (hunts) and outreach drafts gained a nullable ``profile_id``
(a Growth › Social ``SocialProfile`` id). Rows written before that carry none.
For every workspace holding such rows: an unbranded draft first takes its
prospect's brand; every row still unbranded then gets the workspace's default
brand from ``growth.service.resolve_brand``, the same rule every new row is
written under: the OLDEST unarchived profile (by ``createdAt``, ties on
``_id``), or a new one named after the workspace when it has none (``active``
stays off, so nothing starts drafting posts). The Mongo URI is printed as host
and database only, never with credentials.

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
from urllib.parse import urlsplit

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


def _safe_uri(uri: str) -> str:
    """Host(s) and database name only: no user, password or query string."""
    parts = urlsplit(uri)
    hosts = parts.netloc.rsplit("@", 1)[-1]
    db = parts.path.lstrip("/") or "(default)"
    return f"{hosts}/{db}"


async def _drafts_by_prospect_brand(workspace_id: str) -> dict[str, list[Any]]:
    """Unbranded drafts whose prospect HAS a brand: ``{brand: [draft _id]}``.
    Both reads are scoped to the one workspace."""
    from beanie import PydanticObjectId

    cols = _collections()
    drafts = (
        await cols["drafts"]
        .get_pymongo_collection()
        .find({"workspace": workspace_id, **_UNBRANDED}, {"prospect_id": 1})
        .to_list(None)
    )
    oids = []
    for d in drafts:
        try:
            oids.append(PydanticObjectId(d.get("prospect_id")))
        except Exception:  # noqa: BLE001 — a malformed ref falls back to the workspace brand
            continue
    brand_of: dict[str, str] = {}
    if oids:
        cursor = (
            cols["prospects"]
            .get_pymongo_collection()
            .find(
                {"workspace": workspace_id, "_id": {"$in": oids}, "profile_id": {"$ne": None}},
                {"profile_id": 1},
            )
        )
        for row in await cursor.to_list(None):
            brand_of[str(row["_id"])] = row["profile_id"]
    out: dict[str, list[Any]] = {}
    for d in drafts:
        brand = brand_of.get(str(d.get("prospect_id")))
        if brand:
            out.setdefault(brand, []).append(d["_id"])
    return out


async def backfill(*, dry_run: bool) -> dict[str, Any]:
    """Brand every unbranded outreach row. Returns a per-workspace report:
    ``{workspace_id: {"profile_id", "created", "counts": {collection: n}}}``.
    In dry-run ``profile_id`` is None when a brand WOULD be created, and
    nothing is written."""
    from pocketpaw_ee.cloud.growth.service import resolve_brand
    from pocketpaw_ee.cloud.growth.social.service import oldest_profile_id

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
        # Drafts first take their prospect's brand.
        inherited = await _drafts_by_prospect_brand(workspace_id)
        counts["drafts_from_prospect"] = sum(len(ids) for ids in inherited.values())
        if not dry_run:
            for brand, ids in inherited.items():
                await _resolve(
                    cols["drafts"]
                    .get_pymongo_collection()
                    .update_many(
                        {"workspace": workspace_id, "_id": {"$in": ids}, **_UNBRANDED},
                        {"$set": {"profile_id": brand}},
                    )
                )
        if (
            not counts["prospects"]
            and not counts["icps"]
            and counts["drafts_from_prospect"] == counts["drafts"]
        ):
            # Every unbranded row was a draft whose prospect had a brand: no
            # workspace brand is needed, so none is resolved or created.
            print(f"workspace={workspace_id} {counts} -> drafts took their prospect's brand")
            report[workspace_id] = {"profile_id": None, "created": False, "counts": counts}
            continue
        profile_id = await oldest_profile_id(workspace_id)
        created = profile_id is None

        if dry_run:
            target = "a new brand named after the workspace" if created else f"brand {profile_id}"
            print(f"[dry-run] workspace={workspace_id} {counts} -> {target}")
            report[workspace_id] = {"profile_id": profile_id, "created": created, "counts": counts}
            continue

        profile_id = await resolve_brand(workspace_id, None)
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
    print(f"Connecting to: {_safe_uri(uri)}")

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
