"""Re-express ``sites.client_invoices[].amount_cents`` as ISO 4217 minor units.

Until 2026-10-01 every client sent ``amount_cents`` as major × 100 whatever the
currency, so ¥1,500 was stored as 150000 and 1.250 KWD as 125. Amounts now mean
ISO 4217 minor units of the invoice's currency (``pocketpaw.money``). This
rewrites the invoices whose currency exponent is not 2 with
``round(old × 10^(e−2))`` (÷100 for yen, ×10 for dinar). USD/EUR/… rows are not
touched, and most databases have none to convert.

One-shot: a successful ``--apply`` records ``invoice_minor_units_v1`` in the
``schema_migrations`` collection and a second ``--apply`` refuses. Each site is
updated with a filter on its CURRENT invoice array, so an invoice recorded while
the script runs is never overwritten (that site is reported as skipped; run
again with ``--force`` after checking it).

Usage (the URI comes from the environment so it stays out of shell history):

    CLOUD_MONGODB_URI=... uv run python scripts/migrations/2026_10_01_invoice_minor_units.py
    CLOUD_MONGODB_URI=... uv run python scripts/migrations/2026_10_01_invoice_minor_units.py --apply

Without ``--apply`` it is a dry run: it prints counts per currency and writes
nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from datetime import UTC, datetime
from typing import Any

from pocketpaw.money import DEFAULT_EXPONENT, convert_legacy_minor, exponent

MIGRATION = "invoice_minor_units_v1"


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


def convert_invoices(invoices: Any, counts: Counter) -> list[Any] | None:
    """The converted invoice list, or None when nothing in it changes."""
    if not isinstance(invoices, list):
        return None
    out: list[Any] = []
    changed = False
    for inv in invoices:
        if isinstance(inv, dict):
            code = inv.get("currency") or "USD"
            amount = inv.get("amount_cents")
            if (
                isinstance(amount, int)
                and not isinstance(amount, bool)
                and exponent(code) != DEFAULT_EXPONENT
            ):
                inv = {**inv, "amount_cents": convert_legacy_minor(amount, code)}
                counts[str(code).strip().upper()] += 1
                changed = True
        out.append(inv)
    return out if changed else None


async def run(db: Any, *, apply: bool, force: bool = False) -> dict[str, Any]:
    """Count (and with ``apply``, convert) every non-2-decimal invoice amount."""
    markers = db["schema_migrations"]
    already = await markers.find_one({"_id": MIGRATION})
    if apply and already and not force:
        return {"applied": False, "refused": "already_applied", "at": already.get("applied_at")}

    sites = db["sites"]
    counts: Counter = Counter()
    planned: list[tuple[Any, list[Any], list[Any]]] = []
    query = {"client_invoices.0": {"$exists": True}}
    async for site in sites.find(query, {"client_invoices": 1}):
        old = site.get("client_invoices")
        new = convert_invoices(old, counts)
        if new is not None:
            planned.append((site["_id"], old, new))

    updated, skipped = 0, []
    if apply:
        for site_id, old, new in planned:
            res = await sites.update_one(
                {"_id": site_id, "client_invoices": old}, {"$set": {"client_invoices": new}}
            )
            if res.modified_count:
                updated += 1
            else:
                skipped.append(str(site_id))
        if not skipped:
            await markers.update_one(
                {"_id": MIGRATION},
                {"$set": {"applied_at": datetime.now(UTC).isoformat()}},
                upsert=True,
            )

    return {
        "applied": apply,
        "invoices_by_currency": dict(sorted(counts.items())),
        "sites_affected": len(planned),
        "sites_updated": updated,
        "sites_skipped": skipped,
        "previously_applied": bool(already),
    }


def _render(result: dict[str, Any], *, db_name: str, host_class: str) -> None:
    if result.get("refused"):
        print(f"{MIGRATION} already applied at {result.get('at')}; nothing done.")
        print("Pass --force to run it again (only after checking why).")
        return
    mode = "APPLIED" if result["applied"] else "DRY RUN — nothing was written"
    print(f"invoice minor-unit migration — {mode}")
    print(f"  database : {db_name} ({host_class})")
    print(f"  sites    : {result['sites_affected']} with invoices to convert")
    if not result["invoices_by_currency"]:
        print("  no non-2-decimal invoice amounts found — nothing to migrate.")
    for code, n in result["invoices_by_currency"].items():
        print(f"    {code}: {n} invoice(s), exponent {exponent(code)}")
    if result["applied"]:
        print(f"  updated  : {result['sites_updated']} site(s)")
        if result["sites_skipped"]:
            print("  SKIPPED (invoices changed while running; marker not written):")
            for site_id in result["sites_skipped"]:
                print(f"    {site_id}")
    elif result["sites_affected"]:
        print("  re-run with --apply to write these changes.")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="the default; writes nothing")
    parser.add_argument("--apply", action="store_true", help="actually write")
    parser.add_argument("--force", action="store_true", help="run again after a recorded apply")
    parser.add_argument("--json", action="store_true", dest="as_json", help="machine-readable")
    args = parser.parse_args()
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")

    uri = os.environ.get("CLOUD_MONGODB_URI", "").strip()
    if not uri:
        print("CLOUD_MONGODB_URI is not set (passed by env, not argument).", file=sys.stderr)
        return 2

    db_name, host_class = _db_label(uri)
    from motor.motor_asyncio import AsyncIOMotorClient

    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=8000)
    try:
        result = await run(client[db_name], apply=args.apply, force=args.force)
    finally:
        client.close()

    if args.as_json:
        print(json.dumps({"database": db_name, "host": host_class, **result}, indent=2))
    else:
        _render(result, db_name=db_name, host_class=host_class)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
