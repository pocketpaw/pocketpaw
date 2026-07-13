# fabric_ingest — Generic source→Fabric ingestion worker (Firestore + connectors).
# Created: 2026-06-11 — generic Firestore→Fabric ingestion worker.
# Updated: 2026-07-11 (feat/real-pipeline-s1) — the transform surface. Mappings
#   now carry a ``source_kind`` ("firestore" | "connector"): connector mappings
#   dispatch through the OSS ``FABRIC_INGESTORS`` registry with the workspace's
#   own WorkspaceConnector binding (per-user token via ``wc.user_id``). New
#   ``router.py`` exposes author/list/delete + run-now over the mappings
#   (mounted at /api/v1/fabric/ingest/*), backed by new service helpers
#   (``list_mappings`` / ``upsert_mapping`` / ``delete_mapping``).
# Updated: 2026-06-11 (rebase onto dev) — the upsert path now delegates to the
#   canonical OSS connector→Fabric mapper (pocketpaw.connectors.fabric_ingest,
#   landed in the Calendar ingestion PR #1418) instead of a private loop.
"""Continuously mirror selected sources into Fabric objects.

Fully generic: the per-deployment mapping (which sources, which Fabric
object types, field maps, cursor fields, link rules) lives in a per-workspace
``FabricIngestConfig`` document. NOTHING domain-specific lives in this package.
Two source kinds per mapping: ``"firestore"`` (the original reader path) and
``"connector"`` (records pulled through the OSS connector→Fabric ingestor
registry — gcalendar first).

Pieces
------
* ``router`` — the transform surface: ``GET/POST/DELETE
  /fabric/ingest/mappings`` (author the workspace's mappings) and
  ``POST /fabric/ingest/run`` (run one mapping now). Same gates as the fabric
  router: license + plan feature ``fabric``, ``fabric.read``/``fabric.write``
  per route, workspace from ``current_workspace_id``.
* ``service.ingest_collection`` — mirror ONE source into Fabric objects for
  one workspace. Reads the mapping from the workspace's config; a firestore
  mapping upserts each document through the canonical OSS connector→Fabric
  mapper (``pocketpaw.connectors.fabric_ingest.ingest_records`` — the same
  loop the Calendar connector ingestion uses), keyed on the doc's source path
  so a re-run updates rather than duplicates; a connector mapping resolves the
  workspace's enabled ``WorkspaceConnector`` row and calls the registered
  ingestor with that row's ``user_id`` (misconfiguration is an error result,
  never a raise). Every object is stamped with ``workspace_id`` +
  ``source_connector`` + ``source_id``; the firestore path advances a REAL
  high-water cursor taken from document data and applies any link rules.
  Backfill on first run, incremental thereafter.
* ``service.run_ingest_sweep`` — fan ``ingest_collection`` out across every
  configured (workspace, collection) pair under a bounded concurrency cap;
  per-collection failures are isolated.
* ``service.FirestoreReader`` — the narrow read Protocol the worker depends on,
  so tests inject fakes with no google credentials.
* ``firestore_reader.GoogleFirestoreReader`` — the concrete reader over the
  optional ``google-cloud-firestore`` dependency (lazy import + clear install
  error).
* ``scheduler.FabricIngestScheduler`` — the periodic background sweep (every
  5 min), gated on ``POCKETPAW_CLOUD_SCHEDULER_ENABLED`` and wired into
  ``mount_cloud``.
* ``models.fabric_ingest_state.FabricIngestState`` — per-(workspace, source)
  sync status (status, backfill_done, last_sync_at, cursor, objects_ingested).
* ``models.fabric_ingest_state.FabricIngestConfig`` — the per-workspace mapping.

Cursor — the deliberate improvement over MemberIngestState
----------------------------------------------------------
The incremental cursor is a REAL high-water mark from document data — the
mapping's configured ``cursor_field`` on the newest document seen (falling back
to the snapshot ``update_time``). It is NOT the run's wall clock. A document
that lands late but carries an older updated-at is still picked up, and a re-run
never re-scans a wall-clock window. MemberIngestState's wall-clock cursor is a
known flaw and is deliberately not copied here.

Follow-ups (v1 ships a solid-but-bounded slice):
* Batched writes. ``FabricStore.create_object`` commits per row today —
  acceptable for v1, noted as a TODO; a batch-insert path would cut commits on
  a large backfill.
* A status/read router (``GET /api/v1/fabric-ingest/status``) for operator
  visibility over ``FabricIngestState``.
* A first-config-write trigger that kicks an immediate backfill instead of
  waiting for the next sweep tick.
"""
