# ee/pocketpaw_ee/sites/site_data/csv_io.py — CSV in and out of a site's D1 table
# for the Data dashboard: parsing an upload, matching its columns to the table,
# checking and coercing its rows, planning the import statements, keeping the
# upload between preview and commit, and rendering an export.
#
# ``sites.data_service`` resolves the site and the editable table first, then calls
# these. Parsing is the stdlib ``csv`` module with ``Sniffer`` (the approach of
# ``paw_bar/catalog_csv.py``): UTF-8 with or without a BOM (cp1252 as a fallback),
# ``,`` ``;`` tab or ``|``, quoted newlines, a header row required.
#
# Mapping: a CSV column maps to at most one table column and each table column takes
# at most one CSV column. Auto-match is exact name, then case-insensitive, then
# snake / space normalised (``Order Total`` = ``orderTotal`` = ``order_total``).
# ``mappable`` is the one rule of what may be mapped: ``d1.column_writable``, plus the
# table's own key column even when it is the rowid alias (so an upsert can match on
# ``id`` and an import can keep its ids). Credential, generated and blob columns
# never are.
#
# Cells: an empty cell is NULL, except in a NOT NULL column with a default, where it
# leaves the column out of that row (the default on insert, the current value on an
# upsert's update). Rows are grouped by which columns they carry, and each statement
# holds ``floor(90 / columns)`` rows, so no statement binds more than 90 values.
#
# Uploads: kept 15 minutes (Redis through the dashboard's shared client, else this
# process within a 50 MB budget) under an unguessable id and a key bound to the user,
# workspace, site and table they were uploaded for; anyone else gets a 404. At most
# 3 wait per user and workspace (429 ``sites.data_import_busy``). A commit CLAIMS its
# upload atomically (``GETDEL`` / pop), so it imports once; ``put_back`` returns it
# only when the commit was refused before any write.
#
# Invariants: identifiers reach SQL only from the introspected ``d1.Table``, quoted;
# every value binds. Exported cells that start with ``= + - @`` tab or CR get a
# leading ``'`` so a spreadsheet never runs them as formulas (numbers stored as
# numbers are left alone), and an import strips that one ``'`` again, so an export
# re-imports as it was.
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pocketpaw_ee.cloud._core.errors import (
    NotFound,
    PayloadTooLarge,
    RateLimited,
    ValidationError,
)
from pocketpaw_ee.sites.project_data import quote_ident
from pocketpaw_ee.sites.site_data import budget, d1, writes
from pocketpaw_ee.sites.site_data.dto import ImportRowError, ImportTarget

logger = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20_000
MAX_COLUMNS = 500
MAX_HEADER_NAME = 200
PREVIEW_ROWS = 20
MAX_ERRORS = 100
UPLOAD_TTL_S = 15 * 60
MAX_LIVE_UPLOADS = 3  # per user and workspace
LOCAL_MAX_BYTES = 50 * 1024 * 1024  # every upload this process holds, together
PARAMS_PER_STATEMENT = 90
STATEMENTS_PER_BATCH = 50
EXPORT_PAGE = 500
EXPORT_MAX_ROWS = 100_000  # paged by key
EXPORT_MAX_SORTED_ROWS = 20_000  # an owner-chosen sort pages by OFFSET, which costs more
BOM = "﻿"

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
_CAMEL = re.compile(r"(?<=[a-z0-9])([A-Z])")
_NON_WORD = re.compile(r"[^0-9a-z]+")
_UPLOAD_ID = re.compile(r"^[A-Za-z0-9_-]{20,64}$")
_UPLOAD_KEY = "site_data:csv_upload:"


def _bad(code: str, message: str, details: dict[str, Any] | None = None) -> ValidationError:
    return ValidationError(f"sites.data_csv_{code}", message, details)


# --------------------------------------------------------------------------- parsing


@dataclass(frozen=True)
class ParsedCsv:
    header: list[str]
    # (row number as a spreadsheet shows it, the header being row 1; the cells)
    rows: list[tuple[int, list[str]]] = field(default_factory=list)


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _dialect(text: str) -> Any:
    try:
        return csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        return csv.excel


def _clean_header(raw: list[str]) -> list[str]:
    if len(raw) > MAX_COLUMNS:
        raise _bad("too_wide", f"A file can have at most {MAX_COLUMNS} columns.")
    header: list[str] = []
    for i, cell in enumerate(raw):
        name = cell.replace(BOM, "").strip() or f"column_{i + 1}"
        if len(name) > MAX_HEADER_NAME:
            raise _bad("bad_header", f"Column {i + 1}'s name is too long.")
        if name in header:
            raise _bad(
                "bad_header", f"Two columns are both called {name!r}.", {"column": name[:80]}
            )
        header.append(name)
    return header


