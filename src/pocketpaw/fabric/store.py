# Fabric store — async SQLite operations for the ontology layer.
# Created: 2026-03-28 — CRUD for object types, objects, and links.
# Updated: 2026-04-19 (Cluster C / PR3) — Added list_links() for the new
#   GET /api/v1/fabric/links endpoint that the Links sub-tab in
#   PocketDataPanel now consumes instead of its hardcoded placeholder.
# Updated: 2026-06-10 (W0d) — query() now honors FabricQuery.filters. Property
#   filters were previously parsed into the model but silently dropped, so
#   "leases where rent > X" returned ALL objects of the type. Filters are
#   applied against the JSON properties bag via json_extract with whitelisted
#   operators; property names go through a fixed validation gate and values are
#   always bound parameters (no value interpolation). Comparison operators use
#   CAST(... AS REAL) so numeric comparisons stay numeric regardless of param
#   type. New helper _build_filter_conditions() keeps the change localized to
#   the filter logic so a later workspace_id-scoping change merges cleanly.
# Updated: 2026-06-10 (W4a — workspace-scope fabric store) — closes a
#   cross-tenant data leak on shared deployments (the micro tier / an agency
#   running multiple client tenants share one ``fabric.db``). Objects and links
#   now carry a ``workspace_id`` column. Writes (``create_object`` / ``link``)
#   stamp the caller's workspace; reads (``query`` / ``list_links`` /
#   ``get_object`` / ``get_linked_objects``) take an optional ``workspace_id``
#   and, when supplied, restrict results to that tenant. The scoping is an
#   ADDITIONAL WHERE condition layered ALONGSIDE W0d's property filters in
#   ``query()`` — the filter logic is untouched. ``workspace_id`` crosses from
#   the EE router as a PLAIN str (the OSS store never imports pocketpaw_ee).
#   Legacy/NULL treatment: rows written before this change (or by a non-cloud
#   OSS caller that passes no workspace) have NULL ``workspace_id``. A scoped
#   read matches ``workspace_id = ? OR workspace_id IS NULL`` so legacy/global
#   data predating tenancy stays visible to every tenant (it cannot be safely
#   attributed to one workspace after the fact, and single-tenant deployments
#   must keep working). New writes always stamp a workspace when one is given,
#   so going-forward data is cleanly isolated. A read with ``workspace_id=None``
#   applies no scoping at all (full backward-compat for OSS / agent-tool
#   callers). Additive ALTER migration mirrors the W2b assignee/hash-chain
#   pattern — no crash on a pre-existing DB.
# Updated: 2026-06-10 (FIX 3 — hardening) — tightened the filter property-name
#   validator in _build_filter_conditions() from ``c.isalnum() or c in "_-"`` to
#   ``c.isalnum() or c == "_"``. Hyphens were not a vulnerability (json_extract
#   reads ``$.a-b`` as a literal key) but were unnecessarily permissive and made
#   ``$.a-b`` ambiguous in a SQL trace. No object type uses hyphenated property
#   names, so this loses nothing. W0d filter tests unchanged and still pass.
# Updated: 2026-06-11 (gap1-connfabric — connector→Fabric ingestion slice) — added
#   get_object_by_source(): looks up a single object by its
#   (source_connector, source_id) provenance pair via the existing
#   idx_objects_source index. This is the idempotency primitive the new
#   connector ingest path (connectors/fabric_ingest.py) needs to upsert instead
#   of duplicate — until now Fabric had no source-keyed read, so a re-sync
#   always created a second object (documented in
#   tests/cloud/test_e2e_connector_to_fabric.py::test_fabric_source_deduplication).
#   Read-only and additive; honors the W4a workspace scope like get_object().
# Updated: 2026-06-11 (gap-housekeeping) — three small hardening fixes:
#   (1) fabric_object_types.name gets a UNIQUE index so a concurrent ensure_type
#       race can't define the same type twice with different ids. Created AFTER
#       SCHEMA_SQL (mirrors the _WORKSPACE_INDEX_SQL pattern), NOT inside the
#       executescript — a pre-W4a / pre-this-change DB may already hold duplicate
#       name rows, so _ensure_schema de-dups defensively first (keeps the lowest
#       rowid per case-folded name, re-points objects of the losing types at the
#       survivor, then drops the loser rows) and wraps the index creation in
#       try/except so a residual dup can never crash _ensure_schema — it logs a
#       warning and leaves the unique index uncreated instead.
#   (2) update_object() now threads an optional workspace_id and applies the same
#       `workspace_id = ? OR workspace_id IS NULL` scope as get_object /
#       get_object_by_source, so the write has its OWN tenancy guard rather than
#       trusting the caller to have scoped the prior read. connectors.fabric_ingest
#       passes workspace_id through.
# Updated: 2026-06-11 (fix/fabric-stats-workspace-scope) — stats() and
#   list_types() take an optional workspace_id, closing the LAST unscoped W4a
#   reads (a live shared box leaked one tenant's experimental type names into
#   another tenant's chat via fabric_stats). Scoped stats mirrors query()'s
#   visibility exactly (own rows + legacy NULL rows, via _workspace_scope) so
#   stats and query always agree; scoped types/list_types return only DEFINED
#   types with at least one visible object row — definitions are global (no
#   workspace_id column on fabric_object_types), but which types a tenant sees
#   is tenant metadata. workspace_id=None keeps the original unscoped behavior
#   (OSS / registry-tool / single-tenant callers).
# Updated: 2026-06-13 (feat/fabric-multihop) — query() now supports multi-hop /
#   path traversal. When FabricQuery.path is non-empty it walks an ontology join
#   server-side instead of the single linked_to hop: the audit's 2-hop query
#   ("open Deals whose Customer competes_with a Competitor") that returned [] as
#   one query and had to be hand-stitched as two get_linked_objects calls is now
#   ONE call. New _query_path() resolves a START frontier (the linked_to seed, or
#   every object matching the top-level type/filters when linked_to is absent),
#   then _advance_hop() steps the frontier one PathHop at a time across
#   fabric_links — each hop applies its direction (out/in/any), link_type,
#   terminal object_type, property filters, AND the W4a workspace scope to the
#   FAR object. Iterative per-hop resolution (one parameterized query per hop)
#   was chosen over a recursive CTE: per-hop type+property+direction+tenant
#   filters stay simple and injection-safe as a normal parameterized WHERE, and
#   paths are shallow (2-3 hops). All link_type / object_type / filter values
#   remain bound parameters; only fixed SQL fragments are concatenated. The
#   single-hop linked_to/link_type path is UNTOUCHED (backward compatible) —
#   path and the legacy single-hop are mutually exclusive (path wins).
# Updated: 2026-06-13 (review fixes #1465) — bounded the traversal so it can't
#   crash SQLite or run away: (1) MAX_FRONTIER (500) guards _advance_hop on entry
#   AND the terminal re-fetch, raising a clear ValueError before a frontier IN-
#   list could exceed SQLite's 999 bound-variable limit (path depth itself is
#   capped at MAX_HOPS=5 by the FabricQuery validator). (2) the terminal re-fetch
#   now carries the same (workspace_id = ? OR workspace_id IS NULL) scope as the
#   single-hop query()'s SELECT — defense-in-depth; the frontier ids are already
#   tenant-scoped per hop, so this changes no result, it just makes the last read
#   self-guarding. Walk is iterative, fixed-depth, no cross-hop cycle re-visit
#   (see the note in _query_path).
# Updated: 2026-06-19 (SZD-2 — workspace-scope object TYPES) — closes the LAST
#   cross-tenant leak in the Fabric store: the object-TYPE catalog was GLOBAL.
#   W4a deliberately left ``fabric_object_types`` without a ``workspace_id``
#   column ("definitions are not per-tenant data"), and ``list_types`` / ``stats``
#   only hid type NAMES indirectly by joining to a visible object row. But the
#   "sovereign zero-setup discovery" feature requires the DISCOVERED type catalog
#   to be private per workspace, and ``define_type`` / ``get_type_by_name`` took
#   no workspace — so a type defined in workspace A was directly visible and
#   reusable from workspace B (``get_type_by_name`` returned it, ``ensure_type``
#   reused its id). This change:
#     1. Adds a ``workspace_id TEXT`` column to ``fabric_object_types`` via the
#        same additive ALTER migration the W4a object/link columns use (idempotent
#        — the duplicate-column OperationalError is swallowed on every boot). A
#        pre-existing row keeps NULL ``workspace_id`` = legacy/global; the backfill
#        below attributes a row to a workspace ONLY when every object of that type
#        unambiguously shares one workspace (otherwise it stays NULL = global, the
#        documented sentinel — a type cannot be safely attributed after the fact
#        when its objects span tenants or predate tenancy).
#     2. ``define_type`` stamps the caller's ``workspace_id``; ``get_type_by_name``
#        and ``get_type`` take an optional ``workspace_id`` and apply the same
#        ``(workspace_id = ? OR workspace_id IS NULL)`` scope as every other W4a
#        read. A scoped read for workspace B can therefore neither see nor reuse
#        workspace A's type. ``workspace_id=None`` = unscoped (OSS / agent-tool /
#        single-tenant callers), full backward compatibility.
#     3. ``list_types`` / ``stats`` now scope on the TYPE's OWN ``workspace_id``
#        (own rows + legacy NULL) instead of joining through a visible object row,
#        so a tenant's empty (object-less) type is visible to its owner and an
#        owned-but-empty type no longer disappears — the type catalog is now first
#        -class tenant data, not metadata inferred from object rows.
#     4. The unique-name index becomes UNIQUE on ``(workspace_id, LOWER(name))``
#        (was a global UNIQUE on ``LOWER(name)``) so two workspaces may each define
#        their own "Customer" type; the concurrent-``ensure_type`` race guard now
#        holds PER workspace. The de-dup pass keys on ``(workspace_id, name)`` to
#        match. ``ensure_type`` / ``ingest_records`` (connectors/fabric_ingest.py)
#        thread ``workspace_id`` so existing ingestion keeps working and minted
#        types land in the caller's tenant.
# Updated: 2026-06-26 (ISO-1 — physical per-workspace isolation) — added
#   aclose(): a best-effort WAL-checkpoint + state reset the new workspace-keyed
#   store factory (src/pocketpaw/stores.py) runs when it evicts a per-workspace
#   FabricStore from its bounded LRU. The store still holds no long-lived
#   connection; aclose exists only so an idle tenant's write-ahead-log sidecar
#   gets truncated instead of growing unbounded across 128+ cached tenants. The
#   W4a in-row workspace_id WHERE-filter is UNCHANGED — physical file isolation
#   is ADDITIVE defense-in-depth layered on top of it, never a replacement.
# Updated: 2026-07-10 (ontology-operator-ux) — makes the ontology operable by a
#   non-engineer, in three additive pieces:
#     1. WRITE-TIME TYPE ENFORCEMENT. ``validate_object_properties()`` checks a
#        provided property bag against the type's declared ``PropertyDef``s and
#        raises ``FabricTypeError`` on a clash. ``create_object`` and
#        ``update_object`` now call it (create validates the full bag; update
#        validates only the provided delta). Enforcement is DECLARED-ONLY and
#        LENIENT by design so it never breaks live connector / agent ingest: only
#        declared properties are checked, a ``None`` / absent value is skipped
#        (``required`` is NOT enforced at write time), unknown keys pass through,
#        an empty schema is a no-op, and scalar checks accept the JSON-string form
#        of a number/bool/date (a connector often ships "42"). A genuinely wrong
#        value — "not-a-number" for a ``number`` field, a value outside a declared
#        ``enum`` — is rejected.
#     2. SCHEMA VERSIONING + NON-DESTRUCTIVE MIGRATION. ``fabric_object_types``
#        gains a ``version INTEGER DEFAULT 1`` column (additive ALTER, swallow the
#        duplicate-column error like the W4a/SZD-2 tenancy columns). ``update_type``
#        bumps the version and migrates existing objects for a property RENAME (the
#        key is moved on every object of the type) and an ADDITIVE add (a new
#        property carrying a default is backfilled onto objects that lack it).
#        DESTRUCTIVE removal is DEFERRED: a property dropped from the schema leaves
#        its now-orphaned key untouched on existing objects (documented behaviour,
#        asserted in tests).
#     3. Both are OSS-side and framework-agnostic; ``FabricTypeError`` lives in
#        models so the EE router (422) and OSS callers (ValueError) both consume it.
# Updated: 2026-07-11 (feat/paw-cli, C2) — added ``get_link(link_id,
#   workspace_id=None)``, a scoped single-link read mirroring ``get_object``.
#   It exists as the tenancy guard for deletes: ``unlink`` is deliberately
#   unscoped, so multi-tenant callers (the EE DELETE /fabric/links route and
#   the fabric MCP link-delete tool) resolve the link through this scoped read
#   first and refuse a cross-tenant id.


