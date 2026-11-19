"""Split every legacy Growth Blueprint (``growth_blueprints``) into per-unit rows
(``growth_blueprint_units``: brand, strategy, plan and one ``play:<id>`` per
channel the brand posts or sends on).

Per brand, the legacy approved row becomes the approved version of every unit at
the same version number, and a legacy draft becomes a draft of each unit it
changes. The split itself lives in ``growth.social.blueprint.migrate_profile``
(the only writer of either collection), which every Blueprint write also calls,
so a brand this script has not reached yet is migrated on its first edit.

Safe by construction: a brand that already has any unit row is skipped, so a
re-run changes nothing, and the legacy collection is only read, never dropped.
It ends with the count check: brands with a legacy approved row must equal how
many of those brands have an approved ``brand`` AND ``strategy`` unit (a brand
that started on units has no legacy row and is not counted). Exit 1 when they
differ after a real run. The Mongo URI is printed as host and database only.

Usage (dry run is the default; nothing is written without ``--apply``):
    uv run python scripts/migrate_growth_blueprint_units.py           # report only
    uv run python scripts/migrate_growth_blueprint_units.py --apply   # write

    POCKETPAW_CLOUD_MONGO_URI=mongodb://localhost:27017/paw-cloud \\
        uv run python scripts/migrate_growth_blueprint_units.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
from typing import Any
from urllib.parse import urlsplit


def _safe_uri(uri: str) -> str:
    """Host(s) and database name only: no user, password or query string."""
    parts = urlsplit(uri)
    hosts = parts.netloc.rsplit("@", 1)[-1]
    db = parts.path.lstrip("/") or "(default)"
    return f"{hosts}/{db}"


async def migrate(*, dry_run: bool) -> dict[str, Any]:
    """Migrate every brand holding a legacy row. Returns ``{"brands": {key: report},
    "legacy_approved": n, "unit_approved": m, "ok": bool}``; the counts are taken
    after the run (in a dry run they show the state before it)."""
    from pocketpaw_ee.cloud.growth.social import blueprint

    brands: dict[str, Any] = {}
    for workspace_id, profile_id in await blueprint.legacy_profiles():
        report = await blueprint.migrate_profile(workspace_id, profile_id, dry_run=dry_run)
        brands[f"{workspace_id}/{profile_id}"] = report
        tag = "[dry-run] " if dry_run else ""
        units = ", ".join(f"{u}={s}@v{v}" for u, s, v in report["units"]) or "-"
        print(f"{tag}workspace={workspace_id} profile={profile_id} {report['status']}: {units}")
    if not brands:
        print("No legacy Blueprint rows.")
    legacy, units_ok = await blueprint.migration_counts()
    ok = legacy == units_ok
    print(
        f"count check: {legacy} brand(s) with a legacy approved Blueprint, "
        f"{units_ok} of them with approved brand and strategy units"
        f" -> {'OK' if ok else 'MISMATCH'}"
    )
    return {"brands": brands, "legacy_approved": legacy, "unit_approved": units_ok, "ok": ok}


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write the unit rows.")
    args = parser.parse_args()

    uri = os.environ.get("POCKETPAW_CLOUD_MONGO_URI", "mongodb://localhost:27017/paw-cloud")
    print(f"Connecting to: {_safe_uri(uri)}")

    from pocketpaw_ee.cloud.shared.db import close_cloud_db, init_cloud_db

    await init_cloud_db(uri)
    try:
        result = await migrate(dry_run=not args.apply)
    finally:
        await close_cloud_db()
    return 0 if result["ok"] or not args.apply else 1


if __name__ == "__main__":
    import sys

    sys.exit(asyncio.run(main()))
