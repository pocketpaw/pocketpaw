# src/pocketpaw/paw_bar/catalog_store.py — a concierge's product catalog as rows
# in paw_bar.db, beside the widget that owns it (``CatalogStoreMixin``, mixed into
# ``PawBarStore``). Pure SQLite, no EE import.
#
# One row per (widget_id, item_id) in ``paw_bar_catalog_items``. ``position`` is
# the owner's order (prompt and list order); ``page_key`` is
# ``pages.url_page_key(url)`` so the visitor's page finds its product by index.
# Every row is written through ``PawBarCatalogItem``'s cleaning validators, the
# same normalisation a spec-held catalog got. ``seq`` is an INTEGER PRIMARY KEY
# on purpose: it is the FTS content rowid, and an implicit rowid may change on
# VACUUM, which would silently desync the index.
#
# Search: an FTS5 external-content table over name + description (unicode61
# under the porter stemmer, so "mugs" finds "mug"), kept in sync by
# triggers. FTS5 is probed once per store at schema setup; without it every search
# is a LIKE scan over the widget's rows (logged once per process). The probe
# function (``fts5_available``) is the test seam for the fallback.
#
# Tenancy: every method takes ``workspace_id`` and goes through the widget's
# scope (``PawBarStore._widget_in_scope``). Writes always check the widget exists
# in scope (None back when it does not); reads check only when a workspace is
# given, because the hot visitor paths resolve the widget first.
#
# Caps: ``catalog_max_items()`` (config ``pawbar_catalog_max_items``) bounds a
# widget's rows; a write past it raises ``CatalogFull`` and writes nothing.
#
# Provenance: ``origin`` is "site" for a row the site sync wrote and "owner" for
# everything else (the default, so rows from before the column are the owner's).
# Every owner write (upsert / replace / spec) stamps "owner", so an owner edit
# takes a site row over for good. ``sync_site_catalog`` only ever inserts new
# ids, updates "site" rows and marks missing "site" rows sold out (complete
# imports only); it never deletes, and skips ids in ``paw_bar_catalog_tombstones``
# (written by ``delete_catalog_items``), so a product the owner deleted stays gone.
#
# ``migrate_catalog_out_of_specs`` moves every widget spec's legacy ``catalog``
# into rows, one transaction per widget (marker ``catalog_to_table_v1``), archiving
# the spec first and only ADDING rows (``add_missing_rows``): a row already there
# wins, nothing is deleted.

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import aiosqlite
from pydantic import ValidationError

from pocketpaw.paw_bar.models import PawBarCatalogItem, PawBarCatalogRow
from pocketpaw.paw_bar.pages import url_host, url_page_key
from pocketpaw.sqlite_migrations import is_applied, mark_applied

logger = logging.getLogger(__name__)

CATALOG_MIGRATION = "catalog_to_table_v1"
DEFAULT_CATALOG_MAX_ITEMS = 5000
CATALOG_SOURCES = frozenset(
    {"manual", "shopify", "woocommerce", "jsonld", "opengraph", "csv", "site"}
)
# Ids per read; a card or a bulk call never names more.
MAX_LOOKUP_IDS = 500
_MAX_QUERY_TOKENS = 16
_LIKE_SCAN_ROWS = 1000

CATALOG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS paw_bar_catalog_items (
    seq INTEGER PRIMARY KEY,
    widget_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    name TEXT NOT NULL,
    price_cents INTEGER NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'USD',
    image_url TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    page_key TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    in_stock INTEGER,
    source TEXT NOT NULL DEFAULT 'manual',
    origin TEXT NOT NULL DEFAULT 'owner',
    updated_at TEXT NOT NULL,
    UNIQUE (widget_id, item_id)
);
CREATE TABLE IF NOT EXISTS paw_bar_catalog_tombstones (
    widget_id TEXT NOT NULL,
    item_id TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    PRIMARY KEY (widget_id, item_id)
);
CREATE INDEX IF NOT EXISTS idx_catalog_page ON paw_bar_catalog_items(widget_id, page_key);
CREATE INDEX IF NOT EXISTS idx_catalog_pos ON paw_bar_catalog_items(widget_id, position);
"""

CATALOG_FTS_SQL = """
CREATE VIRTUAL TABLE IF NOT EXISTS paw_bar_catalog_fts USING fts5(
    name, description, content='paw_bar_catalog_items', content_rowid='seq',
    tokenize='porter unicode61 remove_diacritics 2'
);
CREATE TRIGGER IF NOT EXISTS paw_bar_catalog_fts_ai AFTER INSERT ON paw_bar_catalog_items BEGIN
    INSERT INTO paw_bar_catalog_fts(rowid, name, description)
    VALUES (new.seq, new.name, new.description);
