"""MongoDB connection and Beanie ODM initialization.

2026-07-22 (fix/starter-project-collision): added
``_drop_legacy_code_project_index`` and called it after ``init_beanie``, beside
the invite-token reconcile and for the identical reason. ``CodeProject``'s
registry key grew a ``registry_key`` column so two projects can be built from
one starter template; the superseded four-column unique index
``ws_user_provider_repo_unique`` would still reject that second row, and Beanie
never drops an index the model stopped declaring. Without this the fix passes on
mongomock and 500s in production.

2026-07-15 (fix/workspace-vm-map-to-db): added ``migrate_workspace_vm_map_to_db``
— a best-effort, one-time boot task that imports the captain's existing
workspace→VM entries from the legacy local JSON file
(``~/.pocketpaw/daytona_workspace_vm_map.json``) into the new ``workspace_vms``
Mongo collection, then renames the file to ``.migrated`` so it never re-imports.
Skips any workspace whose DB row already exists (never clobbers newer DB state),
wraps everything in try/except so a migration hiccup can't block boot, and is
called once from ``init_cloud_db`` after Beanie is initialized.

2026-06-26 (ART-4): added ``is_multi_tenant_cloud()`` — the single, named home
for the established "this process is serving tenants" signal
(``get_client() is not None``, set exactly when ``init_cloud_db`` ran). The
agent cwd jail (ART-2) and the new cloud-storage boot guard both read it instead
of re-spelling the ``get_client()`` truthiness check inline. ``init_cloud_db``
also now calls ``verify_cloud_storage_backend()`` right after the memory guard so
a cloud deploy missing the S3 upload adapter is loud (warn, or a hard boot
failure under ``POCKETPAW_REQUIRE_S3_IN_CLOUD``) instead of silently writing
"blob" artifacts to local disk.

2026-06-07: added ``_drop_legacy_invite_token_index`` and called it after
``init_beanie``. The invite-token hashing rollout dropped ``unique=True``
from ``Invite.token`` (uniqueness moved to ``token_hash``), but Beanie only
*creates* indexes from the model — it never drops ones that disappear. The
old non-sparse unique index ``token_1`` therefore survived in every existing
deployment, and because new invites persist ``token=None``, the second new
invite collided on ``{ token: null }`` with a ``DuplicateKeyError`` that
escaped as an unhandled 500 on ``POST /workspaces/{id}/invites``. This
reconciles the live schema with the current model on startup.
"""

from __future__ import annotations

import logging

from beanie import init_beanie
from pymongo import AsyncMongoClient

logger = logging.getLogger(__name__)

_client: AsyncMongoClient | None = None


async def _drop_legacy_invite_token_index(db) -> None:  # type: ignore[no-untyped-def]
    """Drop the stale ``token_1`` unique index on ``invites`` if present.

    The current ``Invite`` model declares uniqueness on ``token_hash``, not
    ``token`` — ``token`` is a nullable legacy column. A leftover unique
    index on ``token`` makes ``token: null`` collide across new invites
    (which all persist ``token=None``), so the insert raises
    ``DuplicateKeyError``. Beanie never removes indexes the model no longer
    declares, so we reconcile here.

    Idempotent and best-effort: only drops an index that is exactly the
    legacy single-field unique ``token`` index, logs what it did, and never
    raises into startup (a failed drop must not take the app down — the
    service-layer guard still converts the resulting collision into a
    handled 409).
    """
    try:
        coll = db["invites"]
        info = await coll.index_information()
    except Exception:  # pragma: no cover - startup must not fail on probe
        logger.exception("Could not read invites index information; skipping legacy-index cleanup")
        return

    legacy = info.get("token_1")
    if not legacy:
        return

    key = legacy.get("key")
    # Only touch the exact legacy shape: single-field unique index on `token`,
    # not a TTL index and not a compound index that happens to lead with token.
    is_legacy_shape = (
        list(key) == [("token", 1)]
        and legacy.get("unique") is True
        and "expireAfterSeconds" not in legacy
    )
    if not is_legacy_shape:
        return

    try:
        await coll.drop_index("token_1")
        logger.warning(
            "Dropped stale unique index 'token_1' on invites — the model now "
            "enforces uniqueness via token_hash; the legacy index made new "
            "invites collide on token=null."
        )
    except Exception:  # pragma: no cover - startup must not fail on drop
        logger.exception("Failed to drop legacy 'token_1' index on invites")


