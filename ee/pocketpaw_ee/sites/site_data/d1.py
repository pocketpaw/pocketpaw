# ee/pocketpaw_ee/sites/site_data/d1.py — reading a site's D1 database for the Data
# dashboard: introspection, column kinds, and the rows-page query builder.
#
# Introspection is one ``sqlite_master`` read (table names, CREATE text, the
# ``WITHOUT ROWID`` flag) plus, on a cache miss, one batch of ``PRAGMA table_xinfo``
# and ``PRAGMA foreign_key_list`` for every user table. The result is cached in
# process for 60 s keyed by the database id and a fingerprint of the CREATE texts, so
# a migration (which changes them) misses the cache at once.
#
# Key choice: exactly one primary-key column → that column; no primary key in a
# rowid table → ``rowid``; anything else (composite key, WITHOUT ROWID without a
# single key, a key column that holds a credential) → no key, read only.
#
# Login tables are read only: the better-auth names (``LOGIN_READ_ONLY``) and any
# ``session_like`` table (a foreign key to a user table plus an ``expires…``
# column, as Lucia's ``user_sessions``), whose key and credential columns are
# masked too. Reads, writes, import and export all take this from ``Table``.
#
# ``column_writable`` / ``insertable`` are the one statement of which columns and
# tables the dashboard may write; ``site_data.writes`` enforces them and
# ``ColumnInfo.writable`` reports them. ``write_exposure`` says whether a write would
# reach sign-in data through FK actions (``Column.on_delete`` / ``on_update``) or the
# triggers introspection also reads (``Schema.triggers``).
#
# Invariants:
#   * Read only. Every value binds through ``params``; no statement here writes.
#   * Every identifier reaching SQL came from ``sqlite_master`` / PRAGMA and is
#     quoted with ``project_data.quote_ident``. Caller-supplied names (sort, filter
#     columns, the table) are only ever looked up in the introspected schema.
#   * Masked columns (``project_data.masked_columns``) are never searched, filtered
#     or sorted on, and their values read ``MASK``.
#   * A statement never carries more than 100 bound parameters (D1's cap).
from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.project_data import (
    MASK,
    auth_table,
    is_user_table,
    mask_rows,
    masked_columns,
    quote_ident,
)
from pocketpaw_ee.sites.site_data.dto import ColumnInfo

LIST_SQL = (
    "SELECT type, name, tbl_name, sql FROM sqlite_master "
    "WHERE type IN ('table', 'trigger') ORDER BY name"
)

COUNT_CAP = 10_000
LIMIT_DEFAULT = 50
LIMIT_MAX = 200
OFFSET_MAX = 100_000
MAX_FILTERS = 10
MAX_Q = 100
MAX_PARAMS = 100
MAX_FILTER_VALUE = 1_000
SCHEMA_TTL_S = 60.0
COUNTS_TTL_S = 30.0
_CACHE_MAX = 256

# Login tables the dashboard never edits (better-auth's, its plugins' and the
# dynamic lane's), as ``project_data.auth_table`` names them.
LOGIN_READ_ONLY = frozenset(
    {
        "session",
        "sessions",
        "account",
        "accounts",
        "verification",
        "verifications",
        "twofactor",
        "twofactors",
        "passkey",
        "passkeys",
        "apikey",
        "apikeys",
        "jwks",
    }
)
# User tables: editable, but only their profile columns, and rows can't be added
# (people sign up on the site). Column names compare lowercased with "_" removed.
# ``emailVerified`` is deliberately NOT writable: setting a new email AND marking it
# verified would take the account over; changing the email clears it instead.
USER_TABLES = frozenset({"user", "users"})
USER_WRITABLE = frozenset({"name", "email", "image"})


def is_users(name: str) -> bool:
    """Whether ``name`` is a user table (``user``, ``Users``, ``auth_user`` …)."""
    return auth_table(name) in USER_TABLES


def session_like(columns: tuple[Column, ...] | list[Column]) -> bool:
    """A session table under any name (Lucia's ``user_sessions``, say): a foreign
    key to a user table AND an ``expires…`` column. Its rows are sign-ins, and its
    key is often the session secret itself."""
    refs_user = any(
        c.references and auth_table(c.references.partition(".")[0]) in USER_TABLES for c in columns
    )
    return refs_user and any(norm(c.name).startswith("expires") for c in columns)


def norm(column: str) -> str:
    """A column name compared loosely: lowercased, ``_`` removed."""
    return column.replace("_", "").lower()


_WITHOUT_ROWID = re.compile(r"\bWITHOUT\s+ROWID\b", re.I)
_ROWID_ALIASES = ("rowid", "_rowid_", "oid")


