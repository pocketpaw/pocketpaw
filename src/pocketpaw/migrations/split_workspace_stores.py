# pocketpaw/migrations/split_workspace_stores.py — One-time migration that splits
# the shared per-process SQLite stores (fabric.db, instinct.db) into per-workspace
# files under ~/.pocketpaw/workspaces/<workspace_id>/<name>.db.
#
# Created: 2026-06-26 (ISO-4) — Companion to ISO-1/2/3 physical isolation work.
#
# Background
# ----------
# ISO-1 and ISO-2 introduced per-workspace physical isolation: new writes land in
# ~/.pocketpaw/workspaces/<workspace_id>/fabric.db (or instinct.db). But existing
# rows were written to the SHARED ~/.pocketpaw/fabric.db and instinct.db files.
# This migration reads those shared files and copies each row into the correct
# per-workspace file (grouping by the ``workspace_id`` column; NULL rows go to the
# ``system_workspace`` bucket, default ``"system0"``).
#
# Why "system0" not "__system__": the path-traversal allowlist in
# pocketpaw.stores._safe_workspace_dir requires the first character to be
# alphanumeric ([A-Za-z0-9][A-Za-z0-9_-]*). A leading underscore is rejected
# by that guard, so "__system__" is not a valid workspace dir name. "system0"
# satisfies the allowlist and is unambiguous.
#
# Idempotency
# -----------
# We use a marker file: after a successful migration we rename each shared file to
# <name>.db.migrated. Re-running detects the .migrated files and skips gracefully,
# returning a summary with skipped=True. The original shared data is therefore
# preserved (in .migrated form) for rollback; we NEVER delete it.
#
# Instinct audit re-chain (+ source-integrity gate)
# --------------------------------------------------
# The shared instinct_audit ledger has a GLOBAL hash chain whose prev_hash links
# span all workspaces interleaved. After splitting into per-workspace files, each
# workspace's chain must be RE-COMPUTED from genesis (prev_hash="") in rowid order
# within that workspace, using the existing _canonical_audit_payload + compute_audit_hash
# helpers. Non-audit tables are plain row copies.
#
# SECURITY GATE: re-chaining is only sound over AUTHENTIC rows, so BEFORE we
# re-chain we ``verify_audit_chain`` on the SHARED instinct.db. If the source
# chain is broken/tampered we ABORT (SourceChainTamperedError) — a clean
# re-chain over tampered rows would launder them into fresh valid-looking
# per-tenant chains, blessing exactly what tamper-evidence exists to catch.
# ``force=True`` overrides (logs LOUD at WARNING). The source verdict is recorded
# in the marker (see below) so the migration leaves an auditable attestation of
# what it re-chained over. The verify is read-only and runs before the rename, so
# an abort leaves the source intact.
#
# Safety contract
# ---------------
# * build_workspace_store() applies the path-traversal allowlist — no traversal possible.
# * Source instinct chain is verified intact before any re-chain (abort on tamper unless force).
# * No DELETE on source files — only rename to .migrated AFTER all rows land.
# * Idempotent: a second call is a no-op (detects .migrated marker or .workspace_split_done).
# * The marker is JSON recording the source-chain verdict + timestamp + force flag.
# * Returns a structured summary dict: per-store row counts, skipped flag, source_chain verdict.

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from pocketpaw.instinct.store import (
    InstinctStore,
    _canonical_audit_payload,
    compute_audit_hash,
)
from pocketpaw.stores import build_workspace_store

logger = logging.getLogger(__name__)

_DEFAULT_DATA_DIR = Path.home() / ".pocketpaw"

# Marker written in data_dir after a successful migration so a second run is a no-op.
_MARKER_FILENAME = ".workspace_split_done"


class SourceChainTamperedError(RuntimeError):
    """Raised when the shared instinct.db audit chain is NOT intact at migration.

    Re-chaining the per-workspace ledgers is only sound over authentic rows. If
    the source global chain is broken/tampered, the migration aborts with this
    rather than silently re-chain — a clean re-chain over tampered rows would
    launder them into fresh valid-looking per-tenant chains, blessing exactly
    what the tamper-evidence exists to catch. Pass ``force=True`` to override.
    """


async def _verify_source_instinct_chain(src: Path) -> dict[str, Any]:
    """Run verify_audit_chain on the SHARED instinct.db before any re-chain.

    Returns the verdict dict (intact / broken_at / hashed / …). Read-only — it
    never mutates the source — so it is safe to call before the rename.
    """
    return await InstinctStore(str(src)).verify_audit_chain()


