# ee/pocketpaw_ee/sites/site_data/writes.py — writing rows of a site's D1 database
# for the Data dashboard: insert, update with an optimistic conflict check, delete by
# key, duplicate, the value coercion they share, and the audit form of a row.
#
# ``sites.data_service`` resolves the site, checks the table is editable and calls
# these with the introspected ``d1.Table``. Each operation is ONE Cloudflare call
# (plus one sample read when a date is written, to match how the column stores
# dates), and every multi-statement write is one ``query_d1_batch``, which D1 runs
# atomically (spike S2): a bulk delete, or a user delete with its sessions and
# accounts, lands whole or not at all.
#
# Update: ``UPDATE "t" SET … WHERE <key> = ? AND "c" IS ? … RETURNING …`` in a
# batch after a ``SELECT`` of the same row, so the row before the change comes back
# in the same call. No row updated → the row is gone (404) or changed since the
# owner read it (409 ``sites.data_conflict`` with the current, masked row).
#
# Invariants:
#   * Every identifier comes from the introspected ``Table`` and is quoted with
#     ``quote_ident``; caller-supplied names are only looked up in it. Every value
#     binds through ``params``, and no statement carries more than 100 of them.
#   * Only ``d1.column_writable`` columns are ever written. Credential (masked)
#     columns are never written, never compared in ``expected`` and never audited.
#   * ``expected`` must cover every column in ``set``; an update can't skip the
#     conflict check by leaving it out.
#   * Booleans always bind as 0 / 1: D1 stores a bound JSON ``true`` as the text
#     "true" (spike S1).
#   * A delete takes at most 500 keys, 90 per statement.
from __future__ import annotations

import json
import math
import re
from datetime import UTC, date, datetime
from typing import Any

from pocketpaw_ee.cloud._core.errors import CloudError, NotFound, ValidationError
from pocketpaw_ee.sites.project_data import quote_ident
from pocketpaw_ee.sites.site_data import d1

MAX_KEYS = 500
KEYS_PER_STATEMENT = 90
MAX_VALUE_BYTES = 1_000_000
AUDIT_VALUE_MAX = 1024
_SAMPLE_ROWS = 20

# Login tables whose rows go when their user is deleted (signing the user out).
CASCADE_TABLES = frozenset({"session", "sessions", "account", "accounts"})

_INT = re.compile(r"^[+-]?\d+$")
_TRUE = frozenset({"true", "1", "yes", "on", "t", "y"})
_FALSE = frozenset({"false", "0", "no", "off", "f", "n"})
_TZ_SUFFIX = re.compile(r"[+-]\d{2}:?\d{2}$")


# --------------------------------------------------------------------------- errors


def invalid(fields: dict[str, str]) -> ValidationError:
    return ValidationError(
        "sites.data_invalid_values", "Some values can't be saved.", {"fields": fields}
    )


def unavailable(reason: str) -> CloudError:
    return CloudError(
        409,
        "sites.data_unavailable",
        "This site's data can't be edited right now.",
        {"reason": reason},
    )


_READ_ONLY_MESSAGES = {
    "login_table": "Sign-in data can't be edited here.",
    "no_key": "This table has no ID column, so rows can't be edited here.",
    "not_created": "This table doesn't exist in the database yet. Publish the site first.",
}


def read_only(reason: str) -> CloudError:
    """403 for login tables (a rule, not a state), 422 for any other read-only table."""
    status = 403 if reason == "login_table" else 422
    message = _READ_ONLY_MESSAGES.get(reason, "This table can't be edited here.")
    return CloudError(status, "sites.data_read_only", message, {"reason": reason})


def _conflict(current: dict[str, Any]) -> CloudError:
    return CloudError(
        409,
        "sites.data_conflict",
        "This row changed since you opened it.",
        {"current": current},
    )