async def _drop_legacy_code_project_index(db) -> None:  # type: ignore[no-untyped-def]
    """Drop the superseded ``ws_user_provider_repo_unique`` index on ``code_projects``.

    ``CodeProject`` now keys the registry on
    (workspace_id, user_id, provider, repo, registry_key) — the extra column is
    what lets two starter projects share one template id. The superseded
    four-column index would still reject that second row, and Beanie only ever
    *creates* the indexes a model declares, so it survives in every existing
    deployment. Without this, the starter fix passes on mongomock and fails in
    production with a ``DuplicateKeyError`` on the second create.

    Idempotent and best-effort, exactly like the invite-token reconcile above:
    only drops the precise legacy shape, and never raises into startup.
    """
    try:
        coll = db["code_projects"]
        info = await coll.index_information()
    except Exception:  # pragma: no cover - startup must not fail on probe
        logger.exception(
            "Could not read code_projects index information; skipping legacy-index cleanup"
        )
        return

    legacy = info.get("ws_user_provider_repo_unique")
    if not legacy:
        return

    # Only touch the exact legacy shape: the four-column unique registry key,
    # not some later index that happens to reuse the name.
    is_legacy_shape = (
        list(legacy.get("key", []))
        == [
            ("workspace_id", 1),
            ("user_id", 1),
            ("provider", 1),
            ("repo", 1),
        ]
        and legacy.get("unique") is True
    )
    if not is_legacy_shape:
        return

    try:
        await coll.drop_index("ws_user_provider_repo_unique")
        logger.warning(
            "Dropped superseded unique index 'ws_user_provider_repo_unique' on "
            "code_projects — the registry key now includes registry_key; the old "
            "index made a second project from the same starter collide."
        )
    except Exception:  # pragma: no cover - startup must not fail on drop
        logger.exception(
            "Failed to drop legacy 'ws_user_provider_repo_unique' index on code_projects"
        )


async def migrate_workspace_vm_map_to_db() -> None:
    """One-time import of the legacy workspace→VM JSON map into Mongo.

    The workspace-level VM map used to live in
    ``~/.pocketpaw/daytona_workspace_vm_map.json`` (``ee.cloud.daytona.store``).
    It is now the ``workspace_vms`` Mongo collection. So an existing deploy's
    VMs aren't orphaned by the move, this reads the file (if present), upserts
    each workspace's entry into ``WorkspaceVm`` — SKIPPING any workspace whose
    row already exists so newer DB state is never clobbered — then renames the
    file to ``<name>.migrated`` so it doesn't re-import on the next boot.

    Best-effort: the whole body is wrapped so a migration hiccup never blocks
    boot. Idempotent — once the file is renamed, subsequent calls are no-ops.
    """
    import json

    from pocketpaw_ee.cloud.daytona.store import WS_VM_MAP_PATH
    from pocketpaw_ee.cloud.models.workspace_vm import WorkspaceVm

    try:
        if not WS_VM_MAP_PATH.exists():
            return

        with open(WS_VM_MAP_PATH) as f:
            data = json.load(f)

        if not isinstance(data, dict):
            logger.warning(
                "Legacy workspace VM map at %s is not a dict — skipping migration",
                WS_VM_MAP_PATH,
            )
            data = {}

        imported = 0
        for workspace_id, entry in data.items():
            if not isinstance(entry, dict):
                continue
            existing = await WorkspaceVm.find_one(WorkspaceVm.workspace == workspace_id)
            if existing is not None:
                # Don't clobber newer DB state.
                continue
            doc = WorkspaceVm(
                workspace=workspace_id,
                sandbox_id=entry.get("sandbox_id", ""),
                sandbox_name=entry.get("sandbox_name", ""),
                config=dict(entry.get("config", {}) or {}),
            )
            await doc.insert()
            imported += 1
            logger.info(
                "Migrated workspace VM map for workspace %s (sandbox %s) into Mongo",
                workspace_id,
                doc.sandbox_id,
            )

        # Rename so we never re-import — even if nothing was imported this run
        # (all rows already existed), the file has served its purpose.
        migrated_path = WS_VM_MAP_PATH.with_suffix(WS_VM_MAP_PATH.suffix + ".migrated")
        WS_VM_MAP_PATH.rename(migrated_path)
        logger.info(
            "Workspace VM map migration complete: %d imported, file renamed to %s",
            imported,
            migrated_path.name,
        )
    except Exception:  # pragma: no cover - migration must never block boot
        logger.exception("Workspace VM map migration failed; continuing boot")


