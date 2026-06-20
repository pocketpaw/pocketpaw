# ee/pocketpaw_ee/sites/dedupe.py — PERF-2: non-destructive dedupe migration for
# duplicate Site docs.
#
# Created: 2026-06-18 (feat/sites-dedupe-migration, PERF-2).
#
# Why this exists: PERF-1 (feat/sites-stable-identity) made NEW publishes stable —
# ``publish`` now UPSERTS ONE Site doc per (workspace, pocket_id) keyed on a
# deterministic ``_live_object_id``. But the pre-PERF-1 code minted a fresh
# ``ObjectId()`` per publish, so EXISTING data carries a pile of duplicate Site docs
# per pocket (the "AKB" pocket had 14). ``service._canonical_site_doc`` (PERF-1)
# resolves the live one among dupes at READ time, but the dupes still clutter the
# gallery list. This migration COLLAPSES each pocket's dupes to ONE canonical doc,
# non-destructively.
#
# Contract:
#   * NON-DESTRUCTIVE — it sets ``archived=True`` on the non-canonical dupes; it
#     NEVER deletes, so the data is fully recoverable. ``list_for_workspace`` /
#     ``site_pocket_ids`` filter ``archived`` so the gallery shows one card per
#     pocket.
#   * IDEMPOTENT — re-running is safe: a pocket already collapsed to one ACTIVE doc
#     has nothing to archive on the second pass (the picker only ever considers the
#     active docs, so the canonical it already picked stays, and there is nothing
#     left to archive).
#   * DRY-RUN by default — ``dedupe_workspace`` / ``dedupe_all`` default to
#     ``apply=False``: they REPORT what they would archive and write nothing.
#     ``apply=True`` is the explicit flag that actually flips ``archived``.
#   * RUNNABLE + UNIT-TESTABLE — the canonical-pick rule is a PURE function
#     (``pick_canonical``) a test feeds in-memory docs to; the async
#     ``dedupe_workspace`` / ``dedupe_all`` drive it over the DB. The module is also
#     runnable as a management command: ``python -m pocketpaw_ee.sites.dedupe``
#     (dry-run) / ``--apply`` / ``--workspace <id>``.
#
# Canonical-pick rule (``pick_canonical``), highest priority first:
#   1. the PERF-1 stable-id doc (``_live_object_id(workspace, pocket_id)``) — every
#      post-PERF-1 publish writes here, so it IS the live identity;
#   2. otherwise the newest (by ``createdAt``) doc that is DEPLOYED and carries a
#      real ``url`` — the freshest real live build among legacy dupes;
#   3. otherwise the newest doc that carries a real ``url`` (deployed flag aside);
#   4. otherwise the newest doc overall (a pre-url-era doc still resolves).
# This mirrors ``service._canonical_site_doc`` so the migration converges on the
# SAME doc the read path already treats as live.

from __future__ import annotations

import argparse
import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from pocketpaw_ee.cloud.models.site import Site as _SiteDoc
from pocketpaw_ee.sites.service import _live_object_id

logger = logging.getLogger(__name__)


@dataclass
class PocketDedupeResult:
    """What the dedupe decided for ONE (workspace, pocket_id) group."""

    workspace_id: str
    pocket_id: str
    canonical_id: str
    archived_ids: list[str] = field(default_factory=list)

    @property
    def docs_archived(self) -> int:
        return len(self.archived_ids)


@dataclass
class DedupeReport:
    """Aggregate accounting for a dedupe run (one workspace or all)."""

    applied: bool
    pockets_scanned: int = 0
    docs_archived: int = 0
    groups: list[PocketDedupeResult] = field(default_factory=list)

    def add(self, result: PocketDedupeResult) -> None:
        self.groups.append(result)
        self.pockets_scanned += 1
        self.docs_archived += result.docs_archived


def _created_at(doc: _SiteDoc) -> datetime:
    """The doc's createdAt as a sortable value. ``TimestampedDocument`` always sets
    it, but guard a None (a hand-built in-memory doc in a test) with the epoch so
    the sort is total and never raises."""
    ts = getattr(doc, "createdAt", None)
    return ts if isinstance(ts, datetime) else datetime.min


