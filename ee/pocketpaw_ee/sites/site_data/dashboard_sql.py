# ee/pocketpaw_ee/sites/site_data/dashboard_sql.py — the SQL gate, runner and result
# shaping for Sites AI dashboard widgets. Every widget query (typed by an owner or
# written by a model) passes ``gate`` on every save, test and run; nothing else turns
# widget SQL into a D1 statement.
#
# ``check`` (pure): the console's ``classify`` (one statement, <= 10 KB here, no control
# characters, no parameters, no DDL / transaction / ATTACH / PRAGMA keyword anywhere,
# no internal table name anywhere), then a read whose verb is SELECT or VALUES (a WITH
# prefix allowed). EXPLAIN, PRAGMA reads, ``WITH RECURSIVE``, every ``pragma_*``
# function and a few memory / extension functions are refused too.
# ``gate``: ``check``, then the console's ``plan`` against the live schema: every source
# is a user table or a CTE, any table holding masked columns is rewritten into a
# subquery reading ``MASK`` (so no expression, filter, join, CTE or compound sees a
# credential), and the read is wrapped ``SELECT * FROM (...) LIMIT cap + 1`` with the
# viz's row cap (metric / stat / gauge 1, funnel 50, table 100, chart / sparkline 366).
# It also records ``deps``: the tables and ``table.column`` names the SQL mentions,
# which drift checks compare against a later schema.
# Every refusal is 422 ``sql_rejected`` with ``details.reason`` (the console's code
# without its ``sites.data_`` prefix, or ``not_a_select`` / ``recursive`` / ``pragma`` /
# ``function`` / ``bad_viz``).
#
# ``heavy`` runs ``EXPLAIN QUERY PLAN`` on the planned SQL (save / test time only) and
# reports a full ``SCAN`` of a table over 10k rows; a failed explain is "not heavy".
# ``run_batch`` sends a dashboard's widgets as one D1 batch; a failing batch is re-run
# one statement at a time so one bad widget can't blank the rest. Cloudflare's 429
# raises ``budget.DataBusy``; every other failure becomes that widget's error text
# (``console.clean_error``: ids and anything after the SQLite code removed).
# ``to_result`` re-masks (``console.shape``), cuts to the cap, clips long cells and
# shapes ``data`` for the viz; a mapping column missing from the result is an error.
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any

import httpx

from pocketpaw_ee.cloud._core.errors import CloudError, ValidationError
from pocketpaw_ee.sites.project_data import MASK
from pocketpaw_ee.sites.site_data import budget, console, d1
from pocketpaw_ee.sites.site_data.dashboard_dto import WidgetMapping, WidgetResult

logger = logging.getLogger(__name__)

MAX_SQL_BYTES = 10_000
ROW_CAPS: dict[str, int] = {
    "metric": 1,
    "stat": 1,
    "gauge": 1,
    "funnel": 50,
    "table": 100,
    "chart": 366,
    "sparkline": 366,
}
HEAVY_ROWS = d1.COUNT_CAP  # count_tables answers None above it
CELL_MAX = 2_000
_SINGLE = frozenset({"metric", "stat", "gauge"})
_SERIES = frozenset({"chart", "sparkline", "funnel"})
_CF_BUSY_RETRY_S = 60
UNREACHABLE = "Couldn't reach the site's database."
# Functions D1 should never be asked to run, or that only exist to burn memory.
_FUNCTIONS = frozenset(
    {"load_extension", "randomblob", "zeroblob", "readfile", "writefile", "fts3_tokenizer"}
)