def decode(data: bytes) -> str:
    """The upload as text, refusing over 5 MB (413) and an empty file (422)."""
    if len(data) > MAX_BYTES:
        raise PayloadTooLarge("sites.data_csv_too_large", "A CSV file can be at most 5 MB.")
    text = _decode(data)
    if not text.strip():
        raise _bad("empty", "This file is empty.")
    return text


def parse(text: str) -> ParsedCsv:
    """The header and every non-blank data row of an uploaded CSV. 422
    ``sites.data_csv_*`` for an empty, unreadable, too wide (500 columns) or too
    long (20,000 rows) file, or a header with a repeated name."""
    if not text.strip():
        raise _bad("empty", "This file is empty.")
    reader = csv.reader(io.StringIO(text, newline=""), _dialect(text))
    rows: list[tuple[int, list[str]]] = []
    try:
        header = _clean_header(next(reader, []))
        number = 1
        for cells in reader:
            number += 1
            if not any(c.strip() for c in cells):
                continue
            if len(rows) >= MAX_ROWS:
                raise _bad("too_many_rows", f"A file can have at most {MAX_ROWS:,} rows.")
            rows.append((number, cells))
    except csv.Error as exc:
        detail = "A cell is too long." if "field larger" in str(exc) else "It isn't valid CSV."
        raise _bad("unreadable", f"This file can't be read. {detail}") from exc
    if not header:
        raise _bad("empty", "This file has no header row.")
    return ParsedCsv(header=header, rows=rows)


# --------------------------------------------------------------------------- mapping


def mappable(table: d1.Table, kinds: dict[str, str]) -> list[d1.Column]:
    """The columns an import may write, in table order."""
    out: list[d1.Column] = []
    for c in table.columns:
        if c.name in table.masked or c.hidden in (2, 3):
            continue
        if d1.column_writable(table, c, kinds[c.name]) or (
            c.name == table.key and c.name == table.rowid_alias
        ):
            out.append(c)
    return out


def upsert_key(table: d1.Table) -> str | None:
    """The column an upsert matches on: the single primary key (never ``rowid``)."""
    return table.key if table.key and table.key != "rowid" else None


def targets(table: d1.Table, kinds: dict[str, str]) -> list[ImportTarget]:
    key = upsert_key(table)
    return [
        ImportTarget(
            name=c.name,
            kind=kinds[c.name],  # type: ignore[arg-type]
            required=writes.is_required(table, c),
            key=c.name == key,
        )
        for c in mappable(table, kinds)
    ]


def norm(name: str) -> str:
    """A column name compared loosely: camelCase split, lowercased, every run of
    spaces / punctuation an ``_``."""
    return _NON_WORD.sub("_", _CAMEL.sub(r"_\1", name.strip()).lower()).strip("_")


def auto_mapping(header: list[str], names: list[str]) -> dict[str, str | None]:
    """Each CSV column's best table column (or None), every table column used once:
    exact matches first, then case-insensitive, then normalised."""
    mapping: dict[str, str | None] = dict.fromkeys(header)
    taken: set[str] = set()
    stages = (
        lambda h, n: h == n,
        lambda h, n: h.casefold() == n.casefold(),
        lambda h, n: norm(h) != "" and norm(h) == norm(n),
    )
    for same in stages:
        for h in header:
            if mapping[h] is not None:
                continue
            hit = next((n for n in names if n not in taken and same(h, n)), None)
            if hit is not None:
                mapping[h] = hit
                taken.add(hit)
    return mapping


def _mapping_error(fields: dict[str, str]) -> ValidationError:
    return ValidationError(
        "sites.data_import_bad_mapping",
        "Some columns can't be imported like that.",
        {"fields": dict(list(fields.items())[: writes.MAX_FIELD_ERRORS])},
    )


