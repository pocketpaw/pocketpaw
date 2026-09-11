# ee/pocketpaw_ee/cloud/uploads/upload_budget.py — a hard daily ceiling on how
# much one workspace may upload.
#
# Created 2026-09-11 (feat/abuse-budgets).
#
# The gap this closes: the per-plan storage cap in ``cloud/storage/service.py``
# is gated on ``billing_enforced``, which defaults to False. A deployment that
# has not switched billing on therefore has NO per-workspace bound on uploads.
# What is left is per-REQUEST only — 25 MiB a file, 50 files a batch, a ~1.27
# GB body ceiling — and nothing at all bounds how many requests one account
# sends. The per-IP limiter in ``dashboard_auth`` is 10 req/s, which is 10
# requests per second of unbounded uploading.
#
# Deliberately coarse, and deliberately NOT billing: one counter per workspace
# per UTC day, checked and incremented in ONE atomic update. It does not need
# to be exact under concurrency, it needs to make an order-of-magnitude abuse
# impossible.
#
# TWO ceilings, both in the same row and the same ``$inc``: a file count and a
# byte total. A count alone is beaten by fifty 25 MiB files; a byte total alone
# is beaten by a hundred thousand one-byte files, each of which still costs a
# Mongo row, an object in the bucket and a FileReady event.
#
# NOT gated on ``billing_enforced``, and that is the whole point — it is an
# abuse ceiling, not a plan. A workspace that pays for more storage raises its
# plan cap; this one only stops a single day from being pathological, so the
# defaults sit far above any real day's use.
#
# ``0`` means NO CAP for that dimension. This DIVERGES from
# ``comprehension_budget``, where ``0`` disables the feature and so blocks
# everything, and the divergence is deliberate: comprehension is an extra a
# workspace can live without for a day, whereas uploading IS the product. An
# env typo that reads as ``0`` must not take file upload off the air.
#
# It fails CLOSED on a database error, like its siblings. That costs nothing
# here: the upload path writes its metadata row to the same Mongo, so a
# database that cannot serve this counter cannot complete the upload either.
#
# Structure copied from ``uploads/comprehension_budget.py``, including the
# increment-then-compare ordering and the rollback of an over-cap claim.

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

from pymongo import ReturnDocument

from pocketpaw_ee.cloud.models.workspace_upload_usage import WorkspaceUploadUsage

logger = logging.getLogger(__name__)

_ENV_FILES = "POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY"
_ENV_BYTES = "POCKETPAW_WORKSPACE_UPLOAD_BYTES_DAILY"

#: Files per workspace per UTC day. 2000 is roughly forty full 50-file batches
#: — far past any real day of work, and far short of a script left running.
_DEFAULT_FILES = 2000

#: Bytes per workspace per UTC day. 20 GB: four times the Free plan's entire
#: 5 GB storage cap, so it never fires before a plan cap would, and it still
#: bounds an unbilled deployment's exposure to one workspace-day.
_DEFAULT_BYTES = 20_000_000_000


def _cap(name: str, default: int) -> int:
    """Read an env ceiling. ``0`` means uncapped; a bad value means the default.

    A non-integer is a misconfiguration, not an instruction — warn and use the
    default rather than reading ``"twenty gigs"`` as ``0`` and quietly removing
    the ceiling for the whole deployment.
    """
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("%s is not an integer (%r) — using the default", name, raw)
        return default


def daily_file_cap() -> int:
    return _cap(_ENV_FILES, _DEFAULT_FILES)


def daily_byte_cap() -> int:
    return _cap(_ENV_BYTES, _DEFAULT_BYTES)


def _today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


async def try_spend(workspace_id: str | None, files: int, size_bytes: int) -> tuple[bool, str]:
    """Claim ``files`` files and ``size_bytes`` bytes against today's budget.

    Returns ``(allowed, reason)``; ``reason`` is empty when allowed and names
    the dimension that tripped otherwise, so the caller can say which ceiling
    the user hit.

    Increments FIRST and compares after. The increment is the atomic part: a
    check-then-increment lets two concurrent batches both read "just under" and
    both spend. An over-cap claim is rolled back in full, so a refused batch
    does not hold a slot a later one could have used.
    """
    file_cap = daily_file_cap()
    byte_cap = daily_byte_cap()
    if file_cap <= 0 and byte_cap <= 0:
        return True, ""
    if files <= 0 and size_bytes <= 0:
        return True, ""

    if not workspace_id:
        # No tenant means no counter to charge, and an uncharged upload is
        # exactly what this file exists to prevent.
        logger.warning("upload refused — no workspace to charge")
        return False, "workspace"

    day = _today()
    key = f"{workspace_id}:{day}"
    try:
        # ``get_pymongo_collection``, NOT ``get_motor_collection`` — the latter
        # is beanie 1.x and this repo is on 2.1.0. Getting it wrong is invisible
        # in the worst way: the AttributeError lands in the fail-closed
        # ``except`` below and every upload is refused, which reads as "uploads
        # are broken" rather than as a bug in the ceiling.
        coll = WorkspaceUploadUsage.get_pymongo_collection()
        doc = await coll.find_one_and_update(
            {"key": key},
            {
                "$inc": {"used": int(files), "bytes_used": int(size_bytes)},
                "$setOnInsert": {
                    "workspace": workspace_id,
                    "day": day,
                    "createdAt": datetime.now(UTC),
                },
                "$set": {"updatedAt": datetime.now(UTC)},
            },
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        if doc is None:
            # A successful update with no returned document should not happen,
            # but reading it as "0 spent" would be a permanently open gate.
            raise RuntimeError("upload budget upsert returned no document")
        spent_files = int(doc.get("used", 0))
        spent_bytes = int(doc.get("bytes_used", 0))
    except Exception:
        logger.warning(
            "upload budget unavailable for workspace=%s; refusing", workspace_id, exc_info=True
        )
        return False, "unavailable"

    over = ""
    if file_cap > 0 and spent_files > file_cap:
        over = "files"
    elif byte_cap > 0 and spent_bytes > byte_cap:
        over = "bytes"
    if not over:
        return True, ""

    try:
        await coll.update_one(
            {"key": key}, {"$inc": {"used": -int(files), "bytes_used": -int(size_bytes)}}
        )
    except Exception:
        logger.debug("could not roll back an over-cap upload claim", exc_info=True)
    return False, over


__all__ = ["daily_byte_cap", "daily_file_cap", "try_spend"]
