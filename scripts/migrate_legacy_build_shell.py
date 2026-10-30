"""Move build-shell files that old Paw Sites authored out of their source maps.

Created 2026-09-24 (fix/sites-legacy-build-shell-migration, PP-4). Run it BEFORE
deploying paw-sites #55 (PS-1). PS-1 makes the generator refuse a svelte/react
source map that authors package.json, vite.config.*, svelte.config.js,
src/routes/+layout.ts/.js, a non-canonical paw.dependencies.json, or install config
(bunfig.toml, .npmrc, lockfiles). Before PS-1 an authored copy silently won, so
some pockets carry these files and would fail their next build or preview.

Each file is classified by ``pocketpaw_ee.sites.legacy_build_shell``:

    safe_drop     the generator emits the same thing; the file is removed
    convertible   package.json / a misspelled manifest carrying npm packages; they
                  are re-vetted by the PP-1 resolver and written to
                  paw.dependencies.json, then the file is removed
    needs_review  anything not provably equivalent; reported, never touched

DRY RUN BY DEFAULT. ``--apply`` writes through the pockets service: one write per
pocket, recorded as a draft version (revertible from the version timeline). It
never publishes and never touches a deployed artifact: the change reaches a live
site on its next publish. Idempotent (a migrated pocket has nothing left to find)
and resumable (``--after <last_pocket_id>`` from a previous report). A file edited
between the dry run and the apply is skipped with a conflict, not overwritten.

The dry run queries the npm registry to classify package.json conversions; pass
``--no-resolve`` for an offline report (conversions then show as unresolved).

Usage — the URI comes from the environment, never from a command-line argument, so
a production connection string does not land in shell history:

    POCKETPAW_CLOUD_MONGO_URI=... uv run python scripts/migrate_legacy_build_shell.py
    POCKETPAW_CLOUD_MONGO_URI=... uv run python scripts/migrate_legacy_build_shell.py \\
        --out report.json
    POCKETPAW_CLOUD_MONGO_URI=... uv run python scripts/migrate_legacy_build_shell.py \\
        --apply --batch-size 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


def _print_summary(report: dict) -> None:
    print(
        f"{report['mode']}: scanned {report['scanned']} site pocket(s), "
        f"{report['affected_pockets']} carry generator-owned files, "
        f"{report['migrated_pockets']} migrated, {len(report['errors'])} error(s)."
    )
    print("files: " + ", ".join(f"{k}={v}" for k, v in report["summary"].items()))
    for entry in report["pockets"]:
        for row in entry["files"]:
            print(
                f"  {entry['workspace']} {entry['pocket_id']} {row['file']}: "
                f"{row['class']} -> {row['action']}"
            )
    for err in report["errors"]:
        print(f"  ERROR {err['pocket_id']}: {err['error']}", file=sys.stderr)
    if report["last_pocket_id"]:
        print(f"resume with: --after {report['last_pocket_id']}")
    if report["mode"] == "dry_run":
        print("dry run: nothing written. Review needs_review rows, then re-run with --apply.")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write. Without it: dry run.")
    parser.add_argument("--workspace", help="only this workspace id")
    parser.add_argument("--after", help="resume after this pocket id")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--limit", type=int, help="stop after scanning this many pockets")
    parser.add_argument(
        "--no-resolve", action="store_true", help="dry run without registry lookups"
    )
    parser.add_argument("--json", action="store_true", dest="as_json", help="print JSON")
    parser.add_argument("--out", help="also write the JSON report to this file")
    args = parser.parse_args()

    uri = os.environ.get("POCKETPAW_CLOUD_MONGO_URI", "").strip()
    if not uri:
        print(
            "POCKETPAW_CLOUD_MONGO_URI is not set. Passed by env on purpose: a\n"
            "connection string in a command line ends up in shell history.",
            file=sys.stderr,
        )
        return 2

    from pocketpaw_ee.cloud.shared.db import close_cloud_db, init_cloud_db
    from pocketpaw_ee.sites.legacy_build_shell import run_migration

    await init_cloud_db(uri)
    try:
        report = await run_migration(
            apply=args.apply,
            workspace_id=args.workspace,
            after=args.after,
            batch_size=args.batch_size,
            limit=args.limit,
            resolve_packages=not args.no_resolve,
        )
    finally:
        await close_cloud_db()

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2, default=str)
    if args.as_json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_summary(report)
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