async def init_cloud_db(mongo_uri: str = "mongodb://localhost:27017/paw-enterprise") -> None:
    """Initialize Beanie ODM with all document models."""
    global _client

    from pocketpaw_ee.cloud.memory.bootstrap import (
        register_default_backend,
        verify_cloud_memory_backend,
    )
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    _client = AsyncMongoClient(mongo_uri)
    db_name = mongo_uri.rsplit("/", 1)[-1].split("?")[0] or "paw-enterprise"
    db = _client[db_name]

    # Memory-facts doc lives in its own package to avoid circular imports with
    # ee.cloud.models; register it alongside the core documents here.
    documents = [*ALL_DOCUMENTS, MemoryFactDoc]
    await init_beanie(database=db, document_models=documents)
    logger.info("Cloud DB initialized: %s (%d models)", db_name, len(documents))

    # Reconcile a schema drift Beanie can't: drop indexes the model no longer
    # declares. The invite-token hashing rollout left a unique index on the
    # now-nullable `token` column that 500s every second invite.
    await _drop_legacy_invite_token_index(db)
    await _drop_legacy_code_project_index(db)

    # One-time import of the legacy workspace→VM JSON map into Mongo. Runs after
    # init_beanie so the ``workspace_vms`` collection is live. Best-effort — the
    # helper swallows its own errors so a migration hiccup never blocks boot.
    await migrate_workspace_vm_map_to_db()

    # Flip the memory backend AFTER Beanie is initialized so the
    # MongoMemoryStore's first .insert()/.find() call can never race a
    # not-yet-initialized collection. The bootstrap is a no-op until this
    # point, so callers always see a working store.
    register_default_backend()

    # Fail-fast: refuse to boot the cloud on a local memory backend. A
    # POCKETPAW_MEMORY_BACKEND override (or an install failure) would route
    # chat history to disk; the cloud must keep everything in Mongo.
    verify_cloud_memory_backend()

    # Loud guard on the upload/blob backend (ART-4). A cloud deploy that left
    # POCKETPAW_UPLOAD_ADAPTER on its local default would write delivered agent
    # artifacts to the box's disk instead of tenant blob storage — the whole
    # deliver_artifact feature silently no-ops to local disk. WARN by default;
    # POCKETPAW_REQUIRE_S3_IN_CLOUD escalates it to a hard boot failure. Runs
    # AFTER _client is set so the is_multi_tenant_cloud() signal it reads is True.
    from pocketpaw_ee.cloud.uploads.bootstrap import verify_cloud_storage_backend

    verify_cloud_storage_backend()


async def close_cloud_db() -> None:
    """Close the client."""
    global _client
    if _client:
        _client.close()
        _client = None


def get_client() -> AsyncMongoClient | None:
    """Return the current MongoDB client, or None if not initialized."""
    return _client


def is_multi_tenant_cloud() -> bool:
    """``True`` when this process is serving tenants (multi-tenant cloud mode).

    The cloud DB client is set exactly when ``init_cloud_db`` ran
    (``CloudLifecycleHook`` on ``CLOUD_MONGODB_URI``), so ``get_client() is not
    None`` is the authoritative "this process is serving tenants" flag — there is
    no separate cloud-mode env var to invent. OFF cloud (OSS / a process that
    never initialized the cloud DB) it is ``False``.

    Single home for the signal so callers (the ART-2 agent cwd jail, the ART-4
    cloud-storage boot guard) read one name instead of re-spelling the
    ``get_client()`` truthiness check — one place to change if the signal ever
    moves.
    """
    return _client is not None
