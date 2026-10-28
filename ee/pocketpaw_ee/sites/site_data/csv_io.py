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
# Invariants: identifiers reach SQL only from the introspected ``d1.Table``, quoted;
# every value binds. An upload is kept 15 minutes (Redis through the dashboard's
# shared client, else this process) under an unguessable id, and only the user,
# workspace, site and table it was uploaded for can read it back; anyone else gets a
# 404. Exported cells that start with ``= + - @`` tab or CR get a leading ``'`` so a
# spreadsheet never runs them as formulas; numbers stored as numbers are left alone.
from __future__ import annotations

import csv
import io
import json
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pocketpaw_ee.cloud._core.errors import NotFound, PayloadTooLarge, ValidationError
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
PARAMS_PER_STATEMENT = 90
STATEMENTS_PER_BATCH = 50
EXPORT_PAGE = 500
EXPORT_MAX_ROWS = 100_000
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
            if keys:
                key_marks = ", ".join("?" for _ in keys)
                select = (
                    f'SELECT {quote_ident(key)} AS "__key" FROM {ident} '  # noqa: S608
                    f"WHERE {quote_ident(key)} IN ({key_marks})",  # type: ignore[arg-type]
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

# upload id -> (expires at, monotonic; the JSON record)
_local_uploads: dict[str, tuple[float, str]] = {}
_LOCAL_MAX = 64


def reset_uploads() -> None:
    _local_uploads.clear()


def _owner(workspace_id: str, pocket_id: str, user_id: str, table: str) -> list[str]:
    return [workspace_id, pocket_id, user_id, table]


async def save_upload(
    *, workspace_id: str, pocket_id: str, user_id: str, table: str, filename: str, text: str
) -> str:
    """Keep an upload for 15 minutes; returns its id."""
    upload_id = secrets.token_urlsafe(24)
    record = json.dumps(
        {
            "owner": _owner(workspace_id, pocket_id, user_id, table),
            "filename": filename,
            "text": text,
        }
    )
    client = budget.redis_client()
    if client is not None:
        try:
            await client.set(_UPLOAD_KEY + upload_id, record, ex=UPLOAD_TTL_S)
            return upload_id
        except Exception:  # noqa: BLE001 — fall back to this process
            logger.warning("site_data.csv: Redis unavailable, keeping the upload in process")
    now = time.monotonic()
    for k in [k for k, (exp, _) in _local_uploads.items() if exp <= now]:
        _local_uploads.pop(k, None)
    if len(_local_uploads) >= _LOCAL_MAX:
        _local_uploads.pop(next(iter(_local_uploads)))
    _local_uploads[upload_id] = (now + UPLOAD_TTL_S, record)
    return upload_id


async def load_upload(
    upload_id: str, *, workspace_id: str, pocket_id: str, user_id: str, table: str
) -> tuple[str, str]:
    """``(filename, text)`` of an upload this caller made for this table, or NotFound
    (expired, never made, or someone else's)."""
    if not isinstance(upload_id, str) or not _UPLOAD_ID.match(upload_id):
        raise NotFound("site_data_upload", "")
    record: str | None = None
    client = budget.redis_client()
    if client is not None:
        try:
            raw = await client.get(_UPLOAD_KEY + upload_id)
            record = raw.decode() if isinstance(raw, bytes) else raw
        except Exception:  # noqa: BLE001
            logger.warning("site_data.csv: Redis unavailable reading an upload")
    if record is None:
        hit = _local_uploads.get(upload_id)
        if hit is not None and hit[0] > time.monotonic():
            record = hit[1]
    if record is None:
        raise NotFound("site_data_upload", "")
    data = json.loads(record)
    if data.get("owner") != _owner(workspace_id, pocket_id, user_id, table):
        raise NotFound("site_data_upload", "")
    return str(data.get("filename") or ""), str(data.get("text") or "")


async def drop_upload(upload_id: str) -> None:
    _local_uploads.pop(upload_id, None)
    client = budget.redis_client()
    if client is not None:
        try:
            await client.delete(_UPLOAD_KEY + upload_id)
        except Exception:  # noqa: BLE001 — it expires on its own
            logger.warning("site_data.csv: could not drop upload %s", upload_id[:8])


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
