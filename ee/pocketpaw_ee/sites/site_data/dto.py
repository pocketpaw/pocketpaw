# ee/pocketpaw_ee/sites/site_data/dto.py — wire shapes of the Sites Data dashboard
# (``sites.data_router``): the inventory, one table's schema, a page of rows, the row
# write bodies and results, a row's change history, the CSV import preview and
# commit, and point-in-time restore (status, restore, undo).
#
# Every response carries ``available`` / ``reason``: a site with no reachable
# database (local mode, not published yet) answers ``available=false`` with
# ``reason`` ``live_on_cloudflare_only`` / ``not_published`` instead of an error, the
# same contract as the legacy ``/data`` routes. Each row in a ``RowPage`` carries
# ``__key``, the value the write routes address the row by (the single primary key,
# else the rowid, else null for a read-only table).
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ColumnKind = Literal[
    "text", "longtext", "integer", "real", "boolean", "datetime", "date", "json", "blob"
]
ReadOnlyReason = Literal["", "no_key", "login_table", "lane", "not_created"]


class ColumnInfo(BaseModel):
    name: str
    sql_type: str  # declared type, lowercased
    kind: ColumnKind
    # How a datetime / date is stored: epoch seconds, epoch milliseconds or ISO text.
    time_format: Literal["", "epoch_s", "epoch_ms", "iso"] = ""
    nullable: bool
    has_default: bool
    auto: bool  # rowid alias / generated: never sent on insert
    primary_key: bool
    masked: bool  # credential column on a login table: value reads "•••"
    writable: bool
    references: str | None = None  # "customers.id" from PRAGMA foreign_key_list


class TableSchema(BaseModel):
    pocket_id: str
    name: str
    available: bool
    reason: str = ""
    columns: list[ColumnInfo] = Field(default_factory=list)
    key: str | None = None  # single-column primary key, else "rowid", else None
    editable: bool = False
    # Rows can be added / duplicated. False on read-only tables and on user tables,
    # whose rows people create by signing up on the site.
    insertable: bool = False
    read_only_reason: ReadOnlyReason = ""
    row_count: int | None = None  # None when over the count cap (or unavailable)


class TableSummary(BaseModel):
    name: str
    row_count: int | None = None
    editable: bool = False
    read_only_reason: ReadOnlyReason = ""


class StoreRef(BaseModel):
    binding: str
    label: str


class ConnectorRef(BaseModel):
    provider: str
    status: str
    dashboard_url: str | None = None


class DataInventory(BaseModel):
    pocket_id: str
    available: bool
    reason: str = ""
    tables: list[TableSummary] = Field(default_factory=list)
    files: list[StoreRef] = Field(default_factory=list)
    kv: list[StoreRef] = Field(default_factory=list)
    connectors: list[ConnectorRef] = Field(default_factory=list)
    can_write: bool = False
    can_restore: bool = False


class RowPage(BaseModel):
    pocket_id: str
    table: str
    available: bool
    reason: str = ""
    columns: list[str] = Field(default_factory=list)
    rows: list[dict[str, Any]] = Field(default_factory=list)  # each carries "__key"
    total: int | None = 0  # None = more than the count cap (10,000)
    limit: int
    offset: int
    # True when the search had to skip some text columns to stay under D1's
    # 100-bound-parameter cap.
    search_truncated: bool = False


# --------------------------------------------------------------------------- writes
#
# Bodies refuse unknown fields, so nothing beyond these can reach the write path.
# Keys and values are JSON scalars; the server coerces them to each column's type.

Scalar = str | int | float | bool | None


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class InsertRowBody(_Body):
    values: dict[str, Any] = Field(default_factory=dict)


class UpdateRowBody(_Body):
    # ``set``: the new values. ``expected``: what the owner saw for (at least) every
    # column in ``set``; the update lands only if the row still holds those values.
    set_: dict[str, Any] = Field(default_factory=dict, alias="set")
    expected: dict[str, Any] = Field(default_factory=dict)


