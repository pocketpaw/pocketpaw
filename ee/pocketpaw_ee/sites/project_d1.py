# ee/pocketpaw_ee/sites/project_d1.py: apply a ``project`` site's own D1 migrations
# on publish, through the Cloudflare D1 HTTP API.
#
# A project that binds D1 (the d1-drizzle recipe) ships its schema as
# ``migrations/*.sql`` in its source map. Publish reads them from the source map the
# deployed bundle was built from (the bundle is keyed by that source's content hash,
# so the two cannot disagree) and applies the pending ones to the site's database
# BEFORE the Worker upload. Nothing here runs wrangler, drizzle-kit or any author
# config on this host: only SQL text goes over the API.
#
# Rules:
#   * Only top-level ``migrations/*.sql`` count, applied in filename order. Anything
#     else under ``migrations/`` (drizzle's ``meta/_journal.json``) is ignored.
#   * Applied migrations are tracked in ``_paw_migrations`` (name, applied_at,
#     sha256) inside the site's D1. An applied one is skipped; an applied one whose
#     file changed refuses the publish (applied migrations are immutable).
#   * Statements split on drizzle's ``--> statement-breakpoint`` and on ``;``,
#     never inside a string, quoted identifier or comment, and not inside a
#     ``CREATE TRIGGER ... END`` body. Each migration and its tracking row are sent
#     as one D1 batch.
#   * A pending DROP TABLE, ALTER TABLE ... DROP COLUMN or DELETE without WHERE that
#     would hit a table holding rows refuses the publish unless the owner confirmed
#     destructive migrations. Drizzle's table rebuild (copy into a new table, drop
#     the old, rename the new one into place) keeps the data and is not refused.
#   * A failed migration aborts the publish with the migration's name and D1's
#     error; nothing has been uploaded at that point.
from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pocketpaw_ee.cloud._core.errors import ValidationError
from pocketpaw_ee.sites.engines import safe_rel

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = "migrations"
TRACKING_TABLE = "_paw_migrations"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.sql$")
_BREAKPOINT = "> statement-breakpoint"
_TRIGGER = re.compile(r"^\s*CREATE\s+(?:TEMP\s+|TEMPORARY\s+)?TRIGGER\b", re.I)
_ENDS_WITH_END = re.compile(r"\bEND\s*$", re.I)
_PART = r"(?:\"(?:[^\"]|\"\")*\"|`[^`]*`|\[[^\]]*\]|[A-Za-z_][\w$]*)"
_IDENT = rf"({_PART}(?:\s*\.\s*{_PART})?)"
_DROP_TABLE = re.compile(rf"^DROP\s+TABLE\s+(?:IF\s+EXISTS\s+)?{_IDENT}", re.I)
_DROP_COLUMN = re.compile(rf"^ALTER\s+TABLE\s+{_IDENT}\s+DROP\s+(?:COLUMN\s+)?", re.I)
_DELETE = re.compile(rf"^(?:WITH\b.*?\)\s*)?DELETE\s+FROM\s+{_IDENT}", re.I | re.S)
_WHERE = re.compile(r"\bWHERE\b", re.I)
_COPY_FROM = re.compile(rf"^INSERT\s+INTO\s+.*?\bSELECT\b.*?\bFROM\s+{_IDENT}", re.I | re.S)
_RENAME_TO = re.compile(rf"^ALTER\s+TABLE\s+{_IDENT}\s+RENAME\s+TO\s+{_IDENT}", re.I)


@dataclass(frozen=True)
class Statement:
    sql: str
    # ``sql`` with every string literal blanked, for keyword checks only.
    masked: str


@dataclass(frozen=True)
class Migration:
    name: str
    sql: str
    sha256: str
    statements: list[Statement] = field(default_factory=list)


def _refuse(code: str, message: str) -> ValidationError:
    return ValidationError(code, message)


# ------------------------------------------------------------------ parsing