@dataclass(frozen=True)
class Column:
    name: str
    sql_type: str  # lowercased declared type
    notnull: bool
    default: str | None
    pk: int  # position in the primary key, 0 when not part of it
    hidden: int  # PRAGMA table_xinfo: 2 / 3 = generated
    references: str | None
    on_delete: str = ""  # the FK's ON DELETE action, upper case ("CASCADE", "SET NULL" …)
    on_update: str = ""


@dataclass(frozen=True)
class Table:
    name: str
    columns: tuple[Column, ...]
    without_rowid: bool
    key: str | None  # the public key name: a column, "rowid", or None
    key_sql: str | None  # what is selected AS "__key" (None → NULL)
    order_sql: tuple[str, ...]  # default ORDER BY expressions (newest first)
    masked: frozenset[str]
    read_only_reason: str  # "" when editable
    rowid_alias: str = ""  # the INTEGER PRIMARY KEY column, if any
    create_sql: str = ""  # the CREATE text sqlite_master holds (schema view)

    @property
    def editable(self) -> bool:
        return self.read_only_reason == ""

    def column(self, name: str) -> Column | None:
        return next((c for c in self.columns if c.name == name), None)


@dataclass(frozen=True)
class Schema:
    fingerprint: str
    tables: dict[str, Table] = field(default_factory=dict)
    # Trigger CREATE texts by the (folded) name of the table they fire on.
    triggers: dict[str, tuple[str, ...]] = field(default_factory=dict)


_schema_cache: dict[tuple[str, ...], tuple[float, Schema]] = {}
_counts_cache: dict[tuple[str, ...], tuple[float, dict[str, int | None]]] = {}


def clear_caches() -> None:
    _schema_cache.clear()
    _counts_cache.clear()


def forget_counts(database_id: str) -> None:
    """Drop the cached row counts of ``database_id`` (after a write)."""
    for key in [k for k in _counts_cache if k[0] == database_id]:
        _counts_cache.pop(key, None)


def forget_database(database_id: str) -> None:
    """Drop everything cached about ``database_id`` (after a restore rewrote it)."""
    forget_counts(database_id)
    for key in [k for k in _schema_cache if k[0] == database_id]:
        _schema_cache.pop(key, None)


def _remember(cache: dict, key: tuple[str, ...], ttl: float, value: Any) -> None:
    if len(cache) >= _CACHE_MAX:
        cache.pop(next(iter(cache)))
    cache[key] = (time.monotonic() + ttl, value)


def _recall(cache: dict, key: tuple[str, ...]) -> Any | None:
    hit = cache.get(key)
    if hit is None or hit[0] <= time.monotonic():
        cache.pop(key, None)
        return None
    return hit[1]


def column_writable(table: Table, column: Column, kind: str) -> bool:
    """Whether the dashboard may write ``column``: the table is editable, and the
    column isn't generated, the rowid alias, a credential, a blob, or (on a user
    table) anything but a profile column. The write paths and ``ColumnInfo.writable``
    both read this, so the flags the UI gets are the rules the server enforces."""
    if not table.editable or column.hidden in (2, 3) or column.name == table.rowid_alias:
        return False
    if column.name in table.masked or kind == "blob":
        return False
    if is_users(table.name):
        return norm(column.name) in USER_WRITABLE
    return True


_ASCII_LOWER = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")


def fold(name: str) -> str:
    """``name`` lower-cased the way SQLite compares names: ASCII letters only."""
    return name.translate(_ASCII_LOWER)


def is_login(table: Table) -> bool:
    """A sign-in table: a better-auth name (users included) or a session-like one."""
    return bool(auth_table(table.name)) or table.read_only_reason == "login_table"


# FK actions that change a child row when its parent row changes.
_FK_ACTS = frozenset({"CASCADE", "SET NULL", "SET DEFAULT"})
# Every name a trigger body mentions: quoted, bracketed, backticked or bare.
_TRIGGER_NAME = re.compile(
    r'"((?:[^"]|"")*)"|`((?:[^`]|``)*)`|\[([^\]]*)\]|'
    r"'((?:[^']|'')*)'|([A-Za-z_\x80-\U0010ffff][\w$\x80-\U0010ffff]*)"
)


def _mentions(sql: str) -> set[str]:
    out: set[str] = set()
    for m in _TRIGGER_NAME.finditer(sql or ""):
        raw = next(g for g in m.groups() if g is not None)
        out.add(fold(raw.replace('""', '"').replace("``", "`").replace("''", "'")))
    return out