async def _write(call: Any) -> Any:
    """Await a D1 write, turning a refused statement into a clean 422."""
    try:
        return await call
    except ValidationError as exc:
        if exc.code != "sites.cloudflare_error":
            raise
        raise ValidationError(
            "sites.data_write_failed",
            "The database refused this change. A value may already be taken, or "
            "other rows may depend on this one.",
        ) from exc


# --------------------------------------------------------------------------- kinds and keys


def kinds_of(table: d1.Table, spec_fields: dict[str, str] | None = None) -> dict[str, str]:
    """Each column's kind for coercion: the §5.2 kind from the declared type (or the
    spec type), and ``any`` for a column with neither, whose values bind as sent."""
    spec_fields = spec_fields or {}
    out: dict[str, str] = {}
    for c in table.columns:
        spec = spec_fields.get(c.name)
        if not c.sql_type and not spec:
            out[c.name] = "any"
        else:
            out[c.name] = d1.infer_kind(c.name, c.sql_type, c.default, [], spec_type=spec)
    return out


def key_value(table: d1.Table, kinds: dict[str, str], raw: Any) -> Any:
    """A row key from the URL or a body, as the key column's type. A value that can't
    be a key of this table is NotFound (no row has it)."""
    if raw is None or isinstance(raw, bool) or not isinstance(raw, str | int | float):
        raise NotFound("site_row", str(raw))
    if table.key == "rowid" or (table.key and table.key == table.rowid_alias):
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and _INT.match(raw.strip()):
            return int(raw.strip())
        if isinstance(raw, float) and raw.is_integer():
            return int(raw)
        raise NotFound("site_row", str(raw))
    kind = kinds.get(table.key or "", "text")
    if kind in ("integer", "real", "boolean"):
        try:
            return _number(raw, integer=kind != "real")
        except ValueError:
            return str(raw)
    if kind == "any":
        return raw
    return str(raw) if not isinstance(raw, str) else raw


def target_key(key: Any) -> str:
    """The key as it appears in an audit ``target_id``."""
    return str(key)


# --------------------------------------------------------------------------- coercion


def _number(value: Any, *, integer: bool) -> int | float:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        f = value
    elif isinstance(value, str) and value.strip():
        s = value.strip()
        if _INT.match(s):
            return int(s)
        try:
            f = float(s)
        except ValueError as exc:
            raise ValueError("Enter a number.") from exc
    else:
        raise ValueError("Enter a number.")
    if not math.isfinite(f):
        raise ValueError("Enter a number.")
    if integer:
        if not f.is_integer():
            raise ValueError("Enter a whole number.")
        return int(f)
    return f


def _bool(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int | float) and value in (0, 1):
        return int(value)
    if isinstance(value, str):
        s = value.strip().lower()
        if s in _TRUE:
            return 1
        if s in _FALSE:
            return 0
    raise ValueError("Use true or false.")


