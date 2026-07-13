# Fabric store — async SQLite operations for the ontology layer.
# Created: 2026-03-28 — CRUD for object types, objects, and links.
# Updated: 2026-07-11 (FST-7 — freshness) — the merge-site statement pass now
#   threads an aware-UTC ``now`` into resolve() (within-family staleness
#   demotion live in shadow AND enforce); the divergence line gained a
#   trailing `` freshness=<fresh|aging|stale|none>`` token (additive — field
#   order unchanged, the FST-8 grep contract holds); NEW opt-in read surface
#   ``get_object_provenance(object_id)`` — per TRACKED property: disputed /
#   unresolvable / freshness / statement count / winner writer+source summary
#   (a sibling method, the default read path pays nothing).
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
# Updated: 2026-07-11 (self-serve-analysis S1) — transparent-analysis read engine
#   on ``query``: when a FabricQuery carries ``group_by``/``aggregate`` (and the
#   POCKETPAW_FABRIC_ANALYST flag is ON — otherwise FabricAnalystDisabledError,
#   fail-loud), the query runs as a SQL GROUP BY over the ALREADY workspace-scoped
#   + filtered set (scope-then-aggregate: the same WHERE the plain path builds,
#   extracted into ``_flat_query_conditions``, is applied BEFORE grouping so a
#   cross-workspace object can never enter a group total). Supports
#   count/sum/avg/min/max (numeric folds CAST to REAL like the numeric filters),
#   optional RangeBucket bucketing of a numeric group key (a fully-parameterized
#   CASE chain), and value/key sort. The result carries ``aggregates``
#   ({key, value} rows, paginated by limit/offset) plus human-readable ``steps``
#   (QueryPlanStep — the ReasoningTrace contract). Plain queries are untouched
#   (same SQL, no steps); aggregation composes with filters/linked_to but not
#   ``path`` (rejected at the model).
# Updated: 2026-07-10 (FST-1 — Fabric source-truth schema) — two NEW tables,
#   ``fabric_statements`` + ``fabric_sources``, with append-only CRUD:
#   ``append_statement()`` / ``get_statements()`` / ``upsert_source()``. A
#   statement is one observed (object, property, value) claim with provenance
#   (writer_class, a SourceRef FK, bitemporal observed/recorded/valid_from/
#   valid_to, a curation rank); a source row is deduplicated on its identity
#   tuple (kind + connector/run_id/document_uri/actor_id/session_id +
#   workspace_id) both by an upsert-time lookup and a DB-level expression
#   UNIQUE index (race guard, NULLs normalized via IFNULL so absent fields
#   dedup too). Both tables carry the W4a ``workspace_id`` (schema-freeze
#   ruling): ``append_statement`` stamps it, ``get_statements`` applies the
#   standard ``_workspace_scope`` read scope (own rows + legacy NULL), and on
#   sources it is PART of the dedup identity — the same source identity in two
#   workspaces is two rows (tenancy isolation beats dedup). The tables ride
#   SCHEMA_SQL's CREATE TABLE IF NOT EXISTS, so the migration is idempotent on
#   an existing fabric.db (brand-new tables; the W4a ALTER loop additionally
#   covers them so a DB from an early FST-1 build without workspace_id is
#   healed, and that build's identity index is dropped for the
#   workspace-aware ``idx_sources_identity_ws``). Statements are APPEND-ONLY:
#   no update/delete verbs (rank changes come later as curation writes). NO
#   existing read or write path is touched — the flat
#   ``fabric_objects.properties`` dict remains the primary read path; nothing
#   writes statements in production until ``fabric_source_truth_mode``
#   (default "off") gains shadow/enforce semantics in later slices.
# Updated: 2026-07-10 (FST-3 — SHADOW mode at merge site 1) — update_object()
#   now records statements when ``fabric_source_truth_mode`` is shadow/enforce
#   (enforce == shadow until FST-5). The mode is read ONCE per call; 'off'
#   (default) is byte-for-byte the prior behavior — zero new queries/writes.
#   The LWW cache write is UNCHANGED in every mode. New OPTIONAL keyword-only
#   provenance kwargs on update_object (writer_class, source_kind,
#   source_connector, source_run_id, source_document_uri, source_actor_id,
#   source_session_id, observed_at — all default None, every existing caller
#   keeps working); connectors/fabric_ingest.py threads its connector context
#   through them. The shadow pass (_shadow_record_statements) implements: the
#   opt-in discipline (untracked single-source properties write NO
#   statements), auto-promotion (an untracked property hit by a SECOND
#   distinct source with a materially different value seeds a statement from
#   the current cache value with object-level provenance + touch-time
#   observed_at backfill), provenance derivation for unattributed writes
#   (object's source_connector → writer_class "connector", else "agent"),
#   FST-2 resolution over the property's statements, and ONE grep-stable
#   divergence log line per statement-producing property ("fabric shadow:
#   object=... property=... lww=... resolver=... diverged=... disputed=...
#   unresolvable=..." — the FST-8 harness contract). Shadow runs AFTER the
#   cache commit and is exception-shielded: a shadow failure logs a warning,
#   never breaks the primary write. Supporting fix: _row_to_object now parses
#   created_at/updated_at from the row (previously dropped — the model
#   defaulted them to read-time now()), so the promotion backfill uses the
#   TRUE last-touch time.
# Updated: 2026-07-10 (FST-4 — SHADOW mode at merge sites 2 + 3) — two changes
#   that let the remaining write paths ride the SAME shadow machinery instead
#   of duplicating it:
#   1. shadow_record_event_update(): a public, journal-event-keyed entry into
#      the FST-3 shadow pass for the projection replay path (merge site 2 —
#      fabric/projection.py stages observations, this method records them).
#      Replay idempotence rides the NEW ``fabric_shadow_events`` table: the
#      event id is claimed with INSERT OR IGNORE BEFORE the statement pass, so
#      replaying the same journal N times (or two replayers racing) records a
#      given event's statements AT MOST ONCE. Mode 'off' returns early — not
#      even the marker row is written.
#   2. _writer_family(): the second-distinct-source rule now compares writer
#      FAMILIES, collapsing "connector" and "mirror" into one machine-sync
#      family. Site 3 (the EE Firestore mirror) writes as writer_class
#      "mirror" on objects whose baseline derives to "connector"; without the
#      family rule every mirror self-refresh would look like a second source
#      and promote every changed property, gutting the opt-in discipline. No
#      behavior change for pre-FST-4 cohorts: "mirror" never reached this
#      comparison before this slice.
# Updated: 2026-07-10 (FST-5 — ENFORCE mode: the resolver owns the cache) —
#   three changes; 'off' and 'shadow' are byte-for-byte the FST-3/4 behavior:
#   1. update_object() in ENFORCE runs the statement pass BEFORE the cache
#      commit and, for every TRACKED property (one that has statements after
#      the pass — pre-tracked or just promoted), writes the RESOLVER'S WINNER
#      into the flat properties dict instead of the blind LWW value.
#      Untracked properties keep LWW (no statements → nothing to resolve).
#      Write-once: the final dict is computed first, then committed in ONE
#      UPDATE — never LWW-then-overwrite. The divergence line still logs with
#      the same grep-stable shape; in enforce ``lww=`` is what LWW WOULD have
#      kept and ``resolver=`` is what the cache now holds. A statement-pass
#      failure in enforce falls back to plain LWW for that write (log + keep
#      writing — the cache write must never break), mirroring FST-3's shield.
#      _shadow_record_statements now RETURNS {property: Resolution} for the
#      statement-producing properties so enforce reuses the pass's own
#      resolutions instead of resolving twice; shadow ignores the return.
#   2. change_property() / correct_property(): the curation verbs (the seams
#      FST-6's PIN/IGNORE executor calls). CHANGE closes the current winner
#      statement's valid_to and appends the new value as rank="preferred"
#      (open validity); CORRECT marks the current winner deprecated with
#      rank_reason and appends the corrected value as rank="normal". Both
#      auto-promote an untracked property first (seed the current cache value
#      with FST-3's baseline provenance + touch-time observed_at) so history
#      is preserved, and both return the NEW Resolution. Cache behavior is
#      mode-respecting: enforce writes the new resolver winner into the
#      cache; shadow/off leave the cache alone (the verbs are statement-layer
#      operations in every mode). These verbs are the ONLY two writes that
#      touch existing statement rows — narrow curation UPDATEs on
#      valid_to / rank+rank_reason only (the append-only doctrine's
#      documented "later curation writes"); value and provenance columns are
#      never rewritten.
#   3. The FST-3 provenance derivation is factored into _derive_provenance()
#      (byte-identical rules) so the verbs and the shadow pass share ONE
#      definition instead of two drifting copies.
#   Site-2 note (settled in fabric/projection.py): the PROJECTION stays
#   event-faithful in enforce — it folds what the journal says; enforce
#   ownership applies at THIS store's cache layer (the primary read path).
#   Site-3 note: the EE mirror's update path goes through update_object, so
#   enforce flows through automatically (proven in
#   tests/cloud/fabric_ingest/test_fabric_ingest_enforce.py).
# Updated: 2026-07-10 (FST-6 — the conflict lifecycle: PIN / IGNORE steward
#   verbs) — four changes; 'off' and 'shadow' cache behavior is untouched:
#   1. pin_statement() / unpin_statement() / ignore_statement(): the steward
#      verbs (siblings of change/correct — the operations FST-6's Instinct
#      stewardship executor calls). PIN sets ``pinned=True`` on ONE existing,
#      non-deprecated statement (the resolver's pinned short-circuit then
#      makes it win outright — the durable "this one wins"); UNPIN retracts
#      the flag; IGNORE deprecates the statement with rank_reason=<reason>
#      (struck from resolution entirely — the steward's "this claim is
#      bogus"). All three return the NEW Resolution and are mode-respecting
#      on the cache exactly like CHANGE/CORRECT (enforce writes the new
#      resolver winner; shadow/off leave the cache alone). PIN does NOT
#      auto-unpin other pins: two pins on one property is a curation conflict
#      the resolver deliberately flags as disputed. No auto-promotion here —
#      the verbs target an EXISTING statement id, so an untracked property
#      (no statements) has nothing to pin/ignore.
#   2. The FST-5 "ONLY two writes" doctrine widens to THREE narrow curation
#      UPDATEs on statement rows: valid_to (CHANGE), rank+rank_reason
#      (CORRECT / IGNORE), and now pinned (PIN/UNPIN via
#      _set_statement_pinned). Value and provenance columns are still never
#      rewritten.
#   3. list_statement_keys() + get_source(): two small read helpers.
#      list_statement_keys returns the DISTINCT (object_id, property) pairs
#      that HAVE statements (W4a-scoped) — the cheap scan surface
#      fabric/conflicts.py recomputes open conflicts from (only objects WITH
#      statements are ever visited; the statements ARE the conflict state, no
#      conflicts table). get_source reads one SourceRef by id so the EE
#      stewardship proposal can show a human WHERE each competing value came
#      from.
#   4. The enforce cache write is factored into _write_winner_to_cache()
#      (byte-identical behavior) so _curate_property and the steward verbs
#      share ONE definition of "the resolver owns the cache".