def check_mapping(
    table: d1.Table,
    kinds: dict[str, str],
    header: list[str],
    mapping: dict[str, str | None],
    mode: str,
) -> dict[str, str]:
    """``{csv column: table column}`` for the mapped columns, or a 422: an unknown CSV
    or table column, a column mapped twice, a credential / generated / blob / other
    non-writable column, a required column left unmapped or nothing mapped
    (``sites.data_import_bad_mapping``, ``details.fields``), or an upsert without
    its key (``sites.data_import_needs_key``)."""
    allowed = {c.name for c in mappable(table, kinds)}
    errors: dict[str, str] = {}
    out: dict[str, str] = {}
    used: dict[str, str] = {}
    for src, dest in mapping.items():
        if src not in header:
            errors[str(src)[:80]] = "This file has no such column."
            continue
        if dest is None:
            continue
        col = table.column(dest)
        if col is None:
            errors[src] = f"This table has no column named {dest[:80]!r}."
        elif dest in table.masked:
            errors[src] = "Credentials can't be imported."
        elif dest not in allowed:
            errors[src] = writes.not_writable_reason(table, col, kinds[dest])
        elif dest in used:
            errors[src] = f"{used[dest]!r} is already mapped to {dest!r}."
        else:
            used[dest] = src
            out[src] = dest
    if errors:
        raise _mapping_error(errors)
    missing = {
        c.name: "Required: map a column to it."
        for c in mappable(table, kinds)
        if writes.is_required(table, c) and c.name not in used
    }
    if missing:
        raise _mapping_error(missing)
    if not out:
        raise _mapping_error({"mapping": "Map at least one column."})
    if mode == "upsert":
        key = upsert_key(table)
        if key is None:
            raise ValidationError(
                "sites.data_import_needs_key", "This table has no ID column to match rows on."
            )
        if key not in used:
            raise ValidationError(
                "sites.data_import_needs_key",
                f"Map a column to {key!r} so existing rows can be matched.",
            )
    return out


# --------------------------------------------------------------------------- rows


@dataclass(frozen=True)
class Row:
    number: int
    values: dict[str, Any]  # table column -> coerced value, in mapping order
    omitted: frozenset[str]  # NOT NULL columns with a default whose cell was empty


def check_rows(
    table: d1.Table,
    kinds: dict[str, str],
    parsed: ParsedCsv,
    mapping: dict[str, str],
    formats: dict[str, tuple[str, str | None]],
) -> tuple[list[Row], list[ImportRowError], int]:
    """The rows that can be written, the first 100 cell errors and the number of
    rows with an error (a row with any bad cell is not written)."""
    index = {name: i for i, name in enumerate(parsed.header)}
    good: list[Row] = []
    errors: list[ImportRowError] = []
    bad_rows = 0
    width = len(parsed.header)
    for number, cells in parsed.rows:
        row_errors: list[ImportRowError] = []
        if len(cells) > width:
            row_errors.append(
                ImportRowError(
                    row=number, error=f"This row has {len(cells)} values; the header has {width}."
                )
            )
        values: dict[str, Any] = {}
        omitted: set[str] = set()
        for src, dest in mapping.items():
            i = index[src]
            cell = cells[i] if i < len(cells) else ""
            col = table.column(dest)
            if col is None:  # check_mapping already refused it
                continue
            if not cell.strip():
                if col.notnull and col.default is not None:
                    omitted.add(dest)
                elif writes.is_required(table, col):
                    row_errors.append(ImportRowError(row=number, column=src, error="Required."))
                else:
                    values[dest] = None
                continue
            if len(cell) > 1 and cell[0] == "'" and cell.startswith(_FORMULA_START, 1):
                cell = cell[1:]  # our export's formula guard, undone
            fmt, sample = formats.get(dest, ("", None))
            try:
                values[dest] = writes.coerce(kinds[dest], fmt, cell, sample=sample)
            except ValueError as exc:
                row_errors.append(ImportRowError(row=number, column=src, error=str(exc)))
        if row_errors:
            bad_rows += 1
            errors.extend(row_errors[: max(0, MAX_ERRORS - len(errors))])
        else:
            good.append(Row(number=number, values=values, omitted=frozenset(omitted)))
    return good, errors, bad_rows


# --------------------------------------------------------------------------- statements


@dataclass(frozen=True)
class Chunk:
    """One INSERT statement's rows; for an upsert, preceded in the same batch by a
    SELECT of which of their keys already exist."""

    rows: list[Row]
    statements: list[tuple[str, list[Any]]]


def _conflict_tail(key: str, cols: tuple[str, ...]) -> str:
    updates = [c for c in cols if c != key]
    if not updates:
        return f" ON CONFLICT({quote_ident(key)}) DO NOTHING"
    sets = ", ".join(f"{quote_ident(c)} = excluded.{quote_ident(c)}" for c in updates)
    return f" ON CONFLICT({quote_ident(key)}) DO UPDATE SET {sets}"


