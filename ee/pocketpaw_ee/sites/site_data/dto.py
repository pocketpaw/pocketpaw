# ee/pocketpaw_ee/sites/site_data/dto.py — wire shapes of the Sites Data dashboard
# read routes (``sites.data_router``): the inventory, one table's schema, and a page
# of rows.
#
# Every response carries ``available`` / ``reason``: a site with no reachable
# database (local mode, not published yet) answers ``available=false`` with
# ``reason`` ``live_on_cloudflare_only`` / ``not_published`` instead of an error, the
# same contract as the legacy ``/data`` routes. Each row in a ``RowPage`` carries
# ``__key``, the value the write routes address the row by (the single primary key,
# else the rowid, else null for a read-only table).
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

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
