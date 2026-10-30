"""Rewrite stored ``Site.plan_tier`` values from the pre-2026-08-22 tier names
(basic / pro / business) to the pricing-spec ladder (free / site / staff).

Created 2026-08-22 (feat/site-pricing-ladder). Companion to
``scripts/census_site_plans.py``, which counts the same rows read-only — run the
census first, run this, then run the census again to confirm zero legacy keys
remain.

THIS SCRIPT IS OPTIONAL AND ALWAYS HAS BEEN. ``site_plans`` resolves the legacy
keys permanently through ``_LEGACY_SITE_TIER_ALIASES``, so a database that never
sees this script keeps working: ``pro`` resolves to ``site``, ``business`` to
``staff``, ``basic`` to ``free``, with identical capabilities. What the rewrite
buys is that reads stop going through an alias, dashboards stop showing a key the
catalog no longer lists, and the aliases become dead weight rather than
load-bearing. It is a tidy-up, not a rescue — which is why it defaults to a DRY
RUN and will not write anything until told to.

WHY THE MAPPING IS WHAT IT IS. It maps by capability, not by ladder position:

    basic    -> free    $0,  badged, one domained site
    pro      -> site    was $5/mo, badge off + domains, no concierge
    business -> staff   was $19/mo, adds the visitor concierge

``pro`` did not sell the concierge and ``site`` does not either. ``business``
did and ``staff`` does. A position-based mapping over the new five-rung ladder
would have landed ``business`` on ``studio``, which is an ORG flat — a key that
is not even legal in this field.

WHAT IT REFUSES TO TOUCH. Any value that is not one of the three legacy keys:
already-migrated rows, empty strings, nulls, and anything unrecognised. An
unrecognised tier is left exactly as found and reported, because the safe move
for a value nobody planned for is to leave it for a human — rewriting it to the
floor would silently revoke a capability, and this script has no way to know
whether that value came from a future schema or a typo.

Usage — the URI comes from the environment, never from a command-line argument,
so a production connection string does not land in shell history:

    CLOUD_MONGODB_URI=... uv run python scripts/migrate_site_plan_keys.py
    CLOUD_MONGODB_URI=... uv run python scripts/migrate_site_plan_keys.py --apply
    CLOUD_MONGODB_URI=... uv run python scripts/migrate_site_plan_keys.py --apply --json

Without ``--apply`` it connects, counts, prints exactly what it WOULD change, and
exits 0 having written nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any

# The rewrite table. Imported from the catalog when the ee package is available
# so the two can never disagree; the literals are the fallback for a deployment
# host that has no Python environment for ee installed. The import is the
# preferred path precisely because THIS table is the thing most likely to rot.
_FALLBACK_ALIASES: dict[str, str] = {"basic": "free", "pro": "site", "business": "staff"}


def _aliases() -> tuple[dict[str, str], str]:
    """(alias table, provenance) — from the catalog if importable."""
    try:
        from pocketpaw_ee.cloud.billing import site_plans

        table = {
            old: new
            for old, new in site_plans._LEGACY_SITE_TIER_ALIASES.items()  # noqa: SLF001
        }
        # Cross-check rather than trust: an alias pointing at a key the catalog
        # does not list would rewrite live rows to a tier that resolves to None,
        # which is the one outcome worse than leaving them alone.
        known = {t.key for t in site_plans.list_site_plans()}
        bad = {old: new for old, new in table.items() if new not in known}
        if bad:
            raise RuntimeError(f"alias table points at unknown tiers: {bad}")
        return table, "imported from site_plans"
    except Exception as exc:  # noqa: BLE001 - degrade to literals, but say why
        return _FALLBACK_ALIASES, f"fallback literals ({type(exc).__name__})"


def _db_label(uri: str) -> tuple[str, str]:
    """(db_name, host_class) — never the URI itself, which carries credentials."""
    tail = uri.rsplit("/", 1)[-1]
    db_name = tail.split("?")[0] or "paw-enterprise"
    lowered = uri.lower()
    if "localhost" in lowered or "127.0.0.1" in lowered:
        host_class = "local"
    elif "mongodb+srv" in lowered:
        host_class = "atlas/srv"
    else:
        host_class = "remote"
    return db_name, host_class


async def _run(db: Any, aliases: dict[str, str], *, apply: bool) -> dict[str, Any]:
    """Count every distinct ``plan_tier``, then rewrite the legacy ones.

    The count runs first and separately from the write so the report is the same
    shape in dry-run and apply mode — the operator sees the identical breakdown
    either way, and the only difference is whether ``matched``/``modified`` came
    back from a real ``update_many``.
    """
    sites = db["sites"]

    pipeline = [{"$group": {"_id": "$plan_tier", "count": {"$sum": 1}}}]
    counts: dict[str, int] = {}
    async for row in sites.aggregate(pipeline):
        key = row["_id"]
        counts["<null>" if key is None else str(key)] = int(row["count"])

    legacy = {old: counts.get(old, 0) for old in sorted(aliases) if counts.get(old, 0)}
    planned = [
        {"from": old, "to": aliases[old], "documents": n} for old, n in sorted(legacy.items())
    ]

    # Anything that is neither a legacy key nor resolvable by the catalog. Reported
    # and never touched — see the module docstring.
    unrecognised = {
        key: n
        for key, n in sorted(counts.items())
        if key not in aliases and key not in {"<null>", ""} and not _is_current(key)
    }

    results: list[dict[str, Any]] = []
    if apply:
        for old, new in sorted(aliases.items()):
            if not legacy.get(old):
                continue
            # Filtered on the OLD value, so re-running is a no-op rather than a
            # second rewrite: the second pass matches nothing.
            res = await sites.update_many({"plan_tier": old}, {"$set": {"plan_tier": new}})
            results.append(
                {
                    "from": old,
                    "to": new,
                    "matched": int(res.matched_count),
                    "modified": int(res.modified_count),
                }
            )

    return {
        "applied": apply,
        "total_sites": sum(counts.values()),
        "tier_counts": counts,
        "planned": planned,
        "results": results,
        "unrecognised": unrecognised,
    }


def _is_current(key: str) -> bool:
    """Is ``key`` a tier the catalog lists under its current name?"""
    try:
        from pocketpaw_ee.cloud.billing import site_plans

        return key in {t.key for t in site_plans.list_site_plans()}
    except Exception:
        return key in {"free", "site", "staff", "studio", "agency"}


def _render(result: dict[str, Any], *, db_name: str, host_class: str, provenance: str) -> None:
    mode = "APPLIED" if result["applied"] else "DRY RUN — nothing was written"
    print(f"site plan-key migration — {mode}")
    print(f"  database   : {db_name} ({host_class})")
    print(f"  alias table: {provenance}")
    print(f"  sites      : {result['total_sites']}")
    print()

    if not result["planned"]:
        print("  no legacy tier keys found — nothing to migrate.")
    else:
        print("  legacy keys found:")
        for row in result["planned"]:
            print(f"    {row['from']:9} -> {row['to']:7} {row['documents']:>6} document(s)")

    if result["results"]:
        print()
        print("  writes:")
        for row in result["results"]:
            print(
                f"    {row['from']:9} -> {row['to']:7} "
                f"matched {row['matched']}, modified {row['modified']}"
            )

    if result["unrecognised"]:
        print()
        print("  LEFT ALONE — tier values this script does not recognise.")
        print("  Not rewritten on purpose: rewriting an unplanned value to the floor")
        print("  would silently revoke whatever it was granting. Decide these by hand.")
        for key, n in result["unrecognised"].items():
            print(f"    {key!r}: {n} document(s)")

    if not result["applied"] and result["planned"]:
        print()
        print("  re-run with --apply to write these changes.")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually write. Without it this is a read-only dry run.",
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="machine-readable")
    args = parser.parse_args()

    uri = os.environ.get("CLOUD_MONGODB_URI", "").strip()
    if not uri:
        print(
            "CLOUD_MONGODB_URI is not set.\n"
            "Passed by env and not by argument on purpose: a production connection\n"
            "string in a command line ends up in shell history.",
            file=sys.stderr,
        )
        return 2

    db_name, host_class = _db_label(uri)
    aliases, provenance = _aliases()

    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
    try:
        result = await _run(client[db_name], aliases, apply=args.apply)
    finally:
        client.close()

    if args.as_json:
        print(
            json.dumps(
                {"database": db_name, "host": host_class, **result},
                indent=2,
                default=str,
            )
        )
    else:
        _render(result, db_name=db_name, host_class=host_class, provenance=provenance)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