_EVENTS = frozenset({"INSERT", "UPDATE", "DELETE"})
# Quoted / bracketed names, blanked before the event is read (a trigger may be named
# "on insert").
_QUOTED = re.compile(r'"(?:[^"]|"")*"|`(?:[^`]|``)*`|\[[^\]]*\]')
_TRIGGER_HEAD = re.compile(
    r"^\s*CREATE\s+(?:TEMP(?:ORARY)?\s+)?TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?"
    r"\S+\s+(?:(?:BEFORE|AFTER|INSTEAD\s+OF)\s+)?(INSERT|UPDATE|DELETE)\b",
    re.I,
)


def trigger_events(sql: str) -> frozenset[str]:
    """The event a trigger fires on (``INSERT`` / ``UPDATE`` / ``DELETE``), read from
    its CREATE text; every event when the text can't be read."""
    m = _TRIGGER_HEAD.match(_QUOTED.sub("x", sql or ""))
    return frozenset({m.group(1).upper()}) if m else _EVENTS


def write_exposure(
    schema: Schema,
    targets: list[str] | set[str] | frozenset[str],
    *,
    deletes: bool,
    updates: bool,
    inserts: bool = False,
    keep_user_cascade: bool = False,
) -> str:
    """Why writing ``targets`` would change sign-in data or credentials behind the
    dashboard's back, or "" when it can't: ``cascades_to_login_table`` when a foreign
    key with ON DELETE (``deletes``) / ON UPDATE (``updates``) CASCADE / SET NULL / SET
    DEFAULT reaches a login table or a masked column, ``trigger_on_table`` when a
    trigger for one of the write's events (``inserts`` / ``updates`` / ``deletes``) on
    a reached table mentions a login table or a masked column. The walk follows every
    reached table on with every event (its rows change in ways it can't tell).
    ``keep_user_cascade`` lets a user table's own login rows go with it (the Rows
    tab's deliberate user delete, and its profile edits)."""
    by_name = {fold(n): t for n, t in schema.tables.items()}
    queue = [(fold(t), deletes, updates, inserts) for t in targets]
    seen: set[tuple[str, bool, bool, bool]] = set()
    while queue:
        name, dels, ups, ins = item = queue.pop()
        if item in seen:
            continue
        seen.add(item)
        events = {e for e, on in (("DELETE", dels), ("UPDATE", ups), ("INSERT", ins)) if on}
        parent = by_name.get(name)
        for child in schema.tables.values():
            for c in child.columns:
                if not c.references or fold(c.references.partition(".")[0]) != name:
                    continue
                acts = (dels and c.on_delete in _FK_ACTS) or (ups and c.on_update in _FK_ACTS)
                if not acts:
                    continue
                if keep_user_cascade and parent is not None and is_users(parent.name):
                    if is_login(child):
                        continue
                if is_login(child) or c.name in child.masked:
                    return "cascades_to_login_table"
                queue.append((fold(child.name), True, True, True))
        for sql in schema.triggers.get(name, ()):
            if not trigger_events(sql) & events:
                continue
            mentioned = _mentions(sql)
            for other in mentioned:
                t = by_name.get(other)
                if t is None:
                    continue
                if {fold(c) for c in t.masked} & mentioned:
                    return "trigger_on_table"
                if other == name:
                    continue  # the trigger's own table (``ON name``) is the target
                if is_login(t):
                    return "trigger_on_table"
                queue.append((other, True, True, True))
    return ""


def insertable(table: Table) -> bool:
    """Whether rows can be added (and duplicated): editable, and not a user table."""
    return table.editable and not is_users(table.name)


# --------------------------------------------------------------------------- introspection


