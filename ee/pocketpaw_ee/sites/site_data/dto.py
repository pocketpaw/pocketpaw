# ee/pocketpaw_ee/sites/site_data/dto.py — wire shapes of the Sites Data dashboard
# (``sites.data_router``): the inventory, one table's schema, a page of rows, the row
# write bodies and results, and a row's change history.
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
    restore_point: str | None = None  # a D1 bookmark once restore points ship


class HistoryEvent(BaseModel):
    action: str  # site_data.row_insert | row_update | row_delete
    actor_id: str
    actor_name: str | None = None
    at: str  # ISO 8601
    before: dict[str, Any] | None = None  # credentials removed, values capped at 1 KB
    after: dict[str, Any] | None = None


class RowHistory(BaseModel):
    events: list[HistoryEvent] = Field(default_factory=list)  # newest first, max 50
