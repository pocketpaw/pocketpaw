"""Rewrite every approved and draft Growth Blueprint ``strategy`` row
(``growth_blueprint_units``) into the one-audience shape: ``gtm.icp.segments`` and
``gtm.icp.icp_sentences`` folded into ``gtm.icp.target``, and
``market_research.alternatives`` moved onto ``positioning.alternatives``.

The fold is ``blueprint_units_dto.fold_strategy``, the same one the API runs on
every save and read, so a row this script has not reached still reads and saves
in the new shape. Old values fold only when non-empty and append, never replace;
a row already in the new shape is left as it is. Superseded rows are not touched
(their reads fold too).

A dry run (the default) writes nothing to Mongo. It prints each row's before and
after (target, not_for, alternatives) and writes every row it would touch (``_id``,
workspace, profile, status, version, rev and the full body before) as JSON to
``--dump <path>``, which a dry run requires; keep that file outside the repo. A
real run (``--apply``) rewrites changed rows in place, each conditional on the rev
it read, and bumps ``rev`` so an editor open on the old body gets a 409 and reloads.

It ends with the count check: rows processed must equal the strategy rows that are
approved or draft, and after a real run no such row may still hold an old key. Both
counts are printed; a mismatch exits 1.

Plain pymongo, no app boot, so it runs inside the backend container. The URI comes
from ``CLOUD_MONGODB_URI`` (else ``POCKETPAW_CLOUD_MONGO_URI``) and is printed as
host and database only.

Usage:
    uv run python scripts/migrate_growth_strategy_one_audience.py --dry-run --dump /tmp/s.json
    uv run python scripts/migrate_growth_strategy_one_audience.py --apply --dump /tmp/s.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from pocketpaw_ee.cloud.growth.social.blueprint_units_dto import fold_strategy

COLLECTION = "growth_blueprint_units"
STATUSES = ("approved", "draft")
_QUERY = {"unit": "strategy", "status": {"$in": list(STATUSES)}}


def _safe_uri(uri: str) -> str:
    """Host(s) and database name only: no user, password or query string."""
    parts = urlsplit(uri)
    return f"{parts.netloc.rsplit('@', 1)[-1]}/{parts.path.lstrip('/') or '(default)'}"


def _db_name(uri: str) -> str:
    """The database the app uses for ``uri`` (``shared.db.init_cloud_db``)."""
    return uri.rsplit("/", 1)[-1].split("?")[0] or "paw-enterprise"


def _old_shape(body: Any) -> bool:
    body = body if isinstance(body, dict) else {}
    icp = (body.get("gtm") or {}).get("icp") or {}
    research = body.get("market_research") or {}
    return any(k in icp for k in ("segments", "icp_sentences")) or "alternatives" in research


def _summary(body: dict) -> dict[str, Any]:
    icp = (body.get("gtm") or {}).get("icp") or {}
    return {
        "target": icp.get("target", ""),
        "not_for": icp.get("not_for", []),
        "alternatives": (body.get("positioning") or {}).get("alternatives", []),
        "old": {
            "segments": icp.get("segments"),
            "icp_sentences": icp.get("icp_sentences"),
            "market_research.alternatives": (body.get("market_research") or {}).get("alternatives"),
        },
    }


def run(db: Any, *, apply: bool, dump: str | None) -> int:
    """Migrate (or, without ``apply``, report) every strategy row. Returns the exit code."""
    coll = db[COLLECTION]
    rows = list(coll.find(_QUERY))
    tag = "" if apply else "[dry-run] "
    if dump:
        with open(dump, "w") as fh:
            json.dump(
                [
                    {k: r.get(k) for k in ("_id", "workspace", "profile_id", "status")}
                    | {"version": r.get("version"), "rev": r.get("rev"), "body": r.get("body")}
                    for r in rows
                ],
                fh,
                default=str,
                indent=2,
            )
        print(f"{tag}dumped {len(rows)} strategy row(s) to {dump}")
    processed = changed = raced = 0
    for r in rows:
        processed += 1
        before = r.get("body") or {}
        after = fold_strategy(before)
        where = f"workspace={r.get('workspace')} profile={r.get('profile_id')}"
        head = f"{tag}{where} {r.get('status')} v{r.get('version')}"
        if after == before:
            print(f"{head}: already new shape")
            continue
        changed += 1
        print(f"{head}:\n  before {_summary(before)}\n  after  {_summary(after)}")
        if apply:
            res = coll.update_one(
                {"_id": r["_id"], "rev": r.get("rev", 0)},
                {"$set": {"body": after, "updatedAt": datetime.now(UTC)}, "$inc": {"rev": 1}},
            )
            if res.modified_count != 1:
                raced += 1
                print("  NOT WRITTEN: the row changed since it was read (re-run to retry)")
    total = coll.count_documents(_QUERY)
    left = sum(1 for r in coll.find(_QUERY) if _old_shape(r.get("body"))) if apply else 0
    ok = processed == total and not raced and not left
    verdict = "OK" if ok else "MISMATCH"
    print(
        f"{tag}count check: {total} strategy row(s) approved or draft, {processed} processed, "
        f"{changed} {'changed' if apply else 'to change'}, {raced} raced, "
        f"{left} still old-shape -> {verdict}"
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report only (the default).")
    mode.add_argument("--apply", action="store_true", help="Rewrite the strategy rows.")
    parser.add_argument("--dump", help="Write the rows it would touch as JSON here.")
    args = parser.parse_args(argv)
    if not args.apply and not args.dump:
        parser.error("a dry run needs --dump <path> (keep it outside the repo)")

    uri = os.environ.get("CLOUD_MONGODB_URI") or os.environ.get("POCKETPAW_CLOUD_MONGO_URI")
    if not uri:
        parser.error("set CLOUD_MONGODB_URI or POCKETPAW_CLOUD_MONGO_URI")
    print(f"Connecting to: {_safe_uri(uri)}")

    from pymongo import MongoClient

    client = MongoClient(uri)
    try:
        return run(client[_db_name(uri)], apply=args.apply, dump=args.dump)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