def _moment(value: Any) -> datetime:
    if isinstance(value, str) and _INT.match(value.strip()):
        value = int(value.strip())
    if isinstance(value, int | float) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ValueError("Enter a date like 2026-01-31.")
        secs = value / 1000 if abs(value) > 1e11 else value
        try:
            return datetime.fromtimestamp(secs, UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError("That date is out of range.") from exc
    if isinstance(value, str) and value.strip():
        s = value.strip()
        try:
            if len(s) == 10:
                d = date.fromisoformat(s)
                return datetime(d.year, d.month, d.day, tzinfo=UTC)
            dt = datetime.fromisoformat(s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s)
        except ValueError as exc:
            raise ValueError("Enter a date like 2026-01-31.") from exc
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    raise ValueError("Enter a date like 2026-01-31.")


def _iso(kind: str, moment: datetime, sample: str | None) -> str:
    """``moment`` written the way the column's existing values are (``sample``)."""
    if kind == "date":
        return moment.strftime("%Y-%m-%d")
    s = sample or ""
    out = moment.strftime("%Y-%m-%d" + ("T" if "T" in s else " ") + "%H:%M:%S")
    if "." in s:
        out += f".{moment.microsecond // 1000:03d}"
    if s.endswith(("Z", "z")):
        out += "Z"
    elif _TZ_SUFFIX.search(s):
        out += "+00:00"
    return out


def _time(kind: str, time_format: str, value: Any, sample: str | None) -> int | str:
    moment = _moment(value)
    if time_format == "epoch_ms":
        return round(moment.timestamp() * 1000)
    if time_format == "epoch_s":
        return int(moment.timestamp())
    return _iso(kind, moment, sample)


def _cap(text: str) -> str:
    if len(text.encode("utf-8")) > MAX_VALUE_BYTES:
        raise ValueError("That value is too long.")
    return text


def coerce(kind: str, time_format: str, value: Any, *, sample: str | None = None) -> Any:
    """``value`` as the column stores it (TDD §5.4). Raises ValueError with a message
    for the owner when it can't be stored."""
    if value is None:
        return None
    if kind == "blob":
        raise ValueError("Files can't be edited here.")
    if kind == "boolean":
        return _bool(value)
    if isinstance(value, bool):
        if kind in ("integer", "real", "any"):
            return int(value)
        if kind == "json":
            return json.dumps(value)
        raise ValueError("Enter text, not true or false.")
    if kind in ("integer", "real"):
        return _number(value, integer=kind == "integer")
    if kind in ("datetime", "date"):
        return _time(kind, time_format or "iso", value, sample)
    if kind == "json":
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise ValueError("That isn't valid JSON.") from exc
        try:
            return _cap(json.dumps(value, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError) as exc:
            raise ValueError("That isn't valid JSON.") from exc
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Enter a number.")
    if kind == "any" and isinstance(value, int | float):
        return value
    if isinstance(value, str):
        return _cap(value)
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, dict | list):
        return _cap(json.dumps(value, ensure_ascii=False))
    raise ValueError("That value can't be stored here.")


def expected_value(kind: str, value: Any) -> Any:
    """An ``expected`` value as it was read: bound as sent, except booleans (stored
    0 / 1) and JSON values sent parsed. Not reformatted, so a value the owner saw
    compares equal to what the row holds."""
    if value is None:
        return None
    if isinstance(value, bool):
        return json.dumps(value) if kind == "json" else int(value)
    if isinstance(value, dict | list):
        if kind not in ("json", "text", "longtext", "any"):
            raise ValueError("That value can't be compared.")
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Enter a number.")
    if isinstance(value, str | int | float):
        return value
    raise ValueError("That value can't be compared.")


def _why(table: d1.Table, column: d1.Column, kind: str) -> str:
    if column.hidden in (2, 3) or column.name == table.rowid_alias:
        return "Filled in by the database."
    if column.name in table.masked:
        return "Credentials can't be edited here."
    if kind == "blob":
        return "Files can't be edited here."
    if table.name.lower() in d1.USER_TABLES:
        return "Only a user's name, email, image and verified flag can be edited."
    return "This column can't be edited."


def _writable_values(
    table: d1.Table, kinds: dict[str, str], values: dict[str, Any], errors: dict[str, str]
) -> None:
    for name in values:
        col = table.column(name)
        if col is None:
            errors[name] = "Unknown column."
        elif not d1.column_writable(table, col, kinds[name]):
            errors[name] = _why(table, col, kinds[name])


async def _time_formats(
    cf: Any, database_id: str, table: d1.Table, kinds: dict[str, str], names: list[str]
) -> dict[str, tuple[str, str | None]]:
    """``{column: (time_format, a stored string)}`` for the date columns in ``names``,
    sampled from the table in one call (none when no date is written)."""
    names = [n for n in names if kinds.get(n) in ("datetime", "date")]
    if not names:
        return {}
    ident = quote_ident(table.name)
    statements = [
        (
            f"SELECT {quote_ident(n)} AS v FROM {ident} "  # noqa: S608 — schema identifiers
            f"WHERE {quote_ident(n)} IS NOT NULL LIMIT {_SAMPLE_ROWS}",
            [],
        )
        for n in names
    ]
    results = await cf.query_d1_batch(database_id=database_id, statements=statements)
    out: dict[str, tuple[str, str | None]] = {}
    for name, rows in zip(names, results, strict=False):
        samples = [r.get("v") for r in rows if r.get("v") is not None]
        col = table.column(name)
        fmt = d1.time_format(kinds[name], col.sql_type if col else "", samples)
        out[name] = (fmt, next((s for s in samples if isinstance(s, str)), None))
    return out


async def _coerced(
    cf: Any,
    database_id: str,
    table: d1.Table,
    kinds: dict[str, str],
    values: dict[str, Any],
    errors: dict[str, str],
) -> dict[str, Any]:
    live = [n for n, v in values.items() if v is not None and n not in errors]
    formats = await _time_formats(cf, database_id, table, kinds, live)
    out: dict[str, Any] = {}
    for name, value in values.items():
        if name in errors:
            continue
        fmt, sample = formats.get(name, ("", None))
        try:
            out[name] = coerce(kinds[name], fmt, value, sample=sample)
        except ValueError as exc:
            errors[name] = str(exc)
    return out


def _returning(table: d1.Table) -> str:
    cols = ", ".join(quote_ident(c.name) for c in table.columns)
    return f'{cols}, {table.key_sql} AS "__key"'


def _check_params(params: list[Any]) -> None:
    if len(params) > d1.MAX_PARAMS:
        raise ValidationError(
            "sites.data_too_many_values", "Change fewer columns at once (D1 caps a change)."
        )


def _required(table: d1.Table, column: d1.Column) -> bool:
    if column.hidden in (2, 3) or column.name == table.rowid_alias:
        return False
    if column.name == table.key:  # a key the database doesn't fill
        return True
    return column.notnull and column.default is None


# --------------------------------------------------------------------------- operations


async def insert(
    cf: Any, database_id: str, table: d1.Table, kinds: dict[str, str], values: dict[str, Any]
) -> dict[str, Any]:
    """Insert one row; returns it (raw, with ``__key``). 422 per field for an unknown,
    non-writable or missing required column or a value that can't be stored."""
    errors: dict[str, str] = {}
    _writable_values(table, kinds, values, errors)
    for col in table.columns:
        if _required(table, col) and values.get(col.name) is None and col.name not in errors:
            errors[col.name] = "Required."
    coerced = await _coerced(cf, database_id, table, kinds, values, errors)
    if errors:
        raise invalid(errors)
    names = list(coerced)
    params = [coerced[n] for n in names]
    _check_params(params)
    ident = quote_ident(table.name)
    if names:
        cols = ", ".join(quote_ident(n) for n in names)
        marks = ", ".join("?" for _ in names)
        sql = f"INSERT INTO {ident} ({cols}) VALUES ({marks}) RETURNING {_returning(table)}"
    else:
        sql = f"INSERT INTO {ident} DEFAULT VALUES RETURNING {_returning(table)}"
    rows = await _write(cf.query_d1(database_id=database_id, sql=sql, params=params))
    if not rows:
        raise ValidationError("sites.data_write_failed", "The row wasn't saved.")
    return rows[0]


async def update(
    cf: Any,
    database_id: str,
    table: d1.Table,
    kinds: dict[str, str],
    key: Any,
    set_: dict[str, Any],
    expected: dict[str, Any],
    *,
    mask: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Update one row only if it still holds ``expected``; returns ``(before,
    after)`` raw. 404 when the row is gone, 409 ``sites.data_conflict`` (with the
    current row through ``mask``) when it changed."""
    if not set_:
        raise ValidationError("sites.data_nothing_to_change", "There's nothing to change.")
    errors: dict[str, str] = {}
    _writable_values(table, kinds, set_, errors)
    for name, value in set_.items():
        col = table.column(name)
        if col is None or name in errors:
            continue
        if name == table.key:
            errors[name] = "A row's ID can't be changed."
        elif value is None and col.notnull:
            errors[name] = "Required."
    checks: dict[str, Any] = {}
    for name, value in expected.items():
        col = table.column(name)
        if col is None:
            errors[name] = "Unknown column."
        elif name in table.masked:
            errors[name] = "Credentials can't be compared."
        else:
            try:
                checks[name] = expected_value(kinds[name], value)
            except ValueError as exc:
                errors[name] = str(exc)
    for name in set_:
        if name not in expected and name not in errors:
            errors[name] = "Send the value you saw, so a newer change isn't overwritten."
    coerced = await _coerced(cf, database_id, table, kinds, set_, errors)
    if errors:
        raise invalid(errors)

    ident = quote_ident(table.name)
    assign = ", ".join(f"{quote_ident(n)} = ?" for n in coerced)
    conds = "".join(f" AND {quote_ident(n)} IS ?" for n in checks)
    params = [*coerced.values(), key, *checks.values()]
    _check_params(params)
    select = f"SELECT {_returning(table)} FROM {ident} WHERE {table.key_sql} = ?"  # noqa: S608
    sql = (
        f"UPDATE {ident} SET {assign} WHERE {table.key_sql} = ?{conds} "  # noqa: S608
        f"RETURNING {_returning(table)}"
    )
    before, after = await _write(
        cf.query_d1_batch(database_id=database_id, statements=[(select, [key]), (sql, params)])
    )
    if after:
        return before[0], after[0]
    if not before:
        raise NotFound("site_row", target_key(key))
    raise _conflict(mask(before[0]))


def _cascades(schema: d1.Schema, table: d1.Table) -> list[tuple[str, str, str]]:
    """``(child table, its user-id column, the user column it holds)`` for the login
    tables whose rows belong to a user of ``table``."""
    if table.name.lower() not in d1.USER_TABLES:
        return []
    out: list[tuple[str, str, str]] = []
    for child in schema.tables.values():
        if child.name.lower() not in CASCADE_TABLES:
            continue
        col = next((c for c in child.columns if c.name.replace("_", "").lower() == "userid"), None)
        if col is None:
            continue
        parent_col: str | None
        if col.references:
            parent, _, parent_col = col.references.partition(".")
            if parent != table.name:
                continue
        elif child.name.lower().endswith("s") != table.name.lower().endswith("s"):
            continue  # no FK: pair user/session and users/sessions
        else:
            parent_col = None
        if not parent_col:
            parent_col = table.key if table.key and table.key != "rowid" else "id"
        if table.column(parent_col) is None:
            continue
        out.append((child.name, col.name, parent_col))
    return out


def delete_keys(table: d1.Table, kinds: dict[str, str], keys: Any) -> list[Any]:
    """The request's keys, coerced and de-duplicated; 422 when none or over 500."""
    if not isinstance(keys, list) or not keys:
        raise invalid({"keys": "Pick at least one row."})
    if len(keys) > MAX_KEYS:
        raise ValidationError(
            "sites.data_too_many_keys", f"Delete at most {MAX_KEYS} rows at a time."
        )
    out: list[Any] = []
    for raw in keys:
        try:
            key = key_value(table, kinds, raw)
        except NotFound:
            raise invalid({"keys": f"{raw!r} isn't a row ID of this table."}) from None
        if key not in out:
            out.append(key)
    return out


async def delete(
    cf: Any, database_id: str, schema: d1.Schema, table: d1.Table, keys: list[Any]
) -> list[dict[str, Any]]:
    """Delete the rows with ``keys`` (already through ``delete_keys``), in chunks of
    90, in one atomic batch; a user's sessions and accounts go first in the same
    batch. Returns the deleted rows (raw, with ``__key``)."""
    ident = quote_ident(table.name)
    cascades = _cascades(schema, table)
    statements: list[tuple[str, list[Any]]] = []
    mains: list[int] = []
    for i in range(0, len(keys), KEYS_PER_STATEMENT):
        chunk = keys[i : i + KEYS_PER_STATEMENT]
        marks = ", ".join("?" for _ in chunk)
        for child, child_col, parent_col in cascades:
            statements.append(
                (
                    f"DELETE FROM {quote_ident(child)} WHERE {quote_ident(child_col)} IN "  # noqa: S608
                    f"(SELECT {quote_ident(parent_col)} FROM {ident} "
                    f"WHERE {table.key_sql} IN ({marks}))",
                    list(chunk),
                )
            )
        mains.append(len(statements))
        statements.append(
            (
                f"DELETE FROM {ident} WHERE {table.key_sql} IN ({marks}) "  # noqa: S608
                f"RETURNING {_returning(table)}",
                list(chunk),
            )
        )
    results = await _write(cf.query_d1_batch(database_id=database_id, statements=statements))
    return [row for i in mains for row in (results[i] if i < len(results) else [])]


async def duplicate(
    cf: Any,
    database_id: str,
    table: d1.Table,
    kinds: dict[str, str],
    key: Any,
    values: dict[str, Any],
) -> dict[str, Any]:
    """Copy one row into a new one (``INSERT … SELECT``, one statement), without its
    generated columns and key; ``values`` override copied ones (a new text key).
    Returns the new row (raw); 404 when the source row is gone."""
    errors: dict[str, str] = {}
    _writable_values(table, kinds, values, errors)
    if (
        table.key not in (None, "rowid")
        and table.key != table.rowid_alias
        and values.get(table.key) is None
        and table.key not in errors
    ):
        errors[table.key] = "Required."  # type: ignore[index]
    coerced = await _coerced(cf, database_id, table, kinds, values, errors)
    if errors:
        raise invalid(errors)
    copy = [
        c.name
        for c in table.columns
        if c.hidden not in (2, 3)
        and c.name not in (table.rowid_alias, table.key)
        and c.name not in table.masked
        and c.name not in coerced
    ]
    names = copy + list(coerced)
    exprs = [quote_ident(n) for n in copy] + ["?" for _ in coerced]
    if not names:
        if not table.rowid_alias:
            raise invalid({"key": "This row has nothing to copy."})
        names, exprs = [table.rowid_alias], ["NULL"]
    params = [*coerced.values(), key]
    _check_params(params)
    ident = quote_ident(table.name)
    sql = (
        f"INSERT INTO {ident} ({', '.join(quote_ident(n) for n in names)}) "  # noqa: S608
        f"SELECT {', '.join(exprs)} FROM {ident} WHERE {table.key_sql} = ? "
        f"RETURNING {_returning(table)}"
    )
    rows = await _write(cf.query_d1(database_id=database_id, sql=sql, params=params))
    if not rows:
        raise NotFound("site_row", target_key(key))
    return rows[0]


# --------------------------------------------------------------------------- audit


def _audit_value(value: Any) -> Any:
    if isinstance(value, bytes | bytearray | memoryview):
        return f"<{len(bytes(value))} bytes>"
    if isinstance(value, dict | list):
        value = json.dumps(value, ensure_ascii=False, default=str)
    if isinstance(value, str) and len(value) > AUDIT_VALUE_MAX:
        return value[:AUDIT_VALUE_MAX] + "…"
    return value


def audit_row(table: d1.Table, row: dict[str, Any] | None) -> dict[str, Any] | None:
    """``row`` as the audit log keeps it: no credential columns, no ``__key``, each
    value capped at 1 KB."""
    if row is None:
        return None
    hidden = table.masked | frozenset(d1.masked_columns(table.name, list(row)))
    return {k: _audit_value(v) for k, v in row.items() if k != "__key" and k not in hidden}