END;
CREATE TRIGGER IF NOT EXISTS paw_bar_catalog_fts_ad AFTER DELETE ON paw_bar_catalog_items BEGIN
    INSERT INTO paw_bar_catalog_fts(paw_bar_catalog_fts, rowid, name, description)
    VALUES ('delete', old.seq, old.name, old.description);
END;
CREATE TRIGGER IF NOT EXISTS paw_bar_catalog_fts_au
AFTER UPDATE OF name, description ON paw_bar_catalog_items BEGIN
    INSERT INTO paw_bar_catalog_fts(paw_bar_catalog_fts, rowid, name, description)
    VALUES ('delete', old.seq, old.name, old.description);
    INSERT INTO paw_bar_catalog_fts(rowid, name, description)
    VALUES (new.seq, new.name, new.description);
END;
"""

_COLUMNS = (
    "item_id, name, price_cents, currency, image_url, url, description, in_stock,"
    " position, source, updated_at, origin"
)
_UPSERT_SQL = (
    "INSERT INTO paw_bar_catalog_items (widget_id, item_id, position, name, price_cents,"
    " currency, image_url, url, page_key, description, in_stock, source, updated_at, origin)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT(widget_id, item_id) DO UPDATE SET name = excluded.name,"
    " price_cents = excluded.price_cents, currency = excluded.currency,"
    " image_url = excluded.image_url, url = excluded.url, page_key = excluded.page_key,"
    " description = excluded.description, in_stock = excluded.in_stock,"
    " source = excluded.source, updated_at = excluded.updated_at, origin = excluded.origin"
)
# The site sync's update: only a row the site still owns, so an owner edit that
# landed first is never overwritten.
_SITE_UPDATE_SQL = (
    "UPDATE paw_bar_catalog_items SET name = ?, price_cents = ?, currency = ?, image_url = ?,"
    " url = ?, page_key = ?, description = ?, in_stock = ?, source = ?, updated_at = ?"
    " WHERE widget_id = ? AND item_id = ? AND origin = 'site'"
)
ORIGIN_SITE = "site"
ORIGIN_OWNER = "owner"
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)
# Dropped from a visitor's question before it is searched: they match every
# description and only add noise to an OR query.
_STOPWORDS = frozenset(
    "a an and any are can do does for have has how i in is it me much my of on or "
    "the this that to what with you your".split()
)
_ID_PREFIX_SOURCES = {
    "shopify": "shopify",
    "woo": "woocommerce",
    "web": "jsonld",
    "csv": "csv",
    "site": "site",
}
_warned_no_fts = False


class CatalogFull(ValueError):
    """A catalog write would take the widget past its item cap."""

    code = "catalog_full"

    def __init__(self, limit: int) -> None:
        super().__init__(f"a concierge catalog holds at most {limit} items")
        self.limit = limit


def catalog_max_items() -> int:
    """The configured per-widget item cap (``pawbar_catalog_max_items``)."""
    try:
        from pocketpaw.config import get_settings

        return int(getattr(get_settings(), "pawbar_catalog_max_items", DEFAULT_CATALOG_MAX_ITEMS))
    except Exception:  # noqa: BLE001 — a broken config must not unbound the cap
        return DEFAULT_CATALOG_MAX_ITEMS


def source_for(item_id: str, given: Any = None) -> str:
    """A row's ``source``: the one given when valid, else read off the id prefix
    the importers mint (``shopify:`` …), else ``manual``."""
    if isinstance(given, str) and given in CATALOG_SOURCES:
        return given
    prefix = item_id.split(":", 1)[0] if ":" in item_id else ""
    return _ID_PREFIX_SOURCES.get(prefix, "manual")


async def fts5_available(db: aiosqlite.Connection) -> bool:
    """Whether this SQLite build has FTS5, by creating a throwaway table."""
    try:
        await db.execute("CREATE VIRTUAL TABLE temp._pawbar_fts5_probe USING fts5(x)")
        await db.execute("DROP TABLE temp._pawbar_fts5_probe")
        return True
    except aiosqlite.OperationalError:
        return False


async def ensure_catalog_search(db: aiosqlite.Connection) -> bool:
    """Create the FTS index and its triggers when FTS5 exists; True when it does.

    An index created over existing rows (a file first opened without FTS5) is
    rebuilt from them, so search never misses rows written before it existed."""
    global _warned_no_fts
    if not await fts5_available(db):
        if not _warned_no_fts:
            _warned_no_fts = True
            logger.warning("paw_bar: SQLite has no FTS5; catalog search falls back to LIKE")
        return False
    async with db.execute("SELECT 1 FROM sqlite_master WHERE name = 'paw_bar_catalog_fts'") as cur:
        existed = await cur.fetchone() is not None
    await db.executescript(CATALOG_FTS_SQL)
    if not existed:
        await db.execute("INSERT INTO paw_bar_catalog_fts(paw_bar_catalog_fts) VALUES ('rebuild')")
    await db.commit()
    return True


def _tokens(text: str, *, drop_stopwords: bool) -> list[str]:
    seen: dict[str, None] = {}
    for token in _TOKEN_RE.findall((text or "").lower()):
        if drop_stopwords and token in _STOPWORDS:
            continue
        seen.setdefault(token, None)
    return list(seen)[:_MAX_QUERY_TOKENS]


def _fts_match(tokens: Sequence[str], *, any_token: bool) -> str:
    """An FTS5 MATCH string: each token quoted (``\\w`` only, so no quote can
    appear inside), prefix-matched from three characters."""
    terms = [f'"{t}"*' if len(t) >= 3 else f'"{t}"' for t in tokens]
    return (" OR " if any_token else " AND ").join(terms)


def _like(token: str) -> str:
    escaped = token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _like_clause(tokens: Sequence[str], *, any_token: bool) -> tuple[str, list[Any]]:
    one = "(LOWER(name) LIKE ? ESCAPE '\\' OR LOWER(description) LIKE ? ESCAPE '\\')"
    params: list[Any] = []
    for token in tokens:
        params.extend([_like(token), _like(token)])
    return "(" + (" OR " if any_token else " AND ").join([one] * len(tokens)) + ")", params


def _row_to_item(row: Sequence[Any]) -> PawBarCatalogRow:
    return PawBarCatalogRow(
        id=row[0],
        name=row[1],
        price_cents=int(row[2] or 0),
        currency=row[3] or "USD",
        image_url=row[4] or "",
        url=row[5] or "",
        description=row[6] or "",
        in_stock=None if row[7] is None else bool(row[7]),
        position=int(row[8] or 0),
        source=row[9] or "manual",
        updated_at=row[10] or "",
        origin=row[11] or ORIGIN_OWNER,
    )


def clean_items(items: Iterable[Any]) -> list[tuple[PawBarCatalogItem, str]]:
    """Each item validated as a ``PawBarCatalogItem`` with its row ``source``.

    Raises ``ValueError`` on a duplicate id and pydantic's ``ValidationError`` on
    an item the model rejects."""
    out: list[tuple[PawBarCatalogItem, str]] = []
    seen: set[str] = set()
    for raw in items:
        given = raw.get("source") if isinstance(raw, dict) else getattr(raw, "source", None)
        if isinstance(raw, PawBarCatalogItem):
            item = PawBarCatalogItem.model_validate(raw.model_dump(include=_ITEM_FIELDS))
        else:
            item = PawBarCatalogItem.model_validate(raw)
        if item.id in seen:
            raise ValueError(f"duplicate catalog id {item.id!r}")
        seen.add(item.id)
        out.append((item, source_for(item.id, given)))
    return out


_ITEM_FIELDS = set(PawBarCatalogItem.model_fields)


def _row_params(
    widget_id: str,
    item: PawBarCatalogItem,
    position: int,
    source: str,
    now: str,
    origin: str = ORIGIN_OWNER,
) -> tuple[Any, ...]:
    return (
        widget_id,
        item.id,
        position,
        item.name,
        item.price_cents,
        item.currency,
        item.image_url,
        item.url,
        url_page_key(item.url) or "",
        item.description,
        None if item.in_stock is None else int(item.in_stock),
        source,
        now,
        origin,
    )


async def replace_rows(
    db: aiosqlite.Connection,
    widget_id: str,
    cleaned: Sequence[tuple[PawBarCatalogItem, str]],
    now: str,
) -> int:
    """Swap a widget's rows for ``cleaned`` in owner order, on the caller's
    transaction. Returns the new row count."""
    await db.execute("DELETE FROM paw_bar_catalog_items WHERE widget_id = ?", (widget_id,))
    await db.executemany(
        _UPSERT_SQL,
        [_row_params(widget_id, item, i, src, now) for i, (item, src) in enumerate(cleaned)],
    )
    return len(cleaned)


async def upsert_rows(
    db: aiosqlite.Connection,
    widget_id: str,
    cleaned: Sequence[tuple[PawBarCatalogItem, str]],
    now: str,
    cap: int,
) -> tuple[int, int]:
    """Create or update ``cleaned`` by id on the caller's transaction, never
    deleting: an existing item keeps its position, a new one is appended.
    Returns ``(upserted, total)``; ``CatalogFull`` (nothing written) when the new
    ids would take the widget past ``cap``."""
    async with db.execute(
        "SELECT item_id FROM paw_bar_catalog_items WHERE widget_id = ?", (widget_id,)
    ) as cur:
        existing = {r[0] for r in await cur.fetchall()}
    new = [item for item, _ in cleaned if item.id not in existing]
    total = len(existing) + len(new)
    if total > cap:
        raise CatalogFull(cap)
    async with db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM paw_bar_catalog_items WHERE widget_id = ?",
        (widget_id,),
    ) as cur:
        row = await cur.fetchone()
        next_position = int(row[0]) if row else 0
    params = []
    for item, source in cleaned:
        position = 0
        if item.id not in existing:
            position, next_position = next_position, next_position + 1
        params.append(_row_params(widget_id, item, position, source, now))
    await db.executemany(_UPSERT_SQL, params)
    return len(cleaned), total


_INSERT_MISSING_SQL = _UPSERT_SQL.split(" ON CONFLICT")[0] + (
    " ON CONFLICT(widget_id, item_id) DO NOTHING"
)


def clean_legacy_catalog(catalog: Any, widget_id: str) -> list[tuple[PawBarCatalogItem, str]]:
    """A stored spec's legacy ``catalog`` list, cleaned like any write. An item
    the model rejects or a repeated id is skipped and logged, never raised:
    this runs on data an older build already accepted."""
    cleaned: list[tuple[PawBarCatalogItem, str]] = []
    seen: set[str] = set()
    for raw in catalog if isinstance(catalog, list) else []:
        try:
            item = PawBarCatalogItem.model_validate(raw)
        except ValidationError:
            logger.warning("paw_bar catalog migration: %s: invalid item skipped", widget_id)
            continue
        if item.id in seen:
            continue
        seen.add(item.id)
        cleaned.append((item, source_for(item.id)))
    return cleaned


async def add_missing_rows(
    db: aiosqlite.Connection,
    widget_id: str,
    cleaned: Sequence[tuple[PawBarCatalogItem, str]],
    now: str,
) -> int:
    """Insert the items the widget does not hold yet, after its existing rows, on
    the caller's transaction. A row already there wins: it may be a newer edit
    made through the catalog routes. Never deletes, and not capped (it moves
    data an older build accepted). Returns how many rows were added."""
    async with db.execute(
        "SELECT item_id FROM paw_bar_catalog_items WHERE widget_id = ?", (widget_id,)
    ) as cur:
        existing = {r[0] for r in await cur.fetchall()}
    async with db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM paw_bar_catalog_items WHERE widget_id = ?",
        (widget_id,),
    ) as cur:
        row = await cur.fetchone()
        position = int(row[0]) if row else 0
    params = []
    for item, source in cleaned:
        if item.id in existing:
            continue
        params.append(_row_params(widget_id, item, position, source, now))
        position += 1
    await db.executemany(_INSERT_MISSING_SQL, params)
    return len(params)


@dataclass
class CatalogSyncCounts:
    """What one site sync did to a widget's rows."""

    added: int = 0
    updated: int = 0
    unchanged: int = 0
    sold_out: int = 0  # site rows the complete import no longer lists, marked in_stock=False
    owner_kept: int = 0  # ids the owner has taken over (edited or created): left alone
    deleted_skipped: int = 0  # ids the owner deleted (tombstoned): not re-added
    capped: int = 0  # new products past the cap: not added