def _split(sql: str) -> list[Statement]:
    out: list[Statement] = []
    text: list[str] = []
    masked: list[str] = []
    i, n = 0, len(sql)

    def flush() -> None:
        stmt, mask = "".join(text).strip(), "".join(masked).strip()
        if stmt:
            out.append(Statement(stmt, mask))
        text.clear()
        masked.clear()

    while i < n:
        ch = sql[i]
        if sql.startswith("--", i):
            end = sql.find("\n", i)
            end = n if end == -1 else end
            if sql[i + 2 : end].strip().startswith(_BREAKPOINT):
                flush()
            else:
                text.append(" ")
                masked.append(" ")
            i = end
            continue
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            i = n if end == -1 else end + 2
            text.append(" ")
            masked.append(" ")
            continue
        if ch in "'\"`[":
            close = "]" if ch == "[" else ch
            j = i + 1
            while j < n:
                if sql[j] == close:
                    # A doubled quote is an escaped quote, not the end.
                    if close != "]" and j + 1 < n and sql[j + 1] == close:
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                raise _refuse("sites.migration_invalid", "a quote is never closed")
            chunk = sql[i : j + 1]
            text.append(chunk)
            masked.append("''" if ch == "'" else chunk)
            i = j + 1
            continue
        if ch == ";":
            current = "".join(masked)
            if _TRIGGER.match(current) and not _ENDS_WITH_END.search(current):
                text.append(ch)
                masked.append(ch)
                i += 1
                continue
            flush()
            i += 1
            continue
        text.append(ch)
        masked.append(ch)
        i += 1
    flush()
    return out


def split_statements(sql: str) -> list[str]:
    """The statements of one migration file, without comments or separators."""
    return [s.sql for s in _split(sql)]


def migrations_from_source(source: Mapping[str, Any] | None) -> list[Migration]:
    """The project's ``migrations/*.sql`` files in filename order, parsed."""
    found: list[Migration] = []
    for raw, contents in (source or {}).items():
        path = safe_rel(raw)
        if path is None:
            continue
        parts = path.split("/")
        if len(parts) != 2 or parts[0] != MIGRATIONS_DIR or not parts[1].lower().endswith(".sql"):
            continue
        name = parts[1]
        if not _NAME.match(name):
            raise _refuse(
                "sites.migration_invalid",
                f"The migration file name {name!r} is not allowed. Use letters, digits, "
                "dots, dashes and underscores.",
            )
        if not isinstance(contents, str):
            raise _refuse("sites.migration_invalid", f"The migration {name} must be text.")
        try:
            statements = _split(contents)
        except ValidationError as exc:
            raise _refuse("sites.migration_invalid", f"Migration {name}: {exc.message}") from exc
        found.append(
            Migration(
                name=name,
                sql=contents,
                sha256=hashlib.sha256(contents.encode("utf-8")).hexdigest(),
                statements=statements,
            )
        )
    return sorted(found, key=lambda m: m.name)


# -------------------------------------------------------------- destructive


def _ident(raw: str) -> str:
    """A table name as SQLite compares it: unquoted, schema dropped, lower case."""
    last = re.split(r"\s*\.\s*(?=[\"`\[A-Za-z_])", raw.strip())[-1]
    if last[:1] in '"`[':
        last = last[1:-1].replace('""', '"')
    return last.lower()


def destructive_targets(migration: Migration) -> list[tuple[str, str]]:
    """``(table, statement)`` for each statement in ``migration`` that can delete
    rows. A DROP TABLE that is part of drizzle's rebuild (the table's rows copied
    into another table earlier, and a table renamed to its name later) is not."""
    stmts = [s.masked for s in migration.statements]
    hits: list[tuple[str, str]] = []
    for index, masked in enumerate(stmts):
        if m := _DROP_TABLE.match(masked):
            table = _ident(m.group(1))
            copied = any(
                (c := _COPY_FROM.match(prev)) and _ident(c.group(1)) == table
                for prev in stmts[:index]
            )
            renamed = any(
                (r := _RENAME_TO.match(nxt)) and _ident(r.group(2)) == table
                for nxt in stmts[index + 1 :]
            )
            if not (copied and renamed):
                hits.append((table, migration.statements[index].sql))
        elif m := _DROP_COLUMN.match(masked):
            hits.append((_ident(m.group(1)), migration.statements[index].sql))
        elif (m := _DELETE.match(masked)) and not _WHERE.search(masked[m.end() :]):
            hits.append((_ident(m.group(1)), migration.statements[index].sql))
    return hits


