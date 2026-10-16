# ee/pocketpaw_ee/sites/project_data.py: the owner's read-only data viewer for a
# ``project`` site's own D1 database (the d1-drizzle recipe), plus the column masking
# both data-viewer lanes share.
#
# A project declares no ``objects`` the way a dynamic site does, so its tables come
# from introspecting the database: ``sqlite_master`` for the names, ``PRAGMA
# table_info`` for the columns. ``sites.service`` owns the routing (which lane, which
# database id, local mode); this module only talks to D1 through a client with
# ``query_d1`` / ``query_d1_batch`` (``CloudflareClient`` or a test fake).
#
# Invariants:
#   * Read only. Nothing here writes, and every value binds through ``params``.
#   * A table identifier reaching SQL is always a name ``sqlite_master`` returned for
#     this database (an unknown name is a 404 before any read), and it is quoted.
#   * Internal tables stay hidden: SQLite's, Cloudflare's (``_cf_*``,
#     ``d1_migrations``), drizzle's (``__drizzle*``) and ours (``_paw_*``).
#   * Auth tables (better-auth's ``user`` / ``session`` / ``account`` /
#     ``verification`` and the dynamic lane's ``users`` / ``sessions``) never show a
#     credential: a column named like password / token / secret / hash / key, and
#     ``verification.value``, reads ``MASK``.
from __future__ import annotations

import re
from typing import Any

from pocketpaw_ee.cloud._core.errors import NotFound

MASK = "•••"

LIST_TABLES_SQL = "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"

_HIDDEN_PREFIXES = ("sqlite_", "_cf_", "__drizzle", "_paw_")
_HIDDEN_NAMES = frozenset({"d1_migrations"})

AUTH_TABLES = frozenset(
    {"user", "users", "session", "sessions", "account", "accounts", "verification", "verifications"}
)
_SENSITIVE = re.compile(r"password|token|secret|hash|key", re.I)
# Columns that hold a credential under a harmless name: better-auth keeps the
# one-time token of an email verification or password reset in ``value``.
_AUTH_EXTRA = {"verification": frozenset({"value"}), "verifications": frozenset({"value"})}


def is_user_table(name: str) -> bool:
    """False for SQLite, Cloudflare, drizzle and Paw bookkeeping tables."""
    low = name.lower()
    return low not in _HIDDEN_NAMES and not low.startswith(_HIDDEN_PREFIXES)


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def masked_columns(table: str, columns: list[str] | set[str]) -> set[str]:
    """The columns of ``table`` whose values the viewer must not show."""
    low = table.lower()
    if low not in AUTH_TABLES:
        return set()
    extra = _AUTH_EXTRA.get(low, frozenset())
    return {c for c in columns if _SENSITIVE.search(c) or c.lower() in extra}


def mask_rows(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``rows`` with every sensitive non-null value replaced by ``MASK``."""
    if table.lower() not in AUTH_TABLES:
        return rows
    out: list[dict[str, Any]] = []
    for row in rows:
        hidden = masked_columns(table, set(row))
        out.append({k: (MASK if k in hidden and v is not None else v) for k, v in row.items()})
    return out


async def list_tables(cf: Any, database_id: str) -> list[str]:
    """The user tables of the database, by name."""
    rows = await cf.query_d1(database_id=database_id, sql=LIST_TABLES_SQL, params=[])
    names = [r.get("name") for r in rows]
    return [n for n in names if isinstance(n, str) and n and is_user_table(n)]


def _columns(pragma_rows: list[dict[str, Any]]) -> tuple[dict[str, str], str]:
    """``({column: type}, primary key)`` from ``PRAGMA table_info`` rows. The primary
    key is "" unless exactly one column is the key (the viewer shows one name)."""
    fields: dict[str, str] = {}
    keys: list[str] = []
    for row in pragma_rows:
        name = row.get("name")
        if not isinstance(name, str) or not name:
            continue
        fields[name] = str(row.get("type") or "").lower()
        if row.get("pk"):
            keys.append(name)
    return fields, keys[0] if len(keys) == 1 else ""


async def describe_tables(cf: Any, database_id: str) -> list[dict[str, Any]]:
    """Every user table as ``{name, fields, primary_key}``: one listing query, then
    one batch of ``PRAGMA table_info`` for all of them."""
    names = await list_tables(cf, database_id)
    if not names:
        return []
    pragmas = await cf.query_d1_batch(
        database_id=database_id,
        statements=[(f"PRAGMA table_info({quote_ident(n)})", []) for n in names],
    )
    out = []
    for name, rows in zip(names, pragmas, strict=False):
        fields, primary_key = _columns(rows)
        out.append({"name": name, "fields": fields, "primary_key": primary_key})
    return out


async def read_table(
    cf: Any, database_id: str, table: str, *, limit: int, offset: int = 0
) -> tuple[list[str], list[dict[str, Any]]]:
    """``(columns, rows)`` of one user table, rows masked and capped at ``limit``.

    ``table`` must be a user table of this database (NotFound otherwise, before any
    row is read), so the quoted identifier is never caller-chosen text."""
    if table not in await list_tables(cf, database_id):
        raise NotFound("site_table", table)
    ident = quote_ident(table)
    sql = f"SELECT * FROM {ident} LIMIT ?"  # noqa: S608 — identifier came from sqlite_master
    params: list[Any] = [limit]
    if offset > 0:
        sql += " OFFSET ?"
        params.append(offset)
    pragma, rows = await cf.query_d1_batch(
        database_id=database_id,
        statements=[(f"PRAGMA table_info({ident})", []), (sql, params)],
    )
    fields, _pk = _columns(pragma)
    return list(fields), mask_rows(table, rows)