from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

from pocketpaw.fabric.models import (
    FabricAnalystDisabledError,
    FabricLink,
    FabricObject,
    FabricQuery,
    FabricQueryResult,
    FabricTypeError,
    ObjectType,
    PathHop,
    PropertyDef,
    QueryPlanStep,
    SourceRef,
    Statement,
)

# FST-3: the shadow pass reuses the resolver's material-difference rule
# (strings compared stripped, everything else plain ==) rather than
# duplicating it here — one definition of "materially different" for the
# whole source-truth chain. The name is module-private in resolver.py but
# intra-package reuse is deliberate.
from pocketpaw.fabric.resolver import Resolution, _materially_different, resolve
from pocketpaw.fabric.trust import default_trust_rules

logger = logging.getLogger(__name__)


def _source_truth_mode() -> str:
    """The fabric_source_truth_mode setting: 'off' | 'shadow' | 'enforce'.

    Read lazily (import inside the function) so importing the store never
    pulls the full config module, and so tests can monkeypatch either this
    helper or ``pocketpaw.config.get_settings``. Callers read it ONCE per
    operation — 'off' must stay byte-for-byte free of new queries/writes.
    """
    from pocketpaw.config import get_settings

    return get_settings().fabric_source_truth_mode


def _writer_family(writer_class: str) -> str:
    """Collapse writer classes into families for the second-distinct-source
    comparison (FST-4).

    "connector" and "mirror" are ONE machine-sync family: the EE Firestore
    mirror (writer_class "mirror") refreshing an object whose baseline
    derives to "connector" for the SAME connector is the object's owning
    sync, not a second writer. Without this, every mirror self-refresh would
    auto-promote every materially changed property — the opt-in discipline
    ("single-source objects stay scalar/cheap") would be dead for mirrored
    data. Every other class ("human", "agent", "inferred") is its own
    family, so human-vs-connector and agent-vs-connector still count as
    second sources exactly as FST-3 defined. NOTE: this only affects the
    promotion GATE — the statement itself still records the true
    writer_class ("mirror"), and the trust ladder still ranks connector >
    mirror at resolve time.
    """
    return "sync" if writer_class in ("connector", "mirror") else writer_class


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

