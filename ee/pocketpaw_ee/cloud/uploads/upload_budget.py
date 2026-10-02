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
# the file-comprehension meter, where ``0`` disables the feature and so blocks
# everything, and the divergence is deliberate: comprehension is an extra a
# workspace can live without for a day, whereas uploading IS the product. An
# env typo that reads as ``0`` must not take file upload off the air.
#
# It fails OPEN on a database error, logged at WARNING. The upload path writes
# its metadata row to the same Mongo on the next statement, so a database that
# cannot serve this counter cannot complete the upload either — refusing here
# would protect nothing (see the chat-turn meter for the harness that proved it).
#
# Updated 2026-10-01 (CN-3): the counters are the shared ``metering.service``
# daily primitive — two meters, ``upload_files`` and ``upload_bytes`` — and the
# caps come from its resolvers (same env vars; ``0`` still means uncapped for a
# dimension, mapped to the primitive's ``None``). This module only composes the
# two: claim files, then bytes; a bytes refusal refunds the files claim, so a
# refused batch holds nothing. Two drifts from the one-row ``$inc`` this
# replaced, both harmless: the two increments are separate operations, and an
# uncapped dimension is no longer counted at all.
#
# 2026-09-14 (feat/uploads-multipart-endpoints): added ``release`` and
# ``today()``. A multipart upload claims its budget at INIT — before any bytes
# move, which is the point of the feature — and the claim has to be given back
# when the session is aborted or expires without completing, or an abandoned
# 5 GB upload permanently consumes a workspace's day.
#
# ``release`` takes the DAY explicitly and this is the whole reason it is not
# just ``try_spend`` with negative arguments. The counter is keyed
# ``{workspace}:{utc_day}``, and a multipart session lives for 7 days: a
# session opened on the 14th and aborted on the 20th, refunded against "today",
# would decrement a row the claim was never made against and hand the workspace
# free quota on a day it had not spent. Callers persist the day they claimed on
# and pass it back. Decrementing a past day's row is harmless — that day is
# over and nothing reads it.
#
# It clamps at zero rather than letting a row go negative, so a double refund
# (two aborts racing, an abort after an expiry sweep) cannot mint quota either.

from __future__ import annotations

from pocketpaw_ee.cloud.metering import service as metering
from pocketpaw_ee.cloud.metering.domain import DailyMeter


def today() -> str:
    """The UTC day a claim made right now is charged to.

    Public because a caller that holds a claim across requests (multipart
    sessions) must persist which day it spent on in order to refund it there.
    """
    return metering.today()


async def release(workspace_id: str | None, day: str, files: int, size_bytes: int) -> None:
    """Give back a claim made by ``try_spend`` on ``day``. Best-effort, clamped
    at zero, never raises — see the module header for why the day travels with
    the claim instead of being read as "now"."""
    for meter, amount in ((DailyMeter.UPLOAD_FILES, files), (DailyMeter.UPLOAD_BYTES, size_bytes)):
        await metering.refund(
            subject_type="workspace", subject_id=workspace_id, meter=meter, amount=amount, day=day
        )


async def try_spend(workspace_id: str | None, files: int, size_bytes: int) -> tuple[bool, str]:
    """Claim ``files`` files and ``size_bytes`` bytes against today's budget.

    Returns ``(allowed, reason)``; ``reason`` is empty when allowed and names
    the dimension that tripped otherwise (``"files"``, ``"bytes"`` or
    ``"workspace"``), so the caller can say which ceiling the user hit.
    Fails OPEN on a storage error, like the chat-turn meter: the metadata row
    is written to the same Mongo on the next statement.
    """
    file_cap = metering.upload_files_cap()
    byte_cap = metering.upload_bytes_cap()
    if file_cap is None and byte_cap is None:
        return True, ""
    if files <= 0 and size_bytes <= 0:
        return True, ""
    if not workspace_id:
        # No tenant means no counter to charge, and an uncharged upload is
        # exactly what this ceiling exists to prevent.
        return False, "workspace"

    day = today()
    if not await metering.try_spend(
        subject_type="workspace",
        subject_id=workspace_id,
        meter=DailyMeter.UPLOAD_FILES,
        amount=files,
        cap=file_cap,
        fail_open=True,
    ):
        return False, "files"
    if not await metering.try_spend(
        subject_type="workspace",
        subject_id=workspace_id,
        meter=DailyMeter.UPLOAD_BYTES,
        amount=size_bytes,
        cap=byte_cap,
        fail_open=True,
    ):
        await release(workspace_id, day, files, 0)
        return False, "bytes"
    return True, ""


__all__ = ["release", "today", "try_spend"]