def _build_table(name: str, create_sql: str, xinfo: list[dict], fks: list[dict]) -> Table:
    refs: dict[str, str] = {}
    actions: dict[str, tuple[str, str]] = {}
    for fk in fks:
        src, parent = fk.get("from"), fk.get("table")
        if isinstance(src, str) and isinstance(parent, str):
            to = fk.get("to")
            refs[src] = f"{parent}.{to}" if isinstance(to, str) and to else parent
            actions[src] = (
                str(fk.get("on_delete") or "").upper(),
                str(fk.get("on_update") or "").upper(),
            )
    cols: list[Column] = []
    for row in xinfo:
        cname = row.get("name")
        hidden = int(row.get("hidden") or 0)
        if not isinstance(cname, str) or not cname or hidden == 1:
            continue
        default = row.get("dflt_value")
        cols.append(
            Column(
                name=cname,
                sql_type=str(row.get("type") or "").strip().lower(),
                notnull=bool(row.get("notnull")),
                default=None if default is None else str(default),
                pk=int(row.get("pk") or 0),
                hidden=hidden,
                references=refs.get(cname),
                on_delete=actions.get(cname, ("", ""))[0],
                on_update=actions.get(cname, ("", ""))[1],
            )
        )
    without_rowid = bool(_WITHOUT_ROWID.search(create_sql or ""))
    session = session_like(cols)
    masked = frozenset(masked_columns(name, [c.name for c in cols]))
    if session:  # like a named session table: credentials and the key are secret
        masked |= frozenset(masked_columns("session", [c.name for c in cols]))
        masked |= frozenset(c.name for c in cols if c.pk)
    pks = sorted((c for c in cols if c.pk), key=lambda c: c.pk)
    lowered = {c.name.lower() for c in cols}
    rowid = None
    if not without_rowid:
        rowid = next((a for a in _ROWID_ALIASES if a not in lowered), None)

    key: str | None = None
    key_sql: str | None = rowid
    if len(pks) == 1 and pks[0].name not in masked:
        key, key_sql = pks[0].name, quote_ident(pks[0].name)
    elif not pks and rowid:
        key = "rowid"
    if key_sql:
        order: tuple[str, ...] = (key_sql,)
    else:
        unmasked_pks = [quote_ident(c.name) for c in pks if c.name not in masked]
        first = next((quote_ident(c.name) for c in cols if c.name not in masked), None)
        order = tuple(unmasked_pks) or ((first,) if first else ())

    if session:
        reason = "login_table"
    elif key is None:
        reason = "no_key"
    elif auth_table(name) in LOGIN_READ_ONLY:
        reason = "login_table"
    else:
        reason = ""
    alias = ""
    if len(pks) == 1 and pks[0].sql_type == "integer" and not without_rowid:
        alias = pks[0].name
    return Table(
        name=name,
        columns=tuple(cols),
        without_rowid=without_rowid,
        key=key,
        key_sql=key_sql,
        order_sql=order,
        masked=masked,
        read_only_reason=reason,
        rowid_alias=alias,
        create_sql=create_sql or "",
    )


async def read_master(cf: Any, database_id: str) -> dict[str, str]:
    """``{table name: CREATE text}`` for every user table, by name."""
    return (await _read_master(cf, database_id))[0]


async def _read_master(
    cf: Any, database_id: str
) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """User tables' CREATE texts, and every trigger's CREATE text by the folded name of
    the table it fires on (a row without ``type`` reads as a table)."""
    rows = await cf.query_d1(database_id=database_id, sql=LIST_SQL, params=[])
    tables: dict[str, str] = {}
    triggers: dict[str, list[str]] = {}
    for r in rows:
        name = r.get("name")
        if not isinstance(name, str) or not name:
            continue
        if r.get("type") == "trigger":
            on = r.get("tbl_name")
            if isinstance(on, str) and on:
                triggers.setdefault(fold(on), []).append(str(r.get("sql") or ""))
        elif is_user_table(name):
            tables[name] = str(r.get("sql") or "")
    return tables, {k: tuple(v) for k, v in triggers.items()}


def _fingerprint(master: dict[str, str], triggers: dict[str, tuple[str, ...]]) -> str:
    body = [sorted(master.items()), sorted(triggers.items())]
    return hashlib.sha256(json.dumps(body).encode()).hexdigest()


async def introspect(cf: Any, database_id: str) -> Schema:
    """Every user table of the database (and the triggers on them), cached 60 s per
    schema fingerprint."""
    master, triggers = await _read_master(cf, database_id)
    fp = _fingerprint(master, triggers)
    cached = _recall(_schema_cache, (database_id, fp))
    if cached is not None:
        return cached
    tables: dict[str, Table] = {}
    if master:
        statements: list[tuple[str, list]] = []
        for name in master:
            ident = quote_ident(name)
            statements.append((f"PRAGMA table_xinfo({ident})", []))
            statements.append((f"PRAGMA foreign_key_list({ident})", []))
        results = await cf.query_d1_batch(database_id=database_id, statements=statements)
        for i, name in enumerate(master):
            xinfo = results[2 * i] if 2 * i < len(results) else []
            fks = results[2 * i + 1] if 2 * i + 1 < len(results) else []
            tables[name] = _build_table(name, master[name], xinfo, fks)
    schema = Schema(fingerprint=fp, tables=tables, triggers=triggers)
    _remember(_schema_cache, (database_id, fp), SCHEMA_TTL_S, schema)
    return schema


def _count_sql(table: str, where: str = "") -> str:
    # The identifier came from sqlite_master; the cap is our own constant.
    return (
        f"SELECT COUNT(*) AS n FROM (SELECT 1 FROM {quote_ident(table)}{where} "  # noqa: S608
        f"LIMIT {COUNT_CAP + 1})"
    )