def clean_site_items(items: Iterable[Any]) -> list[tuple[PawBarCatalogItem, str]]:
    """The importer's products cleaned like any write, in its order. An item the
    model rejects or a repeated id is skipped, never raised: one odd product on
    the site must not stop the rest from syncing."""
    cleaned: list[tuple[PawBarCatalogItem, str]] = []
    seen: set[str] = set()
    for raw in items:
        data = raw.model_dump() if hasattr(raw, "model_dump") else raw
        try:
            item = PawBarCatalogItem.model_validate(
                {k: v for k, v in dict(data).items() if k in _ITEM_FIELDS}
            )
        except (ValidationError, TypeError, ValueError):
            continue
        if item.id in seen:
            continue
        seen.add(item.id)
        cleaned.append((item, source_for(item.id)))
    return cleaned


def _site_values(item: PawBarCatalogItem) -> tuple[Any, ...]:
    """The fields a site sync owns, as the row holds them (page_key aside)."""
    in_stock = None if item.in_stock is None else int(item.in_stock)
    return (
        item.name,
        item.price_cents,
        item.currency,
        item.image_url,
        item.url,
        item.description,
        in_stock,
    )


async def sync_site_rows(
    db: aiosqlite.Connection,
    widget_id: str,
    cleaned: Sequence[tuple[PawBarCatalogItem, str]],
    now: str,
    *,
    complete: bool,
    cap: int,
) -> CatalogSyncCounts:
    """Apply a site import to a widget's rows on the caller's transaction.

    New id: appended as a "site" row until the widget reaches ``cap`` (the rest
    counted ``capped``, the importer's order kept). A "site" row: updated when
    the site changed it. An "owner" row or a tombstoned id: left alone. With
    ``complete``, a "site" row the import no longer lists is marked sold out.
    Never deletes."""
    counts = CatalogSyncCounts()
    async with db.execute(
        "SELECT item_id, origin, name, price_cents, currency, image_url, url, description,"
        " in_stock FROM paw_bar_catalog_items WHERE widget_id = ?",
        (widget_id,),
    ) as cur:
        existing = {r[0]: (r[1], tuple(r[2:])) for r in await cur.fetchall()}
    async with db.execute(
        "SELECT item_id FROM paw_bar_catalog_tombstones WHERE widget_id = ?", (widget_id,)
    ) as cur:
        tombstones = {r[0] for r in await cur.fetchall()}
    async with db.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 FROM paw_bar_catalog_items WHERE widget_id = ?",
        (widget_id,),
    ) as cur:
        row = await cur.fetchone()
        position = int(row[0]) if row else 0
    room = cap - len(existing)
    inserts: list[tuple[Any, ...]] = []
    updates: list[tuple[Any, ...]] = []
    for item, source in cleaned:
        if item.id in tombstones:
            counts.deleted_skipped += 1
            continue
        current = existing.get(item.id)
        if current is None:
            if room <= 0:
                counts.capped += 1
                continue
            inserts.append(_row_params(widget_id, item, position, source, now, ORIGIN_SITE))
            position, room = position + 1, room - 1
            counts.added += 1
        elif current[0] != ORIGIN_SITE:
            counts.owner_kept += 1
        elif current[1] == _site_values(item):
            counts.unchanged += 1
        else:
            name, price, currency, image, url, description, in_stock = _site_values(item)
            page = url_page_key(url) or ""
            updates.append(
                (name, price, currency, image, url, page, description, in_stock, source, now)
                + (widget_id, item.id)
            )
            counts.updated += 1
    await db.executemany(_INSERT_MISSING_SQL, inserts)
    await db.executemany(_SITE_UPDATE_SQL, updates)
    if complete:
        listed = {item.id for item, _ in cleaned}
        gone = [
            item_id
            for item_id, (origin, values) in existing.items()
            if origin == ORIGIN_SITE and item_id not in listed and values[-1] != 0
        ]
        await db.executemany(
            "UPDATE paw_bar_catalog_items SET in_stock = 0, updated_at = ?"
            " WHERE widget_id = ? AND item_id = ? AND origin = 'site'",
            [(now, widget_id, item_id) for item_id in gone],
        )
        counts.sold_out = len(gone)
    if counts.capped:
        logger.info(
            "paw_bar: site sync for widget %s stopped at the catalog cap (%d); %d left out",
            widget_id,
            cap,
            counts.capped,
        )
    return counts


