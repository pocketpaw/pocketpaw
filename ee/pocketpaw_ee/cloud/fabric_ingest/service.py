# service.py — Generic Firestore→Fabric ingestion worker.
# Created: 2026-06-11 — generic Firestore→Fabric ingestion worker.
# Updated: 2026-07-11 (feat/real-pipeline-s1) — connector-source dispatch. The
#   run path now branches on ``mapping.source_kind``: "firestore" rides the
#   existing reader path UNCHANGED; "connector" routes through the new
#   ``_ingest_from_connector`` — resolve the workspace's enabled
#   ``WorkspaceConnector`` row (missing/disabled → error result, never raise),
#   look the ingestor up in the OSS ``FABRIC_INGESTORS`` registry
#   (lazy-imported; unregistered → error result), then
#   ``await ingestor(store, workspace_id=..., user_id=wc.user_id)`` so a
#   user-scoped connector reads with THAT member's OAuth token bucket (None =
#   the shared/workspace bucket — the member_ingest seam). State bookkeeping +
#   FabricIngestCompleted emit are reused verbatim; the cursor is untouched on
#   connector runs (the ingestor owns its own read window). Also added the
#   mapping-authoring service surface for the new router: ``list_mappings`` /
#   ``upsert_mapping`` / ``delete_mapping`` (service.py stays the only reader
#   of FabricIngestConfig — cloud rule §2).
# Updated: 2026-07-11 (feat/external-alerting-c2c3) — ``run_ingest_sweep`` now
#   filters sources by the per-workspace automation opt-out
#   (``automations_status.service.filter_sweep_enabled_workspaces``): a tenant
#   that turned its background sweeps off is skipped. Fails OPEN.
# Updated: 2026-06-11 (rebase onto dev) — the per-document upsert now delegates
#   to the OSS connector→Fabric mapper (pocketpaw.connectors.fabric_ingest
#   .ingest_records, landed on dev in the Calendar ingestion PR #1418) instead
#   of a private _upsert_object/_map_properties pair — one canonical
#   upsert-by-source loop, not two parallel ones. Each Firestore doc becomes a
#   connector record carrying its doc path under a sentinel key; the workspace
#   config mapping translates into an OSS FabricMapping (field map inverted to
#   the OSS {property: record_key} direction; the configured object_type_id
#   pinned via the mapping's new optional type_id). Behavior note inherited
#   from the canonical loop: a mapped field absent from a document projects as
#   None (the mirror reflects the source) rather than being skipped.
#
# What this does
# --------------
# Continuously mirrors selected Firestore collections into Fabric objects for a
# deployment. The per-deployment mapping (which collections, which object types,
# field maps, cursor fields, link rules) lives entirely in a per-workspace
# ``FabricIngestConfig`` document — there is NOTHING domain-specific in this
# module. ``ingest_collection`` mirrors one collection for one workspace;
# ``run_ingest_sweep`` fans that out across every configured (workspace,
# collection) pair under a concurrency cap; the scheduler ticks the sweep.
#
# Cursor — a REAL high-water mark, not run wall-clock
# ---------------------------------------------------
# The incremental cursor stored in ``FabricIngestState.cursor`` is the value of
# the mapping's configured ``cursor_field`` on the newest document seen (falling
# back to the Firestore snapshot ``update_time`` when a document has no value
# for the field). It is NOT the run's wall clock. MemberIngestState advances its
# cursor to ``now`` on every run (service.py:188 there), which means a document
# that lands late but carries an older updated-at is missed and a re-run
# re-scans a time window. Here the worker passes the stored cursor as the
# reader's lower bound and advances it only to the max cursor value it actually
# observed, so re-runs don't re-ingest unchanged documents and late-but-older
# documents are still picked up on their own merits.
#
# Write sink — the OSS FabricStore via the canonical connector mapper
# --------------------------------------------------------------------
# Every object is written through the OSS connector→Fabric mapper
# (``pocketpaw.connectors.fabric_ingest.ingest_records``) into the OSS
# ``FabricStore`` (the same store the Fabric router uses), stamped with the
# caller's ``workspace_id`` (plain str — OSS must never import pocketpaw_ee),
# ``source_connector="firestore"``, and ``source_id`` = the full Firestore
# document path. The mapper upserts by ``(source_connector, source_id)`` via
# ``FabricStore.get_object_by_source`` — a re-run UPDATEs instead of inserting
# a duplicate. ``create_object`` commits per row — acceptable for v1; batching
# is a TODO (see the follow-ups in __init__.py), not built here.
#
# Reader Protocol — testability
# ------------------------------
# All Firestore access goes through the ``FirestoreReader`` Protocol so tests
# inject fakes with no google credentials. The concrete reader wrapping
# ``google-cloud-firestore`` is constructed lazily and only when no fake was
# passed, and the google import is deferred with a clear install error so the
# dependency stays optional.

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import FabricIngestCompleted
from pocketpaw_ee.cloud.fabric_ingest.dto import (
    IngestCollectionRequest,
    IngestConfigRequest,
    SweepRequest,
)