-- Source-truth provenance (FST-1). Two NEW tables. On a pre-FST fabric.db
-- this CREATE TABLE IF NOT EXISTS creates them whole; a DB created by an
-- early FST-1 build (before the schema-freeze review added workspace_id) is
-- healed by the same ALTER loop the W4a columns use (see _ensure_schema).
-- Nothing in the existing read/write path touches them; they are inert until
-- fabric_source_truth_mode gains shadow/enforce semantics in later slices.
CREATE TABLE IF NOT EXISTS fabric_sources (
    id TEXT PRIMARY KEY,
    -- 'connector_run' | 'document' | 'human_actor' | 'agent_session'
    kind TEXT NOT NULL,
    -- Identity fields (union across kinds; NULL = absent, still part of the
    -- dedup identity — see _SOURCES_IDENTITY_UNIQUE_INDEX_SQL).
    connector TEXT,
    run_id TEXT,
    document_uri TEXT,
    actor_id TEXT,
    session_id TEXT,
    retrieved_at TEXT,
    -- Tenancy (W4a semantics): the owning workspace. NULL = OSS /
    -- single-tenant caller. PART OF THE DEDUP IDENTITY — the same source
    -- identity in two workspaces is two rows (tenancy isolation beats dedup).
    -- On an early-FST-1 DB this column is added by the ALTER migration in
    -- _ensure_schema, NOT here.
    workspace_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fabric_statements (
    id TEXT PRIMARY KEY,
    object_id TEXT NOT NULL REFERENCES fabric_objects(id),
    property TEXT NOT NULL,
    -- JSON-encoded value (any JSON type, incl. null).
    value TEXT NOT NULL DEFAULT 'null',
    source_ref_id TEXT NOT NULL REFERENCES fabric_sources(id),
    -- 'human' | 'connector' | 'mirror' | 'agent' | 'inferred'
    writer_class TEXT NOT NULL,
    -- Bitemporal fields, ISO-8601 TEXT (same affinity as every other
    -- timestamp column in this schema).
    observed_at TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    -- Curation: 'preferred' | 'normal' | 'deprecated'; pinned = human-fixed.
    rank TEXT NOT NULL DEFAULT 'normal',
    rank_reason TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    -- Tenancy (W4a): same workspace semantics as fabric_objects. Stamped on
    -- append; scoped reads see own rows + legacy NULL. Same ALTER-migration
    -- note as fabric_sources.workspace_id above.
    workspace_id TEXT
);

-- Journal-replay dedupe for the shadow pass (FST-4, merge site 2). One row per
-- journal event whose update has been shadow-recorded. The event id (the
-- journal EventEntry's UUID — stable across replays) is claimed with INSERT OR
-- IGNORE BEFORE the statement pass runs, so replaying the same journal twice
-- records a given event's statements at most once. No workspace column: event
-- ids are globally unique, and the statements themselves carry workspace_id.
CREATE TABLE IF NOT EXISTS fabric_shadow_events (
    event_id TEXT PRIMARY KEY,
    object_id TEXT,
    recorded_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_objects_type ON fabric_objects(type_id);
CREATE INDEX IF NOT EXISTS idx_objects_source ON fabric_objects(source_connector, source_id);
CREATE INDEX IF NOT EXISTS idx_links_from ON fabric_links(from_object_id);
CREATE INDEX IF NOT EXISTS idx_links_to ON fabric_links(to_object_id);
CREATE INDEX IF NOT EXISTS idx_links_type ON fabric_links(link_type);
CREATE INDEX IF NOT EXISTS idx_statements_object ON fabric_statements(object_id);
CREATE INDEX IF NOT EXISTS idx_statements_object_property ON fabric_statements(object_id, property);
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
    # FST-1: same W4a pairing for the source-truth tables — the plain
    # workspace index rides ALONGSIDE the (object_id, property) read index in
    # SCHEMA_SQL, exactly like idx_objects_workspace pairs with
    # idx_objects_type/idx_objects_source.
    "CREATE INDEX IF NOT EXISTS idx_statements_workspace ON fabric_statements(workspace_id)",
    "CREATE INDEX IF NOT EXISTS idx_sources_workspace ON fabric_sources(workspace_id)",
)

# Race guard for upsert_source: one row per source identity PER WORKSPACE.
# Created AFTER the ALTER migration (same "no such column" hazard as
# _WORKSPACE_INDEX_SQL: an early-FST-1 DB has the tables but not the
# workspace_id column until the ALTER runs). SQLite treats NULLs as DISTINCT
# in a UNIQUE index, so every nullable identity field — INCLUDING
# workspace_id — is normalized through IFNULL(..., '') : two rows that both
# omit run_id ARE the same identity, and two OSS (NULL-workspace) upserts of
# the same source dedup to one row, while the same identity in two different
# workspaces stays two rows (tenancy isolation beats dedup). upsert_source
# does a lookup-first anyway; this index only closes the concurrent-insert
# race the same way the type-name index does. The early-FST-1 index of the
# same shape MINUS workspace_id is dropped first (it would collapse two
# tenants' rows into one) — mirror of _OLD_GLOBAL_TYPE_NAME_INDEX.
_OLD_SOURCES_IDENTITY_INDEX = "idx_sources_identity"
_SOURCES_IDENTITY_UNIQUE_INDEX_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_identity_ws ON fabric_sources("
    " kind,"
    " IFNULL(connector, ''),"
    " IFNULL(run_id, ''),"
    " IFNULL(document_uri, ''),"
    " IFNULL(actor_id, ''),"
    " IFNULL(session_id, ''),"
    " IFNULL(workspace_id, '')"
    ")"
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


def _flat_query_conditions(q: FabricQuery, workspace_id: str | None) -> tuple[list[str], list[Any]]:
    """Build the flat (non-path) WHERE fragments + params for a FabricQuery.

    Extracted from ``FabricStore.query`` (self-serve-analysis S1) so the plain
    fetch path and the aggregation path share ONE condition builder — the
    scope-then-aggregate invariant holds by construction, not by parallel
    maintenance. Covers type constraint, single-hop ``linked_to``/``link_type``,
    property filters (``_build_filter_conditions``), and the W4a tenancy scope
    (``_workspace_scope``). Every value is a bound ``?`` parameter.
    """
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

    return conditions, params


# Aggregate function whitelist (self-serve-analysis S1): FabricQuery.aggregate
# -> the SQL function name. Mirrors _FILTER_OPS — user input selects FROM this
# fixed map and never reaches the SQL text itself.
_AGG_FN_SQL: dict[str, str] = {
    "sum": "SUM",
    "avg": "AVG",
    "min": "MIN",
    "max": "MAX",
}

# Sort whitelist for the aggregate output rows. ``val``/``grp`` are the fixed
# SELECT aliases; the default (sort=None) is value descending — "biggest group
# first", the order an analyst reads.
_AGG_SORT_SQL: dict[str, str] = {
    "value_desc": "val DESC",
    "value_asc": "val ASC",
    "key_asc": "grp ASC",
    "key_desc": "grp DESC",
}


def _group_key_expr(q: FabricQuery) -> tuple[str, list[Any]]:
    """SQL expression + bound params for the GROUP BY key (S1).

    Plain grouping extracts the raw JSON property value. With ``q.ranges`` the
    numeric value (CAST to REAL, same affinity rule as numeric filters) is
    bucketed by a CASE chain — bounds AND labels are bound ``?`` params, and a
    value matching no bucket (or a missing property, which CASTs to NULL) falls
    to ELSE NULL and is dropped by the caller's HAVING.
    """
    path = f"$.{q.group_by}"
    if not q.ranges:
        return "json_extract(o.properties, ?)", [path]

    cast = "CAST(json_extract(o.properties, ?) AS REAL)"
    whens: list[str] = []
    params: list[Any] = []
    for bucket in q.ranges:
        conds: list[str] = []
        # A NULL property must never match an open-ended bucket by accident:
        # NULL comparisons yield NULL (not-matched) in SQLite, so the bound
        # checks alone are safe — no explicit IS NOT NULL needed.
        if bucket.min is not None:
            conds.append(f"{cast} >= ?")
            params.extend([path, bucket.min])
        if bucket.max is not None:
            conds.append(f"{cast} < ?")
            params.extend([path, bucket.max])
        whens.append(f"WHEN {' AND '.join(conds)} THEN ?")
        params.append(bucket.resolved_label())
    return f"CASE {' '.join(whens)} ELSE NULL END", params


def _aggregate_expr(q: FabricQuery) -> tuple[str, list[Any]]:
    """SQL expression + bound params for the aggregate value (S1).

    ``count`` counts the rows in each group; the numeric folds read
    ``aggregate_field`` CAST to REAL so "sum of price" adds numbers even when a
    connector stored them as JSON strings (the same affinity rule the numeric
    filter operators use). The function name comes from ``_AGG_FN_SQL``.
    """
    if q.aggregate == "count":
        return "COUNT(*)", []
    fn = _AGG_FN_SQL[q.aggregate or ""]
    return f"{fn}(CAST(json_extract(o.properties, ?) AS REAL))", [f"$.{q.aggregate_field}"]


def _describe_filters(filters: dict[str, Any]) -> str:
    """Human-readable one-line summary of a FabricQuery.filters bag (S1 steps)."""
    parts: list[str] = []
    for key, raw_val in filters.items():
        if isinstance(raw_val, dict):
            for op, value in raw_val.items():
                parts.append(f"{key} {op} {value}")
        else:
            parts.append(f"{key} = {raw_val}")
    return " and ".join(parts)


def _build_plan_steps(q: FabricQuery, *, total: int, group_count: int) -> list[QueryPlanStep]:
    """The human-readable reasoning trace for an aggregation run (S1).

    Emits the ``{title, detail?, status?}`` QueryPlanStep contract ripple's
    ReasoningTrace consumes: what was filtered/scanned (with the matched-object
    count), how it was grouped (property or range buckets), and what was
    computed. Every step is ``status="done"`` — the read engine reports a
    finished run; "thinking" is for streaming surfaces.
    """
    type_label = q.type_name or q.type_id or "objects"
    steps: list[QueryPlanStep] = []
    if q.filters:
        steps.append(
            QueryPlanStep(
                title=f"Filtered {type_label} where {_describe_filters(q.filters)}",
                detail=f"{total} matching objects",
                status="done",
            )
        )
    else:
        steps.append(
            QueryPlanStep(
                title=f"Scanned {type_label}",
                detail=f"{total} objects",
                status="done",
            )
        )
    groups_label = f"{group_count} group{'s' if group_count != 1 else ''}"
    grouped_detail = (
        f"{len(q.ranges)} value range{'s' if len(q.ranges) != 1 else ''}"
        if q.ranges
        else groups_label
    )
    steps.append(
        QueryPlanStep(title=f"Grouped by {q.group_by}", detail=grouped_detail, status="done")
    )
    computed = f"Computed {q.aggregate}"
    if q.aggregate_field:
        computed += f" of {q.aggregate_field}"
    steps.append(
        QueryPlanStep(
            title=computed,
            detail=f"{groups_label} from {total} objects",
            status="done",
        )
    )
    return steps


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
            # Additive migration (W4a + SZD-2 + FST-1): tenancy columns on a
            # pre-existing DB. CREATE TABLE IF NOT EXISTS won't add a column to
            # a table that already exists, so ALTER and swallow the
            # duplicate-column error that fires on every subsequent boot — same
            # pattern as the W2b instinct hash-chain / assignee migrations.
            # Pre-existing rows keep NULL workspace_id (legacy/global; see the
            # module header). SZD-2 extends the same ALTER to
            # fabric_object_types; FST-1 extends it to fabric_statements /
            # fabric_sources (a no-op on any DB whose tables were created by
            # this SCHEMA_SQL — it only heals a DB from an early FST-1 build
            # that predates the schema-freeze workspace_id ruling).
            for _tbl in (
                "fabric_objects",
                "fabric_links",
                "fabric_object_types",
                "fabric_statements",
                "fabric_sources",
            ):
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
            # FST-1: the per-workspace source-identity UNIQUE index. Drop the
            # early-FST-1 identity index (no workspace in its key — it would
            # collapse two tenants' identical source identities into one row)
            # before creating the workspace-aware replacement; both steps are
            # no-ops on a fresh / already-migrated DB. Created here, after the
            # ALTER, for the same "no such column" reason as
            # _WORKSPACE_INDEX_SQL.
            await db.execute(f"DROP INDEX IF EXISTS {_OLD_SOURCES_IDENTITY_INDEX}")
            await db.execute(_SOURCES_IDENTITY_UNIQUE_INDEX_SQL)
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
        *,
        writer_class: str | None = None,
        source_kind: str | None = None,
        source_connector: str | None = None,
        source_run_id: str | None = None,
        source_document_uri: str | None = None,
        source_actor_id: str | None = None,
        source_session_id: str | None = None,
        observed_at: datetime | None = None,
    ) -> FabricObject | None:
        """Merge-update one object's properties, optionally scoped to a tenant (W4a).

        ``workspace_id`` gives the WRITE its own tenancy guard rather than relying
        on the caller to have scoped the prior read: the same
        ``workspace_id = ? OR workspace_id IS NULL`` scope as :meth:`get_object`
        and :meth:`get_object_by_source` is applied to BOTH the read-before-merge
        and the UPDATE. A cross-tenant ``obj_id`` returns ``None`` and writes
        nothing. ``None`` leaves the update unscoped (OSS / agent-tool callers),
        exactly as before.

        FST-3 (source-truth shadow) — the new keyword-only kwargs are OPTIONAL
        provenance for the write (all default ``None``; every pre-FST-3 caller
        keeps working unchanged). They only matter when
        ``fabric_source_truth_mode`` is ``shadow`` or ``enforce``: the write is
        then ALSO recorded as statements per
        :meth:`_shadow_record_statements` (auto-promotion, provenance
        derivation, divergence log — see its docstring for the exact rules).
        With mode ``off`` (the default) not a single new query or write
        happens — the mode is read once per call and the whole statement block
        is skipped.

        Cache semantics per mode (FST-5):

        - ``off`` / ``shadow`` — UNCHANGED: last-write-wins merge into the
          flat properties dict, which remains the primary read path. In
          shadow the statement pass runs AFTER the cache commit and only
          observes.
        - ``enforce`` — the RESOLVER OWNS THE CACHE for tracked properties.
          The statement pass runs FIRST; every property that has statements
          after the pass (already tracked, or promoted by it) lands in the
          cache as the resolver's winner instead of the blind LWW value.
          Untracked properties keep LWW (no statements → nothing to
          resolve). Write-once: the final dict is computed, then committed in
          ONE UPDATE. A statement-pass failure falls back to plain LWW for
          this write (warning logged) — the cache write must never break.
        """
        mode = _source_truth_mode()  # read ONCE per call; "off" skips everything
        existing = await self.get_object(obj_id, workspace_id=workspace_id)
        if not existing:
            return None
        # Write-time type enforcement (ontology-operator-ux): validate the PROVIDED
        # delta against the type's declared schema. Only the incoming keys are
        # checked (a merge-update touches only those); the type is loaded unscoped
        # because the object read above already carries the tenancy guard.
        validate_object_properties(await self.get_type(existing.type_id), properties)
        merged = {**existing.properties, **properties}
        if mode == "enforce":
            # FST-5: statement pass BEFORE the commit so the resolver's winner
            # for each tracked property can be folded into the ONE cache
            # write. Failure-shielded like shadow: a broken pass degrades this
            # write to plain LWW rather than blocking it.
            try:
                resolutions = await self._shadow_record_statements(
                    existing,
                    properties,
                    writer_class=writer_class,
                    source_kind=source_kind,
                    source_connector=source_connector,
                    source_run_id=source_run_id,
                    source_document_uri=source_document_uri,
                    source_actor_id=source_actor_id,
                    source_session_id=source_session_id,
                    observed_at=observed_at,
                    workspace_id=workspace_id,
                )
            except Exception:
                logger.warning(
                    "fabric enforce: statement pass failed for object=%s"
                    " — falling back to LWW for this write",
                    obj_id,
                    exc_info=True,
                )
                resolutions = {}
            for prop, resolution in resolutions.items():
                if resolution.winner_statement is not None:
                    merged[prop] = resolution.value
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
        if mode == "shadow":
            # Shadow runs AFTER the cache write so the primary path's
            # semantics and error behavior stay byte-identical (FST-3); a
            # shadow failure must never break the write.
            try:
                await self._shadow_record_statements(
                    existing,
                    properties,
                    writer_class=writer_class,
                    source_kind=source_kind,
                    source_connector=source_connector,
                    source_run_id=source_run_id,
                    source_document_uri=source_document_uri,
                    source_actor_id=source_actor_id,
                    source_session_id=source_session_id,
                    observed_at=observed_at,
                    workspace_id=workspace_id,
                )
            except Exception:
                logger.warning(
                    "fabric shadow: statement pass failed for object=%s — cache write unaffected",
                    obj_id,
                    exc_info=True,
                )
        return await self.get_object(obj_id, workspace_id=workspace_id)

    @staticmethod
    def _derive_provenance(
        existing: FabricObject,
        *,
        writer_class: str | None,
        source_kind: str | None,
        source_connector: str | None,
        source_actor_id: str | None,
        source_session_id: str | None,
        source_document_uri: str | None,
    ) -> tuple[str, str | None, str]:
        """Effective ``(kind, connector, writer_class)`` of one write (FST-3).

        The single definition of the provenance derivation rules, shared by
        the statement pass and the FST-5 curation verbs (byte-identical to
        the inline FST-3 logic this was factored from):

        - kind, in order: explicit ``source_kind`` > ``source_connector`` →
          ``connector_run`` > ``source_actor_id`` → ``human_actor`` >
          ``source_session_id`` → ``agent_session`` > ``source_document_uri``
          → ``document`` > the object's own ``source_connector`` →
          ``connector_run`` for that connector > ``agent_session`` with no
          identity (the honest default for unattributed writes).
        - writer_class: explicit > ``connector`` for ``connector_run`` >
          ``human`` for ``human_actor`` > ``agent`` for everything else.
        """
        kind = source_kind
        connector = source_connector
        if kind is None:
            if connector is not None:
                kind = "connector_run"
            elif source_actor_id is not None:
                kind = "human_actor"
            elif source_session_id is not None:
                kind = "agent_session"
            elif source_document_uri is not None:
                kind = "document"
            elif existing.source_connector:
                kind = "connector_run"
                connector = existing.source_connector
            else:
                kind = "agent_session"
        eff_writer = writer_class
        if eff_writer is None:
            if kind == "connector_run":
                eff_writer = "connector"
            elif kind == "human_actor":
                eff_writer = "human"
            else:
                eff_writer = "agent"
        return kind, connector, eff_writer

    async def _shadow_record_statements(
        self,
        existing: FabricObject,
        incoming: dict[str, Any],
        *,
        writer_class: str | None,
        source_kind: str | None,
        source_connector: str | None,
        source_run_id: str | None,
        source_document_uri: str | None,
        source_actor_id: str | None,
        source_session_id: str | None,
        observed_at: datetime | None,
        workspace_id: str | None,
    ) -> dict[str, Resolution]:
        """The FST-3 statement pass: record an update's claims as statements.

        Called from :meth:`update_object` (merge site 1) only when
        ``fabric_source_truth_mode`` is shadow/enforce — AFTER the LWW cache
        write in shadow, BEFORE the cache commit in enforce (FST-5, so the
        caller can fold each Resolution into the one cache write).
        ``existing`` is the PRE-update snapshot (its properties and
        ``updated_at`` are the old cache state).

        Returns ``{property: Resolution}`` for every statement-producing
        (tracked) property — the enforce path consumes it; shadow ignores it.

        Provenance derivation (when the caller passed no explicit kwargs):

        - source kind, in order: explicit ``source_kind`` > ``source_connector``
          → ``connector_run`` > ``source_actor_id`` → ``human_actor`` >
          ``source_session_id`` → ``agent_session`` > ``source_document_uri``
          → ``document`` > the object's own ``source_connector`` →
          ``connector_run`` for that connector (the historical main caller of
          update_object is the connector re-sync refreshing its own object) >
          ``agent_session`` with no identity (the honest default for
          unattributed legacy writes).
        - writer_class: explicit > ``connector`` when the effective kind is
          ``connector_run`` > ``human`` for ``human_actor`` > ``agent`` for
          everything else.

        Second-distinct-source rule (drives auto-promotion): the object-level
        baseline is (``existing.source_connector``, its derived writer class —
        ``connector`` when a connector is stamped, else ``agent``). The
        incoming write is a SECOND source when its effective connector differs
        from the baseline connector OR its effective writer FAMILY differs
        from the baseline writer family (FST-4: "connector" and "mirror" are
        one machine-sync family — see :func:`_writer_family` — so the EE
        mirror refreshing its own object is not a second source, while a
        human or agent write still is). An unattributed write on a connector-owned
        object derives to that same connector, so it is NOT a second source —
        without provenance threading a second writer cannot be detected, which
        is exactly why ingest/API callers pass the kwargs.

        Per incoming property:

        1. already tracked (has statements) → append the incoming statement.
        2. untracked → PROMOTE only when (second distinct source) AND (the
           property exists in the current cache — a brand-new key has no prior
           claim to preserve) AND (the values materially differ): seed a
           statement from the current cache value with the object-level
           baseline provenance and touch-time backfill
           (``observed_at = existing.updated_at or created_at`` — the best
           available approximation of when the cache value was written), then
           append the incoming statement. Otherwise the property stays
           scalar/cheap: NO statements, NO log line (the opt-in discipline).
        3. resolve the property's statements via the FST-2 trust ladder and
           log ONE structured divergence line (the FST-8 harness contract —
           grep-stable, single line, values JSON-encoded so they can never
           wrap):

           ``fabric shadow: object=<id> property=<p> lww=<lww-value>
           resolver=<winner-value> diverged=<bool> disputed=<bool>
           unresolvable=<bool>``

           The line's shape is mode-independent. In shadow ``lww`` is what
           the cache holds and ``resolver`` is what it WOULD hold; in
           enforce (FST-5) ``lww`` is what LWW would have kept and
           ``resolver`` is what the cache now holds — ``diverged=True``
           means the resolver overrode the incoming write.

        The cache is NEVER touched here — the CALLER owns the cache write
        (LWW in off/shadow; the returned resolutions in enforce).
        """
        # --- Effective provenance of the incoming write (FST-3 rules,
        # shared with the FST-5 curation verbs via _derive_provenance) ---
        kind, connector, eff_writer = self._derive_provenance(
            existing,
            writer_class=writer_class,
            source_kind=source_kind,
            source_connector=source_connector,
            source_actor_id=source_actor_id,
            source_session_id=source_session_id,
            source_document_uri=source_document_uri,
        )

        # --- Object-level baseline (who owns the current cache value) ---
        baseline_connector = existing.source_connector
        baseline_writer = "connector" if baseline_connector else "agent"

        # --- Second-distinct-source rule (FST-4: compare writer FAMILIES,
        # not raw classes, so a "mirror" write from the object's own
        # connector is the owning sync refreshing itself, not a second
        # source — see _writer_family) ---
        incoming_connector = connector if kind == "connector_run" else None
        is_second_source = incoming_connector != baseline_connector or _writer_family(
            eff_writer
        ) != _writer_family(baseline_writer)

        # Sources are upserted lazily so a fully scalar update (nothing tracked,
        # nothing promoted) writes NOTHING — not even a SourceRef row.
        incoming_source: SourceRef | None = None
        seed_source: SourceRef | None = None
        resolutions: dict[str, Resolution] = {}

        # FST-7 — the store's clock convention: ONE aware-UTC read per pass
        # (datetime.now(UTC)), threaded into resolve() so shadow AND enforce
        # apply within-family staleness demotion live. Statement stamps are
        # UTC-normalized at the comparison boundary (naive = UTC — see
        # trust._as_utc); stored data is never rewritten.
        now = datetime.now(UTC)

        for prop, value in incoming.items():
            stmts = await self.get_statements(existing.id, prop, workspace_id=workspace_id)
            if not stmts:
                # Untracked property — promotion gate.
                if not is_second_source:
                    continue
                if prop not in existing.properties:
                    continue  # brand-new key: no prior claim to preserve
                old_value = existing.properties[prop]
                if not _materially_different(old_value, value):
                    continue
                if seed_source is None:
                    seed_source = await self.upsert_source(
                        "connector_run" if baseline_connector else "agent_session",
                        connector=baseline_connector,
                        workspace_id=workspace_id,
                    )
                seed = await self.append_statement(
                    existing.id,
                    prop,
                    old_value,
                    seed_source.id,
                    baseline_writer,
                    observed_at=existing.updated_at or existing.created_at,
                    workspace_id=workspace_id,
                )
                stmts = [seed]
            if incoming_source is None:
                incoming_source = await self.upsert_source(
                    kind,
                    connector=connector,
                    run_id=source_run_id,
                    document_uri=source_document_uri,
                    actor_id=source_actor_id,
                    session_id=source_session_id,
                    workspace_id=workspace_id,
                )
            stmts.append(
                await self.append_statement(
                    existing.id,
                    prop,
                    value,
                    incoming_source.id,
                    eff_writer,
                    observed_at=observed_at,
                    workspace_id=workspace_id,
                )
            )
            resolution = resolve(
                stmts,
                default_trust_rules(),
                object_type=existing.type_name or None,
                now=now,
            )
            # ``lww`` is the incoming value — what the cache holds in shadow
            # and what LWW WOULD have kept in enforce (where the caller
            # writes ``resolver`` into the cache instead). Same line either
            # way: the FST-8 harness contract.
            logger.info(
                "fabric shadow: object=%s property=%s lww=%s resolver=%s"
                " diverged=%s disputed=%s unresolvable=%s freshness=%s",
                existing.id,
                prop,
                json.dumps(value, default=str),
                json.dumps(resolution.value, default=str),
                _materially_different(value, resolution.value),
                resolution.is_disputed,
                resolution.unresolvable,
                resolution.winner_freshness or "none",
            )
            resolutions[prop] = resolution
        return resolutions

    async def shadow_record_event_update(
        self,
        existing: FabricObject,
        incoming: dict[str, Any],
        *,
        event_id: str,
        writer_class: str | None = None,
        source_kind: str | None = None,
        source_connector: str | None = None,
        source_run_id: str | None = None,
        source_document_uri: str | None = None,
        source_actor_id: str | None = None,
        source_session_id: str | None = None,
        observed_at: datetime | None = None,
        workspace_id: str | None = None,
    ) -> bool:
        """Journal-event-keyed entry into the FST-3 shadow pass (merge site 2).

        The projection replay path (fabric/projection.py::_apply_updated)
        merges in memory — it never goes through :meth:`update_object` — so it
        stages observations and records them through THIS method, reusing
        :meth:`_shadow_record_statements` verbatim (same promotion gate,
        provenance derivation, divergence line) instead of duplicating it.

        THE REPLAY-DEDUPE RULE: one shadow pass per journal event id. The
        ``event_id`` (the journal EventEntry's UUID — stable across replays)
        is claimed in ``fabric_shadow_events`` with INSERT OR IGNORE *before*
        the statement pass; if the row already existed the event was recorded
        by a previous replay (or a concurrent replayer won the race) and this
        call returns ``False`` without writing anything. Claiming FIRST makes
        the pass at-most-once: a failure after the claim drops that event's
        shadow pass (consistent with FST-3's failure-shield, which also drops
        a failed pass) rather than risking double-appended statements on
        retry — "replaying the same journal twice never double-appends" is
        the contract this method exists to keep.

        Mode ``off`` returns ``False`` immediately — not even the marker row
        is written (the off-mode byte-for-byte guarantee). Exceptions
        propagate to the caller: the projection's flush shields per
        observation, mirroring where FST-3 put the shield for site 1.

        Returns ``True`` when the event's statements were recorded by this
        call.
        """
        mode = _source_truth_mode()  # read ONCE per call; "off" writes NOTHING
        if mode == "off":
            return False
        await self._ensure_schema()
        async with self._conn() as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO fabric_shadow_events (event_id, object_id) VALUES (?, ?)",
                (event_id, existing.id),
            )
            await db.commit()
            if cur.rowcount == 0:
                return False  # already recorded — replay/idempotency dedupe
        await self._shadow_record_statements(
            existing,
            incoming,
            writer_class=writer_class,
            source_kind=source_kind,
            source_connector=source_connector,
            source_run_id=source_run_id,
            source_document_uri=source_document_uri,
            source_actor_id=source_actor_id,
            source_session_id=source_session_id,
            observed_at=observed_at,
            workspace_id=workspace_id,
        )
        return True

    # --- Curation verbs (FST-5 — CHANGE / CORRECT) ---

    async def change_property(
        self,
        object_id: str,
        property: str,
        new_value: Any,
        *,
        writer_class: str | None = None,
        source_kind: str | None = None,
        source_connector: str | None = None,
        source_run_id: str | None = None,
        source_document_uri: str | None = None,
        source_actor_id: str | None = None,
        source_session_id: str | None = None,
        observed_at: datetime | None = None,
        workspace_id: str | None = None,
    ) -> Resolution:
        """CHANGE one property's value as an explicit curation act (FST-5).

        Semantics: close the CURRENT WINNER statement's validity
        (``valid_to = now`` — it becomes superseded history, still auditable)
        and append ``new_value`` as a ``rank="preferred"``, open-validity
        statement carrying the caller's provenance (same optional kwargs and
        derivation rules as :meth:`update_object` — callers SHOULD pass
        provenance; unattributed calls inherit the object's baseline).

        The property must be TRACKED. On an untracked property the verb
        first PROMOTES it (seeds the current cache value with the object's
        baseline provenance + touch-time ``observed_at``, exactly FST-3's
        promotion seed) so the pre-change history is preserved, THEN applies.
        A property absent from both statements and the cache has no prior
        claim — the new statement is simply appended.

        Returns the NEW :class:`Resolution` over the property's statements.
        The new preferred statement wins within its writer tier; a
        higher-tier statement or a pin still outranks it — the resolver owns
        the outcome, by design. Cache behavior is mode-respecting: in
        ``enforce`` the flat properties dict is updated to the new winner; in
        ``shadow``/``off`` the cache is untouched (the verb is a
        statement-layer operation in every mode).

        This is one of the seams FST-6's PIN/IGNORE executor calls.
        """
        return await self._curate_property(
            object_id,
            property,
            new_value,
            verb="change",
            reason=None,
            writer_class=writer_class,
            source_kind=source_kind,
            source_connector=source_connector,
            source_run_id=source_run_id,
            source_document_uri=source_document_uri,
            source_actor_id=source_actor_id,
            source_session_id=source_session_id,
            observed_at=observed_at,
            workspace_id=workspace_id,
        )

    async def correct_property(
        self,
        object_id: str,
        property: str,
        new_value: Any,
        *,
        reason: str,
        writer_class: str | None = None,
        source_kind: str | None = None,
        source_connector: str | None = None,
        source_run_id: str | None = None,
        source_document_uri: str | None = None,
        source_actor_id: str | None = None,
        source_session_id: str | None = None,
        observed_at: datetime | None = None,
        workspace_id: str | None = None,
    ) -> Resolution:
        """CORRECT one property's value: the current winner was WRONG (FST-5).

        Semantics: mark the CURRENT WINNER statement ``rank="deprecated"``
        with ``rank_reason=reason`` (a deprecated statement never wins, never
        loses, never disputes — it is struck from resolution entirely, unlike
        CHANGE's closed-but-candidate history) and append ``new_value`` as a
        ``rank="normal"``, open-validity statement with the caller's
        provenance.

        Tracking, promotion, provenance derivation, the returned NEW
        :class:`Resolution`, and the mode-respecting cache behavior (enforce
        writes the new winner; shadow/off leave the cache alone) all match
        :meth:`change_property` — see its docstring.

        This is one of the seams FST-6's PIN/IGNORE executor calls.
        """
        return await self._curate_property(
            object_id,
            property,
            new_value,
            verb="correct",
            reason=reason,
            writer_class=writer_class,
            source_kind=source_kind,
            source_connector=source_connector,
            source_run_id=source_run_id,
            source_document_uri=source_document_uri,
            source_actor_id=source_actor_id,
            source_session_id=source_session_id,
            observed_at=observed_at,
            workspace_id=workspace_id,
        )

    async def _curate_property(
        self,
        object_id: str,
        property: str,
        new_value: Any,
        *,
        verb: str,
        reason: str | None,
        writer_class: str | None,
        source_kind: str | None,
        source_connector: str | None,
        source_run_id: str | None,
        source_document_uri: str | None,
        source_actor_id: str | None,
        source_session_id: str | None,
        observed_at: datetime | None,
        workspace_id: str | None,
    ) -> Resolution:
        """Shared core of :meth:`change_property` / :meth:`correct_property`.

        ``verb`` is ``"change"`` (close the winner's validity, append
        preferred) or ``"correct"`` (deprecate the winner with ``reason``,
        append normal). Raises ``ValueError`` when the object doesn't exist
        (or is outside the caller's workspace scope) — the FST-6 executor
        needs a clean failure, not a silent no-op.
        """
        mode = _source_truth_mode()  # read ONCE; decides only the cache write
        existing = await self.get_object(object_id, workspace_id=workspace_id)
        if existing is None:
            raise ValueError(f"fabric object not found: {object_id!r}")

        stmts = await self.get_statements(object_id, property, workspace_id=workspace_id)
        if not stmts and property in existing.properties:
            # Auto-promotion (FST-3 seeding, unconditional here — the verb is
            # explicit curation, so preserving the pre-verb claim IS the
            # point): seed the current cache value with the object-level
            # baseline provenance and touch-time observed_at.
            baseline_connector = existing.source_connector
            baseline_writer = "connector" if baseline_connector else "agent"
            seed_source = await self.upsert_source(
                "connector_run" if baseline_connector else "agent_session",
                connector=baseline_connector,
                workspace_id=workspace_id,
            )
            seed = await self.append_statement(
                object_id,
                property,
                existing.properties[property],
                seed_source.id,
                baseline_writer,
                observed_at=existing.updated_at or existing.created_at,
                workspace_id=workspace_id,
            )
            stmts = [seed]

        current = resolve(stmts, default_trust_rules(), object_type=existing.type_name or None)
        winner = current.winner_statement
        if winner is not None:
            if verb == "change":
                # Close the winner's validity — only if still open; a closed
                # winner is already superseded history and its interval must
                # not be rewritten.
                if winner.valid_to is None:
                    await self._close_statement_validity(winner.id, datetime.now())
            else:
                await self._deprecate_statement(winner.id, reason)

        kind, connector, eff_writer = self._derive_provenance(
            existing,
            writer_class=writer_class,
            source_kind=source_kind,
            source_connector=source_connector,
            source_actor_id=source_actor_id,
            source_session_id=source_session_id,
            source_document_uri=source_document_uri,
        )
        new_source = await self.upsert_source(
            kind,
            connector=connector,
            run_id=source_run_id,
            document_uri=source_document_uri,
            actor_id=source_actor_id,
            session_id=source_session_id,
            workspace_id=workspace_id,
        )
        await self.append_statement(
            object_id,
            property,
            new_value,
            new_source.id,
            eff_writer,
            observed_at=observed_at,
            rank="preferred" if verb == "change" else "normal",
            workspace_id=workspace_id,
        )

        refreshed = await self.get_statements(object_id, property, workspace_id=workspace_id)
        resolution = resolve(
            refreshed, default_trust_rules(), object_type=existing.type_name or None
        )

        if mode == "enforce":
            await self._write_winner_to_cache(
                object_id, property, resolution, workspace_id=workspace_id, existing=existing
            )

        return resolution

    async def _write_winner_to_cache(
        self,
        object_id: str,
        property: str,
        resolution: Resolution,
        *,
        workspace_id: str | None,
        existing: FabricObject,
    ) -> None:
        """Write one property's resolver winner into the flat cache (enforce).

        The resolver owns the cache in enforce (FST-5): one targeted write of
        the property's new winner, merged onto the FRESH cache state so
        concurrent-property updates aren't clobbered. A Resolution with no
        winner (e.g. every statement deprecated) writes NOTHING — the cache
        keeps its last value rather than losing the key. Shared by the
        curation verbs (CHANGE/CORRECT) and the steward verbs
        (PIN/UNPIN/IGNORE); callers gate on mode — this helper never reads it.
        """
        if resolution.winner_statement is None:
            return
        fresh = await self.get_object(object_id, workspace_id=workspace_id)
        base = fresh.properties if fresh is not None else existing.properties
        merged = {**base, property: resolution.value}
        ws_cond, ws_params = _workspace_scope(workspace_id)
        sql = "UPDATE fabric_objects SET properties = ?, updated_at = datetime('now') WHERE id = ?"
        params: list[Any] = [json.dumps(merged), object_id]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        async with self._conn() as db:
            await db.execute(sql, params)
            await db.commit()

    async def pin_statement(
        self,
        object_id: str,
        property: str,
        statement_id: str,
        *,
        workspace_id: str | None = None,
    ) -> Resolution:
        """PIN one statement: the durable steward "this one wins" (FST-6).

        Sets ``pinned=True`` on the identified statement. The resolver's
        pinned short-circuit (FST-2) then makes it win outright — above every
        ladder tier, immune to newer rival observations — which is why the
        conflict-lifecycle executor maps an approved stewardship choice to
        PIN rather than IGNORE-the-rival: a pin also settles FUTURE rivals,
        and the losing statements stay intact for audit.

        The statement must exist for exactly this ``(object_id, property)``
        (within the caller's workspace scope) and must be non-deprecated —
        a deprecated statement never reaches resolution, so pinning it would
        be a silent no-op lie; both violations raise ``ValueError``. Pinning
        an already-pinned statement is idempotent. PIN does NOT auto-unpin
        other pins: multiple pins are a curation conflict the resolver
        deliberately surfaces as ``is_disputed``.

        Returns the NEW :class:`Resolution`. Cache behavior is
        mode-respecting like the FST-5 verbs: enforce writes the new resolver
        winner into the flat properties dict; shadow/off leave the cache
        alone.
        """
        return await self._steward_statement(
            object_id, property, statement_id, verb="pin", reason=None, workspace_id=workspace_id
        )

    async def unpin_statement(
        self,
        object_id: str,
        property: str,
        statement_id: str,
        *,
        workspace_id: str | None = None,
    ) -> Resolution:
        """UNPIN one statement: retract a steward pin (FST-6).

        Sets ``pinned=False``; resolution falls back to the trust ladder.
        The statement must exist for exactly this ``(object_id, property)``
        (ValueError otherwise). Unpinning a statement that isn't pinned is
        idempotent, and rank is not checked — retracting a flag is always a
        safe act. Returns the NEW :class:`Resolution`; cache behavior is
        mode-respecting (see :meth:`pin_statement`).
        """
        return await self._steward_statement(
            object_id, property, statement_id, verb="unpin", reason=None, workspace_id=workspace_id
        )

    async def ignore_statement(
        self,
        object_id: str,
        property: str,
        statement_id: str,
        *,
        reason: str = "steward_ignored",
        workspace_id: str | None = None,
    ) -> Resolution:
        """IGNORE one statement: the steward's "this claim is bogus" (FST-6).

        Deprecates the identified statement with ``rank_reason=reason`` —
        the same narrow curation write CORRECT applies to a wrong winner,
        but aimed at ANY statement (typically a losing rival) and without
        appending a replacement value. A deprecated statement never wins,
        never loses, never disputes: it is struck from resolution entirely
        while remaining in the table for audit.

        The statement must exist for exactly this ``(object_id, property)``
        (within the caller's workspace scope) — ValueError otherwise.
        Ignoring an already-deprecated statement just refreshes the reason.
        Returns the NEW :class:`Resolution`; cache behavior is
        mode-respecting (see :meth:`pin_statement`). Note: deprecating the
        ONLY live statement leaves a winner-less Resolution and the cache
        untouched — reads never lose a value to a steward strike.
        """
        return await self._steward_statement(
            object_id,
            property,
            statement_id,
            verb="ignore",
            reason=reason,
            workspace_id=workspace_id,
        )

    async def _steward_statement(
        self,
        object_id: str,
        property: str,
        statement_id: str,
        *,
        verb: str,
        reason: str | None,
        workspace_id: str | None,
    ) -> Resolution:
        """Shared core of :meth:`pin_statement` / :meth:`unpin_statement` /
        :meth:`ignore_statement`.

        Unlike ``_curate_property`` there is NO auto-promotion: the steward
        verbs target an EXISTING statement id, and an untracked property has
        no statements to target. Raises ``ValueError`` when the object or the
        statement doesn't exist (or is outside the caller's workspace scope),
        or when PIN targets a deprecated statement — the FST-6 executor needs
        clean failures, not silent no-ops.
        """
        mode = _source_truth_mode()  # read ONCE; decides only the cache write
        existing = await self.get_object(object_id, workspace_id=workspace_id)
        if existing is None:
            raise ValueError(f"fabric object not found: {object_id!r}")

        stmts = await self.get_statements(object_id, property, workspace_id=workspace_id)
        target = next((s for s in stmts if s.id == statement_id), None)
        if target is None:
            raise ValueError(
                f"statement {statement_id!r} not found for"
                f" ({object_id!r}, {property!r}) in the caller's workspace scope"
            )

        if verb == "pin":
            if target.rank == "deprecated":
                raise ValueError(
                    f"cannot pin deprecated statement {statement_id!r} — a deprecated"
                    " statement never reaches resolution; un-ignore it via a new"
                    " curation write first"
                )
            await self._set_statement_pinned(statement_id, True)
        elif verb == "unpin":
            await self._set_statement_pinned(statement_id, False)
        else:  # ignore
            await self._deprecate_statement(statement_id, reason)

        refreshed = await self.get_statements(object_id, property, workspace_id=workspace_id)
        resolution = resolve(
            refreshed, default_trust_rules(), object_type=existing.type_name or None
        )

        if mode == "enforce":
            await self._write_winner_to_cache(
                object_id, property, resolution, workspace_id=workspace_id, existing=existing
            )

        return resolution

    async def _set_statement_pinned(self, statement_id: str, pinned: bool) -> None:
        """Set the ``pinned`` flag on one statement (the PIN/UNPIN verbs).

        The THIRD narrow curation write permitted on statement rows (see the
        FST-6 module-header note; valid_to and rank/rank_reason are the other
        two). Value/provenance columns are never rewritten.
        """
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "UPDATE fabric_statements SET pinned = ? WHERE id = ?",
                (1 if pinned else 0, statement_id),
            )
            await db.commit()

    async def _close_statement_validity(self, statement_id: str, closed_at: datetime) -> None:
        """Set ``valid_to`` on one statement (the CHANGE verb's close).

        One of the TWO narrow curation writes permitted on statement rows
        (see the FST-5 module-header note) — the append-only doctrine's
        documented "later curation writes". Value/provenance columns are
        never rewritten.
        """
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "UPDATE fabric_statements SET valid_to = ? WHERE id = ?",
                (closed_at.isoformat(), statement_id),
            )
            await db.commit()

    async def _deprecate_statement(self, statement_id: str, reason: str | None) -> None:
        """Mark one statement ``rank="deprecated"`` (the CORRECT verb's strike).

        The second of the TWO narrow curation writes permitted on statement
        rows (see the FST-5 module-header note). ``rank_reason`` records why;
        value/provenance columns are never rewritten.
        """
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "UPDATE fabric_statements SET rank = 'deprecated', rank_reason = ? WHERE id = ?",
                (reason, statement_id),
            )
            await db.commit()

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

    # --- Statements & Sources (FST-1 — source-truth provenance) ---

    async def upsert_source(
        self,
        kind: str,
        *,
        connector: str | None = None,
        run_id: str | None = None,
        document_uri: str | None = None,
        actor_id: str | None = None,
        session_id: str | None = None,
        retrieved_at: datetime | None = None,
        workspace_id: str | None = None,
    ) -> SourceRef:
        """Return the SourceRef for this source identity, creating it if new.

        Dedup key is the identity tuple ``(kind, connector, run_id,
        document_uri, actor_id, session_id, workspace_id)`` — a second call
        with the same identity returns the SAME row (``retrieved_at`` is
        provenance metadata, not identity; an existing row is returned as-is,
        never mutated). ``workspace_id`` IS part of the identity, unlike the
        other fabric tables' W4a read-scope treatment: the same source seen
        from two workspaces yields two rows, so tenant provenance never
        rendezvouses on a shared row (tenancy isolation beats dedup);
        ``None`` = the OSS / single-tenant identity. A concurrent
        double-insert is closed by the ``idx_sources_identity_ws`` expression
        UNIQUE index: the loser's INSERT raises and resolves to a re-read of
        the winner's row.
        """
        identity_sql = (
            "SELECT * FROM fabric_sources WHERE kind = ?"
            " AND connector IS ? AND run_id IS ? AND document_uri IS ?"
            " AND actor_id IS ? AND session_id IS ? AND workspace_id IS ?"
        )
        identity_params = (
            kind,
            connector,
            run_id,
            document_uri,
            actor_id,
            session_id,
            workspace_id,
        )
        source = SourceRef(
            kind=kind,  # type: ignore[arg-type]  # Literal validated by pydantic
            connector=connector,
            run_id=run_id,
            document_uri=document_uri,
            actor_id=actor_id,
            session_id=session_id,
            retrieved_at=retrieved_at,
            workspace_id=workspace_id,
        )
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(identity_sql, identity_params) as cur:
                row = await cur.fetchone()
            if row:
                return self._row_to_source(row)
            try:
                await db.execute(
                    "INSERT INTO fabric_sources"
                    " (id, kind, connector, run_id, document_uri, actor_id,"
                    " session_id, retrieved_at, workspace_id)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        source.id,
                        source.kind,
                        connector,
                        run_id,
                        document_uri,
                        actor_id,
                        session_id,
                        retrieved_at.isoformat() if retrieved_at else None,
                        workspace_id,
                    ),
                )
                await db.commit()
            except aiosqlite.IntegrityError:
                # Concurrent upsert won the race — return its row.
                async with db.execute(identity_sql, identity_params) as cur:
                    row = await cur.fetchone()
                if row:
                    return self._row_to_source(row)
                raise
        return source

    async def append_statement(
        self,
        object_id: str,
        property: str,
        value: Any,
        source_ref_id: str,
        writer_class: str,
        *,
        observed_at: datetime | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        rank: str = "normal",
        rank_reason: str | None = None,
        pinned: bool = False,
        workspace_id: str | None = None,
    ) -> Statement:
        """Append ONE observed (object, property, value) claim with provenance.

        APPEND-ONLY: statements are never updated or deleted (this store
        exposes no verbs for either; rank changes land in a later slice as new
        curation writes). ``recorded_at`` is stamped here; ``observed_at``
        defaults to now (a live observation) and ``valid_from`` defaults to
        ``observed_at``. ``value`` is JSON-encoded — any JSON-serializable
        value round-trips, including ``None``. ``workspace_id`` stamps the
        owning tenant on the row (W4a write semantics, same as
        :meth:`create_object`); ``None`` = OSS / single-tenant caller.

        Does NOT touch the object's flat ``properties`` dict — that dict
        remains the primary read path; nothing consumes statements until
        ``fabric_source_truth_mode`` gains shadow/enforce semantics.
        """
        stmt = Statement(
            object_id=object_id,
            property=property,
            value=value,
            source_ref_id=source_ref_id,
            writer_class=writer_class,  # type: ignore[arg-type]  # Literal validated by pydantic
            rank=rank,  # type: ignore[arg-type]  # Literal validated by pydantic
            rank_reason=rank_reason,
            pinned=pinned,
            workspace_id=workspace_id,
        )
        if observed_at is not None:
            stmt.observed_at = observed_at
        stmt.valid_from = valid_from if valid_from is not None else stmt.observed_at
        stmt.valid_to = valid_to
        await self._ensure_schema()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO fabric_statements"
                " (id, object_id, property, value, source_ref_id, writer_class,"
                " observed_at, recorded_at, valid_from, valid_to, rank,"
                " rank_reason, pinned, workspace_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    stmt.id,
                    stmt.object_id,
                    stmt.property,
                    json.dumps(stmt.value),
                    stmt.source_ref_id,
                    stmt.writer_class,
                    stmt.observed_at.isoformat(),
                    stmt.recorded_at.isoformat(),
                    stmt.valid_from.isoformat(),
                    stmt.valid_to.isoformat() if stmt.valid_to else None,
                    stmt.rank,
                    stmt.rank_reason,
                    1 if stmt.pinned else 0,
                    workspace_id,
                ),
            )
            await db.commit()
        return stmt

    async def get_statements(
        self,
        object_id: str,
        property: str | None = None,
        workspace_id: str | None = None,
    ) -> list[Statement]:
        """All statements for one object, optionally narrowed to one property.

        Ordered by ``recorded_at`` (then id, for a stable order within the
        same timestamp) — oldest first, so a resolver reading the full history
        replays claims in the order the store learned them. ``workspace_id``
        applies the standard W4a read scope (own rows + legacy NULL rows, via
        ``_workspace_scope`` — same as :meth:`get_object`); ``None`` leaves
        the read unscoped (OSS / single-tenant callers).
        """
        ws_cond, ws_params = _workspace_scope(workspace_id)
        sql = "SELECT * FROM fabric_statements WHERE object_id = ?"
        params: list[Any] = [object_id]
        if property is not None:
            sql += " AND property = ?"
            params.append(property)
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        sql += " ORDER BY recorded_at, id"
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [self._row_to_statement(r) for r in rows]

    async def list_statement_keys(
        self,
        *,
        workspace_id: str | None = None,
        object_id: str | None = None,
    ) -> list[tuple[str, str]]:
        """DISTINCT ``(object_id, property)`` pairs that HAVE statements (FST-6).

        The cheap scan surface the conflict lifecycle recomputes open
        conflicts from: only objects WITH statements (the opted-in / promoted
        minority) are ever visited, so the scan cost tracks the tracked set,
        not the whole fabric. ``workspace_id`` applies the standard W4a read
        scope (own rows + legacy NULL); ``object_id`` narrows to one object.
        Ordered by ``(object_id, property)`` for a deterministic walk.
        """
        conditions: list[str] = []
        params: list[Any] = []
        if object_id is not None:
            conditions.append("object_id = ?")
            params.append(object_id)
        ws_cond, ws_params = _workspace_scope(workspace_id)
        if ws_cond:
            conditions.append(ws_cond)
            params.extend(ws_params)
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        sql = (
            "SELECT DISTINCT object_id, property FROM fabric_statements"
            f"{where} ORDER BY object_id, property"
        )
        await self._ensure_schema()
        async with self._conn() as db:
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return [(str(r[0]), str(r[1])) for r in rows]

    async def get_source(
        self, source_ref_id: str, workspace_id: str | None = None
    ) -> SourceRef | None:
        """Read one SourceRef by id (FST-6).

        The provenance lookup behind the stewardship proposal payload: a
        human arbitrating a conflict sees WHERE each competing value came
        from (connector run / document / actor / session). ``workspace_id``
        applies the standard W4a read scope (own rows + legacy NULL).
        """
        ws_cond, ws_params = _workspace_scope(workspace_id)
        sql = "SELECT * FROM fabric_sources WHERE id = ?"
        params: list[Any] = [source_ref_id]
        if ws_cond:
            sql += f" AND {ws_cond}"
            params.extend(ws_params)
        await self._ensure_schema()
        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cur:
                row = await cur.fetchone()
        return self._row_to_source(row) if row else None

    async def get_object_provenance(
        self, object_id: str, *, workspace_id: str | None = None
    ) -> dict[str, dict[str, Any]]:
        """Opt-in provenance read surface (FST-7).

        Per statement-TRACKED property of one object:
        ``{property: {disputed, unresolvable, freshness, statements, winner}}``
        where ``winner`` carries the resolving statement's writer_class /
        observed_at / rank / pinned plus a compact source summary. Untracked
        properties (the scalar majority) do not appear — absence means
        "single-source, nothing to explain". A SIBLING method rather than a
        ``get_object`` flag so the default read path pays nothing; the
        agent-facing ``fabric_query`` MCP tool and the future "disputed
        facts" view are the consumers. Freshness/dispute state is computed
        live (statements + resolve() with the store clock), never persisted.
        """
        keys = await self.list_statement_keys(workspace_id=workspace_id, object_id=object_id)
        if not keys:
            return {}
        obj = await self.get_object(object_id, workspace_id=workspace_id)
        object_type = (obj.type_name or None) if obj else None
        now = datetime.now(UTC)
        rules = default_trust_rules()
        out: dict[str, dict[str, Any]] = {}
        for _oid, prop in keys:
            stmts = await self.get_statements(object_id, prop, workspace_id=workspace_id)
            if not stmts:
                continue
            resolution = resolve(stmts, rules, object_type=object_type, now=now)
            winner = resolution.winner_statement
            winner_info: dict[str, Any] | None = None
            if winner is not None:
                src = await self.get_source(winner.source_ref_id, workspace_id=workspace_id)
                winner_info = {
                    "writer_class": winner.writer_class,
                    "observed_at": winner.observed_at.isoformat(),
                    "rank": winner.rank,
                    "pinned": winner.pinned,
                    "source": (
                        {
                            "kind": src.kind,
                            "connector": src.connector,
                            "run_id": src.run_id,
                            "document_uri": src.document_uri,
                            "actor_id": src.actor_id,
                            "session_id": src.session_id,
                        }
                        if src
                        else None
                    ),
                }
            out[prop] = {
                "disputed": resolution.is_disputed,
                "unresolvable": resolution.unresolvable,
                "freshness": resolution.winner_freshness,
                "statements": len(stmts),
                "winner": winner_info,
            }
        return out

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
        if q.group_by:
            # Self-serve-analysis S1: aggregation runs over the SAME flat
            # scoped+filtered WHERE the plain path builds (scope-then-aggregate).
            # Flag-gated inside — a dark feature rejects fail-loud.
            return await self._query_aggregate(q, workspace_id)

        conditions, params = _flat_query_conditions(q, workspace_id)
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

    async def _query_aggregate(self, q: FabricQuery, workspace_id: str | None) -> FabricQueryResult:
        """Run a flag-gated SQL GROUP BY aggregation (self-serve-analysis S1).

        Contract:

        - GATE: requires the ``fabric_analyst`` settings flag
          (POCKETPAW_FABRIC_ANALYST). Off -> :class:`FabricAnalystDisabledError`
          (fail-loud; the EE router maps it to 422 ``fabric.analyst_disabled``).
        - SCOPE-THEN-AGGREGATE: the WHERE is built by the same
          ``_flat_query_conditions`` the plain path uses — tenancy scope (W4a)
          and property filters constrain the row set BEFORE grouping, so a
          cross-workspace object can never be counted into a group.
        - GROUPING: plain ``group_by`` groups on the raw JSON property value;
          with ``q.ranges`` a fully-parameterized CASE chain buckets the
          numeric value (min inclusive, max exclusive; labels are bound
          params). Rows whose group key resolves to NULL (missing property, or
          a value outside every bucket) are dropped via HAVING.
        - AGGREGATE: ``count`` = COUNT(*); sum/avg/min/max fold
          ``aggregate_field`` CAST to REAL (same numeric affinity rule as the
          numeric filter operators). Function names come from a fixed internal
          map — user input never reaches the SQL text; property names ride as
          bound ``$.name`` json_extract path params.
        - OUTPUT: ``aggregates`` = one ``{"key", "value"}`` dict per group,
          ordered by ``q.sort`` (default: value descending) and paginated by
          ``limit``/``offset``; ``objects`` is empty (this is an analysis read,
          not a fetch); ``total`` = scoped+filtered object count; ``steps`` =
          the human-readable reasoning trace (:class:`QueryPlanStep`).
        """
        from pocketpaw.config import get_settings

        if not get_settings().fabric_analyst:
            raise FabricAnalystDisabledError(
                "Fabric self-serve analysis is disabled: aggregation queries "
                "(group_by/aggregate) require the POCKETPAW_FABRIC_ANALYST "
                "flag. Plain queries remain available."
            )

        # The model validator guarantees: group_by set, aggregate normalized
        # (count default), aggregate_field present iff sum/avg/min/max, no path.
        assert q.group_by is not None and q.aggregate is not None

        conditions, params = _flat_query_conditions(q, workspace_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        group_expr, group_params = _group_key_expr(q)
        agg_expr, agg_params = _aggregate_expr(q)
        order_sql = _AGG_SORT_SQL[q.sort or "value_desc"]

        sql = (
            f"SELECT {group_expr} AS grp, {agg_expr} AS val"
            f" FROM fabric_objects o {where}"
            " GROUP BY grp HAVING grp IS NOT NULL"
            f" ORDER BY {order_sql} LIMIT ? OFFSET ?"
        )

        async with self._conn() as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"SELECT COUNT(*) as cnt FROM fabric_objects o {where}", params
            ) as cur:
                row = await cur.fetchone()
                total = row["cnt"] if row else 0
            async with db.execute(
                sql, [*group_params, *agg_params, *params, q.limit, q.offset]
            ) as cur:
                aggregates = [{"key": r["grp"], "value": r["val"]} async for r in cur]

        steps = _build_plan_steps(q, total=total, group_count=len(aggregates))
        return FabricQueryResult(objects=[], total=total, aggregates=aggregates, steps=steps)

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
        # FST-3: created_at/updated_at now come from the ROW (they were
        # silently dropped before, so the model defaulted them to read-time
        # ``now()``). The shadow pass needs the TRUE last-touch time for the
        # auto-promotion backfill (observed_at of the seeded statement).
        # SQLite's datetime('now') stamps are naive UTC "YYYY-MM-DD HH:MM:SS"
        # strings; fromisoformat parses them as-is. Defensive: a NULL falls
        # back to the model default rather than crashing the read.
        timestamps: dict[str, Any] = {}
        for ts_field in ("created_at", "updated_at"):
            if ts_field in row.keys() and row[ts_field]:
                timestamps[ts_field] = datetime.fromisoformat(row[ts_field])
        return FabricObject(
            id=row["id"],
            type_id=row["type_id"],
            type_name=row["type_name"] or "",
            properties=json.loads(row["properties"]) if row["properties"] else {},
            source_connector=row["source_connector"],
            source_id=row["source_id"],
            **timestamps,
        )

    def _row_to_link(self, row: Any) -> FabricLink:
        return FabricLink(
            id=row["id"],
            from_object_id=row["from_object_id"],
            to_object_id=row["to_object_id"],
            link_type=row["link_type"],
            properties=json.loads(row["properties"]) if row["properties"] else {},
        )

    def _row_to_source(self, row: Any) -> SourceRef:
        return SourceRef(
            id=row["id"],
            kind=row["kind"],
            connector=row["connector"],
            run_id=row["run_id"],
            document_uri=row["document_uri"],
            actor_id=row["actor_id"],
            session_id=row["session_id"],
            retrieved_at=(
                datetime.fromisoformat(row["retrieved_at"]) if row["retrieved_at"] else None
            ),
            workspace_id=row["workspace_id"],
        )

    def _row_to_statement(self, row: Any) -> Statement:
        return Statement(
            id=row["id"],
            object_id=row["object_id"],
            property=row["property"],
            value=json.loads(row["value"]) if row["value"] is not None else None,
            source_ref_id=row["source_ref_id"],
            writer_class=row["writer_class"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            recorded_at=datetime.fromisoformat(row["recorded_at"]),
            valid_from=datetime.fromisoformat(row["valid_from"]),
            valid_to=(datetime.fromisoformat(row["valid_to"]) if row["valid_to"] else None),
            rank=row["rank"],
            rank_reason=row["rank_reason"],
            pinned=bool(row["pinned"]),
            workspace_id=row["workspace_id"],
        )