def _write_marker(marker: Path, *, source_chain: dict[str, Any] | None, forced: bool) -> None:
    """Write the idempotency marker, recording the source-chain attestation.

    The marker doubles as an audit record of what the migration re-chained over:
    the source ``verify_audit_chain`` verdict + a timestamp + whether the tamper
    gate was force-overridden. ``source_chain`` is ``None`` when there was no
    instinct.db to migrate (nothing was re-chained). Best-effort on the body —
    if JSON serialization somehow fails we still create the marker (an empty
    marker is enough for idempotency), so a write hiccup can't break re-run
    safety.
    """
    payload: dict[str, Any] = {
        "migrated_at": datetime.now(UTC).isoformat(),
        "source_chain_verified": source_chain,
        "forced": forced,
    }
    try:
        marker.write_text(json.dumps(payload, indent=2))
    except Exception:  # noqa: BLE001 — marker presence is what matters for idempotency
        logger.warning("split_workspace_stores: could not write marker body", exc_info=True)
        marker.touch()


# Tables belonging to the Fabric store.
_FABRIC_TABLES = ("fabric_object_types", "fabric_objects", "fabric_links")

# Tables belonging to the Instinct store; the audit table is treated specially
# (re-chain); all others are plain copies.
_INSTINCT_NON_AUDIT_TABLES = (
    "instinct_actions",
    "instinct_corrections",
    "instinct_fabric_snapshots",
)
_INSTINCT_AUDIT_TABLE = "instinct_audit"


async def migrate_shared_stores_to_workspaces(
    data_dir: Path | None = None,
    *,
    system_workspace: str = "system0",
    force: bool = False,
) -> dict[str, Any]:
    """Split shared fabric.db and instinct.db into per-workspace files.

    Parameters
    ----------
    data_dir:
        Root of the PocketPaw data directory (where fabric.db and instinct.db
        live). Defaults to ``~/.pocketpaw`` when ``None``.
    system_workspace:
        The workspace id to assign to rows whose ``workspace_id`` column is
        NULL (pre-tenancy rows written before W4a). Defaults to ``"system0"``.
        Must satisfy the path-traversal allowlist: start with [A-Za-z0-9],
        followed only by [A-Za-z0-9_-]. Leading underscores (e.g. "__system__")
        are rejected by the allowlist guard in pocketpaw.stores.
    force:
        Security override for the source-chain integrity gate. Re-chaining the
        Instinct audit ledger per workspace is only safe over AUTHENTIC rows, so
        before re-chaining we ``verify_audit_chain`` on the SHARED instinct.db.
        If the source chain is BROKEN/tampered we ABORT
        (:class:`SourceChainTamperedError`) rather than silently re-chain — a
        clean re-chain over tampered rows would launder them into fresh
        valid-looking per-tenant chains, blessing exactly what tamper-evidence
        exists to catch. ``force=True`` overrides the abort, logs LOUD at
        WARNING, and proceeds anyway (for a deliberate operator who has
        inspected the breakage). The source-verify result is recorded in the
        migration marker either way.

    Returns
    -------
    dict with keys:
        - ``skipped`` (bool) — True when already migrated (no-op run).
        - ``fabric`` (dict) — per-workspace row counts migrated, keyed by table.
        - ``instinct`` (dict) — same for the instinct store.

    Raises
    ------
    ValueError
        If a workspace_id read from the shared DB is unsafe (would fail
        build_workspace_store's path-traversal allowlist). This is loud by
        design: a hostile id in the source data must be flagged, not silently
        dropped. The system_workspace arg is the escape hatch for NULL rows.
    """
    root = data_dir if data_dir is not None else _DEFAULT_DATA_DIR
    marker = root / _MARKER_FILENAME

    if marker.exists():
        logger.info(
            "split_workspace_stores: marker %s exists — skipping (already migrated)", marker
        )
        return {"skipped": True, "fabric": {}, "instinct": {}}

    fabric_src = root / "fabric.db"
    instinct_src = root / "instinct.db"

    # Both migrated files may already exist from a partial run; treat the marker
    # as the definitive idempotency signal so we always finish the rename pair.
    fabric_already_done = (root / "fabric.db.migrated").exists()
    instinct_already_done = (root / "instinct.db.migrated").exists()

    fabric_summary: dict[str, dict[str, int]] = {}
    instinct_summary: dict[str, dict[str, int]] = {}

    if fabric_src.exists() and not fabric_already_done:
        fabric_summary = await _migrate_fabric(fabric_src, root, system_workspace=system_workspace)
        # Rename AFTER success; any exception before this line leaves the source intact.
        fabric_src.rename(root / "fabric.db.migrated")
        logger.info(
            "split_workspace_stores: fabric migration done, source renamed to fabric.db.migrated"
        )
    else:
        if not fabric_src.exists():
            logger.info("split_workspace_stores: fabric.db not found — nothing to migrate")
        else:
            logger.info(
                "split_workspace_stores: fabric.db.migrated already present — skipping fabric"
            )

    source_chain: dict[str, Any] | None = None
    if instinct_src.exists() and not instinct_already_done:
        # SECURITY GATE (captain hard requirement): re-chaining the audit ledger
        # is only sound over AUTHENTIC rows. Verify the SHARED chain is intact
        # BEFORE we re-chain; abort on tamper unless force-overridden — never
        # launder a broken chain into fresh valid-looking per-tenant chains. This
        # runs before the rename, so an abort leaves the source untouched.
        source_chain = await _verify_source_instinct_chain(instinct_src)
        if not source_chain["intact"]:
            if not force:
                raise SourceChainTamperedError(
                    "split_workspace_stores: the shared instinct.db audit chain is "
                    f"NOT intact (broken_at={source_chain.get('broken_at')!r}). "
                    "Refusing to re-chain tampered history into per-workspace files. "
                    "Inspect the breakage; pass force=True to override deliberately."
                )
            logger.warning(
                "split_workspace_stores: source instinct.db audit chain is BROKEN "
                "(broken_at=%r) — proceeding under force=True. The migrated "
                "per-workspace chains will be freshly valid over POSSIBLY-TAMPERED "
                "rows; this override has been recorded in the marker.",
                source_chain.get("broken_at"),
            )
        instinct_summary = await _migrate_instinct(
            instinct_src, root, system_workspace=system_workspace
        )
        instinct_src.rename(root / "instinct.db.migrated")
        logger.info(
            "split_workspace_stores: instinct migration done,"
            " source renamed to instinct.db.migrated"
        )
    else:
        if not instinct_src.exists():
            logger.info("split_workspace_stores: instinct.db not found — nothing to migrate")
        else:
            logger.info(
                "split_workspace_stores: instinct.db.migrated already present — skipping instinct"
            )

    # Write the marker so re-runs are instant no-ops. Record the source-chain
    # verify result (the captain hard requirement) so the migration leaves an
    # auditable attestation of what it re-chained over.
    _write_marker(marker, source_chain=source_chain, forced=force)
    logger.info("split_workspace_stores: marker written at %s", marker)

    return {
        "skipped": False,
        "fabric": fabric_summary,
        "instinct": instinct_summary,
        "source_chain": source_chain,
    }