def _total(rows: list[dict]) -> int | None:
    n = int((rows[0].get("n") if rows else 0) or 0)
    return None if n > COUNT_CAP else n


async def count_tables(
    cf: Any, database_id: str, schema: Schema, names: list[str]
) -> dict[str, int | None]:
    """Row counts of ``names`` (None over the cap), one batch, cached 30 s."""
    key = (database_id, schema.fingerprint, "".join(names))
    cached = _recall(_counts_cache, key)
    if cached is not None:
        return cached
    counts: dict[str, int | None] = {}
    if names:
        results = await cf.query_d1_batch(
            database_id=database_id, statements=[(_count_sql(n), []) for n in names]
        )
        counts = {n: _total(r) for n, r in zip(names, results, strict=False)}
    _remember(_counts_cache, key, COUNTS_TTL_S, counts)
    return counts


# --------------------------------------------------------------------------- kinds

_SPEC_KINDS = {
    "number": "real",
    "real": "real",
    "float": "real",
    "int": "integer",
    "integer": "integer",
    "bool": "boolean",
    "boolean": "boolean",
    "date": "date",
    "datetime": "datetime",
    "timestamp": "datetime",
    "json": "json",
}
_BOOL_NAMES = frozenset({"active", "paid", "enabled", "verified"})
_ISO_DATE = re.compile(r"^'?\d{4}-\d{2}-\d{2}'?$")
_ISO_DATETIME = re.compile(r"^'?\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}")


def _is_bool_name(name: str) -> bool:
    low = name.lower()
    return (
        low.startswith(("is_", "has_"))
        or bool(re.match(r"^(is|has)[A-Z]", name))
        or low.endswith(("_enabled", "verified"))
        or low in _BOOL_NAMES
    )


def _is_datetime_name(name: str) -> bool:
    low = name.lower()
    return (
        low.endswith(("_at", "_on"))
        or low == "timestamp"
        or bool(re.search(r"[a-z](At|On)$", name))
    )


def _is_date_name(name: str) -> bool:
    return name.lower() == "date" or name.lower().endswith("_date") or name.endswith("Date")


def _is_json_name(name: str) -> bool:
    low = name.lower()
    return "json" in low or low.startswith("meta") or low in ("data", "settings")


def _numbers(samples: list[Any]) -> list[float]:
    return [s for s in samples if isinstance(s, int | float) and not isinstance(s, bool)]


def _all_json(samples: list[Any]) -> bool:
    texts = [s for s in samples if isinstance(s, str)]
    if not texts or len(texts) != len(samples):
        return False
    for s in texts:
        try:
            if not isinstance(json.loads(s), dict | list):
                return False
        except ValueError:
            return False
    return True


def infer_kind(
    name: str,
    sql_type: str,
    default: str | None,
    samples: list[Any],
    *,
    spec_type: str | None = None,
) -> str:
    """The display kind of one column (TDD §5.2). ``samples`` are the non-null values
    of the page already read; they never cost a query."""
    samples = [s for s in samples if s is not None]
    spec = (spec_type or "").strip().lower()
    if spec in _SPEC_KINDS:
        return _SPEC_KINDS[spec]
    t = (sql_type or "").lower() or ("text" if spec in ("string", "text") else "")
    d = (default or "").strip().lower()
    nums = _numbers(samples)

    if not t:  # no declared type: let the values decide
        if samples and len(nums) == len(samples):
            t = "integer" if all(float(n).is_integer() for n in nums) else "real"
        else:
            t = "text"

    if "int" in t:
        if _is_bool_name(name) or d in ("false", "true"):
            return "boolean"
        if d in ("0", "1") and nums and all(n in (0, 1) for n in nums):
            return "boolean"
        if _is_datetime_name(name) or "unixepoch" in d or "strftime" in d:
            return "datetime"
        return "integer"
    if "blob" in t:
        return "blob"
    if any(x in t for x in ("char", "clob", "text")):
        if _is_date_name(name) or d == "current_date" or _ISO_DATE.match(d):
            return "date"
        if (
            _is_datetime_name(name)
            or d in ("current_timestamp",)
            or "datetime(" in d
            or "strftime(" in d
            or _ISO_DATETIME.match(d)
        ):
            return "datetime"
        if _is_json_name(name) or _all_json(samples):
            return "json"
        if any(len(s) > 120 for s in samples if isinstance(s, str)):
            return "longtext"
        return "text"
    if any(x in t for x in ("real", "floa", "doub")):
        return "real"
    # NUMERIC affinity: numeric, decimal, boolean, date, datetime, …
    if "bool" in t:
        return "boolean"
    if "datetime" in t or "timestamp" in t:
        return "datetime"
    if "date" in t:
        return "date"
    return "real"