logger = logging.getLogger(__name__)

# Provenance stamp on every mirrored object (cloud rule: tenancy + source on
# every write). ``source_connector`` is fixed; ``source_id`` is the doc path.
_SOURCE_CONNECTOR = "firestore"
# Sentinel record key carrying the Firestore doc path into the OSS mapper —
# the doc path lives OUTSIDE the document's field dict, but ``ingest_records``
# extracts the source id from a record key, so we graft it on under a name no
# real Firestore field would use (and which would be overwritten if one did).
_DOC_PATH_KEY = "__firestore_doc_path__"

# Per-collection page cap so one huge collection can't wedge a sweep tick.
_MAX_DOCS_PER_RUN = 1000
# Default sweep fan-out cap so N collection-syncs don't swamp the box.
_DEFAULT_SWEEP_CONCURRENCY = 4


# --------------------------------------------------------------------------
# Reader protocol — the worker depends on this narrow shape, not on the
# concrete google-cloud-firestore client, so tests inject fakes with no creds.
# --------------------------------------------------------------------------


class FirestoreReader(Protocol):
    async def read_collection(
        self,
        collection: str,
        *,
        cursor_field: str,
        cursor: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Return documents from ``collection`` whose ``cursor_field`` value is
        strictly greater than ``cursor`` (all documents when ``cursor`` is
        empty — the backfill case), ordered ascending by that field, capped at
        ``limit``.

        Each returned dict carries:
          * ``path``        — the full Firestore document path (becomes source_id)
          * ``data``        — the document's field dict
          * ``update_time`` — the snapshot update time as an RFC3339 string
                              (the cursor fallback when a doc lacks cursor_field)
        """
        ...


# ingest_fn(workspace_id, source_id, **kw) -> result dict. The per-collection
# unit the sweep dispatches; defaults to ``ingest_collection`` but injectable.
IngestFn = Callable[..., Awaitable[dict[str, Any]]]
# A store with the create/update/link/get_object_by_source surface this worker
# needs. Typed loosely so tests can pass a fake store without importing the OSS
# FabricStore. The real default is ``pocketpaw.stores.get_fabric_store()``.
StoreLike = Any


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------


async def ingest_collection(
    workspace_id: str,
    source_id: str,
    *,
    reader: FirestoreReader | None = None,
    store: StoreLike | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Mirror one Firestore collection (``source_id``) into Fabric objects for
    one workspace.

    The mapping (object type, field map, cursor field, link rules) is read from
    the workspace's ``FabricIngestConfig``; if no mapping matches ``source_id``
    the call is a no-op error. Backfill-vs-incremental is decided by the
    persisted ``FabricIngestState.backfill_done`` flag. Returns a result dict
    with ``status`` (``ok``/``error``), ``mode`` (``backfill``/``incremental``),
    ``objects`` (count mirrored this run), ``cursor`` (the new high-water mark),
    and any ``errors``.
    """
    # Validate at entry (cloud rule §6) — the sweep and scheduler get the same
    # guard an HTTP body would.
    body = IngestCollectionRequest.model_validate(
        {"workspace_id": workspace_id, "source_id": source_id}
    )
    workspace_id, source_id = body.workspace_id, body.source_id
    now = now or datetime.now(UTC)

    mapping = await _load_mapping(workspace_id, source_id)
    if mapping is None:
        # No mapping for this collection — nothing to mirror. Report an error so
        # a misconfigured sweep entry is visible, but never raise.
        return {
            "workspace_id": workspace_id,
            "source_id": source_id,
            "status": "error",
            "mode": "none",
            "objects": 0,
            "cursor": "",
            "errors": [f"no mapping for collection {source_id!r}"],
        }

    # ISO: thread the caller's workspace so the default store is tenant-scoped
    # (an injected test store is used as-is).
    store = store or _default_store(workspace_id)
    state = await _load_or_create_state(workspace_id, source_id)
    mode = "backfill" if not state.backfill_done else "incremental"
    state.status = "running"
    await state.save()  # no-event: transient in-progress marker.

    errors: list[str] = []
    objects = 0
    new_cursor = state.cursor
    source_kind = getattr(mapping, "source_kind", "firestore")

    # Only the firestore path needs a reader — a connector run's ingestor owns
    # its own read (and constructing the default reader pulls google creds).
    if reader is None and source_kind != "connector":
        reader = _default_reader()

    try:
        if source_kind == "connector":
            # Connector-source dispatch (feat/real-pipeline-s1): pull records
            # through the OSS ingestor registry with the workspace's own
            # connector binding. Misconfiguration (connector not connected,
            # ingestor not registered) is an error RESULT, never a raise —
            # same contract as a missing mapping above. The cursor is left
            # untouched: the ingestor owns its own read window and the
            # upsert-by-(source_connector, source_id) loop keeps re-runs
            # idempotent.
            objects, connector_error = await _ingest_from_connector(store, workspace_id, mapping)
            if connector_error:
                errors.append(f"{source_id}: {connector_error}")
        else:
            # Backfill reads from an empty cursor (everything); incremental
            # reads only documents past the stored high-water mark.
            lower_bound = "" if mode == "backfill" else state.cursor
            docs = await reader.read_collection(
                mapping.collection,
                cursor_field=mapping.cursor_field,
                cursor=lower_bound,
                limit=_MAX_DOCS_PER_RUN,
            )
            # Advance the high-water mark to the MAX cursor value we actually
            # saw across docs with a usable identity — a real watermark from
            # document data, never run wall-clock. Persisted only on a clean
            # run (below).
            for doc in docs:
                if not str(doc.get("path") or ""):
                    continue  # no stable identity — the mapper skips it too
                doc_cursor = _doc_cursor(doc, mapping.cursor_field)
                if doc_cursor and doc_cursor > new_cursor:
                    new_cursor = doc_cursor
            # Upsert through the canonical OSS connector→Fabric loop.
            objects = await _mirror_docs(store, workspace_id, docs, mapping)
            # Link rules run after all objects exist so a link can target a
            # document mirrored earlier in the same batch.
            if mapping.link_rules:
                await _apply_link_rules(store, workspace_id, docs, mapping)
    except Exception as exc:  # noqa: BLE001 — isolate this collection so one bad
        # source never crashes the sweep or the other collections.
        logger.warning(
            "fabric_ingest: read/write failed for ws=%s collection=%s: %s",
            workspace_id,
            source_id,
            exc,
        )
        errors.append(f"{source_id}: {exc}")

    # --- Persist outcome ---
    status = "error" if errors else "ok"
    state.status = status
    state.last_error = "; ".join(errors)
    state.objects_ingested += objects
    if status == "ok":
        # Only flip backfill_done + advance the cursor on a clean run — a failed
        # backfill retries the wide read next time rather than skipping ahead.
        state.backfill_done = True
        state.last_sync_at = now
        state.cursor = new_cursor
    await state.save()  # no-event: domain event emitted explicitly below.

    await emit(
        FabricIngestCompleted(
            data={
                "workspace_id": workspace_id,
                "source_id": source_id,
                "object_type_id": mapping.object_type_id,
                "mode": mode,
                "status": status,
                "objects_ingested": objects,
                "cursor": state.cursor,
            }
        )
    )

    return {
        "workspace_id": workspace_id,
        "source_id": source_id,
        "status": status,
        "mode": mode,
        "objects": objects,
        "cursor": state.cursor,
        "errors": errors,
    }


async def list_ingest_sources(workspace_id: str | None = None) -> list[dict[str, str]]:
    """Enumerate (workspace, collection) pairs the worker should mirror.

    One entry per mapping across every enabled ``FabricIngestConfig``. When
    ``workspace_id`` is given the Beanie query pins it (tenant filter, cloud
    rule §7); the global form (``None``) is the scheduler's cross-tenant sweep.
    """
    from pocketpaw_ee.cloud.models.fabric_ingest_state import FabricIngestConfig

    query: dict[str, Any] = {"enabled": True}
    if workspace_id:
        query["workspace"] = workspace_id  # tenant filter
    # else: global-read — the scheduler sweeps every configured tenant.

    configs = await FabricIngestConfig.find(query).to_list()

    sources: list[dict[str, str]] = []
    for cfg in configs:
        for mapping in cfg.mappings:
            if not mapping.collection:
                continue
            sources.append({"workspace_id": cfg.workspace, "source_id": mapping.collection})
    return sources


async def run_ingest_sweep(
    *,
    workspace_id: str | None = None,
    concurrency: int = _DEFAULT_SWEEP_CONCURRENCY,
    ingest_fn: IngestFn | None = None,
) -> dict[str, Any]:
    """Mirror every configured collection, bounded by a concurrency cap.

    One collection failing never aborts the others — each unit is isolated.
    Returns ``{sources, ok, errors}``. ``ingest_fn`` defaults to
    ``ingest_collection`` (real reader + real store); tests inject a fake.
    """
    body = SweepRequest.model_validate({"concurrency": concurrency})
    concurrency = body.concurrency
    fn = ingest_fn or ingest_collection

    sources = await list_ingest_sources(workspace_id=workspace_id)
    if not sources:
        return {"sources": 0, "ok": 0, "errors": 0}

    # Per-workspace opt-out (feat/external-alerting-c2c3): drop sources whose
    # workspace turned its background sweeps off. One deduped check per unique
    # workspace; fails OPEN so a config-read hiccup keeps the always-on default.
    from pocketpaw_ee.cloud.automations_status.service import (
        filter_sweep_enabled_workspaces,
    )

    enabled_ws = await filter_sweep_enabled_workspaces({s["workspace_id"] for s in sources})
    sources = [s for s in sources if s["workspace_id"] in enabled_ws]
    if not sources:
        return {"sources": 0, "ok": 0, "errors": 0}

    sem = asyncio.Semaphore(concurrency)
    ok = 0
    errors = 0

    async def _one(entry: dict[str, str]) -> None:
        nonlocal ok, errors
        async with sem:
            try:
                result = await fn(entry["workspace_id"], entry["source_id"])
            except Exception as exc:  # noqa: BLE001 — isolate per collection so a
                # single bad source doesn't abort the whole sweep.
                logger.warning(
                    "fabric_ingest: sweep unit failed ws=%s source=%s: %s",
                    entry["workspace_id"],
                    entry["source_id"],
                    exc,
                )
                errors += 1
                return
            if isinstance(result, dict) and result.get("status") == "error":
                errors += 1
            else:
                ok += 1

    await asyncio.gather(*(_one(s) for s in sources))
    logger.info(
        "fabric_ingest: sweep complete sources=%d ok=%d errors=%d (ws=%s)",
        len(sources),
        ok,
        errors,
        workspace_id or "ALL",
    )
    return {"sources": len(sources), "ok": ok, "errors": errors}


# --------------------------------------------------------------------------
# Mapping / state helpers
# --------------------------------------------------------------------------


async def _load_mapping(workspace_id: str, source_id: str):
    """Return the mapping for ``source_id`` from the workspace's config, or None.

    The config doc is validated through ``IngestConfigRequest`` at read (cloud
    rule §6 — validate at entry even on the read path) so a malformed stored
    config surfaces as a validation error rather than a silent half-mirror.
    Tenant filter on the read (cloud rule §7): ``workspace`` pins the row.
    """
    from pocketpaw_ee.cloud.models.fabric_ingest_state import FabricIngestConfig

    cfg = await FabricIngestConfig.find_one(
        FabricIngestConfig.workspace == workspace_id,
        FabricIngestConfig.enabled == True,  # noqa: E712 — Beanie needs ==, not `is`
    )
    if cfg is None:
        return None

    # Validate the stored config at entry. Raises on a malformed mapping.
    IngestConfigRequest.model_validate(
        {
            "workspace_id": cfg.workspace,
            "enabled": cfg.enabled,
            "mappings": [m.model_dump() for m in cfg.mappings],
        }
    )

    for mapping in cfg.mappings:
        if mapping.collection == source_id:
            return mapping
    return None


async def _load_or_create_state(workspace_id: str, source_id: str):
    """Fetch the per-source ingest state, creating a fresh row on first run.

    Tenant filter on the read (cloud rule §7): both ``workspace`` and
    ``source_id`` pin the row so two sources never share state and no
    cross-tenant row is ever touched.
    """
    from pocketpaw_ee.cloud.models.fabric_ingest_state import FabricIngestState

    state = await FabricIngestState.find_one(
        FabricIngestState.workspace == workspace_id,
        FabricIngestState.source_id == source_id,
    )
    if state is None:
        state = FabricIngestState(workspace=workspace_id, source_id=source_id)
        await state.insert()
    return state


# --------------------------------------------------------------------------
# Doc → object mapping + upsert
# --------------------------------------------------------------------------


def _doc_cursor(doc: dict[str, Any], cursor_field: str) -> str:
    """The cursor value for a document: its ``cursor_field`` value, falling back
    to the snapshot ``update_time`` when the field is absent/empty.

    Returned as a string so the comparison stays uniform regardless of the
    Firestore value type (timestamps serialize to RFC3339, which sorts
    lexicographically the same as chronologically).
    """
    data = doc.get("data") or {}
    raw = data.get(cursor_field) if cursor_field else None
    if raw is None or raw == "":
        raw = doc.get("update_time") or ""
    return str(raw)


async def _mirror_docs(
    store: StoreLike,
    workspace_id: str,
    docs: list[dict[str, Any]],
    mapping: Any,
) -> int:
    """Upsert the documents into Fabric via the canonical OSS connector mapper.

    Delegates to ``pocketpaw.connectors.fabric_ingest.ingest_records`` (the
    upsert-by-source loop the Calendar connector ingestion established) rather
    than a parallel private implementation. The translation:

    * each Firestore doc becomes a record of its field dict plus the doc path
      grafted on under ``_DOC_PATH_KEY`` — the mapper extracts the source id
      from a record key, and the path is the stable identity here;
    * the workspace config's ``field_map`` ({firestore_field: property_name})
      inverts into the OSS direction ({property_name: record_key}). If two
      Firestore fields ever mapped to the same property, the last one wins;
    * the configured ``object_type_id`` pins the ObjectType via the mapping's
      ``type_id`` (no define-by-name — the type already exists per config);
      ``type_name`` is only the reporting label on the result.

    Every object lands stamped with ``workspace_id`` (a tenancy leak
    otherwise — legacy NULL-workspace rows are globally visible),
    ``source_connector="firestore"``, and ``source_id`` = the full doc path.
    Returns the number of objects created + updated.
    """
    # Lazy import — keeps EE module import light and consistent with the other
    # OSS imports in this module (_default_store).
    from pocketpaw.connectors.fabric_ingest import FabricMapping, ingest_records

    records = [
        {**(doc.get("data") or {}), _DOC_PATH_KEY: str(doc.get("path") or "")} for doc in docs
    ]
    oss_mapping = FabricMapping(
        type_name=mapping.object_type_id,  # label only; type_id pins the type
        source_id_field=_DOC_PATH_KEY,
        field_map={prop: fs_field for fs_field, prop in mapping.field_map.items()},
        type_id=mapping.object_type_id,
    )
    summary = await ingest_records(
        store,
        _SOURCE_CONNECTOR,
        records,
        oss_mapping,
        workspace_id=workspace_id,
    )
    return summary.total


async def _apply_link_rules(
    store: StoreLike,
    workspace_id: str,
    docs: list[dict[str, Any]],
    mapping: Any,
) -> None:
    """Create links for every document per the mapping's link rules.

    For each rule, read ``via_field`` off the source document; if it names the
    source_id (full doc path) of an already-mirrored object of type ``to_type``
    in this workspace, link the source object to it with ``link_type``. A rule
    that can't resolve its target is skipped silently (the target may simply not
    be mirrored yet) — links are best-effort enrichment, never a hard failure.
    """
    for doc in docs:
        src_source_id = str(doc.get("path") or "")
        src_obj = await store.get_object_by_source(
            _SOURCE_CONNECTOR, src_source_id, workspace_id=workspace_id
        )
        if src_obj is None:
            continue
        data = doc.get("data") or {}
        for rule in mapping.link_rules:
            target_source_id = data.get(rule.via_field)
            if not target_source_id:
                continue
            target = await store.get_object_by_source(
                _SOURCE_CONNECTOR, str(target_source_id), workspace_id=workspace_id
            )
            if target is None or target.type_id != rule.to_type:
                continue
            await store.link(
                src_obj.id,
                target.id,
                rule.link_type,
                workspace_id=workspace_id,
            )


async def _ingest_from_connector(
    store: StoreLike,
    workspace_id: str,
    mapping: Any,
) -> tuple[int, str]:
    """Run one connector-source mapping through the OSS ingestor registry.

    Returns ``(objects_ingested, error)`` — ``error`` is ``""`` on success.
    The two misconfiguration cases report as an error string (never raise):

    1. the workspace has no ENABLED ``WorkspaceConnector`` row for the
       connector — not connected, or the operator disabled it (tenant filter
       on the read, cloud rule §7);
    2. no ingestor is registered under the connector id in the OSS
       ``FABRIC_INGESTORS`` registry.

    On success the ingestor is called with the mapping's tenant-scoped store
    and the connector row's ``user_id`` — a user-scoped connector (VIP
    onboarding) reads with THAT member's OAuth token bucket; ``None`` means
    the shared/workspace bucket (the member_ingest seam, proven at
    member_ingest/service.py's WorkspaceConnector lookup). A genuine ingestor
    failure (API error, expired token) propagates to ``ingest_collection``'s
    per-collection isolation and lands as an error result there.
    """
    # service-to-model read of another entity's doc — same minimal correct
    # path member_ingest/service.py takes (flagged there for a future
    # ``connectors.service.list_user_connectors`` extraction).
    from pocketpaw_ee.cloud.models.connector import WorkspaceConnector

    connector_id = getattr(mapping, "connector_id", None) or mapping.collection
    wc = await WorkspaceConnector.find_one(
        WorkspaceConnector.workspace == workspace_id,  # tenant filter
        WorkspaceConnector.name == connector_id,
        WorkspaceConnector.enabled == True,  # noqa: E712 — Beanie needs ==, not `is`
    )
    if wc is None:
        return 0, f"connector {connector_id!r} is not connected/enabled for this workspace"

    # Lazy import — EE reaches into the OSS registry, never the reverse.
    from pocketpaw.connectors.fabric_ingest import get_fabric_ingestor

    ingestor = get_fabric_ingestor(connector_id)
    if ingestor is None:
        return 0, f"no fabric ingestor registered for connector {connector_id!r}"

    result = await ingestor(store, workspace_id=workspace_id, user_id=wc.user_id)
    return result.total, ""


# --------------------------------------------------------------------------
# Mapping authoring (the transform surface's service layer)
# --------------------------------------------------------------------------


async def list_mappings(workspace_id: str) -> list[dict[str, Any]]:
    """Return the workspace's authored mappings (empty list when no config).

    Tenant filter on the read (cloud rule §7). Reads the config row regardless
    of its ``enabled`` flag — authoring must show what's configured even while
    the sweep is paused.
    """
    from pocketpaw_ee.cloud.models.fabric_ingest_state import FabricIngestConfig

    cfg = await FabricIngestConfig.find_one(FabricIngestConfig.workspace == workspace_id)
    if cfg is None:
        return []
    return [m.model_dump() for m in cfg.mappings]


async def upsert_mapping(workspace_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Create-or-replace one mapping, keyed on ``collection``.

    ``spec`` is validated through the DTO at entry (cloud rule §6) so a
    malformed mapping never lands in the stored config. Creates the
    workspace's config row on first authoring. Returns the stored mapping.
    """
    from pocketpaw_ee.cloud.fabric_ingest.dto import FieldMappingSpec
    from pocketpaw_ee.cloud.models.fabric_ingest_state import (
        FabricFieldMapping,
        FabricIngestConfig,
    )

    validated = FieldMappingSpec.model_validate(spec)
    mapping = FabricFieldMapping.model_validate(validated.model_dump())

    cfg = await FabricIngestConfig.find_one(FabricIngestConfig.workspace == workspace_id)
    if cfg is None:
        cfg = FabricIngestConfig(workspace=workspace_id, mappings=[mapping])
        await cfg.insert()
        return mapping.model_dump()

    replaced = False
    mappings: list[FabricFieldMapping] = []
    for existing in cfg.mappings:
        if existing.collection == mapping.collection:
            mappings.append(mapping)
            replaced = True
        else:
            mappings.append(existing)
    if not replaced:
        mappings.append(mapping)
    cfg.mappings = mappings
    await cfg.save()  # no-event: config authoring, surfaced via the router
    return mapping.model_dump()


async def delete_mapping(workspace_id: str, collection: str) -> bool:
    """Remove the mapping keyed on ``collection``. Returns True if removed."""
    from pocketpaw_ee.cloud.models.fabric_ingest_state import FabricIngestConfig

    cfg = await FabricIngestConfig.find_one(FabricIngestConfig.workspace == workspace_id)
    if cfg is None:
        return False
    kept = [m for m in cfg.mappings if m.collection != collection]
    if len(kept) == len(cfg.mappings):
        return False
    cfg.mappings = kept
    await cfg.save()  # no-event: config authoring, surfaced via the router
    return True


# --------------------------------------------------------------------------
# Default reader + store (lazy — keep google + OSS imports out of unit tests)
# --------------------------------------------------------------------------


def _default_store(workspace_id: str | None = None) -> StoreLike:
    """The OSS FabricStore for ``workspace_id``. Imported lazily so a unit test
    passing a fake store never pulls the OSS store stack.

    ISO: ingest runs on a background/HTTP path with no ``current_workspace``
    ContextVar, so the caller threads its workspace in. Under
    ``POCKETPAW_REQUIRE_WORKSPACE_SCOPE`` a missing one fail-closes."""
    from pocketpaw.stores import get_fabric_store

    return get_fabric_store(workspace_id=workspace_id or None)


def _default_reader() -> FirestoreReader:
    """Construct the real google-cloud-firestore-backed reader.

    Lazy-imports ``google.cloud.firestore`` and raises a clear, actionable
    error if the optional dependency is not installed. Tests never reach here —
    they inject a fake reader.
    """
    from pocketpaw_ee.cloud.fabric_ingest.firestore_reader import GoogleFirestoreReader

    return GoogleFirestoreReader()


__all__ = [
    "FirestoreReader",
    "delete_mapping",
    "ingest_collection",
    "list_ingest_sources",
    "list_mappings",
    "run_ingest_sweep",
    "upsert_mapping",
]
