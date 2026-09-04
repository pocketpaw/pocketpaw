"""MongoDB connection and Beanie ODM initialization.

2026-09-04 (fix/pool-and-body-ceilings, backend-perf H6): the client is built
with explicit timeouts. It had none, so it ran on PyMongo's defaults, and two of
those are the wrong shape for a request-serving process. ``serverSelectionTimeoutMS``
defaults to 30s, which turns a brief Mongo blip into 30-second request hangs
instead of fast failures — and every hung request holds a worker slot for the
duration. ``socketTimeoutMS`` defaults to None, so a socket that stops
responding without closing hangs its operation forever. ``waitQueueTimeoutMS``
is also None, meaning a saturated pool queues callers indefinitely rather than
telling them.

Note for anyone reading the audit alongside this: the finding says the client
has "no pool tuning", which is true, but the pool is NOT unbounded — PyMongo
already defaults ``maxPoolSize`` to 100. It is set explicitly below at that same
100 so the number is visible and tunable, which changes nothing on any existing
deploy. The unbounded pool in that finding is the Redis one, fixed separately in
``_core/redis_client.py``.

Options already present in the connection URI WIN over these defaults, which is
the opposite of PyMongo's own precedence — see ``_client_options``.

2026-09-04 (fix/wallet-migration-guard): ``init_cloud_db`` now awaits
``credits.service.verify_wallet_migrated()`` alongside the memory and storage
guards, and refuses the boot when any wallet document still carries a pre-micro
field. The rename in #2064 shipped without its migration being run; the ledger
endpoint 500d, but the balance rows failed SILENTLY — ``balance_micro`` defaults
to 0, so every customer read as broke and was refused runs they could pay for.
A schema rename in a money path needs a boot-time tie to its migration, because
half of it does not fail loudly on its own.

2026-09-04 (fix/proxy-model-prices): ``init_cloud_db`` also loads the LiteLLM
proxy's own per-model rates and registers them as the top rung of the pricing
ladder. Runs were priced from public lists that cannot know our negotiated
rates, and a model only our proxy serves appeared in none of them — it priced
as None and billed nothing. Fails open: no proxy means the public lists.

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


# Defaults applied to the Mongo client when the connection URI does not already
# say otherwise. Each exists to convert an unbounded wait into a fast, visible
# failure — a request that hangs holds a worker slot, and a single-process
# deploy has very few of them.
_MONGO_DEFAULTS: dict[str, int] = {
    # How long to hunt for a reachable server before giving up. PyMongo's 30s
    # default is tuned for a batch job that would rather wait than fail; a
    # request path would rather fail in five and let the client retry.
    "serverSelectionTimeoutMS": 5_000,
    "connectTimeoutMS": 5_000,
    # Ceiling on a single operation's socket read. PyMongo's default is None,
    # i.e. wait forever. 30s is generous for every query this app issues — it
    # opens no change streams and no tailable cursors, both of which would be
    # severed by a socket timeout, so the only thing this can cut short is a
    # query that has already gone very wrong.
    "socketTimeoutMS": 30_000,
    # How long a caller waits for a free pooled connection. Also None by
    # default, which means a saturated pool queues callers silently instead of
    # telling anyone it is saturated.
    "waitQueueTimeoutMS": 10_000,
    # PyMongo's own default, set explicitly so the number is visible and
    # tunable from the URI. Changes nothing on any existing deploy.
    "maxPoolSize": 100,
}


def _client_options(mongo_uri: str) -> dict[str, int]:
    """Client kwargs, minus anything the URI already configures.

    PyMongo's own precedence is the other way round: a keyword argument beats
    the same option in the URI. Applying these blind would therefore let a
    library default silently overrule an operator who deliberately tuned the
    connection string — and they would have no way to win the argument, because
    the URI is the only knob a deploy actually exposes. So the URI wins here.

    Option names in a MongoDB URI are case-insensitive, so the comparison is
    lowered on both sides.
    """
    query = mongo_uri.partition("?")[2]
    present = {pair.partition("=")[0].strip().lower() for pair in query.split("&") if pair.strip()}
    return {key: value for key, value in _MONGO_DEFAULTS.items() if key.lower() not in present}


async def init_cloud_db(mongo_uri: str = "mongodb://localhost:27017/paw-enterprise") -> None:
    """Initialize Beanie ODM with all document models."""
    global _client

    from pocketpaw_ee.cloud.memory.bootstrap import (
        register_default_backend,
        verify_cloud_memory_backend,
    )
    from pocketpaw_ee.cloud.memory.documents import MemoryFactDoc
    from pocketpaw_ee.cloud.models import ALL_DOCUMENTS

    _client = AsyncMongoClient(mongo_uri, **_client_options(mongo_uri))
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

    # Fail-fast: refuse to boot on a credit wallet the micro-credit migration has
    # not converted. This build reads ``balance_micro``; a row that still holds
    # ``balance_credits`` parses as an EMPTY wallet because the new field defaults
    # to 0, so every customer reads as broke and the run-start gate refuses them —
    # with nothing logged. It happened on 2026-09-04: the rename deployed, the
    # migration did not, and the only audible symptom was a 500 on the ledger
    # endpoint. Ties the code to its migration so the pair can only ship together.
    from pocketpaw_ee.cloud.credits.service import verify_wallet_migrated

    await verify_wallet_migrated()

    # Price runs from OUR proxy's rates before falling back to the public price
    # lists. A model we serve at a negotiated rate otherwise bills at list, and a
    # model that exists only on our proxy is in no public list at all — it prices
    # as None and bills ZERO. Registered here so every consumer of the pricing
    # ladder gets it; the snapshot refreshes again on each metering sweep. Both
    # calls fail open: no proxy means the public lists, exactly as before.
    from pocketpaw_ee.cloud.metering import proxy_prices

    await proxy_prices.refresh(force=True)
    proxy_prices.register()


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