async def _table_has_rows(cf: Any, database_id: str, table: str) -> bool:
    rows = await cf.query_d1(
        database_id=database_id,
        sql="SELECT name FROM sqlite_master WHERE type = 'table' AND lower(name) = ?",
        params=[table],
    )
    if not rows:
        return False
    actual = str(rows[0].get("name") or table)
    quoted = '"' + actual.replace('"', '""') + '"'
    found = await cf.query_d1(database_id=database_id, sql=f"SELECT 1 AS x FROM {quoted} LIMIT 1")
    return bool(found)


# -------------------------------------------------------------------- apply


def _short(statement: str, limit: int = 120) -> str:
    one = " ".join(statement.split())
    return one if len(one) <= limit else one[: limit - 3] + "..."


async def apply_migrations(
    cf: Any,
    database_id: str,
    migrations: list[Migration],
    *,
    confirm_destructive: bool = False,
) -> list[str]:
    """Apply the pending ``migrations`` to ``database_id``; return the names applied.

    Raises ``ValidationError`` (``sites.migration_changed`` /
    ``sites.migration_destructive`` / ``sites.migration_failed``) before or instead
    of applying anything the rules above refuse."""
    if not migrations:
        return []
    try:
        await cf.query_d1(
            database_id=database_id,
            sql=(
                f"CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} ("
                "name TEXT PRIMARY KEY NOT NULL, applied_at TEXT NOT NULL, "
                "sha256 TEXT NOT NULL)"
            ),
        )
        applied = {
            str(row.get("name")): str(row.get("sha256") or "")
            for row in await cf.query_d1(
                database_id=database_id, sql=f"SELECT name, sha256 FROM {TRACKING_TABLE}"
            )
        }
    except ValidationError as exc:
        raise _refuse(
            "sites.migration_failed",
            f"Could not read this site's migration history from its D1 database: {exc.message}",
        ) from exc

    changed = [m.name for m in migrations if m.name in applied and applied[m.name] != m.sha256]
    if changed:
        raise _refuse(
            "sites.migration_changed",
            f"Applied migrations cannot change, but {', '.join(changed)} differs from what "
            "this site's database already ran. Put the file back as it was and add a new "
            "migration for the change.",
        )
    pending = [m for m in migrations if m.name not in applied]

    if not confirm_destructive:
        for migration in pending:
            for table, statement in destructive_targets(migration):
                if await _table_has_rows(cf, database_id, table):
                    raise _refuse(
                        "sites.migration_destructive",
                        f"Migration {migration.name} would delete data this site already "
                        f"holds ({_short(statement)}). Publish again and confirm "
                        "destructive migrations to apply it, or change the migration.",
                    )

    done: list[str] = []
    for migration in pending:
        batch: list[tuple[str, list]] = [(s.sql, []) for s in migration.statements]
        batch.append(
            (
                f"INSERT INTO {TRACKING_TABLE} (name, applied_at, sha256) VALUES (?, ?, ?)",
                [migration.name, datetime.now(UTC).isoformat(), migration.sha256],
            )
        )
        try:
            await cf.query_d1_batch(database_id=database_id, statements=batch)
        except ValidationError as exc:
            raise _refuse(
                "sites.migration_failed",
                f"Migration {migration.name} failed, so the site was not published: {exc.message}",
            ) from exc
        logger.info("sites.project_d1: applied %s to %s", migration.name, database_id)
        done.append(migration.name)
    return done


__all__ = [
    "MIGRATIONS_DIR",
    "TRACKING_TABLE",
    "Migration",
    "Statement",
    "apply_migrations",
    "destructive_targets",
    "migrations_from_source",
    "split_statements",
]