def time_format(kind: str, sql_type: str, samples: list[Any]) -> str:
    """How a datetime / date column stores its values, from the sampled values."""
    if kind not in ("datetime", "date"):
        return ""
    nums = _numbers(samples)
    if nums:
        return "epoch_ms" if max(abs(n) for n in nums) > 1e11 else "epoch_s"
    if any(isinstance(s, str) for s in samples):
        return "iso"
    return "epoch_s" if "int" in (sql_type or "").lower() else "iso"


def column_infos(
    table: Table, rows: list[dict[str, Any]], spec_fields: dict[str, str] | None = None
) -> list[ColumnInfo]:
    """The schema columns of ``table``, kinds sampled from ``rows`` (unmasked)."""
    spec_fields = spec_fields or {}
    out: list[ColumnInfo] = []
    for c in table.columns:
        samples = [r.get(c.name) for r in rows if r.get(c.name) is not None]
        kind = infer_kind(c.name, c.sql_type, c.default, samples, spec_type=spec_fields.get(c.name))
        auto = c.hidden in (2, 3) or c.name == table.rowid_alias
        masked = c.name in table.masked
        out.append(
            ColumnInfo(
                name=c.name,
                sql_type=c.sql_type,
                kind=kind,  # type: ignore[arg-type]
                time_format=time_format(kind, c.sql_type, samples),  # type: ignore[arg-type]
                nullable=not c.notnull,
                has_default=c.default is not None,
                auto=auto,
                primary_key=c.pk > 0,
                masked=masked,
                writable=column_writable(table, c, kind),
                references=c.references,
            )
        )
    return out


# --------------------------------------------------------------------------- query builder

_TEXT_OPS = frozenset({"eq", "ne", "contains", "starts", "empty", "not_empty"})
_NUMBER_OPS = frozenset(
    {"eq", "ne", "lt", "gt", "lte", "gte", "empty", "not_empty", "is_true", "is_false"}
)
_TIME_OPS = frozenset({"before", "after", "on", "empty", "not_empty"})
_BLOB_OPS = frozenset({"empty", "not_empty"})
_FAMILY_OPS = {
    "text": _TEXT_OPS,
    "number": _NUMBER_OPS,
    "time": _TIME_OPS,
    "blob": _BLOB_OPS,
    "any": _TEXT_OPS | _NUMBER_OPS,
}
_NUMBER_KINDS = frozenset({"integer", "real", "boolean"})
_CMP = {"lt": "<", "gt": ">", "lte": "<=", "gte": ">="}
_INT = re.compile(r"^[+-]?\d+$")


def _family(kind: str, column: Column, spec_type: str | None) -> str:
    if kind in ("datetime", "date"):
        return "time"
    if kind == "blob":
        return "blob"
    if not column.sql_type and not spec_type:
        return "any"
    return "number" if kind in _NUMBER_KINDS else "text"


@dataclass(frozen=True)
class Filter:
    col: str
    op: str
    value: Any = None


@dataclass(frozen=True)
class PageQuery:
    sql: str
    params: list[Any]
    count_sql: str
    count_params: list[Any]
    columns: list[str]
    limit: int
    offset: int
    search_truncated: bool


def _bad(code: str, message: str) -> ValidationError:
    return ValidationError(f"sites.data_{code}", message)


def parse_filters(raw: str | None) -> list[Filter]:
    """The ``f`` query parameter: a JSON list of ``{col, op, value}``, at most 10."""
    if raw is None or not raw.strip():
        return []
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise _bad("bad_filter", "Filters must be a JSON list.") from exc
    if not isinstance(data, list):
        raise _bad("bad_filter", "Filters must be a JSON list.")
    if len(data) > MAX_FILTERS:
        raise _bad("too_many_filters", f"Use at most {MAX_FILTERS} filters.")
    out: list[Filter] = []
    for item in data:
        if not isinstance(item, dict):
            raise _bad("bad_filter", "Each filter is an object with col, op and value.")
        col, op = item.get("col"), item.get("op")
        if not isinstance(col, str) or not isinstance(op, str):
            raise _bad("bad_filter", "Each filter needs a column and an operator.")
        out.append(Filter(col=col, op=op, value=item.get("value")))
    return out


def _number(value: Any) -> int | float:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, str):
        s = value.strip()
        if _INT.match(s):
            return int(s)
        try:
            f = float(s)
        except ValueError:
            pass
        else:
            if math.isfinite(f):
                return f
    raise _bad("bad_filter", "That filter needs a number.")