class SqlRejected(ValidationError):
    """422 ``sql_rejected``: the widget's SQL may not run. ``details.reason`` says why."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__("sql_rejected", message, {"reason": reason})
        self.reason = reason


class MappingError(ValueError):
    """A widget's mapping names a column its result doesn't have."""


@dataclass(frozen=True)
class GatedQuery:
    sql: str  # what D1 runs
    params: list[Any]
    mask: frozenset[str]  # result columns to re-mask
    tables: frozenset[str]  # tables referenced (lower case)
    deps: tuple[str, ...]  # "table" and "table.column", schema casing, sorted
    row_cap: int


def _reason(exc: CloudError) -> str:
    return exc.code.removeprefix("sites.data_").removeprefix("sites.")


def check(sql: Any) -> console.Statement:
    """The widget statement, classified, or ``SqlRejected``. Pure: no schema."""
    if not isinstance(sql, str) or not sql.strip():
        raise SqlRejected("sql_empty", "Write a SELECT for this widget.")
    if len(sql.encode("utf-8")) > MAX_SQL_BYTES:
        raise SqlRejected("sql_too_long", "A widget query can be at most 10 KB.")
    try:
        stmt = console.classify(sql)
    except CloudError as exc:
        raise SqlRejected(_reason(exc), exc.message) from exc
    if stmt.kind != "read" or stmt.verb not in ("SELECT", "VALUES"):
        raise SqlRejected("not_a_select", "A widget runs one SELECT. It can't change data.")
    toks = stmt.tokens
    for j, t in enumerate(toks):
        nxt = toks[j + 1] if j + 1 < len(toks) else None
        if t.type == "word" and t.name in _FUNCTIONS and nxt is not None and nxt.type == "lparen":
            raise SqlRejected("function", f"Widgets can't call {t.text}().")
        if t.type == "word" and t.value == "RECURSIVE":
            raise SqlRejected("recursive", "Widgets can't use recursive queries.")
        if t.name_like and t.name.startswith("pragma_"):
            raise SqlRejected("pragma", "Widgets can't read pragma functions.")
    return stmt


def _deps(stmt: console.Statement, schema: d1.Schema) -> tuple[str, ...]:
    by_name = {d1.fold(n): t for n, t in schema.tables.items()}
    used = [by_name[n] for n in sorted(stmt.tables) if n in by_name and n not in stmt.ctes]
    words = {t.name for t in stmt.tokens if t.type in ("word", "ident")}
    out: set[str] = set()
    for table in used:
        out.add(table.name)
        for c in table.columns:
            if d1.fold(c.name) in words:
                out.add(f"{table.name}.{c.name}")
    return tuple(sorted(out))


def gate(sql: Any, viz: str, schema: d1.Schema) -> GatedQuery:
    """The statement D1 runs for this widget, or ``SqlRejected``."""
    if viz not in ROW_CAPS:
        raise SqlRejected("bad_viz", f"Unknown widget type {viz!r}.")
    stmt = check(sql)
    cap = ROW_CAPS[viz]
    try:
        planned = console.plan(stmt, schema, row_cap=cap)
    except CloudError as exc:
        raise SqlRejected(_reason(exc), exc.message) from exc
    return GatedQuery(
        sql=planned.sql,
        params=list(planned.params),
        mask=planned.mask,
        tables=planned.tables,
        deps=_deps(stmt, schema),
        row_cap=cap,
    )


# --------------------------------------------------------------------------- heavy

_SCAN = re.compile(r'^SCAN (?:TABLE )?(?:"((?:[^"]|"")+)"|(\S+))')


async def heavy(cf: Any, database_id: str, q: GatedQuery, schema: d1.Schema) -> str | None:
    """Why the query is heavy (a full scan of a table over 10k rows), else None."""
    try:
        rows = await cf.query_d1(
            database_id=database_id, sql="EXPLAIN QUERY PLAN " + q.sql, params=q.params
        )
    except budget.DataBusy:
        raise
    except Exception:  # noqa: BLE001 — the explain is advice; a widget saves without it
        logger.info("site_dashboards: EXPLAIN QUERY PLAN failed for %s", database_id)
        return None
    by_name = {d1.fold(n): n for n in schema.tables}
    scanned: set[str] = set()
    for r in rows or []:
        m = _SCAN.match(str((r or {}).get("detail") or ""))
        if not m:
            continue
        name = (m.group(1) or "").replace('""', '"') or m.group(2) or ""
        real = by_name.get(d1.fold(name))
        if real:
            scanned.add(real)
    if not scanned:
        return None
    names = sorted(scanned)
    counts = await d1.count_tables(cf, database_id, schema, names)
    big = [n for n in names if n in counts and counts[n] is None]
    if not big:
        return None
    return f"Reads every row of {', '.join(big)} (over {HEAVY_ROWS:,} rows)."


# --------------------------------------------------------------------------- running


def _from_objects(objs: list[dict[str, Any]]) -> dict[str, Any]:
    """A ``/query`` result (rows as objects) in the ``/raw`` shape."""
    columns: list[str] = list(objs[0].keys()) if objs else []
    return {"columns": columns, "rows": [[o.get(c) for c in columns] for o in objs]}


async def run_one(cf: Any, database_id: str, q: GatedQuery) -> dict[str, Any] | str:
    """The raw result of one widget query, or its error text."""
    try:
        return await cf.query_d1_raw(database_id=database_id, sql=q.sql, params=q.params)
    except budget.DataBusy:
        raise
    except httpx.HTTPError as exc:
        logger.warning("site_dashboards: D1 call failed for %s: %r", database_id, exc)
        return UNREACHABLE
    except ValidationError as exc:
        if exc.code != "sites.cloudflare_error":
            raise
        kind = console.failure_kind(exc.message)
        if kind == "busy":
            raise budget.DataBusy(_CF_BUSY_RETRY_S, scope="global") from exc
        if kind == "unreachable":
            logger.warning("site_dashboards: D1 call failed for %s: %s", database_id, exc.message)
            return UNREACHABLE
        return console.clean_error(exc.message)


async def run_batch(
    cf: Any, database_id: str, queries: list[GatedQuery]
) -> list[dict[str, Any] | str]:
    """Each query's raw result or error text, in order: one D1 batch, or one call per
    query when the batch fails."""
    if not queries:
        return []
    if len(queries) == 1:
        return [await run_one(cf, database_id, queries[0])]
    try:
        results = await cf.query_d1_batch(
            database_id=database_id, statements=[(q.sql, q.params) for q in queries]
        )
    except budget.DataBusy:
        raise
    except ValidationError as exc:
        if exc.code != "sites.cloudflare_error":
            raise
        if console.failure_kind(exc.message) == "busy":
            raise budget.DataBusy(_CF_BUSY_RETRY_S, scope="global") from exc
    except httpx.HTTPError as exc:
        logger.warning("site_dashboards: D1 batch failed for %s: %r", database_id, exc)
    else:
        if len(results) == len(queries):
            return [_from_objects(list(r or [])) for r in results]
    return [await run_one(cf, database_id, q) for q in queries]


# --------------------------------------------------------------------------- shaping


def _clip(value: Any) -> Any:
    if isinstance(value, list):  # a blob, as D1's JSON carries it
        return "(binary)"
    if isinstance(value, str) and len(value) > CELL_MAX:
        return value[:CELL_MAX] + "…"
    return value


def _number(value: Any) -> tuple[int | float | None, bool]:
    """``(the chartable number, whether precision was lost)``."""
    if isinstance(value, bool):
        return int(value), False
    if isinstance(value, int):
        return value, False
    if isinstance(value, float):
        return (value, False) if math.isfinite(value) else (None, False)
    if isinstance(value, str) and value != MASK:
        text = value.strip()
        if re.fullmatch(r"[+-]?\d{16,}", text):  # a big integer json_safe sent as digits
            return float(text), abs(int(text)) > d1.JS_SAFE_INT
        try:
            f = float(text)
        except ValueError:
            return None, False
        return (f, False) if math.isfinite(f) else (None, False)
    return None, False


def _label(value: Any) -> str:
    return "" if value is None else str(value)


def _need(name: str, columns: list[str], what: str) -> str:
    if name not in columns:
        raise MappingError(f"The {what} column {name!r} isn't in the result.")
    return name


def shape_data(
    viz: str, mapping: WidgetMapping | None, columns: list[str], rows: list[dict[str, Any]]
) -> tuple[Any, bool]:
    """``(data for the viz, precision_loss)``, or ``MappingError``."""
    m = mapping or WidgetMapping()
    if not columns:  # a result with no rows from a batch carries no column names
        return ({"value": None} if viz in _SINGLE else []), False
    if viz in _SINGLE:
        value_col = _need(m.value_col or columns[0], columns, "value")
        if not rows:
            return {"value": None}, False
        row = rows[0]
        out: dict[str, Any] = {"value": row.get(value_col)}
        if "delta" in columns and value_col != "delta":
            out["delta"] = row.get("delta")
        if viz == "gauge" and "max" in columns and value_col != "max":
            out["max"] = row.get("max")
        return out, False
    if viz == "table":
        shown = [_need(c, columns, "table") for c in (m.columns or columns)]
        return [{c: r.get(c) for c in shown} for r in rows], False
    label = _need(m.label_col or columns[0], columns, "label")
    if m.series_cols:
        values = [_need(c, columns, "series") for c in m.series_cols]
    else:
        default = columns[1] if len(columns) > 1 else columns[0]
        values = [_need(m.value_col or default, columns, "value")]
    points: list[dict[str, Any]] = []
    lossy = False
    for r in rows:
        for col in values:
            v, lost = _number(r.get(col))
            lossy |= lost
            p: dict[str, Any] = {"label": _label(r.get(label)), "value": v}
            if m.series_cols:
                p["series"] = col
            points.append(p)
    return points, lossy


def error_result(message: str) -> WidgetResult:
    return WidgetResult(ok=False, error=message)


def to_result(
    q: GatedQuery,
    viz: str,
    mapping: WidgetMapping | None,
    raw: dict[str, Any] | str,
    *,
    cached_at: str | None = None,
) -> WidgetResult:
    """The widget's result from a raw D1 result (or its error text)."""
    if isinstance(raw, str):
        return error_result(raw)
    names, rows, _ = console.shape(
        list(raw.get("columns") or []), list(raw.get("rows") or []), q.mask
    )
    truncated = len(rows) > q.row_cap
    rows = [{k: _clip(v) for k, v in r.items()} for r in rows[: q.row_cap]]
    try:
        data, lossy = shape_data(viz, mapping, names, rows)
    except MappingError as exc:
        return WidgetResult(ok=False, columns=names, rows=rows, truncated=truncated, error=str(exc))
    return WidgetResult(
        ok=True,
        columns=names,
        rows=rows,
        data=data,
        truncated=truncated,
        cached_at=cached_at,
        precision_loss=lossy,
    )
