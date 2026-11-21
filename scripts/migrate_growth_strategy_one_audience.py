"""Rewrite every approved and draft Growth Blueprint ``strategy`` row
(``growth_blueprint_units``) into the one-audience shape: ``gtm.icp.segments`` and
``gtm.icp.icp_sentences`` folded into ``gtm.icp.target``, and
``market_research.alternatives`` moved onto ``positioning.alternatives``.

The fold is ``blueprint_units_dto.fold_strategy``, the same one the API runs on
every save and read, so a row this script has not reached still reads and saves
in the new shape. Old values fold only when non-empty and append, never replace;
a row already in the new shape is left as it is. Superseded rows are not touched.

Every run needs ``--dump <path>``: a NEW file (an existing one is refused, so a
re-run never overwrites the restore copy) holding every row the run reads
(``_id``, workspace, profile, status, version, rev and the full body before).
Keep it outside the repo. ``--workspace`` / ``--profile`` narrow the run to one
brand (a canary).

A dry run (the default) writes nothing to Mongo; it prints each row's before and
after (target, not_for, alternatives). A fold that would pass a cap (the 2000-char
target, the 20-item alternatives) prints a ``TRUNCATED`` line (field, length
before, length after); ``--apply`` refuses those rows unless ``--allow-truncate``
is passed (their full body is in the dump either way). ``--apply`` rewrites the
other changed rows in place, each conditional on the rev it read (or on no rev,
for a row that never had one), and bumps ``rev`` so an open editor gets a 409.

The count check: rows matched; would write / written; already new shape; raced;
truncating. A real run passes only when written + already new == matched and a
re-scan finds no old-shape row left; a dry run when would write + already new ==
matched. A failure exits 1. The URI is printed as host and database only.

Usage:
    uv run python scripts/migrate_growth_strategy_one_audience.py --dump /tmp/s1.json
    uv run python scripts/migrate_growth_strategy_one_audience.py --apply --dump /tmp/s2.json \\
        --workspace w1 --profile p1
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


def _query(workspace: str | None = None, profile: str | None = None) -> dict[str, Any]:
    q: dict[str, Any] = {"unit": "strategy", "status": {"$in": list(STATUSES)}}
    if workspace:
        q["workspace"] = workspace
    if profile:
        q["profile_id"] = profile
    return q


def _db_name(uri: str) -> str:
    """The database the app uses for ``uri`` (``shared.db.init_cloud_db``)."""
    return uri.rsplit("/", 1)[-1].split("?")[0] or "paw-enterprise"


def _safe_uri(uri: str) -> str:
    """Host(s) and the resolved database name only: no user, password or query."""
    return f"{urlsplit(uri).netloc.rsplit('@', 1)[-1]}/{_db_name(uri)}"


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


def _rev_match(row: dict) -> dict[str, Any]:
    return {"rev": row["rev"]} if "rev" in row else {"rev": {"$exists": False}}


def run(
    db: Any,
    *,
    apply: bool,
    dump: str,
    allow_truncate: bool = False,
    workspace: str | None = None,
    profile: str | None = None,
) -> int:
    """Migrate (or, without ``apply``, report) the strategy rows. Returns the exit code.
    The dump is written first and an existing ``dump`` path raises FileExistsError
    before anything is read or written."""
    query = _query(workspace, profile)
    coll = db[COLLECTION]
    with open(dump, "x") as fh:
        rows = list(coll.find(query))
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
    tag = "" if apply else "[dry-run] "
    print(f"{tag}dumped {len(rows)} strategy row(s) to {dump}")
    writes = already_new = raced = truncating = refused = 0
    for r in rows:
        before = r.get("body") or {}
        overflow: list = []
        after = fold_strategy(before, truncate=True, overflow=overflow)
        where = f"workspace={r.get('workspace')} profile={r.get('profile_id')}"
        head = f"{tag}{where} {r.get('status')} v{r.get('version')}"
        if after == before:
            already_new += 1
            print(f"{head}: already new shape")
            continue
        print(f"{head}:\n  before {_summary(before)}\n  after  {_summary(after)}")
        for field, length, cap in overflow:
            print(f"  TRUNCATED {field}: {length} -> {cap}")
        if overflow:
            truncating += 1
            if apply and not allow_truncate:
                refused += 1
                print("  REFUSED: the fold truncates; pass --allow-truncate (the dump keeps it)")
                continue
        if not apply:
            writes += 1
            continue
        res = coll.update_one(
            {"_id": r["_id"], **_rev_match(r)},
            {"$set": {"body": after, "updatedAt": datetime.now(UTC)}, "$inc": {"rev": 1}},
        )
        if res.modified_count == 1:
            writes += 1
        else:
            raced += 1
            print("  NOT WRITTEN: the row changed since it was read (re-run to retry)")
    total = coll.count_documents(query)
    left = sum(1 for r in coll.find(query) if _old_shape(r.get("body"))) if apply else 0
    ok = writes + already_new == total == len(rows) and not left
    verb = "written" if apply else "would write"
    print(
        f"{tag}count check: {total} matched, {writes} {verb}, {already_new} already new shape, "
        f"{raced} raced, {truncating} truncating ({refused} refused), "
        f"{left} still old-shape -> {'OK' if ok else 'MISMATCH'}"
    )
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report only (the default).")
    mode.add_argument("--apply", action="store_true", help="Rewrite the strategy rows.")
    parser.add_argument(
        "--dump",
        help="Required. A new JSON file (an existing one is refused) for every row read; "
        "keep it outside the repo.",
    )
    parser.add_argument(
        "--allow-truncate", action="store_true", help="Let --apply write folds cut at a cap."
    )
    parser.add_argument("--workspace", help="Only this workspace id.")
    parser.add_argument("--profile", help="Only this brand profile id.")
    args = parser.parse_args(argv)
    if not args.dump:
        parser.error("--dump <path> is required (keep it outside the repo)")
    if os.path.exists(args.dump):
        parser.error(f"--dump {args.dump} already exists; pick a new path")

    uri = os.environ.get("CLOUD_MONGODB_URI") or os.environ.get("POCKETPAW_CLOUD_MONGO_URI")
    if not uri:
        parser.error("set CLOUD_MONGODB_URI or POCKETPAW_CLOUD_MONGO_URI")
    print(f"Connecting to: {_safe_uri(uri)} (database {_db_name(uri)})")

    from pymongo import MongoClient

    client = MongoClient(uri)
    try:
        return run(
            client[_db_name(uri)],
            apply=args.apply,
            dump=args.dump,
            allow_truncate=args.allow_truncate,
            workspace=args.workspace,
            profile=args.profile,
        )
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