def plan(table: d1.Table, rows: list[Row], mode: str) -> list[Chunk]:
    """The import's statements, chunked so none binds more than 90 values."""
    ident = quote_ident(table.name)
    key = upsert_key(table) if mode == "upsert" else None
    groups: dict[tuple[str, ...], list[Row]] = {}
    for row in rows:
        cols = tuple(c for c in row.values if c not in row.omitted)
        groups.setdefault(cols, []).append(row)
    chunks: list[Chunk] = []
    for cols, members in groups.items():
        if not cols:
            for row in members:
                chunks.append(Chunk([row], [(f"INSERT INTO {ident} DEFAULT VALUES", [])]))
            continue
        per = max(1, PARAMS_PER_STATEMENT // len(cols))
        names = ", ".join(quote_ident(c) for c in cols)
        marks = "(" + ", ".join("?" for _ in cols) + ")"
        tail = _conflict_tail(key, cols) if key is not None else ""
        for i in range(0, len(members), per):
            part = members[i : i + per]
            params = [row.values[c] for row in part for c in cols]
            values_sql = ", ".join(marks for _ in part)
            # Identifiers come from the introspected schema, quoted; values bind.
            insert = (f"INSERT INTO {ident} ({names}) VALUES {values_sql}{tail}", params)
            statements = [insert]
            keys = [r.values[key] for r in part if key and r.values.get(key) is not None]
            if key is not None and keys:
                key_marks = ", ".join("?" for _ in keys)
                select = (
                    f'SELECT {quote_ident(key)} AS "__key" FROM {ident} '  # noqa: S608
                    f"WHERE {quote_ident(key)} IN ({key_marks})",
                    keys,
                )
                statements = [select, insert]
            chunks.append(Chunk(rows=part, statements=statements))
    return chunks


def batches(chunks: list[Chunk]) -> list[list[Chunk]]:
    """Chunks grouped into D1 batch calls of at most 50 statements (an upsert's
    SELECT travels with its INSERT)."""
    out: list[list[Chunk]] = []
    current: list[Chunk] = []
    size = 0
    for chunk in chunks:
        n = len(chunk.statements)
        if current and size + n > STATEMENTS_PER_BATCH:
            out.append(current)
            current, size = [], 0
        current.append(chunk)
        size += n
    if current:
        out.append(current)
    return out


# --------------------------------------------------------------------------- uploads

# Redis key -> (expires at, wall clock; owner; the JSON record)
_local_uploads: dict[str, tuple[float, list[str], str]] = {}


def reset_uploads() -> None:
    _local_uploads.clear()


def _owner(workspace_id: str, pocket_id: str, user_id: str, table: str) -> list[str]:
    return [workspace_id, pocket_id, user_id, table]


def _key(upload_id: str, owner: list[str]) -> str:
    """The upload's key, bound to its owner: someone else's id resolves to nothing."""
    tag = hashlib.sha256(json.dumps(owner).encode("utf-8")).hexdigest()[:24]
    return f"{_UPLOAD_KEY}{tag}:{upload_id}"


def _quota_key(owner: list[str]) -> str:
    return f"site_data:csv_uploads:{owner[0]}:{owner[2]}"


def _busy() -> RateLimited:
    return RateLimited(
        "sites.data_import_busy",
        f"You have {MAX_LIVE_UPLOADS} imports waiting. Finish one, or wait a few minutes.",
    )


def _local_put(key: str, owner: list[str], record: str, expires_at: float) -> None:
    now = time.time()
    for k in [k for k, (exp, _o, _r) in _local_uploads.items() if exp <= now]:
        _local_uploads.pop(k, None)
    while _local_uploads and (
        sum(len(r) for _e, _o, r in _local_uploads.values()) + len(record) > LOCAL_MAX_BYTES
    ):
        _local_uploads.pop(next(iter(_local_uploads)))  # the oldest goes first
    _local_uploads[key] = (expires_at, owner, record)


def _local_live(owner: list[str]) -> int:
    now = time.time()
    return sum(
        1
        for exp, o, _r in _local_uploads.values()
        if exp > now and o[0] == owner[0] and o[2] == owner[2]
    )


async def save_upload(
    *, workspace_id: str, pocket_id: str, user_id: str, table: str, filename: str, text: str
) -> str:
    """Keep an upload for 15 minutes; returns its id. 429 ``sites.data_import_busy``
    when this user already has 3 waiting in the workspace."""
    upload_id = secrets.token_urlsafe(24)
    owner = _owner(workspace_id, pocket_id, user_id, table)
    expires_at = time.time() + UPLOAD_TTL_S
    record = json.dumps(
        {"owner": owner, "filename": filename, "text": text, "expires_at": expires_at},
        ensure_ascii=False,
    )
    key = _key(upload_id, owner)
    client = budget.redis_client()
    if client is not None:
        quota = _quota_key(owner)
        try:
            await client.zremrangebyscore(quota, "-inf", time.time())
            live = int(await client.zcard(quota) or 0)
        except Exception:  # noqa: BLE001 — fall back to this process
            logger.warning("site_data.csv: Redis unavailable, keeping the upload in process")
        else:
            if live >= MAX_LIVE_UPLOADS:
                raise _busy()
            try:
                await client.set(key, record, ex=UPLOAD_TTL_S)
                await client.zadd(quota, {upload_id: expires_at})
                await client.expire(quota, UPLOAD_TTL_S)
                return upload_id
            except Exception:  # noqa: BLE001
                logger.warning("site_data.csv: Redis unavailable, keeping the upload in process")
    if _local_live(owner) >= MAX_LIVE_UPLOADS:
        raise _busy()
    _local_put(key, owner, record, expires_at)
    return upload_id


@dataclass(frozen=True)
class Claimed:
    upload_id: str
    owner: list[str]
    filename: str
    text: str
    record: str
    expires_at: float


async def claim_upload(
    upload_id: str, *, workspace_id: str, pocket_id: str, user_id: str, table: str
) -> Claimed:
    """Take an upload this caller made for this table, atomically (Redis ``GETDEL``,
    else a pop): of two commits of one upload, only one gets it. NotFound when it
    expired, was taken, or is someone else's."""
    if not isinstance(upload_id, str) or not _UPLOAD_ID.match(upload_id):
        raise NotFound("site_data_upload", "")
    owner = _owner(workspace_id, pocket_id, user_id, table)
    key = _key(upload_id, owner)
    record: str | None = None
    client = budget.redis_client()
    if client is not None:
        try:
            raw = await client.getdel(key)
            record = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            if record is not None:
                await client.zrem(_quota_key(owner), upload_id)
        except Exception:  # noqa: BLE001
            logger.warning("site_data.csv: Redis unavailable reading an upload")
    if record is None:
        hit = _local_uploads.pop(key, None)
        if hit is not None and hit[0] > time.time():
            record = hit[2]
    if record is None:
        raise NotFound("site_data_upload", "")
    data = json.loads(record)
    if data.get("owner") != _owner(workspace_id, pocket_id, user_id, table):
        raise NotFound("site_data_upload", "")
    return Claimed(
        upload_id=upload_id,
        owner=owner,
        filename=str(data.get("filename") or ""),
        text=str(data.get("text") or ""),
        record=record,
        expires_at=float(data.get("expires_at") or 0),
    )


async def put_back(claimed: Claimed) -> None:
    """Return a claimed upload for the rest of its 15 minutes (its commit was refused
    before anything was written)."""
    left = int(claimed.expires_at - time.time())
    if left < 1:
        return
    key = _key(claimed.upload_id, claimed.owner)
    client = budget.redis_client()
    if client is not None:
        try:
            await client.set(key, claimed.record, ex=left)
            await client.zadd(_quota_key(claimed.owner), {claimed.upload_id: claimed.expires_at})
            return
        except Exception:  # noqa: BLE001
            logger.warning("site_data.csv: Redis unavailable, keeping the upload in process")
    _local_put(key, claimed.owner, claimed.record, claimed.expires_at)


# --------------------------------------------------------------------------- export


def guard(text: str) -> str:
    """``text`` safe to open in a spreadsheet: a leading ``'`` when it would start a
    formula."""
    return "'" + text if text.startswith(_FORMULA_START) else text


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)  # a stored number can't be a formula
    if isinstance(value, dict | list):
        return guard(json.dumps(value, ensure_ascii=False, default=str))
    return guard(str(value))


def export_columns(table: d1.Table) -> list[str]:
    """Every column but credentials, which an export leaves out entirely."""
    names = [c.name for c in table.columns]
    hidden = table.masked | frozenset(d1.masked_columns(table.name, names))
    return [n for n in names if n not in hidden]


def _encode(rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def header_line(columns: list[str]) -> str:
    return BOM + _encode([[guard(c) for c in columns]])


def rows_text(rows: list[dict[str, Any]], columns: list[str]) -> str:
    return _encode([[_cell(r.get(c)) for c in columns] for r in rows])


def export_filename(table: str, today: date) -> str:
    """``<table>-<yyyy-mm-dd>.csv`` with anything but letters, digits, ``.``, ``-``
    and ``_`` replaced, so it is safe in a Content-Disposition header."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", table).strip("._") or "table"
    return f"{safe[:80]}-{today.isoformat()}.csv"