# ---------------------------------------------------------------------------
# Fabric migration — plain row copy, no hash chain
# ---------------------------------------------------------------------------


async def _migrate_fabric(
    src: Path,
    root: Path,
    *,
    system_workspace: str,
) -> dict[str, dict[str, int]]:
    """Copy every fabric table row into the correct per-workspace file."""
    summary: dict[str, dict[str, int]] = {t: {} for t in _FABRIC_TABLES}

    async with aiosqlite.connect(str(src)) as db:
        db.row_factory = aiosqlite.Row

        for table in _FABRIC_TABLES:
            # Check the table exists in the source DB (a very old DB may lack some).
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ) as cur:
                if await cur.fetchone() is None:
                    logger.debug(
                        "split_workspace_stores: table %s not found in fabric.db — skipping", table
                    )
                    continue

            # Read all rows.
            async with db.execute(f"SELECT * FROM {table} ORDER BY rowid ASC") as cur:  # noqa: S608
                rows = await cur.fetchall()

            # Group rows by effective workspace_id.
            by_ws: dict[str, list[Any]] = {}
            for row in rows:
                ws = _effective_ws(row, system_workspace)
                by_ws.setdefault(ws, []).append(row)

            for ws_id, ws_rows in by_ws.items():
                store = build_workspace_store("fabric", ws_id)
                # Ensure schema exists in the target file before writing.
                await store._ensure_schema()

                dest_path = store._db_path
                columns = [description[0] for description in (await _get_columns(db, table))]

                async with aiosqlite.connect(dest_path) as dest:
                    count = 0
                    for row in ws_rows:
                        row_dict = dict(zip(columns, [row[c] for c in columns], strict=False))
                        # Stamp workspace_id where it was NULL.
                        if "workspace_id" in row_dict and row_dict["workspace_id"] is None:
                            row_dict["workspace_id"] = ws_id

                        # Idempotent insert: skip if primary key already exists.
                        pk = row_dict.get("id")
                        if pk is not None:
                            async with dest.execute(
                                f"SELECT 1 FROM {table} WHERE id = ?",
                                (pk,),  # noqa: S608
                            ) as check:
                                if await check.fetchone() is not None:
                                    continue

                        placeholders = ", ".join(["?"] * len(row_dict))
                        col_names = ", ".join(row_dict.keys())
                        await dest.execute(
                            f"INSERT OR IGNORE INTO {table} ({col_names}) VALUES ({placeholders})",  # noqa: S608
                            list(row_dict.values()),
                        )
                        count += 1
                    await dest.commit()

                summary[table][ws_id] = summary[table].get(ws_id, 0) + count
                logger.debug(
                    "split_workspace_stores: fabric/%s -> ws=%s count=%d", table, ws_id, count
                )

    return summary


