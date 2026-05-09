"""Audit every persisted pocket against the current expression grammar.

Usage:

    cd backend && uv run python scripts/audit_ripple_specs.py
    POCKETPAW_CLOUD_MONGO_URI=mongodb://localhost:27017/paw-enterprise \
        uv run python scripts/audit_ripple_specs.py --json > audit.json

What it does:

  * Connects to the same Mongo the app uses (POCKETPAW_CLOUD_MONGO_URI,
    falling back to mongodb://localhost:27017/paw-enterprise).
  * Loads every document in the ``pockets`` collection.
  * Runs the rippleSpec validator against each.
  * Prints either a human report (default) or a JSON dump (``--json``).

Read-only — never writes back to Mongo. Safe to run on production.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from typing import Any

# When run via ``uv run python scripts/...`` the backend root is on sys.path,
# but ``ee/...`` lives one level down. Make sure both ``backend/`` and
# ``backend/src/`` resolve so the existing layout works.
_HERE = os.path.dirname(os.path.abspath(__file__))
_BACKEND = os.path.dirname(_HERE)
for _p in (_BACKEND, os.path.join(_BACKEND, "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _connect():
    from motor.motor_asyncio import AsyncIOMotorClient

    uri = os.environ.get("POCKETPAW_CLOUD_MONGO_URI", "mongodb://localhost:27017/paw-enterprise")
    client = AsyncIOMotorClient(uri)
    db_name = uri.rsplit("/", 1)[-1].split("?")[0] or "paw-enterprise"
    return client, client[db_name]


async def _audit() -> dict[str, Any]:
    from ee.cloud.ripple_validator import validate_ripple_spec

    client, db = _connect()
    try:
        pockets = await db.pockets.find(
            {}, {"name": 1, "workspace": 1, "owner": 1, "rippleSpec": 1, "type": 1}
        ).to_list(None)
    finally:
        client.close()

    rows: list[dict[str, Any]] = []
    code_counter: Counter[str] = Counter()
    clean = 0
    for doc in pockets:
        spec = doc.get("rippleSpec")
        warnings = validate_ripple_spec(spec) if isinstance(spec, dict) else []
        if not warnings:
            clean += 1
            continue
        codes = sorted({w.code for w in warnings})
        for w in warnings:
            code_counter[w.code] += 1
        rows.append(
            {
                "id": str(doc.get("_id")),
                "name": doc.get("name"),
                "workspace": doc.get("workspace"),
                "owner": doc.get("owner"),
                "type": doc.get("type"),
                "warning_count": len(warnings),
                "codes": codes,
                "warnings": [
                    {
                        "code": w.code,
                        "path": w.path,
                        "expression": w.expression,
                        "detail": w.detail,
                    }
                    for w in warnings
                ],
            }
        )

    return {
        "total_pockets": len(pockets),
        "clean": clean,
        "with_warnings": len(rows),
        "warnings_by_code": dict(code_counter),
        "pockets": rows,
    }


def _print_human(report: dict[str, Any]) -> None:
    total = report["total_pockets"]
    clean = report["clean"]
    bad = report["with_warnings"]
    # ASCII-only output — works on Windows cp1252 consoles too.
    print(f"\nRipple spec audit -- {total} pockets scanned")
    print(f"  OK    {clean} clean")
    print(f"  WARN  {bad} with grammar issues\n")

    if not bad:
        return

    print("Warnings by code:")
    for code, n in sorted(report["warnings_by_code"].items(), key=lambda kv: -kv[1]):
        print(f"  {n:>4}  {code}")
    print()

    print("Pockets needing attention:")
    for r in report["pockets"]:
        codes = ",".join(r["codes"])
        print(f"  {r['id']:24}  {r['warning_count']:>3}× [{codes}]  {r['name']!r}")
        # First example per pocket — keeps output compact.
        first = r["warnings"][0]
        print(f"      e.g. {first['path']}: {first['detail']}")
        print(f"           expr: {first['expression']}")
    print()


def main() -> None:
    import asyncio

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args()

    report = asyncio.run(_audit())

    if args.json:
        json.dump(report, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        _print_human(report)

    # Exit 1 when issues were found — useful as a CI guard if you ever
    # decide to gate pocket creation on a clean audit.
    sys.exit(1 if report["with_warnings"] > 0 else 0)


if __name__ == "__main__":
    main()
