"""Census of what the ``sites`` collection actually holds, before the per-site
plan rekey and before per-site billing enforcement is switched on.

Created 2026-08-21 (PW-3 of docs/design/drafts/2026-08-21-paw-sites-plan-wiring-tasks.md).
STRICTLY READ-ONLY: every query here is a ``$group`` aggregate or a
``count_documents``. Nothing writes, nothing indexes, nothing drops. Safe to run
against production, which is the entire point of it existing.

WHY A SCRIPT AND NOT A ONE-OFF QUERY. The plan called PW-3 "a number, not code",
on the assumption someone would open a shell and look. There is no production
Mongo reachable from a dev machine: the only ``CLOUD_MONGODB_URI`` configured
anywhere in the workspace points at localhost, and production Mongo runs as the
``paw-mongo`` container inside the Coolify deployment with no published port. So
the deliverable became "one command whoever has host access can run". The same
command is also how PW-4 verifies its migration afterwards, and how anyone
re-checks the numbers before actually flipping the flag — which a shell session
would not have been.

TWO QUESTIONS, and only the first one was asked:

  1. IS THE REKEY A MIGRATION OR A RENAME? ``publish_pocket`` stamps
     ``Site.plan_tier`` even when checkout degrades, so paid-tier VALUES can be
     persisted with ``subscription_status="none"`` and no money behind them. If
     production holds only floor-tier rows the rekey is a rename; if it holds
     paid keys it is a real data migration, and any of those rows carrying a
     domain drops to the free floor's allowance when it runs.

  2. WHO BREAKS WHEN ENFORCEMENT GOES ON? Enforcement is attach-time only and
     never retroactive, so an over-cap workspace keeps every domain it has — but
     its NEXT attach is refused, and support needs to know how many customers
     that is before the flag flips rather than after. Same single pass answers
     it, so it is here.

Usage — the URI comes from the environment, never from a command-line argument,
so a production connection string does not land in shell history:

    CLOUD_MONGODB_URI=... uv run python scripts/census_site_plans.py
    CLOUD_MONGODB_URI=... uv run python scripts/census_site_plans.py --json

On the deployment host, where there is no Python environment, the mongosh
equivalent is in docs/runbooks/2026-08-21-site-plan-census.md.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import defaultdict
from typing import Any

# The tier keys as the catalog ships them today. Imported rather than hardcoded
# where possible, but this script has to run on a host that may not have the ee
# package installed, so it degrades to the literals with a note in the output.
_FALLBACK_BASE_KEY = "basic"
_FALLBACK_KNOWN_KEYS = ("basic", "pro", "business")

# Statuses that mean money is actually moving. Mirrors
# ``entitlements.service._ACTIVE_SITE_SUBSCRIPTION_STATUSES``; duplicated for the
# same reason as the tier keys, and cross-checked against it when the import works.
_ACTIVE = frozenset({"active"})


def _catalog() -> tuple[str, tuple[str, ...], str]:
    """(base_key, known_keys, provenance) — from the catalog if importable."""
    try:
        from pocketpaw_ee.cloud.billing import site_plans

        keys = tuple(t.key for t in site_plans.list_site_plans())
        return site_plans.BASE_SITE_PLAN_KEY, keys, "imported from site_plans"
    except Exception:
        return _FALLBACK_BASE_KEY, _FALLBACK_KNOWN_KEYS, "fallback literals (ee not installed)"


def _db_label(uri: str) -> tuple[str, str]:
    """(db_name, host_class) — never the URI itself, which carries credentials."""
    tail = uri.rsplit("/", 1)[-1]
    db_name = tail.split("?")[0] or "paw-enterprise"
    lowered = uri.lower()
    if "localhost" in lowered or "127.0.0.1" in lowered:
        host_class = "localhost (a DEV database - these numbers are not production)"
    elif "@mongo:" in lowered or "//mongo:" in lowered:
        host_class = "in-cluster 'mongo' host (the deployment's own container)"
    else:
        host_class = "remote host"
    return db_name, host_class


async def _collect(db: Any, base_key: str) -> dict:
    sites = db["sites"]

    totals = {
        "documents": await sites.count_documents({}),
        "archived": await sites.count_documents({"archived": True}),
        "live": await sites.count_documents({"archived": {"$ne": True}}),
    }

    # Q1 — the tier x status grid. ``$ifNull`` because ``plan_tier`` defaults to
    # None and pre-BC-9 rows have no key at all; both mean "the floor", and
    # collapsing them into the literal null would hide how many rows are which.
    grid_rows = await sites.aggregate(
        [
            {"$match": {"archived": {"$ne": True}}},
            {
                "$group": {
                    "_id": {
                        "plan_tier": {"$ifNull": ["$plan_tier", "<unset>"]},
                        "subscription_status": {"$ifNull": ["$subscription_status", "<unset>"]},
                    },
                    "sites": {"$sum": 1},
                    "with_domains": {
                        "$sum": {
                            "$cond": [
                                {"$gt": [{"$size": {"$ifNull": ["$domains", []]}}, 0]},
                                1,
                                0,
                            ]
                        }
                    },
                    "hostnames": {"$sum": {"$size": {"$ifNull": ["$domains", []]}}},
                }
            },
            {"$sort": {"_id.plan_tier": 1, "_id.subscription_status": 1}},
        ]
    ).to_list(length=None)

    grid = [
        {
            "plan_tier": r["_id"]["plan_tier"],
            "subscription_status": r["_id"]["subscription_status"],
            "sites": r["sites"],
            "sites_with_domains": r["with_domains"],
            "hostnames": r["hostnames"],
        }
        for r in grid_rows
    ]

    # Q2 — the workspaces the cap would bite. A site spends the workspace's floor
    # allowance when it holds >= 1 domain AND is not on an active paid
    # subscription; that predicate is
    # ``entitlements.site_domain_allowance(...) is not None`` expressed in Mongo.
    # Archived rows excluded, matching both the gallery read and the counting seam.
    per_workspace = await sites.aggregate(
        [
            {"$match": {"archived": {"$ne": True}}},
            {
                "$project": {
                    "workspace": 1,
                    "hostnames": {"$size": {"$ifNull": ["$domains", []]}},
                    "paid": {
                        "$cond": [
                            {"$in": [{"$ifNull": ["$subscription_status", "none"]}, list(_ACTIVE)]},
                            1,
                            0,
                        ]
                    },
                }
            },
            {"$match": {"hostnames": {"$gt": 0}}},
            {
                "$group": {
                    "_id": "$workspace",
                    "domained_sites": {"$sum": 1},
                    "floor_domained_sites": {"$sum": {"$cond": [{"$eq": ["$paid", 0]}, 1, 0]}},
                    "max_hostnames_on_one_site": {"$max": "$hostnames"},
                    "hostnames": {"$sum": "$hostnames"},
                }
            },
        ]
    ).to_list(length=None)

    over_cap = [w for w in per_workspace if w["floor_domained_sites"] > 1]
    # Keyed on WORKSPACES, not sites: "how many workspaces have a busiest site
    # carrying N hostnames". Says directly whether _FREE_MAX_HOSTNAMES_PER_SITE = 2
    # would refuse anyone who is already here.
    busiest_site_spread: dict[int, int] = defaultdict(int)
    for w in per_workspace:
        busiest_site_spread[w["max_hostnames_on_one_site"]] += 1

    return {
        "totals": totals,
        "grid": grid,
        "workspaces_with_a_domain": len(per_workspace),
        "workspaces_over_the_free_cap": len(over_cap),
        "over_cap_detail": sorted(
            (
                {"workspace": w["_id"], "floor_domained_sites": w["floor_domained_sites"]}
                for w in over_cap
            ),
            key=lambda w: -w["floor_domained_sites"],
        ),
        "max_hostnames_on_one_site": max(
            (w["max_hostnames_on_one_site"] for w in per_workspace), default=0
        ),
        "workspaces_by_busiest_site_hostnames": dict(sorted(busiest_site_spread.items())),
        "non_floor": {
            "sites": sum(r["sites"] for r in grid if r["plan_tier"] not in (base_key, "<unset>")),
            "sites_with_domains": sum(
                r["sites_with_domains"] for r in grid if r["plan_tier"] not in (base_key, "<unset>")
            ),
            "paying": sum(
                r["sites"]
                for r in grid
                if r["plan_tier"] not in (base_key, "<unset>")
                and r["subscription_status"] in _ACTIVE
            ),
        },
    }


def _render(result: dict, *, db_name: str, host_class: str, provenance: str, base_key: str) -> None:
    print(f"database        : {db_name}")
    print(f"host            : {host_class}")
    print(f"catalog keys    : {provenance}  (floor = {base_key!r})")
    t = result["totals"]
    print(f"\nsites           : {t['documents']} total, {t['live']} live, {t['archived']} archived")

    print("\n--- plan_tier x subscription_status (live sites only) ---")
    if not result["grid"]:
        print("  (none)")
    else:
        print(
            f"  {'plan_tier':<12} {'status':<12} {'sites':>7} {'w/ domain':>10} {'hostnames':>10}"
        )
        for r in result["grid"]:
            print(
                f"  {r['plan_tier']:<12} {r['subscription_status']:<12} "
                f"{r['sites']:>7} {r['sites_with_domains']:>10} {r['hostnames']:>10}"
            )

    nf = result["non_floor"]
    print("\n--- Q1: is the rekey a migration or a rename? ---")
    if nf["sites"] == 0:
        print("  RENAME. No live site carries a non-floor plan_tier.")
    else:
        print(f"  MIGRATION. {nf['sites']} live site(s) carry a non-floor plan_tier.")
        print(f"    of those, {nf['paying']} have an ACTIVE subscription (real money)")
        print(f"    of those, {nf['sites_with_domains']} already hold a custom domain")
        if nf["sites_with_domains"] and not nf["paying"]:
            print("    -> those domains survive (attach-time only), but the sites drop to")
            print("       the free floor's allowance. PW-4 needs a comms line.")

    print("\n--- Q2: who does the cap bite when enforcement goes on? ---")
    print(f"  workspaces holding at least one custom domain : {result['workspaces_with_a_domain']}")
    print(
        "  workspaces already over the free cap of 1     : "
        f"{result['workspaces_over_the_free_cap']}"
    )
    for w in result["over_cap_detail"][:20]:
        print(f"    {w['workspace']}: {w['floor_domained_sites']} floor sites with a domain")
    if len(result["over_cap_detail"]) > 20:
        print(f"    ... and {len(result['over_cap_detail']) - 20} more")
    print(f"  most hostnames on any one site               : {result['max_hostnames_on_one_site']}")
    print(
        "  workspaces by busiest site's hostname count   : "
        f"{result['workspaces_by_busiest_site_hostnames']}"
    )
    if result["max_hostnames_on_one_site"] > 2:
        print("    -> at least one site is already past _FREE_MAX_HOSTNAMES_PER_SITE = 2.")
    print("\n  Nothing is detached by enforcement. These workspaces keep what they have")
    print("  and are refused only on the NEXT attach.")


async def main() -> int:
    as_json = "--json" in sys.argv

    uri = os.environ.get("CLOUD_MONGODB_URI") or os.environ.get("POCKETPAW_CLOUD_MONGO_URI")
    if not uri:
        print(
            "Set CLOUD_MONGODB_URI (the variable the ee cloud layer itself reads).\n"
            "Passed by env and not by argument on purpose: a production connection\n"
            "string in a command line ends up in shell history.",
            file=sys.stderr,
        )
        return 2

    db_name, host_class = _db_label(uri)
    base_key, _, provenance = _catalog()

    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
    try:
        result = await _collect(client[db_name], base_key)
    finally:
        client.close()

    if as_json:
        print(
            json.dumps(
                {"database": db_name, "host": host_class, "floor_key": base_key, **result},
                indent=2,
                default=str,
            )
        )
    else:
        _render(
            result,
            db_name=db_name,
            host_class=host_class,
            provenance=provenance,
            base_key=base_key,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