class DeleteRowsBody(_Body):
    keys: list[Scalar] = Field(default_factory=list)


class DuplicateRowBody(_Body):
    key: Scalar
    # Values to put in the copy instead of the source's (a new text key, say).
    values: dict[str, Any] = Field(default_factory=dict)


class RowOut(BaseModel):
    row: dict[str, Any]  # masked, carries "__key"


class DeleteRowsOut(BaseModel):
    deleted: int
    keys: list[Any]  # the keys that were deleted
    # The D1 bookmark from just before a bulk (2+ key) delete; null for one row
    # (undone by re-inserting it) or when the database has no Time Travel.
    restore_point: str | None = None


class HistoryEvent(BaseModel):
    action: str  # site_data.row_insert | row_update | row_delete
    actor_id: str
    actor_name: str | None = None
    at: str  # ISO 8601
    before: dict[str, Any] | None = None  # credentials removed, values capped at 1 KB
    after: dict[str, Any] | None = None


class RowHistory(BaseModel):
    events: list[HistoryEvent] = Field(default_factory=list)  # newest first, max 50


# --------------------------------------------------------------------------- CSV import


class ImportTarget(BaseModel):
    """A column a CSV column can be mapped to."""

    name: str
    kind: ColumnKind | Literal["any"]
    required: bool  # NOT NULL without a default (or a key the database doesn't fill)
    key: bool  # the table's key: the column an upsert matches rows on


class ImportRowError(BaseModel):
    row: int  # the row number a spreadsheet shows: the header is row 1
    column: str | None = None  # the CSV column, when one cell is at fault
    error: str


class ImportPreview(BaseModel):
    pocket_id: str
    table: str
    upload_id: str  # good for 15 minutes, for this user, site and table only
    expires_in: int  # seconds
    filename: str
    header: list[str]
    rows: list[list[str]]  # the first 20 data rows, as read
    total_rows: int
    mapping: dict[str, str | None]  # CSV column -> table column (auto-matched) or null
    targets: list[ImportTarget]  # every column a CSV column may map to
    key: str | None  # the column ``upsert`` matches on; null when upsert is impossible
    errors: list[ImportRowError] = Field(default_factory=list)  # under ``mapping``, max 100
    error_count: int = 0


class ImportBody(_Body):
    upload_id: str = Field(max_length=128)
    mapping: dict[str, str | None]
    mode: Literal["insert", "upsert"] = "insert"


class ImportFailure(BaseModel):
    row: int
    error: str


class ImportResult(BaseModel):
    inserted: int
    updated: int
    failed: list[ImportFailure] = Field(default_factory=list)  # max 100
    failed_count: int = 0
    # True when the import stopped part way (a spent budget, a dropped connection):
    # the rows not sent are in ``failed`` as "Not imported", and the upload is gone.
    stopped: bool = False
    restore_point: str | None = None  # the D1 bookmark from before the first write


# --------------------------------------------------------------------------- restore


class RestoreStatus(BaseModel):
    pocket_id: str
    available: bool
    # "" | live_on_cloudflare_only | not_published | not_production
    reason: str = ""
    site_name: str = ""  # what ``confirm_name`` must match
    window_days: int
    earliest: str | None = None  # ISO 8601: the oldest moment a restore can reach
    at: str | None = None  # the moment asked about, normalised to UTC
    bookmark: str | None = None  # the bookmark nearest at or before ``at``
    changes_since: int | None = None  # dashboard changes to this site's data since ``at``
    visitor_writes_counted: bool = False  # visitors' own writes are never in that count


class RestoreBody(_Body):
    at: str = Field(max_length=64)
    confirm_name: str = Field(max_length=500)


class RestoreUndoBody(_Body):
    previous_bookmark: str = Field(max_length=200)
    confirm_name: str = Field(max_length=500)


class RestoreOut(BaseModel):
    bookmark: str  # where the database is now
    previous_bookmark: str  # where it was; ``restore:undo`` with this goes back