def pick_canonical(docs: list[_SiteDoc]) -> tuple[_SiteDoc, list[_SiteDoc]]:
    """Pick the ONE canonical Site doc among a pocket's docs (PURE — no DB).

    ``docs`` are the Site docs for a SINGLE (workspace, pocket_id) — the caller
    groups them. Returns ``(canonical, [docs_to_archive])`` where the archive list
    is every other doc. The rule (highest priority first):

      1. the PERF-1 stable-id doc — derived from the group's own
         ``(workspace, pocket_id)`` via ``_live_object_id``; every post-PERF-1
         publish writes here, so it is the live identity;
      2. else the newest deployed doc that carries a real ``url`` (the freshest
         real live build among legacy dupes);
      3. else the newest doc that carries a real ``url``;
      4. else the newest doc overall.

    Mirrors ``service._canonical_site_doc`` so the migration converges on the same
    doc the read path treats as live. Raises ``ValueError`` on an empty list (the
    caller never groups an empty set)."""
    if not docs:
        raise ValueError("pick_canonical requires at least one doc")

    # Derive the stable id from the group itself (all docs share workspace +
    # pocket_id). Rule 1: the stable-id doc wins outright when present.
    stable_id = _live_object_id(docs[0].workspace, docs[0].pocket_id)
    stable = next((d for d in docs if d.id == stable_id), None)
    if stable is not None:
        canonical = stable
    else:
        newest_first = sorted(docs, key=_created_at, reverse=True)
        # Rule 2: newest deployed doc with a real url.
        canonical = next(
            (d for d in newest_first if d.deployed and d.url),
            # Rule 3: newest doc with a real url (deployed flag aside).
            next(
                (d for d in newest_first if d.url),
                # Rule 4: newest doc overall.
                newest_first[0],
            ),
        )

    to_archive = [d for d in docs if d.id != canonical.id]
    return canonical, to_archive


def _group_by_pocket(docs: list[_SiteDoc]) -> dict[tuple[str, str], list[_SiteDoc]]:
    """Group ACTIVE Site docs by (workspace, pocket_id)."""
    groups: dict[tuple[str, str], list[_SiteDoc]] = defaultdict(list)
    for doc in docs:
        groups[(doc.workspace, doc.pocket_id)].append(doc)
    return groups


async def _dedupe(query: dict, *, apply: bool) -> DedupeReport:
    """Core async dedupe over the Site docs matching ``query``.

    Reads only ACTIVE docs (``archived: {"$ne": True}``), groups them per pocket,
    picks the canonical, and archives the rest. A pocket already collapsed to one
    active doc archives nothing — so a second run over the same data is a no-op
    (idempotent). With ``apply=False`` (dry-run) it computes the FULL report but
    writes nothing.
    """
    report = DedupeReport(applied=apply)

    # Only consider ACTIVE docs. A doc already archived by a prior run is invisible
    # here, so the second pass sees one active doc per already-deduped pocket and
    # archives nothing (idempotent). ``$ne: True`` so docs predating the field
    # (no ``archived`` key) still read active.
    active_query = {**query, "archived": {"$ne": True}}
    docs = await _SiteDoc.find(active_query).to_list()

    for (workspace_id, pocket_id), group in _group_by_pocket(docs).items():
        canonical, to_archive = pick_canonical(group)
        if not to_archive:
            # One active doc already — nothing to do (the idempotent steady state).
            continue
        result = PocketDedupeResult(
            workspace_id=workspace_id,
            pocket_id=pocket_id,
            canonical_id=str(canonical.id),
            archived_ids=[str(d.id) for d in to_archive],
        )
        report.add(result)
        if apply:
            for doc in to_archive:
                doc.archived = True
                # save(): a tombstone flip, not a domain mutation — no event fired.
                await doc.save()

    return report