from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import aiosqlite

from pocketpaw.fabric.models import (
    FabricLink,
    FabricObject,
    FabricQuery,
    FabricQueryResult,
    FabricTypeError,
    ObjectType,
    PathHop,
    PropertyDef,
)

logger = logging.getLogger(__name__)

# Hard cap on the working set during a multi-hop traversal. The per-hop query
# binds one ``?`` per frontier id in a ``WHERE l.<col> IN (?, ?, …)`` list; left
# unbounded a frontier of thousands would blow past SQLite's 999-bound-variable
# limit (SQLITE_MAX_VARIABLE_NUMBER) with an OperationalError, and a wide fan-out
# is a latency / memory risk regardless. 500 keeps every per-hop and terminal
# IN-list comfortably under 999 (500 ids + link_type + a few filter params) while
# covering any realistic ontology join. A frontier that exceeds it raises a clear
# ValueError, which the agent tool turns into a readable message — much better
# than a raw SQLite crash.
MAX_FRONTIER = 500

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS fabric_object_types (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    icon TEXT DEFAULT 'box',
    color TEXT DEFAULT '#0A84FF',
    properties_schema TEXT DEFAULT '[]',
    -- Tenancy (SZD-2): the owning workspace of this object TYPE. NULL =
    -- legacy/global type written before per-type tenancy or by an OSS caller;
    -- a scoped read still sees it (own rows + NULL). On a pre-SZD-2 DB this
    -- column is added by the ALTER migration in _ensure_schema, NOT here.
    workspace_id TEXT,
    -- Schema version (ontology-operator-ux). 1 on define_type; bumped by
    -- update_type on a non-destructive change (rename / additive add). On a
    -- pre-version DB this column is added by the ALTER migration, NOT here.
    version INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fabric_objects (
    id TEXT PRIMARY KEY,
    type_id TEXT NOT NULL REFERENCES fabric_object_types(id),
    type_name TEXT DEFAULT '',
    properties TEXT NOT NULL DEFAULT '{}',
    source_connector TEXT,
    source_id TEXT,
    -- Tenancy (W4a): the owning workspace. NULL = legacy/global row written
    -- before tenancy or by a non-cloud OSS caller; a scoped read still sees it.
    workspace_id TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fabric_links (
    id TEXT PRIMARY KEY,
    from_object_id TEXT NOT NULL REFERENCES fabric_objects(id),
    to_object_id TEXT NOT NULL REFERENCES fabric_objects(id),
    link_type TEXT NOT NULL,
    properties TEXT DEFAULT '{}',
    -- Tenancy (W4a): same workspace semantics as fabric_objects. A link is
    -- scoped to the workspace of the caller that created it.
    workspace_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_objects_type ON fabric_objects(type_id);
CREATE INDEX IF NOT EXISTS idx_objects_source ON fabric_objects(source_connector, source_id);
CREATE INDEX IF NOT EXISTS idx_links_from ON fabric_links(from_object_id);
CREATE INDEX IF NOT EXISTS idx_links_to ON fabric_links(to_object_id);
CREATE INDEX IF NOT EXISTS idx_links_type ON fabric_links(link_type);
"""

# Tenancy indexes are created AFTER the ALTER migration (see _ensure_schema),
# NOT in SCHEMA_SQL above. On a pre-W4a DB the table already exists, so
# CREATE TABLE IF NOT EXISTS is a no-op and the workspace_id column is added by
# ALTER — a CREATE INDEX on workspace_id inside the same executescript would
# run before that ALTER and fail with "no such column". (Bug found by live
# smoke 2026-06-10; see tests/cloud/test_w4a_migration.py.)
_WORKSPACE_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_objects_workspace ON fabric_objects(workspace_id)",
    "CREATE INDEX IF NOT EXISTS idx_links_workspace ON fabric_links(workspace_id)",
    # SZD-2: a plain (non-unique) index on the type's workspace for scoped
    # list_types / stats reads. The UNIQUE (workspace_id, LOWER(name)) index is
    # created separately (_TYPE_NAME_UNIQUE_INDEX_SQL) after the de-dup pass.
    "CREATE INDEX IF NOT EXISTS idx_object_types_workspace ON fabric_object_types(workspace_id)",
)

# A UNIQUE index on (workspace_id, case-folded type name) closes a concurrent
# ``ensure_type`` race: two callers both miss ``get_type_by_name`` and both run
# ``define_type``, leaving two type rows with the SAME name but different ids —
# objects of "the same logical type" then split across two ``type_id``s. Created
# AFTER SCHEMA_SQL (same reason as _WORKSPACE_INDEX_SQL): a pre-existing DB may
# already hold duplicate-name rows, so _ensure_schema de-dups defensively before
# creating the index. ``get_type_by_name`` matches case-insensitively, so the
# name key is LOWER(name).
#
# SZD-2: the uniqueness key now includes ``workspace_id`` so two DIFFERENT
# workspaces may each define their own "Customer" type — the race guard is
# per-workspace. SQLite treats NULLs as DISTINCT in a UNIQUE index, so legacy
# NULL-workspace rows are NOT collapsed by this index (multiple NULL-workspace
# "Customer" rows can coexist); the de-dup pass that runs first keys on
# (workspace_id, name) and still collapses true within-workspace duplicates
# (including NULL/NULL pairs) up front. The old global index on LOWER(name)
# (``idx_object_types_name_unique``) is DROPPED in the migration first, because
# it would otherwise reject a second workspace defining a name the first used.
_OLD_GLOBAL_TYPE_NAME_INDEX = "idx_object_types_name_unique"
_TYPE_NAME_UNIQUE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_object_types_ws_name_unique"
    " ON fabric_object_types(workspace_id, LOWER(name))"
)

# Whitelist of filter operators -> SQL operator. User input never reaches the
# SQL string except through this fixed mapping; an unknown operator raises
# rather than being interpolated. Both symbolic and word aliases are accepted
# so callers (agent tool, REST body) can use whichever reads cleaner.
_FILTER_OPS: dict[str, str] = {
    "=": "=",
    "==": "=",
    "eq": "=",
    "!=": "!=",
    "ne": "!=",
    ">": ">",
    "gt": ">",
    ">=": ">=",
    "gte": ">=",
    "<": "<",
    "lt": "<",
    "<=": "<=",
    "lte": "<=",
}

# Operators whose comparison must be numeric. For these we CAST both the stored
# JSON value and the bound parameter to REAL so that "rent > 1000" compares as
# numbers, never as the text affinity SQLite would otherwise pick when one side
# is TEXT. Equality / inequality stay un-CAST so string eq ("status" = "active")
# and numeric eq both behave naturally.
_NUMERIC_OPS: frozenset[str] = frozenset({">", ">=", "<", "<="})


def _is_number(value: Any) -> bool:
    """True for ints/floats but not bools (bool is an int subclass in Python)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _workspace_scope(
    workspace_id: str | None, *, column: str = "workspace_id"
) -> tuple[str | None, list[Any]]:
    """Build the tenancy WHERE fragment + bound params for a scoped read (W4a).

    Returns ``(condition, params)``:

    - ``workspace_id is None`` -> ``(None, [])`` — no scoping. OSS / agent-tool
      callers that don't carry a workspace see everything, exactly as before.
    - a concrete workspace -> ``("(<col> = ? OR <col> IS NULL)", [workspace_id])``
      — the caller's own rows PLUS legacy/global NULL-workspace rows that predate
      tenancy (see the module-header note on the legacy boundary). The value is
      always a bound parameter; ``column`` is a fixed caller-supplied literal
      (``"workspace_id"`` or ``"o.workspace_id"``), never user input.
    """
    if workspace_id is None:
        return None, []
    return f"({column} = ? OR {column} IS NULL)", [workspace_id]


def _build_filter_conditions(filters: dict[str, Any]) -> tuple[list[str], list[Any]]:
    """Translate FabricQuery.filters into SQL WHERE fragments + bound params.

    Two value forms are supported per property key, matching the existing
    ``dict[str, Any]`` shape rather than inventing a new one:

    - scalar      -> equality, e.g. ``{"status": "active"}``
    - operator map -> comparison, e.g. ``{"rent": {">": 1000}}`` or
      ``{"rent": {"gte": 1000}}`` (multiple ops on one key are AND-ed).

    Property names are validated against a conservative identifier charset
    before being placed into the ``$.<name>`` JSON path (SQLite cannot bind a
    JSON path as a parameter). Operator symbols are mapped through the
    ``_FILTER_OPS`` whitelist. Filter VALUES are always emitted as ``?``
    placeholders — never interpolated — so there is no value-side injection
    surface.
    """
    conditions: list[str] = []
    params: list[Any] = []
    for raw_key, raw_val in filters.items():
        key = str(raw_key)
        # Restrict property names to a safe identifier set: alphanumerics plus
        # underscore. Anything else (a quote, a dot, a bracket, a hyphen) is
        # rejected so it can never break out of the JSON path literal.
        #
        # FIX 3 (2026-06-10): tightened from ``c in "_-"`` to ``c == "_"``.
        # Hyphens were never a vulnerability — json_extract treats ``$.a-b`` as
        # a literal key, not an expression — but they are unnecessarily
        # permissive and ``$.a-b`` reads ambiguously in a SQL trace (subtraction
        # vs. a literal key). No object type in the codebase uses hyphenated
        # property names, so the underscore-only identifier rule loses nothing.
        if not key or not all(c.isalnum() or c == "_" for c in key):
            raise ValueError(f"Invalid filter property name: {raw_key!r}")
        path = f"$.{key}"

        # An operator map => one condition per operator; a scalar => equality.
        op_map = raw_val if isinstance(raw_val, dict) else {"=": raw_val}
        for op_token, value in op_map.items():
            sql_op = _FILTER_OPS.get(str(op_token).lower())
            if sql_op is None:
                raise ValueError(f"Unsupported filter operator: {op_token!r}")
            if sql_op in _NUMERIC_OPS and _is_number(value):
                # Numeric comparison: force REAL affinity on both sides.
                conditions.append(f"CAST(json_extract(o.properties, ?) AS REAL) {sql_op} ?")
                params.extend([path, float(value)])
            else:
                conditions.append(f"json_extract(o.properties, ?) {sql_op} ?")
                params.extend([path, value])
    return conditions, params


def _looks_numeric(value: Any) -> bool:
    """True if ``value`` is a real number or a string that parses as one.

    A connector routinely ships a number as a string ("42", "3.14"), so a
    tolerant check keeps write-time enforcement from breaking live ingest while
    still rejecting genuine garbage ("not-a-number"). ``bool`` is excluded — it
    is an ``int`` subclass in Python but is never a valid ``number`` value.
    """
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value.strip())
            return True
        except ValueError:
            return False
    return False


def _property_value_ok(prop: PropertyDef, value: Any) -> bool:
    """Return True if ``value`` satisfies ``prop``'s declared type (lenient).

    Enforcement is deliberately tolerant so it never breaks live connector /
    agent writes (see the module header). Rules per declared ``type``:

    - ``number`` — a real number OR a numeric string (``_looks_numeric``).
    - ``boolean`` — a ``bool``, ``0`` / ``1``, or a common truthy/falsy string.
    - ``date`` — a string or a number (an ISO string or an epoch); a
      ``dict`` / ``list`` / bare ``bool`` is rejected.
    - ``enum`` — membership in ``enum_values`` (compared directly and as a
      string) when that set is declared; no constraint when it is empty.
    - ``string`` / anything unknown — any non-``None`` scalar-or-not is accepted
      (string fields stay lenient; the meaningful checks are the ones above).

    ``None`` is handled by the caller (skipped — absence is not a type clash).
    """
    declared = (prop.type or "string").strip().lower()
    if declared == "number":
        return _looks_numeric(value)
    if declared == "boolean":
        if isinstance(value, bool):
            return True
        if isinstance(value, int) and value in (0, 1):
            return True
        return isinstance(value, str) and value.strip().lower() in {
            "true",
            "false",
            "1",
            "0",
            "yes",
            "no",
        }
    if declared == "date":
        return isinstance(value, str) or (
            isinstance(value, (int, float)) and not isinstance(value, bool)
        )
    if declared == "enum":
        allowed = prop.enum_values or []
        if not allowed:
            return True
        return value in allowed or str(value) in {str(a) for a in allowed}
    # string / unknown declared type -> no strict constraint.
    return True


def validate_object_properties(obj_type: ObjectType | None, properties: dict[str, Any]) -> None:
    """Enforce a property bag against a type's declared schema (ontology-operator-ux).

    Raises :class:`FabricTypeError` on the FIRST declared property whose provided
    value clashes with its ``PropertyDef.type`` / ``enum_values``. Declared-only
    and lenient by design (see the module header and :func:`_property_value_ok`):

    - ``obj_type is None`` or a type with NO declared properties -> no-op (every
      pre-schema type and every ``properties=[]`` type keeps working unchanged).
    - only keys that are BOTH declared AND present with a non-``None`` value are
      checked; ``required`` is NOT enforced at write time, and unknown extra keys
      pass through (the schema is open / additive).
    """
    if obj_type is None or not obj_type.properties:
        return
    declared = {p.name: p for p in obj_type.properties}
    for key, value in properties.items():
        prop = declared.get(key)
        if prop is None or value is None:
            continue
        if not _property_value_ok(prop, value):
            expected = prop.type
            if prop.type == "enum" and prop.enum_values:
                expected = f"enum{list(prop.enum_values)}"
            raise FabricTypeError(
                f"property {key!r} expected type {expected!r} "
                f"but got {value!r} ({type(value).__name__})"
            )


class FabricStore:
    """Async SQLite store for Fabric ontology data."""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)
        self._initialized = False

    async def _ensure_schema(self) -> None:
        if self._initialized:
            return
        async with aiosqlite.connect(self._db_path) as db:
            await db.executescript(SCHEMA_SQL)
            # Additive migration (W4a + SZD-2): tenancy columns on a pre-existing
            # DB. CREATE TABLE IF NOT EXISTS won't add a column to a table that
            # already exists, so ALTER and swallow the duplicate-column error
            # that fires on every subsequent boot — same pattern as the W2b
            # instinct hash-chain / assignee migrations. Pre-existing rows keep
            # NULL workspace_id (legacy/global; see the module header). SZD-2
            # extends the same ALTER to fabric_object_types so the type catalog
            # is per-tenant too.
            for _tbl in ("fabric_objects", "fabric_links", "fabric_object_types"):
                try:
                    await db.execute(f"ALTER TABLE {_tbl} ADD COLUMN workspace_id TEXT")
                except aiosqlite.OperationalError:
                    pass
            # Additive migration (ontology-operator-ux): the schema-version column
            # on a pre-existing fabric_object_types. Same swallow-the-duplicate
            # pattern as the tenancy columns above — a pre-version row defaults to
            # 1, matching the ObjectType model default.
            try:
                await db.execute(
                    "ALTER TABLE fabric_object_types ADD COLUMN version INTEGER DEFAULT 1"
                )
            except aiosqlite.OperationalError:
                pass
            # SZD-2: drop the pre-SZD-2 GLOBAL unique index on LOWER(name) if it
            # exists. It enforced one type name per WHOLE DB; under per-workspace
            # tenancy two tenants must be able to use the same name, so it would
            # otherwise reject the second tenant's define_type. The replacement
            # (workspace_id, LOWER(name)) index is created below. DROP IF EXISTS
            # is a no-op on a fresh / already-migrated DB.
            await db.execute(f"DROP INDEX IF EXISTS {_OLD_GLOBAL_TYPE_NAME_INDEX}")
            # Create the tenancy indexes only after the column is guaranteed to
            # exist (fresh DB via CREATE TABLE, or pre-existing DB via the ALTER
            # above). Doing this inside SCHEMA_SQL would fail on a pre-W4a DB.
            for _idx in _WORKSPACE_INDEX_SQL:
                await db.execute(_idx)
            # SZD-2 backfill: attribute a NULL-workspace type to a tenant ONLY
            # when every object of that type unambiguously shares one workspace.
            # A type whose objects span tenants (or that has no objects, or whose
            # objects are themselves legacy NULL) stays NULL = global, the
            # documented sentinel — it cannot be safely attributed after the
            # fact. Idempotent: a second run finds nothing left to attribute.
            await self._backfill_object_type_workspaces(db)
            # Unique (workspace_id, name) index on fabric_object_types. A
            # pre-existing DB may already hold duplicate (workspace, name) rows
            # (the ensure_type race this index prevents could have fired before
            # this code shipped), so de-dup FIRST, then create the index. Both
            # steps are wrapped so a residual duplicate can never crash
            # _ensure_schema — a metering/ontology nicety must not take the store
            # down on boot.
            await self._dedup_object_types(db)
            try:
                await db.execute(_TYPE_NAME_UNIQUE_INDEX_SQL)
            except aiosqlite.OperationalError:
                # The only realistic cause is a residual duplicate the de-dup
                # pass could not resolve (e.g. an exotic collation). Log and
                # carry on uncreated rather than crashing the boot — the index
                # is a race guard, not a correctness invariant the store needs
                # to function.
                logger.warning(
                    "could not create unique index on "
                    "fabric_object_types(workspace_id, name) — duplicate type "
                    "names may remain; ensure_type race guard is off",
                    exc_info=True,
                )
            await db.commit()
        self._initialized = True

    @staticmethod
    async def _backfill_object_type_workspaces(db: aiosqlite.Connection) -> None:
        """Attribute NULL-workspace object types to a tenant where unambiguous (SZD-2).

        A pre-SZD-2 row has NULL ``workspace_id``. We can sometimes recover the
        owning tenant from its objects: if EVERY non-NULL object of the type
        carries the SAME ``workspace_id`` and there is exactly one such workspace,
        the type clearly belongs to that tenant and we stamp it. Otherwise — the
        type has no objects, or its objects span multiple workspaces, or its
        objects are themselves all legacy NULL — the row stays NULL = global (the
        documented sentinel), because it cannot be safely attributed after the
        fact. Only NULL-workspace type rows are touched, so this never re-homes a
        type a caller already stamped. Idempotent: a second run re-derives the
        same single-workspace attributions and finds nothing new to change.
        Best-effort: any failure is swallowed (logged) so it can never crash
        ``_ensure_schema``.
        """
        try:
            db.row_factory = aiosqlite.Row
            # For each NULL-workspace type, the DISTINCT non-NULL workspaces of
            # its objects. A HAVING COUNT(DISTINCT ...) = 1 keeps only the
            # types whose objects unambiguously live in a single tenant.
            async with db.execute(
                "SELECT o.type_id AS type_id, MIN(o.workspace_id) AS ws "
                "FROM fabric_objects o "
                "JOIN fabric_object_types t ON t.id = o.type_id "
                "WHERE t.workspace_id IS NULL AND o.workspace_id IS NOT NULL "
                "GROUP BY o.type_id "
                "HAVING COUNT(DISTINCT o.workspace_id) = 1"
            ) as cur:
                rows = await cur.fetchall()
            for row in rows:
                await db.execute(
                    "UPDATE fabric_object_types SET workspace_id = ? "
                    "WHERE id = ? AND workspace_id IS NULL",
                    (row["ws"], row["type_id"]),
                )
        except aiosqlite.Error:
            logger.warning(
                "fabric_object_types workspace backfill failed — leaving rows NULL",
                exc_info=True,
            )
        finally:
            db.row_factory = None

    @staticmethod
    async def _dedup_object_types(db: aiosqlite.Connection) -> None:
        """Collapse duplicate (workspace, name) object types before the UNIQUE index.

        Two type rows can share a (workspace_id, case-folded name) only on a DB
        that predates the unique index — the concurrent ``ensure_type`` race.
        Keep the LOWEST rowid per (workspace, case-folded name) (the
        first-defined survivor), re-point any objects bound to a losing type id
        at the survivor's id so no object is orphaned, then delete the loser type
        rows. SZD-2: the de-dup key now includes ``workspace_id`` so two tenants'
        same-named types are NOT collapsed into one — only true within-workspace
        duplicates are (legacy NULL/NULL pairs included, so the survivor of a pair
        of global "Customer" rows is kept and the index can be created). Best
        -effort: a failure here is swallowed (logged) so it can never crash
        ``_ensure_schema``; the index creation that follows is itself try/except
        -guarded as the final backstop.
        """
        try:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT rowid, id, name, workspace_id FROM fabric_object_types ORDER BY rowid ASC"
            ) as cur:
                rows = await cur.fetchall()
            survivor_by_key: dict[tuple[str | None, str], str] = {}
            for row in rows:
                key = (row["workspace_id"], (row["name"] or "").strip().lower())
                survivor_id = survivor_by_key.get(key)
                if survivor_id is None:
                    survivor_by_key[key] = row["id"]
                    continue
                loser_id = row["id"]
                if loser_id == survivor_id:
                    continue
                # Re-home objects from the loser type onto the survivor, then
                # drop the duplicate type row.
                await db.execute(
                    "UPDATE fabric_objects SET type_id = ? WHERE type_id = ?",
                    (survivor_id, loser_id),
                )
                await db.execute(
                    "DELETE FROM fabric_object_types WHERE id = ?",
                    (loser_id,),
                )
        except aiosqlite.Error:
            logger.warning(
                "fabric_object_types de-dup pass failed — leaving rows as-is",
                exc_info=True,
            )
        finally:
            db.row_factory = None

    def _conn(self) -> aiosqlite.Connection:
        """Return a new connection context manager. Use with `async with`."""
        return aiosqlite.connect(self._db_path)

    async def aclose(self) -> None:
        """Release this store's on-disk resources (ISO-1).

        ``FabricStore`` holds NO long-lived connection — every method opens and
        closes its own ``aiosqlite.connect()`` per call — so there is no socket
        or cursor to close. What CAN accumulate is a write-ahead-log sidecar
        (``fabric.db-wal`` / ``-shm``) that grows until a checkpoint folds it
        back into the main file. Under per-workspace physical isolation the
        store factory caches up to 128 of these and evicts the least-recently
        used; ``aclose`` is what the factory runs on eviction so an idle
        tenant's WAL is truncated rather than left to grow unbounded, and the
        next ``_ensure_schema`` re-runs cleanly on the cold handle.

        Best-effort and idempotent: a checkpoint failure (DB never created,
        WAL not in use, file vanished) is swallowed — eviction must never raise.
        """
        self._initialized = False
        try:
            async with aiosqlite.connect(self._db_path) as db:
                await db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:  # noqa: BLE001 — eviction cleanup is best-effort
            logger.debug("FabricStore.aclose checkpoint skipped", exc_info=True)

    # --- Object Types ---

    async def define_type(
        self,
        name: str,
        properties: list[PropertyDef],
        description: str = "",
        icon: str = "box",
        color: str = "#0A84FF",
        workspace_id: str | None = None,
    ) -> ObjectType:
        """Define a new object type, optionally scoped to a tenant (SZD-2).

        ``workspace_id`` stamps the owning workspace on the type row so the
        discovered-type catalog stays private per tenant: a type defined here
        for workspace A is invisible/unusable from workspace B (see
        :meth:`get_type_by_name`). ``None`` writes a legacy/global type (OSS /
        agent-tool / single-tenant callers), visible to every scoped read.
        """
        obj_type = ObjectType(
            name=name,
            description=description,
            icon=icon,
            color=color,
            properties=properties,
            workspace_id=workspace_id,
        )
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO fabric_object_types"
                " (id, name, description, icon, color, properties_schema,"
                " workspace_id, version)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    obj_type.id,
                    obj_type.name,
                    obj_type.description,
                    obj_type.icon,
                    obj_type.color,
                    json.dumps([p.model_dump() for p in properties]),
                    workspace_id,
                    obj_type.version,
                ),
            )
            await db.commit()
        return obj_type

    async def get_type(self, type_id: str, workspace_id: str | None = None) -> ObjectType | None:
        """Fetch one object type by id, optionally scoped to ``workspace_id`` (SZD-2).

        When ``workspace_id`` is supplied, a type owned by another tenant returns
        ``None`` (legacy NULL-workspace types stay visible). ``None`` leaves the
        read unscoped (OSS / agent-tool / single-tenant callers). Note that
        :meth:`create_object` calls this UNSCOPED on purpose — it only needs the
        type's name to denormalize onto the object row, and the object write
        itself carries the tenancy guard.
        """
        ws_cond, ws_params = _workspace_scope(workspace_id)
        sql = "SELECT * FROM fabric_object_types WHERE id = ?"
        params: list[Any] = [type_id]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                return self._row_to_type(row)

    async def get_type_by_name(
        self, name: str, workspace_id: str | None = None
    ) -> ObjectType | None:
        """Resolve an object type by (case-insensitive) name, scoped to a tenant (SZD-2).

        This is the read ``ensure_type`` uses to decide "reuse the existing type
        or define a new one". Scoping it is the core of SZD-2: a type defined in
        workspace A must be invisible AND non-reusable from workspace B, so a
        scoped lookup returns only the caller's own type (or a legacy NULL-
        workspace type). With ``workspace_id`` set, two tenants that both have a
        "Customer" type each resolve to THEIR OWN id; ``None`` is unscoped and
        returns the first match by rowid (OSS / single-tenant callers).
        """
        ws_cond, ws_params = _workspace_scope(workspace_id)
        sql = "SELECT * FROM fabric_object_types WHERE LOWER(name) = LOWER(?)"
        params: list[Any] = [name]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        # When scoped, the caller's OWN type wins over a legacy NULL-workspace
        # type of the same name: order own-rows-first (workspace_id NOT NULL),
        # then by rowid for a stable pick.
        sql += " ORDER BY (workspace_id IS NULL), rowid LIMIT 1"
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                return self._row_to_type(row)

    async def list_types(self, workspace_id: str | None = None) -> list[ObjectType]:
        """List object types, optionally scoped to a tenant (SZD-2).

        SZD-2: type definitions are now first-class TENANT data —
        ``fabric_object_types`` carries its own ``workspace_id``. A scoped call
        returns the caller's OWN types plus legacy NULL-workspace types (the
        same ``_workspace_scope`` visibility every other W4a read uses), scoped
        on the TYPE's column directly. This supersedes the earlier
        join-through-a-visible-object-row approach (fix/fabric-stats-workspace-
        scope): a tenant's empty (object-less) type is now correctly listed for
        its owner and is invisible to other tenants. ``workspace_id=None`` keeps
        the original unscoped behavior (all defined types) for OSS / single-
        tenant callers.
        """
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            ws_cond, params = _workspace_scope(workspace_id)
            if ws_cond is None:
                sql = "SELECT * FROM fabric_object_types ORDER BY name"
            else:
                sql = f"SELECT * FROM fabric_object_types WHERE {ws_cond} ORDER BY name"
            async with db.execute(sql, params) as cur:
                return [self._row_to_type(row) async for row in cur]

    async def remove_type(self, type_id: str) -> None:
        await self._ensure_schema()
        async with self._conn() as db:
            # Cascade: delete links involving objects of this type, then objects, then type
            await db.execute(
                "DELETE FROM fabric_links"
                " WHERE from_object_id IN"
                " (SELECT id FROM fabric_objects WHERE type_id = ?)"
                " OR to_object_id IN"
                " (SELECT id FROM fabric_objects WHERE type_id = ?)",
                (type_id, type_id),
            )
            await db.execute("DELETE FROM fabric_objects WHERE type_id = ?", (type_id,))
            await db.execute("DELETE FROM fabric_object_types WHERE id = ?", (type_id,))
            await db.commit()

    async def update_type(
        self,
        type_id: str,
        *,
        properties: list[PropertyDef] | None = None,
        renames: dict[str, str] | None = None,
        description: str | None = None,
        icon: str | None = None,
        color: str | None = None,
        workspace_id: str | None = None,
    ) -> ObjectType | None:
        """Version + non-destructively migrate an object type (ontology-operator-ux).

        The operator's "edit the schema" path. Bumps ``version`` and migrates the
        type's existing objects for the two SAFE change kinds:

        - **rename** — ``renames`` maps ``old_property_name -> new_property_name``.
          The key is moved on the type's schema AND on every existing object of
          the type (value preserved), so no object is orphaned by the rename.
        - **additive add** — a ``PropertyDef`` present in the new ``properties``
          that the type did not declare before. If it carries a non-``None``
          ``default``, that default is backfilled onto every existing object that
          lacks the key; otherwise the property is simply declared (existing
          objects gain it lazily on their next write).

        **Destructive removal is DEFERRED.** If ``properties`` omits a property the
        type previously declared (and it is not the source of a rename), the
        declaration is dropped but the now-orphaned key is LEFT UNTOUCHED on
        existing objects — no data is scrubbed. This is the documented, intentional
        behaviour for this build (a later task adds an explicit, opt-in purge).

        Returns the updated :class:`ObjectType`, or ``None`` if ``type_id`` does
        not resolve within ``workspace_id`` (a cross-tenant / unknown id). Passing
        neither ``properties`` nor ``renames`` still bumps the version and applies
        any metadata (``description`` / ``icon`` / ``color``) change.
        """
        existing = await self.get_type(type_id, workspace_id=workspace_id)
        if existing is None:
            return None
        renames = {k: v for k, v in (renames or {}).items() if k != v}

        # Resolve the NEW schema. Start from the caller's list (or keep the old
        # one), then fold in renames so a rename is reflected even when the caller
        # passes an unchanged property list.
        new_props = list(properties) if properties is not None else list(existing.properties)
        if renames:
            for prop in new_props:
                if prop.name in renames:
                    prop.name = renames[prop.name]

        old_names = {p.name for p in existing.properties}
        # Additive properties carrying a default -> backfill onto existing objects.
        additive_defaults = {
            p.name: p.default
            for p in new_props
            if p.name not in old_names and p.name not in renames.values() and p.default is not None
        }

        new_version = (existing.version or 1) + 1
        schema_json = json.dumps([p.model_dump() for p in new_props])
        new_description = description if description is not None else existing.description
        new_icon = icon if icon is not None else existing.icon
        new_color = color if color is not None else existing.color

        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            # 1. Migrate existing objects (rename keys + additive default backfill).
            if renames or additive_defaults:
                async with db.execute(
                    "SELECT id, properties FROM fabric_objects WHERE type_id = ?",
                    (type_id,),
                ) as cur:
                    rows = await cur.fetchall()
                for row in rows:
                    props = json.loads(row["properties"]) if row["properties"] else {}
                    changed = False
                    for old_name, new_name in renames.items():
                        if old_name in props:
                            props[new_name] = props.pop(old_name)
                            changed = True
                    for name, default in additive_defaults.items():
                        if name not in props:
                            props[name] = default
                            changed = True
                    if changed:
                        await db.execute(
                            "UPDATE fabric_objects SET properties = ?,"
                            " updated_at = datetime('now') WHERE id = ?",
                            (json.dumps(props), row["id"]),
                        )
            # 2. Persist the new schema + bumped version on the type row.
            await db.execute(
                "UPDATE fabric_object_types SET properties_schema = ?, version = ?,"
                " description = ?, icon = ?, color = ?, updated_at = datetime('now')"
                " WHERE id = ?",
                (schema_json, new_version, new_description, new_icon, new_color, type_id),
            )
            await db.commit()
        return ObjectType(
            id=existing.id,
            name=existing.name,
            description=new_description,
            icon=new_icon,
            color=new_color,
            properties=new_props,
            workspace_id=existing.workspace_id,
            version=new_version,
        )

    # --- Objects ---

    async def create_object(
        self,
        type_id: str,
        properties: dict[str, Any],
        source_connector: str | None = None,
        source_id: str | None = None,
        workspace_id: str | None = None,
    ) -> FabricObject:
        obj_type = await self.get_type(type_id)
        # Write-time type enforcement (ontology-operator-ux): reject a property
        # whose value clashes with its declared PropertyDef. Declared-only and
        # lenient (see validate_object_properties) so pre-schema / empty-schema
        # types and live connector ingest are unaffected.
        validate_object_properties(obj_type, properties)
        obj = FabricObject(
            type_id=type_id,
            type_name=obj_type.name if obj_type else "",
            properties=properties,
            source_connector=source_connector,
            source_id=source_id,
        )
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO fabric_objects"
                " (id, type_id, type_name, properties,"
                " source_connector, source_id, workspace_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    obj.id,
                    obj.type_id,
                    obj.type_name,
                    json.dumps(properties),
                    source_connector,
                    source_id,
                    workspace_id,
                ),
            )
            await db.commit()
        return obj

    async def get_object(self, obj_id: str, workspace_id: str | None = None) -> FabricObject | None:
        """Fetch one object by id, optionally scoped to ``workspace_id`` (W4a).

        When ``workspace_id`` is supplied, a row belonging to another tenant
        returns ``None`` (a 404 to the caller) — the cross-tenant read leak this
        task closes. A legacy NULL-workspace row stays visible.
        """
        ws_cond, ws_params = _workspace_scope(workspace_id)
        sql = "SELECT * FROM fabric_objects WHERE id = ?"
        params: list[Any] = [obj_id]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                return self._row_to_object(row)

    async def get_object_by_source(
        self,
        source_connector: str,
        source_id: str,
        workspace_id: str | None = None,
    ) -> FabricObject | None:
        """Fetch the object that originated from ``(source_connector, source_id)``.

        This is the idempotency lookup for connector ingestion: a connector
        record carries a stable upstream id (a Google Calendar event id, a
        Stripe invoice id), so re-syncing should find the prior object and
        update it rather than create a duplicate. Backed by the existing
        ``idx_objects_source`` index.

        Returns the most recently created match (defensive — provenance pairs
        are expected to be unique, but nothing enforces a DB-level constraint
        yet, so a duplicate from before this path existed resolves to the
        newest row). ``workspace_id`` applies the same W4a tenancy scope as
        :meth:`get_object`; ``None`` leaves the read unscoped.
        """
        ws_cond, ws_params = _workspace_scope(workspace_id)
        sql = "SELECT * FROM fabric_objects WHERE source_connector = ? AND source_id = ?"
        params: list[Any] = [source_connector, source_id]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        sql += " ORDER BY created_at DESC LIMIT 1"
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
                if not row:
                    return None
                return self._row_to_object(row)

    async def update_object(
        self,
        obj_id: str,
        properties: dict[str, Any],
        workspace_id: str | None = None,
    ) -> FabricObject | None:
        """Merge-update one object's properties, optionally scoped to a tenant (W4a).

        ``workspace_id`` gives the WRITE its own tenancy guard rather than relying
        on the caller to have scoped the prior read: the same
        ``workspace_id = ? OR workspace_id IS NULL`` scope as :meth:`get_object`
        and :meth:`get_object_by_source` is applied to BOTH the read-before-merge
        and the UPDATE. A cross-tenant ``obj_id`` returns ``None`` and writes
        nothing. ``None`` leaves the update unscoped (OSS / agent-tool callers),
        exactly as before.
        """
        existing = await self.get_object(obj_id, workspace_id=workspace_id)
        if not existing:
            return None
        # Write-time type enforcement (ontology-operator-ux): validate the PROVIDED
        # delta against the type's declared schema. Only the incoming keys are
        # checked (a merge-update touches only those); the type is loaded unscoped
        # because the object read above already carries the tenancy guard.
        validate_object_properties(await self.get_type(existing.type_id), properties)
        merged = {**existing.properties, **properties}
        ws_cond, ws_params = _workspace_scope(workspace_id)
        sql = "UPDATE fabric_objects SET properties = ?, updated_at = datetime('now') WHERE id = ?"
        params: list[Any] = [json.dumps(merged), obj_id]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(sql, params)
            await db.commit()
        return await self.get_object(obj_id, workspace_id=workspace_id)

    async def remove_object(self, obj_id: str) -> None:
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "DELETE FROM fabric_links WHERE from_object_id = ? OR to_object_id = ?",
                (obj_id, obj_id),
            )
            await db.execute("DELETE FROM fabric_objects WHERE id = ?", (obj_id,))
            await db.commit()

    # --- Links ---

    async def link(
        self,
        from_id: str,
        to_id: str,
        link_type: str,
        properties: dict[str, Any] | None = None,
        workspace_id: str | None = None,
    ) -> FabricLink:
        lnk = FabricLink(
            from_object_id=from_id,
            to_object_id=to_id,
            link_type=link_type,
            properties=properties or {},
        )
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO fabric_links"
                " (id, from_object_id, to_object_id,"
                " link_type, properties, workspace_id)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    lnk.id,
                    lnk.from_object_id,
                    lnk.to_object_id,
                    lnk.link_type,
                    json.dumps(lnk.properties),
                    workspace_id,
                ),
            )
            await db.commit()
        return lnk

    async def list_links(
        self,
        from_id: str | None = None,
        to_id: str | None = None,
        link_type: str | None = None,
        limit: int = 100,
        offset: int = 0,
        workspace_id: str | None = None,
    ) -> tuple[list[FabricLink], int]:
        """List links with optional filters on endpoints and link_type.

        Returns ``(links, total)`` where ``total`` is the unpaginated count.
        All filter arguments are bound parameters — no query-string
        concatenation, so SQL injection through link_type is not possible.

        ``workspace_id`` (W4a) restricts both the count and the page to the
        caller's tenant (plus legacy NULL-workspace links); ``None`` leaves the
        listing unscoped for OSS callers.
        """
        conditions: list[str] = []
        params: list[Any] = []
        if from_id:
            conditions.append("from_object_id = ?")
            params.append(from_id)
        if to_id:
            conditions.append("to_object_id = ?")
            params.append(to_id)
        if link_type:
            conditions.append("link_type = ?")
            params.append(link_type)
        ws_cond, ws_params = _workspace_scope(workspace_id)
        if ws_cond:
            conditions.append(ws_cond)
            params.extend(ws_params)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT COUNT(*) AS cnt FROM fabric_links {where}", params
            ) as cur:
                row = await cur.fetchone()
                total = row["cnt"] if row else 0

            async with db.execute(
                f"SELECT * FROM fabric_links {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ) as cur:
                links = [self._row_to_link(row) async for row in cur]

        return links, total

    async def get_link(self, link_id: str, workspace_id: str | None = None) -> FabricLink | None:
        """Fetch one link by id, optionally scoped to ``workspace_id`` (W4a).

        The scoped read is the tenancy guard for deletes: :meth:`unlink` is
        deliberately unscoped (single-tenant OSS callers), so a multi-tenant
        caller (the EE router / MCP tools) resolves the link THROUGH this scoped
        read first — a cross-tenant ``link_id`` returns ``None`` (legacy
        NULL-workspace links stay visible, matching :meth:`list_links`).
        """
        ws_cond, ws_params = _workspace_scope(workspace_id)
        sql = "SELECT * FROM fabric_links WHERE id = ?"
        params: list[Any] = [link_id]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)

        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
        return self._row_to_link(row) if row else None

    async def unlink(self, link_id: str) -> None:
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute("DELETE FROM fabric_links WHERE id = ?", (link_id,))
            await db.commit()

    async def get_linked_objects(
        self, obj_id: str, link_type: str | None = None, workspace_id: str | None = None
    ) -> list[FabricObject]:
        """Traverse links from ``obj_id`` to the objects on the other end.

        ``workspace_id`` (W4a) scopes the RETURNED objects to the caller's tenant
        (plus legacy NULL-workspace objects) so a traversal can't surface another
        workspace's objects even if a link somehow spanned the boundary.
        """
        # Scope on the returned object's workspace (alias ``o``) — that is the
        # row the caller reads back. Layered as an extra AND on the existing
        # join filter; the link-traversal logic itself is unchanged.
        ws_cond, ws_params = _workspace_scope(workspace_id, column="o.workspace_id")
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            if link_type:
                query = (
                    "SELECT o.* FROM fabric_objects o JOIN fabric_links l "
                    "ON (o.id = l.to_object_id AND l.from_object_id = ?) "
                    "OR (o.id = l.from_object_id AND l.to_object_id = ?) "
                    "WHERE l.link_type = ?"
                )
                params: list[Any] = [obj_id, obj_id, link_type]
            else:
                query = (
                    "SELECT o.* FROM fabric_objects o JOIN fabric_links l "
                    "ON (o.id = l.to_object_id AND l.from_object_id = ?) "
                    "OR (o.id = l.from_object_id AND l.to_object_id = ?)"
                )
                params = [obj_id, obj_id]
            if ws_cond:
                # Append to the WHERE: a link_type query already has WHERE; the
                # no-link_type branch has none yet, so add one.
                query += (" AND " if link_type else " WHERE ") + ws_cond
                params.extend(ws_params)
            async with db.execute(query, params) as cur:
                return [self._row_to_object(row) async for row in cur]

    # --- Query ---

    async def query(self, q: FabricQuery, workspace_id: str | None = None) -> FabricQueryResult:
        """Run a FabricQuery, optionally scoped to a tenant (W4a).

        ``workspace_id`` is a separate method argument rather than a
        ``FabricQuery`` field: tenancy is a server-side authorization concern
        threaded from the request's workspace context, never something a client
        sets on the query body. When supplied, results are restricted to that
        workspace (plus legacy NULL-workspace rows). When ``None``, the query is
        unscoped, exactly as before W4a (OSS / agent-tool callers).

        Multi-hop / path traversal (feat/fabric-multihop): when ``q.path`` is
        non-empty the query walks an ontology join server-side instead of doing
        the single ``linked_to`` hop. This is the 2-hop join the code audit
        flagged ("open Deals whose Customer competes_with a Competitor") that
        previously returned [] from one query and had to be hand-stitched in app
        code. The traversal contract:

        - START frontier: the seed object set the path walks from. If
          ``linked_to`` is set, the seed is exactly that one object id. Otherwise
          the seed is every object matching the top-level ``type_name`` /
          ``type_id`` / ``filters`` (e.g. the open Deals), so a path can read
          "from these objects, walk out…".
        - Each :class:`PathHop` advances the frontier one edge across
          ``fabric_links`` in the hop's ``direction`` (out / in / any), keeping
          only objects that match the hop's ``object_type`` and ``filters``.
        - The RESULT is the objects at the terminal hop, constrained by that
          hop's type/filters. Top-level type/filters constrain the START, not
          the terminal (the terminal is described by the final hop).
        - Tenant scope (W4a) is applied at EVERY hop AND on the seed, so a linked
          object in another workspace can never be reached or returned.

        Implementation note: an ITERATIVE per-hop frontier resolution (one
        parameterized query per hop, threading the id set forward) was chosen
        over a recursive CTE. Per-hop type + property + direction + tenant
        filters are far simpler to express and keep injection-safe as a normal
        parameterized WHERE per hop than as a single recursive CTE, and the path
        depth here is small (2-3 hops). All link_type / object_type / filter
        values remain bound ``?`` parameters; only fixed SQL fragments are
        concatenated.
        """
        await self._ensure_schema()
        if q.path:
            return await self._query_path(q, workspace_id)

        conditions: list[str] = []
        params: list[Any] = []

        if q.type_id:
            conditions.append("o.type_id = ?")
            params.append(q.type_id)
        elif q.type_name:
            conditions.append("LOWER(o.type_name) = LOWER(?)")
            params.append(q.type_name)

        if q.linked_to:
            if q.link_type:
                link_cond = (
                    "o.id IN ("
                    "SELECT to_object_id FROM fabric_links"
                    " WHERE from_object_id = ? AND link_type = ? "
                    "UNION "
                    "SELECT from_object_id FROM fabric_links"
                    " WHERE to_object_id = ? AND link_type = ?"
                    ")"
                )
                conditions.append(link_cond)
                params.extend([q.linked_to, q.link_type, q.linked_to, q.link_type])
            else:
                link_cond = (
                    "o.id IN ("
                    "SELECT to_object_id FROM fabric_links WHERE from_object_id = ? "
                    "UNION "
                    "SELECT from_object_id FROM fabric_links WHERE to_object_id = ?"
                    ")"
                )
                conditions.append(link_cond)
                params.extend([q.linked_to, q.linked_to])

        # Property filters against the JSON properties bag. Kept as a localized
        # block (see _build_filter_conditions) so concurrent work on this method
        # — e.g. workspace_id scoping — merges without touching this logic.
        if q.filters:
            filter_conditions, filter_params = _build_filter_conditions(q.filters)
            conditions.extend(filter_conditions)
            params.extend(filter_params)

        # Tenancy scope (W4a) — an ADDITIONAL condition ANDed alongside the W0d
        # property filters above, never a replacement for them. Restricts the
        # result set to the caller's workspace plus legacy NULL-workspace rows.
        ws_cond, ws_params = _workspace_scope(workspace_id, column="o.workspace_id")
        if ws_cond:
            conditions.append(ws_cond)
            params.extend(ws_params)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            # Count
            async with db.execute(
                f"SELECT COUNT(*) as cnt FROM fabric_objects o {where}", params
            ) as cur:
                row = await cur.fetchone()
                total = row["cnt"] if row else 0

            # Fetch
            async with db.execute(
                f"SELECT o.* FROM fabric_objects o {where}"
                " ORDER BY o.created_at DESC LIMIT ? OFFSET ?",
                [*params, q.limit, q.offset],
            ) as cur:
                objects = [self._row_to_object(row) async for row in cur]

        return FabricQueryResult(objects=objects, total=total)

    async def _query_path(self, q: FabricQuery, workspace_id: str | None) -> FabricQueryResult:
        """Walk ``q.path`` server-side and return the terminal-hop objects.

        See :meth:`query` for the full traversal contract. Iterative per-hop
        frontier resolution: resolve the START frontier (the seed object ids),
        then advance it one :class:`PathHop` at a time; the terminal frontier is
        re-fetched as full objects (newest-first, paginated). Every step is
        tenant-scoped (W4a) and every value is a bound parameter.
        """
        # Walk properties: the traversal is ITERATIVE (one DB round-trip per
        # hop, frontier threaded forward), FIXED-DEPTH (len(q.path) hops, capped
        # at MAX_HOPS by the FabricQuery validator), and does NOT track visited
        # objects across hops — there is no cycle re-visit suppression, so a
        # cyclic graph relies on MAX_HOPS + MAX_FRONTIER to stay bounded rather
        # than on de-duplication. Each hop's frontier is a set, so duplicates
        # WITHIN a single hop's output collapse; revisiting an object on a LATER
        # hop is allowed (and meaningful — A may legitimately be both 2 and 4
        # hops from the seed).
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row

            # --- START frontier ---------------------------------------------
            # linked_to => exactly that one seed object (still tenant-scoped, so
            # a cross-tenant seed id resolves to nothing). Otherwise the seed is
            # every object matching the top-level type/filters.
            if q.linked_to:
                seed_cond = ["o.id = ?"]
                seed_params: list[Any] = [q.linked_to]
            else:
                seed_cond = []
                seed_params = []
                if q.type_id:
                    seed_cond.append("o.type_id = ?")
                    seed_params.append(q.type_id)
                elif q.type_name:
                    seed_cond.append("LOWER(o.type_name) = LOWER(?)")
                    seed_params.append(q.type_name)
                if q.filters:
                    fconds, fparams = _build_filter_conditions(q.filters)
                    seed_cond.extend(fconds)
                    seed_params.extend(fparams)
            ws_cond, ws_params = _workspace_scope(workspace_id, column="o.workspace_id")
            if ws_cond:
                seed_cond.append(ws_cond)
                seed_params.extend(ws_params)
            seed_where = f"WHERE {' AND '.join(seed_cond)}" if seed_cond else ""
            async with db.execute(
                f"SELECT o.id FROM fabric_objects o {seed_where}", seed_params
            ) as cur:
                frontier = {row["id"] async for row in cur}

            # --- Advance one hop at a time ----------------------------------
            for hop in q.path:
                if not frontier:
                    break  # dead end — no path can revive an empty frontier
                frontier = await self._advance_hop(db, frontier, hop, workspace_id)

            if not frontier:
                return FabricQueryResult(objects=[], total=0)

            # Guard the terminal frontier too: a wide fan-out on the LAST hop is
            # only checked by _advance_hop on the NEXT hop's entry, which never
            # comes. Keep the terminal IN-list under SQLite's variable limit.
            if len(frontier) > MAX_FRONTIER:
                raise ValueError(
                    f"multi-hop result reached {len(frontier)} objects, "
                    f"exceeding the cap of {MAX_FRONTIER}. Narrow the path with a "
                    "more selective start filter or a terminal object_type."
                )

            # --- Re-fetch the terminal frontier as full objects -------------
            # Bound the IN-list with placeholders (ids are server-generated, but
            # parameterize anyway — never interpolate). The frontier was already
            # tenant-scoped at every hop, so the terminal ids cannot belong to
            # another workspace; the scope clause below is defense-in-depth that
            # mirrors the single-hop query()'s terminal SELECT exactly. Newest-
            # first + paginate to match the single-hop ordering contract.
            terminal_ids = list(frontier)
            placeholders = ",".join("?" for _ in terminal_ids)
            total = len(terminal_ids)
            term_params: list[Any] = [*terminal_ids]
            ws_cond, ws_params = _workspace_scope(workspace_id, column="o.workspace_id")
            ws_clause = f" AND {ws_cond}" if ws_cond else ""
            if ws_cond:
                term_params.extend(ws_params)
            async with db.execute(
                f"SELECT o.* FROM fabric_objects o WHERE o.id IN ({placeholders})"
                f"{ws_clause}"
                " ORDER BY o.created_at DESC LIMIT ? OFFSET ?",
                [*term_params, q.limit, q.offset],
            ) as cur:
                objects = [self._row_to_object(row) async for row in cur]

        return FabricQueryResult(objects=objects, total=total)

    async def _advance_hop(
        self,
        db: aiosqlite.Connection,
        frontier: set[str],
        hop: PathHop,
        workspace_id: str | None,
    ) -> set[str]:
        """Return the next frontier: objects reached from ``frontier`` via ``hop``.

        One parameterized query. The direction decides which link endpoint is
        the "near" side (matched against the current frontier) and which is the
        "far" side (the object reached). ``"any"`` matches the link in either
        orientation (the legacy single-hop symmetric semantics). Per-hop
        ``object_type`` / ``filters`` / W4a tenant scope are applied to the FAR
        object — the one that becomes the new frontier and is eventually
        returned.

        Raises ``ValueError`` if the incoming frontier exceeds ``MAX_FRONTIER`` —
        the IN-list would otherwise risk SQLite's bound-variable limit. The
        caller (the agent tool) renders this as a clean error string.
        """
        if len(frontier) > MAX_FRONTIER:
            raise ValueError(
                f"multi-hop frontier reached {len(frontier)} objects, "
                f"exceeding the cap of {MAX_FRONTIER}. Narrow the path with a "
                "more selective start filter or an earlier object_type."
            )
        near_ids = list(frontier)
        near_ph = ",".join("?" for _ in near_ids)

        # near/far endpoint columns by direction. The current frontier is the
        # NEAR end; the object we step to is the FAR end.
        #   out: frontier is from_object_id -> step to to_object_id
        #   in : frontier is to_object_id   -> step to from_object_id
        #   any: union of both orientations
        if hop.direction == "out":
            orientations = [("from_object_id", "to_object_id")]
        elif hop.direction == "in":
            orientations = [("to_object_id", "from_object_id")]
        else:  # "any"
            orientations = [("from_object_id", "to_object_id"), ("to_object_id", "from_object_id")]

        # Far-object filters (type + property + tenant) are shared across the
        # orientation union, so build them once.
        far_conds: list[str] = []
        far_params: list[Any] = []
        if hop.object_type:
            far_conds.append("LOWER(o.type_name) = LOWER(?)")
            far_params.append(hop.object_type)
        if hop.filters:
            fconds, fparams = _build_filter_conditions(hop.filters)
            far_conds.extend(fconds)
            far_params.extend(fparams)
        ws_cond, ws_params = _workspace_scope(workspace_id, column="o.workspace_id")
        if ws_cond:
            far_conds.append(ws_cond)
            far_params.extend(ws_params)
        far_where = (" AND " + " AND ".join(far_conds)) if far_conds else ""

        next_frontier: set[str] = set()
        for near_col, far_col in orientations:
            # Join the link's FAR endpoint to fabric_objects so the far-object
            # type/property/tenant filters apply. link_type is a bound param.
            sql = (
                f"SELECT o.id FROM fabric_links l"
                f" JOIN fabric_objects o ON o.id = l.{far_col}"
                f" WHERE l.{near_col} IN ({near_ph})"
                f" AND l.link_type = ?"
                f"{far_where}"
            )
            params = [*near_ids, hop.link_type, *far_params]
            async with db.execute(sql, params) as cur:
                async for row in cur:
                    next_frontier.add(row["id"])
        return next_frontier

    # --- Stats ---

    async def stats(self, workspace_id: str | None = None) -> dict[str, int]:
        """Ontology counts, optionally scoped to a tenant (W4a follow-up).

        A scoped call mirrors ``query()``'s visibility EXACTLY (own rows plus
        legacy NULL-workspace rows, via ``_workspace_scope``) so stats and
        query always agree: ``stats(workspace_id=w)["objects"]`` equals the
        ``total`` of an unfiltered scoped query. ``links`` applies the same
        scope to ``fabric_links``. SZD-2: ``types`` now counts types scoped on
        the TYPE's OWN ``workspace_id`` (own rows + legacy NULL) so it matches
        ``list_types(workspace_id=w)`` exactly — including the caller's empty,
        object-less types. ``workspace_id=None`` keeps the original unscoped,
        instance-wide behavior for OSS / single-tenant callers.
        """
        await self._ensure_schema()
        async with self._conn() as db:
            if workspace_id is None:
                types = await db.execute_fetchall("SELECT COUNT(*) FROM fabric_object_types")
                objects = await db.execute_fetchall("SELECT COUNT(*) FROM fabric_objects")
                links = await db.execute_fetchall("SELECT COUNT(*) FROM fabric_links")
                return {
                    "types": types[0][0] if types else 0,
                    "objects": objects[0][0] if objects else 0,
                    "links": links[0][0] if links else 0,
                }
            obj_cond, obj_params = _workspace_scope(workspace_id)
            link_cond, link_params = _workspace_scope(workspace_id)
            type_cond, type_params = _workspace_scope(workspace_id)
            types = await db.execute_fetchall(
                f"SELECT COUNT(*) FROM fabric_object_types WHERE {type_cond}",
                type_params,
            )
            objects = await db.execute_fetchall(
                f"SELECT COUNT(*) FROM fabric_objects WHERE {obj_cond}", obj_params
            )
            links = await db.execute_fetchall(
                f"SELECT COUNT(*) FROM fabric_links WHERE {link_cond}", link_params
            )
            return {
                "types": types[0][0] if types else 0,
                "objects": objects[0][0] if objects else 0,
                "links": links[0][0] if links else 0,
            }

    # --- Helpers ---

    def _row_to_type(self, row: Any) -> ObjectType:
        props_raw = json.loads(row["properties_schema"]) if row["properties_schema"] else []
        # SZD-2: surface the type's owning workspace. ``keys()`` guard keeps the
        # helper resilient to any legacy SELECT projection that omitted the
        # column (defensive — current callers all SELECT *).
        keys = row.keys() if hasattr(row, "keys") else ()
        workspace_id = row["workspace_id"] if "workspace_id" in keys else None
        # version may be absent on a projection that predates the column; a NULL
        # (pre-migration row) reads back as the model default of 1.
        version = row["version"] if "version" in keys and row["version"] is not None else 1
        return ObjectType(
            id=row["id"],
            name=row["name"],
            description=row["description"] or "",
            icon=row["icon"] or "box",
            color=row["color"] or "#0A84FF",
            properties=[PropertyDef(**p) for p in props_raw],
            workspace_id=workspace_id,
            version=version,
        )

    def _row_to_object(self, row: Any) -> FabricObject:
        return FabricObject(
            id=row["id"],
            type_id=row["type_id"],
            type_name=row["type_name"] or "",
            properties=json.loads(row["properties"]) if row["properties"] else {},
            source_connector=row["source_connector"],
            source_id=row["source_id"],
        )

    def _row_to_link(self, row: Any) -> FabricLink:
        return FabricLink(
            id=row["id"],
            from_object_id=row["from_object_id"],
            to_object_id=row["to_object_id"],
            link_type=row["link_type"],
            properties=json.loads(row["properties"]) if row["properties"] else {},
        )
