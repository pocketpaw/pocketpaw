"""MongoDB connection and Beanie ODM initialization.

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


async def init_cloud_db(mongo_uri: str = "mongodb://localhost:27017/paw-enterprise") -> None:
    """Initialize Beanie ODM with all document models."""
    global _client

    from pocketpaw_ee.cloud.memory.bootstrap import register_default_backend
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

    # Flip the memory backend AFTER Beanie is initialized so the
    # MongoMemoryStore's first .insert()/.find() call can never race a
    # not-yet-initialized collection. The bootstrap is a no-op until this
    # point, so callers always see a working store.
    register_default_backend()


async def close_cloud_db() -> None:
    """Close the client."""
    global _client
    if _client:
        _client.close()
        _client = None


def get_client() -> AsyncMongoClient | None:
    """Return the current MongoDB client, or None if not initialized."""
    return _client