async def dedupe_workspace(workspace_id: str, *, apply: bool = False) -> DedupeReport:
    """Dedupe the Site docs of ONE workspace, non-destructively.

    For each (workspace, pocket_id) with multiple ACTIVE docs, keeps the canonical
    (``pick_canonical``) active and sets ``archived=True`` on the rest. DRY-RUN by
    default (``apply=False`` reports what it WOULD archive, writes nothing);
    ``apply=True`` actually archives. Idempotent — re-running archives nothing new.
    """
    return await _dedupe({"workspace": workspace_id}, apply=apply)


async def dedupe_all(*, apply: bool = False) -> DedupeReport:
    """Dedupe EVERY workspace's Site docs in one sweep (the boot / CLI unscoped
    path). Same non-destructive, idempotent, dry-run-by-default contract as
    ``dedupe_workspace`` — it just does not tenant-filter the read, so every
    (workspace, pocket_id) group is reconciled. The per-pocket grouping keys on
    BOTH fields, so pockets never bleed across workspaces."""
    return await _dedupe({}, apply=apply)


# ---------------------------------------------------------------------------
# Runnable management command.
#   python -m pocketpaw_ee.sites.dedupe                  # dry-run, all workspaces
#   python -m pocketpaw_ee.sites.dedupe --apply          # apply, all workspaces
#   python -m pocketpaw_ee.sites.dedupe --workspace ws1  # dry-run, one workspace
#   python -m pocketpaw_ee.sites.dedupe --workspace ws1 --apply
# Dry-run is the DEFAULT — ``--apply`` is the explicit flag that writes.
# ---------------------------------------------------------------------------


def _format_report(report: DedupeReport, *, workspace_id: str | None) -> str:
    scope = f"workspace {workspace_id}" if workspace_id else "ALL workspaces"
    mode = "APPLIED" if report.applied else "DRY-RUN (no writes)"
    lines = [
        f"Site dedupe — {scope} — {mode}",
        f"  pockets with dupes : {report.pockets_scanned}",
        f"  docs archived      : {report.docs_archived}",
    ]
    for g in report.groups:
        lines.append(
            f"  - {g.workspace_id}/{g.pocket_id}: keep {g.canonical_id}, "
            f"archive {g.docs_archived} ({', '.join(g.archived_ids)})"
        )
    if not report.applied and report.docs_archived:
        lines.append("  (dry-run — re-run with --apply to archive the above)")
    return "\n".join(lines)


async def _run_cli(workspace_id: str | None, apply: bool) -> DedupeReport:
    """Init the EE cloud DB connection, run the dedupe, print the report."""
    # Lazy import: the DB bootstrap is only needed for the runnable path, not for
    # the unit-tested pure / async functions (tests init Beanie via the fixture).
    # ``init_cloud_db`` reads PAW_MONGO_URI when set, else the local default — the
    # same Beanie init the cloud app boots with, so the migration sees the real
    # ``sites`` collection.
    import os

    from pocketpaw_ee.cloud.db import init_cloud_db

    mongo_uri = os.environ.get("PAW_MONGO_URI", "mongodb://localhost:27017/paw-enterprise")
    await init_cloud_db(mongo_uri)
    if workspace_id:
        report = await dedupe_workspace(workspace_id, apply=apply)
    else:
        report = await dedupe_all(apply=apply)
    print(_format_report(report, workspace_id=workspace_id))
    return report


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="python -m pocketpaw_ee.sites.dedupe",
        description=(
            "Non-destructively collapse each pocket's duplicate Site docs to one "
            "canonical (PERF-2). DRY-RUN by default; pass --apply to write."
        ),
    )
    parser.add_argument(
        "--workspace",
        dest="workspace_id",
        default=None,
        help="Limit to ONE workspace id. Omit to sweep all workspaces.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually archive the dupes. Without it the run is a dry-run.",
    )
    args = parser.parse_args(argv)
    asyncio.run(_run_cli(args.workspace_id, args.apply))


if __name__ == "__main__":
    main()