class CatalogStoreMixin:
    """The catalog methods of ``PawBarStore``. The host class provides
    ``_ensure_schema``, ``_conn``, ``_widget_in_scope`` and ``_catalog_fts``."""

    _catalog_fts: bool

    async def _catalog_read_ok(
        self, db: aiosqlite.Connection, widget_id: str, workspace_id: str | None
    ) -> bool:
        if workspace_id is None:
            return True
        return await self._widget_in_scope(db, widget_id, workspace_id)  # type: ignore[attr-defined]

    async def _catalog_write(self, widget_id: str, workspace_id: str | None, fn: Any) -> Any:
        """Run ``fn(db, now)`` in one BEGIN IMMEDIATE transaction once the widget
        is in scope. None when it is not; any exception rolls back and propagates."""
        await self._ensure_schema()  # type: ignore[attr-defined]
        async with self._conn() as db:  # type: ignore[attr-defined]
            await db.execute("BEGIN IMMEDIATE")
            try:
                if not await self._widget_in_scope(db, widget_id, workspace_id):  # type: ignore[attr-defined]
                    await db.rollback()
                    return None
                result = await fn(db, datetime.now().isoformat())
                await db.commit()
                return result
            except BaseException:
                await db.rollback()
                raise

    # ---------------- reads ----------------

    async def catalog_count(self, widget_id: str, *, workspace_id: str | None = None) -> int:
        await self._ensure_schema()  # type: ignore[attr-defined]
        async with self._conn() as db:  # type: ignore[attr-defined]
            if not await self._catalog_read_ok(db, widget_id, workspace_id):
                return 0
            async with db.execute(
                "SELECT COUNT(*) FROM paw_bar_catalog_items WHERE widget_id = ?", (widget_id,)
            ) as cur:
                row = await cur.fetchone()
                return int(row[0]) if row else 0

    async def list_catalog(
        self,
        widget_id: str,
        *,
        offset: int = 0,
        limit: int = 50,
        q: str = "",
        workspace_id: str | None = None,
    ) -> tuple[list[PawBarCatalogRow], int]:
        """``(items, total)`` in owner order. ``q`` keeps the items whose name or
        description holds every word of it (prefix match), and ``total`` counts
        those."""
        offset, limit = max(0, int(offset)), max(0, int(limit))
        tokens = _tokens(q, drop_stopwords=False)
        await self._ensure_schema()  # type: ignore[attr-defined]
        async with self._conn() as db:  # type: ignore[attr-defined]
            if not await self._catalog_read_ok(db, widget_id, workspace_id):
                return [], 0
            if not tokens:
                where, params = "widget_id = ?", [widget_id]
            elif self._catalog_fts:
                where = (
                    "widget_id = ? AND seq IN (SELECT rowid FROM paw_bar_catalog_fts"
                    " WHERE paw_bar_catalog_fts MATCH ?)"
                )
                params = [widget_id, _fts_match(tokens, any_token=False)]
            else:
                clause, like_params = _like_clause(tokens, any_token=False)
                where, params = f"widget_id = ? AND {clause}", [widget_id, *like_params]
            async with db.execute(
                f"SELECT COUNT(*) FROM paw_bar_catalog_items WHERE {where}",  # noqa: S608
                params,
            ) as cur:
                row = await cur.fetchone()
                total = int(row[0]) if row else 0
            async with db.execute(
                f"SELECT {_COLUMNS} FROM paw_bar_catalog_items WHERE {where}"  # noqa: S608
                " ORDER BY position, seq LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ) as cur:
                items = [_row_to_item(r) for r in await cur.fetchall()]
        return items, total

    async def get_catalog_items(
        self, widget_id: str, ids: Iterable[str], *, workspace_id: str | None = None
    ) -> list[PawBarCatalogRow]:
        """The items for ``ids``, in the order asked; unknown and repeated ids
        dropped. At most ``MAX_LOOKUP_IDS`` ids are read."""
        wanted = list(dict.fromkeys(str(i).strip() for i in ids if str(i).strip()))
        wanted = wanted[:MAX_LOOKUP_IDS]
        if not wanted:
            return []
        await self._ensure_schema()  # type: ignore[attr-defined]
        async with self._conn() as db:  # type: ignore[attr-defined]
            if not await self._catalog_read_ok(db, widget_id, workspace_id):
                return []
            marks = ",".join("?" * len(wanted))
            async with db.execute(
                f"SELECT {_COLUMNS} FROM paw_bar_catalog_items"  # noqa: S608
                f" WHERE widget_id = ? AND item_id IN ({marks})",
                [widget_id, *wanted],
            ) as cur:
                found = {r[0]: _row_to_item(r) for r in await cur.fetchall()}
        return [found[i] for i in wanted if i in found]

    async def catalog_item_for_page(
        self,
        widget_id: str,
        page_key: str,
        *,
        host: str | None = None,
        workspace_id: str | None = None,
    ) -> PawBarCatalogRow | None:
        """The first item (owner order) whose url is the page at ``page_key``.

        An item with no url names no page. With ``host``, an absolute url on
        another host does not count; a site path counts as any host."""
        await self._ensure_schema()  # type: ignore[attr-defined]
        async with self._conn() as db:  # type: ignore[attr-defined]
            if not await self._catalog_read_ok(db, widget_id, workspace_id):
                return None
            async with db.execute(
                f"SELECT {_COLUMNS} FROM paw_bar_catalog_items"  # noqa: S608
                " WHERE widget_id = ? AND page_key = ? AND url != ''"
                " ORDER BY position, seq LIMIT 50",
                (widget_id, page_key),
            ) as cur:
                rows = await cur.fetchall()
        wanted = (host or "").lower()
        for row in rows:
            item_host = url_host(row[5])
            if not wanted or not item_host or item_host == wanted:
                return _row_to_item(row)
        return None

    async def search_catalog(
        self, widget_id: str, query: str, k: int = 20, *, workspace_id: str | None = None
    ) -> list[PawBarCatalogRow]:
        """The ``k`` items that best match ``query`` (any of its words, stopwords
        dropped), best first: BM25 over name (weighted twice) and description, or a LIKE scan
        scored by words found (a name hit counts twice) when FTS5 is missing."""
        tokens = _tokens(query, drop_stopwords=True)
        if not tokens or k <= 0:
            return []
        await self._ensure_schema()  # type: ignore[attr-defined]
        async with self._conn() as db:  # type: ignore[attr-defined]
            if not await self._catalog_read_ok(db, widget_id, workspace_id):
                return []
            if self._catalog_fts:
                cols = ", ".join(f"i.{c.strip()}" for c in _COLUMNS.split(","))
                async with db.execute(
                    f"SELECT {cols} FROM paw_bar_catalog_fts"  # noqa: S608
                    " JOIN paw_bar_catalog_items i ON i.seq = paw_bar_catalog_fts.rowid"
                    " WHERE paw_bar_catalog_fts MATCH ? AND i.widget_id = ?"
                    " ORDER BY bm25(paw_bar_catalog_fts, 2.0, 1.0), i.position LIMIT ?",
                    (_fts_match(tokens, any_token=True), widget_id, int(k)),
                ) as cur:
                    return [_row_to_item(r) for r in await cur.fetchall()]
            clause, params = _like_clause(tokens, any_token=True)
            async with db.execute(
                f"SELECT {_COLUMNS} FROM paw_bar_catalog_items"  # noqa: S608
                f" WHERE widget_id = ? AND {clause} ORDER BY position, seq LIMIT ?",
                [widget_id, *params, _LIKE_SCAN_ROWS],
            ) as cur:
                rows = await cur.fetchall()
        scored: list[tuple[int, int, PawBarCatalogRow]] = []
        for index, row in enumerate(rows):
            name, description = str(row[1]).lower(), str(row[6]).lower()
            score = sum(2 * (t in name) + (t in description) for t in tokens)
            scored.append((-score, index, _row_to_item(row)))
        scored.sort(key=lambda s: (s[0], s[1]))
        return [item for _, _, item in scored[: int(k)]]

    # ---------------- writes ----------------

    async def upsert_catalog_items(
        self,
        widget_id: str,
        items: Iterable[Any],
        *,
        workspace_id: str | None = None,
        max_items: int | None = None,
    ) -> tuple[int, int] | None:
        """Create or replace items by id: an existing item keeps its position, a
        new one is appended. Returns ``(upserted, total)``; None when the widget
        is not in scope. ``CatalogFull`` when the new ids would pass the cap."""
        cleaned = clean_items(items)
        cap = max_items if max_items is not None else catalog_max_items()

        async def write(db: aiosqlite.Connection, now: str) -> tuple[int, int]:
            return await upsert_rows(db, widget_id, cleaned, now, cap)

        return await self._catalog_write(widget_id, workspace_id, write)

    async def delete_catalog_items(
        self, widget_id: str, ids: Iterable[str], *, workspace_id: str | None = None
    ) -> tuple[int, int] | None:
        """Remove items by id. Returns ``(deleted, total)``; None out of scope.

        Each id actually deleted is tombstoned, so the site sync never adds it
        back."""
        wanted = list(dict.fromkeys(str(i) for i in ids))[:MAX_LOOKUP_IDS]

        async def write(db: aiosqlite.Connection, now: str) -> tuple[int, int]:
            deleted = 0
            if wanted:
                marks = ",".join("?" * len(wanted))
                await db.execute(
                    "INSERT OR REPLACE INTO paw_bar_catalog_tombstones"  # noqa: S608
                    " (widget_id, item_id, deleted_at) SELECT widget_id, item_id, ?"
                    f" FROM paw_bar_catalog_items WHERE widget_id = ? AND item_id IN ({marks})",
                    [now, widget_id, *wanted],
                )
                cur = await db.execute(
                    "DELETE FROM paw_bar_catalog_items"  # noqa: S608
                    f" WHERE widget_id = ? AND item_id IN ({marks})",
                    [widget_id, *wanted],
                )
                deleted = cur.rowcount or 0
            return deleted, await _count(db, widget_id)

        return await self._catalog_write(widget_id, workspace_id, write)

    async def reorder_catalog(
        self, widget_id: str, ids: Sequence[str], *, workspace_id: str | None = None
    ) -> int | None:
        """Put ``ids`` first, in that order; every other item follows in its
        current order. Unknown ids are ignored. Returns the total; None out of scope."""

        async def write(db: aiosqlite.Connection, _now: str) -> int:
            async with db.execute(
                "SELECT item_id, position FROM paw_bar_catalog_items"
                " WHERE widget_id = ? ORDER BY position, seq",
                (widget_id,),
            ) as cur:
                current = [(r[0], int(r[1])) for r in await cur.fetchall()]
            known = {item_id for item_id, _ in current}
            first = [i for i in dict.fromkeys(str(x) for x in ids) if i in known]
            placed = set(first)
            order = first + [item_id for item_id, _ in current if item_id not in placed]
            was = dict(current)
            await db.executemany(
                "UPDATE paw_bar_catalog_items SET position = ? WHERE widget_id = ? AND item_id = ?",
                [(i, widget_id, item_id) for i, item_id in enumerate(order) if was[item_id] != i],
            )
            return len(order)

        return await self._catalog_write(widget_id, workspace_id, write)

    async def sync_site_catalog(
        self,
        widget_id: str,
        items: Iterable[Any],
        *,
        complete: bool,
        workspace_id: str | None = None,
        max_items: int | None = None,
    ) -> CatalogSyncCounts | None:
        """Bring the widget's site-synced rows in step with ``items`` (the site
        importer's products, in its order) in one transaction; the rules are
        ``sync_site_rows``'s. None when the widget is not in scope."""
        cleaned = clean_site_items(items)
        cap = max_items if max_items is not None else catalog_max_items()

        async def write(db: aiosqlite.Connection, now: str) -> CatalogSyncCounts:
            return await sync_site_rows(db, widget_id, cleaned, now, complete=complete, cap=cap)

        return await self._catalog_write(widget_id, workspace_id, write)

    async def replace_catalog(
        self,
        widget_id: str,
        items: Iterable[Any],
        *,
        workspace_id: str | None = None,
        max_items: int | None = None,
    ) -> int | None:
        """Make ``items`` the whole catalog, in that order. Returns the total;
        None out of scope; ``CatalogFull`` past the cap (nothing written)."""
        cleaned = clean_items(items)
        cap = max_items if max_items is not None else catalog_max_items()
        if len(cleaned) > cap:
            raise CatalogFull(cap)

        async def write(db: aiosqlite.Connection, now: str) -> int:
            return await replace_rows(db, widget_id, cleaned, now)

        return await self._catalog_write(widget_id, workspace_id, write)


async def _count(db: aiosqlite.Connection, widget_id: str) -> int:
    async with db.execute(
        "SELECT COUNT(*) FROM paw_bar_catalog_items WHERE widget_id = ?", (widget_id,)
    ) as cur:
        row = await cur.fetchone()
        return int(row[0]) if row else 0


async def migrate_catalog_out_of_specs(db: aiosqlite.Connection) -> bool:
    """Move each widget spec's legacy ``catalog`` into rows (``catalog_to_table_v1``).

    One BEGIN IMMEDIATE transaction per widget: the spec is re-read inside it,
    archived as a spec revision, its items ADDED after the rows the widget already
    holds (``add_missing_rows``: an existing row wins and nothing is deleted, so a
    write made through the catalog routes before a retried migration survives;
    cleaned like any write, an invalid item or a repeated id skipped and logged),
    and the spec stored with an empty catalog. Only a widget whose spec still
    holds a catalog is touched, so a re-run is a no-op. The marker is written
    once every widget moved; a widget that failed is retried on the next start.
    Logs the largest spec left, which is what the spec size cap is then measured
    against. Returns True when the marker was written by this call.
    """
    if await is_applied(db, CATALOG_MIGRATION):
        return False
    async with db.execute("SELECT id FROM paw_bar_widgets") as cur:
        widget_ids = [r[0] for r in await cur.fetchall()]
    moved_widgets = moved_items = failures = 0
    for widget_id in widget_ids:
        await db.commit()
        await db.execute("BEGIN IMMEDIATE")
        try:
            async with db.execute(
                "SELECT spec FROM paw_bar_widgets WHERE id = ?", (widget_id,)
            ) as cur:
                row = await cur.fetchone()
            try:
                spec = json.loads(row[0]) if row else None
            except (TypeError, ValueError):
                spec = None
            catalog = spec.get("catalog") if isinstance(spec, dict) else None
            if not isinstance(catalog, list) or not catalog:
                await db.rollback()
                continue
            cleaned = clean_legacy_catalog(catalog, widget_id)
            moved = await add_missing_rows(db, widget_id, cleaned, datetime.now().isoformat())
            # The spec as it was, catalog included, stays a rollback point.
            async with db.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 FROM paw_bar_spec_revisions"
                " WHERE widget_id = ?",
                (widget_id,),
            ) as cur:
                revision = (await cur.fetchone())[0]
            await db.execute(
                "INSERT INTO paw_bar_spec_revisions (widget_id, revision, spec) VALUES (?, ?, ?)",
                (widget_id, revision, row[0]),
            )
            spec["catalog"] = []
            await db.execute(
                "UPDATE paw_bar_widgets SET spec = ? WHERE id = ?", (json.dumps(spec), widget_id)
            )
            await db.commit()
            moved_widgets += 1
            moved_items += moved
        except Exception:
            await db.rollback()
            failures += 1
            logger.exception("paw_bar catalog migration: widget %s left in its spec", widget_id)
    async with db.execute("SELECT MAX(LENGTH(CAST(spec AS BLOB))) FROM paw_bar_widgets") as cur:
        row = await cur.fetchone()
        largest = int(row[0] or 0) if row else 0
    logger.info(
        "paw_bar catalog migration: %d item(s) from %d widget(s) moved; largest spec now %d bytes",
        moved_items,
        moved_widgets,
        largest,
    )
    if failures:
        logger.warning(
            "paw_bar catalog migration: %d widget(s) failed; retrying next start", failures
        )
        return False
    await mark_applied(db, CATALOG_MIGRATION)
    return True


__all__ = [
    "CATALOG_MIGRATION",
    "CATALOG_SOURCES",
    "DEFAULT_CATALOG_MAX_ITEMS",
    "MAX_LOOKUP_IDS",
    "ORIGIN_OWNER",
    "ORIGIN_SITE",
    "CatalogFull",
    "CatalogStoreMixin",
    "CatalogSyncCounts",
    "catalog_max_items",
    "add_missing_rows",
    "clean_items",
    "clean_legacy_catalog",
    "clean_site_items",
    "ensure_catalog_search",
    "fts5_available",
    "migrate_catalog_out_of_specs",
    "replace_rows",
    "source_for",
    "sync_site_rows",
    "upsert_rows",
]