# ---------------------------------------------------------------------------
# Instinct migration — plain row copy + audit re-chain
# ---------------------------------------------------------------------------


async def _migrate_instinct(
    src: Path,
    root: Path,
    *,
    system_workspace: str,
) -> dict[str, dict[str, int]]:
    """Copy every instinct table row into the correct per-workspace file.

    Non-audit tables: plain row copy (grouped by workspace_id).
    instinct_audit: re-chain each per-workspace file's rows from genesis (prev_hash="")
    in original rowid order, using the canonical hash helpers from instinct.store.
    """
    summary: dict[str, dict[str, int]] = {}
    for t in (*_INSTINCT_NON_AUDIT_TABLES, _INSTINCT_AUDIT_TABLE):
        summary[t] = {}

    async with aiosqlite.connect(str(src)) as db:
        db.row_factory = aiosqlite.Row

        # --- Non-audit tables: plain copy ---
        for table in _INSTINCT_NON_AUDIT_TABLES:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ) as cur:
                if await cur.fetchone() is None:
                    logger.debug(
                        "split_workspace_stores: table %s not found in instinct.db — skipping",
                        table,
                    )
                    continue

            async with db.execute(f"SELECT * FROM {table} ORDER BY rowid ASC") as cur:  # noqa: S608
                rows = await cur.fetchall()

            by_ws: dict[str, list[Any]] = {}
            for row in rows:
                ws = _effective_ws(row, system_workspace)
                by_ws.setdefault(ws, []).append(row)

            for ws_id, ws_rows in by_ws.items():
                store = build_workspace_store("instinct", ws_id)
                await store._ensure_schema()
                dest_path = store._db_path
                columns = [d[0] for d in (await _get_columns(db, table))]

                async with aiosqlite.connect(dest_path) as dest:
                    count = 0
                    for row in ws_rows:
                        row_dict = dict(zip(columns, [row[c] for c in columns], strict=False))
                        if "workspace_id" in row_dict and row_dict["workspace_id"] is None:
                            row_dict["workspace_id"] = ws_id

                        pk = row_dict.get("id")
                        if pk is not None:
                            async with dest.execute(
                                f"SELECT 1 FROM {table} WHERE id = ?",
                                (pk,),  # noqa: S608
                            ) as check:
                                if await check.fetchone() is not None:
                                    continue

                        placeholders = ", ".join(["?"] * len(row_dict))
                        col_names = ", ".join(row_dict.keys())
                        await dest.execute(
                            f"INSERT OR IGNORE INTO {table} ({col_names}) VALUES ({placeholders})",  # noqa: S608
                            list(row_dict.values()),
                        )
                        count += 1
                    await dest.commit()

                summary[table][ws_id] = summary[table].get(ws_id, 0) + count

        # --- Audit table: read all rows, group by ws, re-chain per-ws ---
        table = _INSTINCT_AUDIT_TABLE
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ) as cur:
            if await cur.fetchone() is None:
                logger.debug(
                    "split_workspace_stores: instinct_audit not found in instinct.db — skipping"
                )
                return summary

        # Read ALL audit rows in original insertion order (rowid ASC).
        # We need EVERY column including prev_hash/entry_hash from the source,
        # because we'll DISCARD them and recompute per-workspace chain links.
        async with db.execute(
            "SELECT rowid, id, action_id, pocket_id, timestamp, actor, event,"
            " category, description, context, ai_recommendation, outcome,"
            " prev_hash, entry_hash, workspace_id"
            " FROM instinct_audit ORDER BY rowid ASC"
        ) as cur:
            audit_rows = await cur.fetchall()

        # Group by effective workspace, PRESERVING original rowid order within each group.
        by_ws_audit: dict[str, list[Any]] = {}
        for row in audit_rows:
            ws = _effective_ws(row, system_workspace)
            by_ws_audit.setdefault(ws, []).append(row)

        import json as _json

        for ws_id, ws_audit_rows in by_ws_audit.items():
            store = build_workspace_store("instinct", ws_id)
            await store._ensure_schema()
            dest_path = store._db_path

            async with aiosqlite.connect(dest_path) as dest:
                # Check which row ids are already present (idempotent).
                async with dest.execute("SELECT id FROM instinct_audit") as existing_cur:
                    existing_ids = {r[0] async for r in existing_cur}

                # Separate new rows from already-migrated ones.
                new_rows = [r for r in ws_audit_rows if r["id"] not in existing_ids]

                if not new_rows:
                    summary[table][ws_id] = 0
                    continue

                # RE-CHAIN: find the current chain head in the destination file
                # (there may be pre-existing hashed rows if schema was already
                # bootstrapped with audit entries). Start prev_hash from the
                # current chain head so we extend it correctly.
                async with dest.execute(
                    "SELECT entry_hash FROM instinct_audit"
                    " WHERE entry_hash IS NOT NULL ORDER BY rowid DESC LIMIT 1"
                ) as head_cur:
                    head_row = await head_cur.fetchone()
                running_prev = head_row[0] if head_row else ""

                count = 0
                for row in new_rows:
                    import json

                    context_raw = row["context"]
                    try:
                        context_obj = json.loads(context_raw) if context_raw else {}
                    except (json.JSONDecodeError, ValueError):
                        context_obj = {}

                    timestamp = row["timestamp"] or ""
                    ws_col = ws_id  # stamp the resolved workspace

                    # Only re-chain rows that originally had an entry_hash (hashed rows).
                    # Legacy NULL-hash rows stay un-chained (NULL prev_hash, NULL entry_hash).
                    if row["entry_hash"] is not None:
                        canonical = _canonical_audit_payload(
                            id=row["id"],
                            action_id=row["action_id"],
                            pocket_id=row["pocket_id"],
                            timestamp=timestamp,
                            actor=row["actor"],
                            event=row["event"],
                            category=row["category"],
                            description=row["description"],
                            context=context_obj,
                            ai_recommendation=row["ai_recommendation"],
                            outcome=row["outcome"],
                        )
                        new_entry_hash = compute_audit_hash(canonical, running_prev)
                        new_prev_hash = running_prev
                        running_prev = new_entry_hash
                    else:
                        # Legacy un-hashed row: preserve NULL hashes.
                        new_prev_hash = None
                        new_entry_hash = None

                    context_json = _json.dumps(context_obj) if not context_raw else context_raw

                    await dest.execute(
                        "INSERT OR IGNORE INTO instinct_audit"
                        " (id, action_id, pocket_id, timestamp, actor, event,"
                        " category, description, context, ai_recommendation,"
                        " outcome, prev_hash, entry_hash, workspace_id)"
                        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            row["id"],
                            row["action_id"],
                            row["pocket_id"],
                            timestamp,
                            row["actor"],
                            row["event"],
                            row["category"] or "decision",
                            row["description"],
                            context_json,
                            row["ai_recommendation"],
                            row["outcome"],
                            new_prev_hash,
                            new_entry_hash,
                            ws_col,
                        ),
                    )
                    count += 1

                await dest.commit()

            summary[table][ws_id] = count
            logger.debug(
                "split_workspace_stores: instinct_audit -> ws=%s count=%d (re-chained)",
                ws_id,
                count,
            )

    return summary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _effective_ws(row: Any, system_workspace: str) -> str:
    """Return the effective workspace id for a row, substituting NULL with system_workspace."""
    try:
        ws = row["workspace_id"]
    except (IndexError, KeyError):
        ws = None
    return ws if (ws is not None and ws != "") else system_workspace


async def _get_columns(db: aiosqlite.Connection, table: str) -> list[tuple[str, ...]]:
    """Return column descriptions for a table via PRAGMA table_info."""
    async with db.execute(f"PRAGMA table_info({table})") as cur:  # noqa: S608
        infos = await cur.fetchall()
    # PRAGMA table_info columns: cid, name, type, notnull, dflt_value, pk
    return [(info[1],) for info in infos]