def _text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise _bad("bad_filter", "That filter needs a text value.")
    s = str(value)
    if len(s) > MAX_FILTER_VALUE:
        raise _bad("bad_filter", "That filter value is too long.")
    return s


def _moment(value: Any) -> datetime:
    """A filter's date / time value as an aware UTC datetime."""
    if isinstance(value, int | float) and not isinstance(value, bool):
        secs = value / 1000 if abs(value) > 1e11 else value
        try:
            return datetime.fromtimestamp(secs, UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise _bad("bad_filter", "That date is out of range.") from exc
    if isinstance(value, str) and value.strip():
        s = value.strip()
        try:
            if len(s) == 10:
                d = date.fromisoformat(s)
                return datetime(d.year, d.month, d.day, tzinfo=UTC)
            dt = datetime.fromisoformat(s)
        except ValueError as exc:
            raise _bad("bad_filter", "That filter needs a date like 2026-01-31.") from exc
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    raise _bad("bad_filter", "That filter needs a date like 2026-01-31.")


def _as_datetime(ident: str) -> str:
    """A SQL expression reading the column as 'YYYY-MM-DD HH:MM:SS' UTC, whether the
    row stores epoch seconds, epoch milliseconds or ISO text."""
    return (
        f"(CASE WHEN typeof({ident}) IN ('integer', 'real') THEN datetime("
        f"CASE WHEN {ident} > 100000000000 THEN {ident} / 1000 ELSE {ident} END, 'unixepoch')"
        f" ELSE datetime({ident}) END)"
    )


def _filter_clause(f: Filter, family: str) -> tuple[str, list[Any]]:
    ident = quote_ident(f.col)
    op = f.op
    if op == "empty":
        return f"({ident} IS NULL OR {ident} = '')", []
    if op == "not_empty":
        return f"({ident} IS NOT NULL AND {ident} != '')", []
    if family == "time":
        moment = _moment(f.value)
        expr = _as_datetime(ident)
        if op == "on":
            return f"date({expr}) = ?", [moment.strftime("%Y-%m-%d")]
        stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
        return f"{expr} {'<' if op == 'before' else '>'} ?", [stamp]
    if op == "is_true":
        return f"({ident} IS NOT NULL AND {ident} != 0)", []
    if op == "is_false":
        return f"{ident} = 0", []
    if op in _CMP:
        return f"{ident} {_CMP[op]} ?", [_number(f.value)]
    if op in ("contains", "starts"):
        tail = "> 0" if op == "contains" else "= 1"
        return f"instr(lower({ident}), lower(?)) {tail}", [_text(f.value)]
    # eq / ne: compare as a number on number columns, as text otherwise
    value = _number(f.value) if family == "number" else _text(f.value)
    return (f"{ident} = ?" if op == "eq" else f"{ident} IS NOT ?"), [value]


def build_page_query(
    table: Table,
    *,
    q: str | None = None,
    sort: str | None = None,
    direction: str | None = None,
    filters: list[Filter] | None = None,
    limit: int | None = None,
    offset: int | None = 0,
    spec_fields: dict[str, str] | None = None,
    max_limit: int = LIMIT_MAX,
    keyset: bool = False,
    after_key: Any = None,
) -> PageQuery:
    """The rows-page SELECT and its capped COUNT for ``table`` (TDD §5.3). Raises a
    422 for a column not in the schema, a masked column, a bad operator or value,
    or out-of-range paging, before any SQL is built. ``max_limit`` is the server's
    own page size cap (the CSV export reads 500 at a time); callers never pass a
    client's value there. ``keyset`` (default order only, a table with a key) pages
    by ``key < after_key`` (``>`` ascending) instead of OFFSET."""
    spec_fields = spec_fields or {}
    limit = LIMIT_DEFAULT if limit is None else limit
    offset = offset or 0
    if not 1 <= limit <= max_limit:
        raise _bad("bad_paging", f"limit must be between 1 and {max_limit}.")
    if not 0 <= offset <= OFFSET_MAX:
        raise _bad("bad_paging", "That page is too far in. Add a filter to narrow the rows.")
    direction = (direction or "").lower() or None
    if direction not in (None, "asc", "desc"):
        raise _bad("bad_sort", "dir must be asc or desc.")
    q = (q or "").strip()
    if len(q) > MAX_Q:
        raise _bad("query_too_long", f"Search is limited to {MAX_Q} characters.")

    kinds = {
        c.name: infer_kind(c.name, c.sql_type, c.default, [], spec_type=spec_fields.get(c.name))
        for c in table.columns
    }

    def _usable(name: str, what: str) -> Column:
        col = table.column(name)
        if col is None:
            raise _bad(f"bad_{what}", f"This table has no column named {name!r}.")
        if name in table.masked:
            raise _bad(f"bad_{what}", f"{name!r} holds credentials and can't be used here.")
        return col

    where: list[str] = []
    params: list[Any] = []
    for f in filters or []:
        col = _usable(f.col, "filter")
        family = _family(kinds[col.name], col, spec_fields.get(col.name))
        if f.op not in _FAMILY_OPS[family]:
            raise _bad("bad_filter", f"{f.op!r} doesn't apply to {col.name!r}.")
        clause, values = _filter_clause(f, family)
        where.append(clause)
        params += values

    truncated = False
    if q:
        searchable = [
            c.name
            for c in table.columns
            if kinds[c.name] in ("text", "longtext") and c.name not in table.masked
        ]
        room = MAX_PARAMS - 2 - len(params)
        if len(searchable) > room:
            searchable, truncated = searchable[: max(room, 0)], True
        if searchable:
            ors = [f"instr(lower({quote_ident(n)}), lower(?)) > 0" for n in searchable]
            where.append("(" + " OR ".join(ors) + ")")
            params += [q] * len(searchable)
        else:
            where.append("0")  # nothing searchable: a search matches no row

    if keyset and (sort or not table.key_sql):
        raise _bad("bad_paging", "Key paging needs the table's key and its default order.")
    if keyset and after_key is not None:
        newest_first = (direction or "desc") == "desc"
        where.append(f"{table.key_sql} {'<' if newest_first else '>'} ?")
        params.append(after_key)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""
    # Default order is newest first; an explicit sort defaults to ascending.
    descending = direction == "desc" or (direction is None and not sort)
    dir_sql = "DESC" if descending else "ASC"
    exprs: list[str] = []
    if sort:
        exprs.append(quote_ident(_usable(sort, "sort").name))
    exprs += [e for e in table.order_sql if e not in exprs]
    order = [f"{e} {dir_sql}" for e in exprs]
    order_sql = (" ORDER BY " + ", ".join(order)) if order else ""

    names = [c.name for c in table.columns]
    select = ", ".join(quote_ident(n) for n in names)
    key = f'{table.key_sql} AS "__key"' if table.key_sql else 'NULL AS "__key"'
    ident = quote_ident(table.name)
    # Identifiers came from the introspected schema and are quoted; values bind.
    sql = f"SELECT {select}, {key} FROM {ident}{where_sql}{order_sql} LIMIT ? OFFSET ?"  # noqa: S608
    return PageQuery(
        sql=sql,
        params=[*params, limit, offset],
        count_sql=_count_sql(table.name, where_sql),
        count_params=list(params),
        columns=names,
        limit=limit,
        offset=offset,
        search_truncated=truncated,
    )


async def read_page(
    cf: Any, database_id: str, table: Table, query: PageQuery
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int | None]:
    """``(raw rows, masked rows, total)`` for one page: rows and count in one batch.
    The raw rows are for kind sampling only and never leave the server."""
    rows, count = await cf.query_d1_batch(
        database_id=database_id,
        statements=[(query.sql, query.params), (query.count_sql, query.count_params)],
    )
    return rows, mask_page(table, rows), _total(count)


#: The largest integer a JavaScript number holds exactly (2**53 - 1).
JS_SAFE_INT = 9_007_199_254_740_991


def json_safe(value: Any) -> Any:
    """``value`` as the browser can hold it: an integer beyond ``JS_SAFE_INT`` goes
    out as its digit string, since ``JSON.parse`` would round it to another number.
    The write paths coerce digit strings back to integers."""
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > JS_SAFE_INT:
        return str(value)
    return value


def json_safe_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: json_safe(v) for k, v in row.items()}


def mask_page(table: Table, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``rows`` masked like the legacy viewer and made JSON-safe for the browser.
    ``__key`` (which the credential pattern would otherwise catch) stays the row id
    it is, always as a string so a large rowid or snowflake id survives the trip;
    every write route accepts it back in that form."""
    keys = [r.get("__key") for r in rows]
    bare = [{k: v for k, v in r.items() if k != "__key"} for r in rows]
    out = [
        {**json_safe_row(r), "__key": None if k is None else str(k)}
        for r, k in zip(mask_rows(table.name, bare), keys, strict=True)
    ]
    for r in out:  # columns the schema masks beyond the name rule (session-like tables)
        for c in table.masked:
            if r.get(c) is not None:
                r[c] = MASK
    return out
