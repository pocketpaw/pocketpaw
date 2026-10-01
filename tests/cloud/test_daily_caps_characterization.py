# tests/cloud/test_daily_caps_characterization.py — pins every daily cap's
# allow/deny behaviour at its boundaries, meter by meter.
#
# Created 2026-10-01 (fix/canon-daily-caps, CN-3). Written against the six
# per-meter budgets BEFORE they were folded into one ``metering`` primitive, and
# run green there first. The refactor changed only the adapters (``_spend`` and
# ``_docs``); every assertion passes unchanged on both sides. That is
# what "behaviour must not change for any existing deployment" means here.
#
# Pinned per meter: the claim that EXCEEDS the cap is refused; a refused claim
# is rolled back (proved by raising the cap and claiming again, so the test
# does not read the storage shape); what a cap of ``0`` means (uncapped for
# chat turns and uploads, disabled for the paid extras and guests); a missing
# subject is refused; which way a storage failure falls; subjects are isolated;
# and two concurrent claims at cap-1 admit exactly one.

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from mongomock_motor import AsyncMongoMockClient

pytestmark = pytest.mark.asyncio


@dataclass(frozen=True)
class Meter:
    name: str
    env: str | None  # None = the cap is passed in, not read from env (guest)
    zero_allows: bool  # what a cap of 0 means: True = uncapped, False = disabled
    fail_open: bool
    takes_missing_subject: bool  # False where the caller can never pass one


async def _spend(meter: Meter, subject: Any, cap: int, monkeypatch) -> bool:
    from pocketpaw_ee.cloud.metering import service as metering
    from pocketpaw_ee.cloud.metering.domain import DailyMeter

    if meter.env:
        monkeypatch.setenv(meter.env, str(cap))
    if meter.name == "upload_files":
        monkeypatch.setenv("POCKETPAW_WORKSPACE_UPLOAD_BYTES_DAILY", str(10**15))
        from pocketpaw_ee.cloud.uploads import upload_budget

        return (await upload_budget.try_spend(subject, 1, 1))[0]
    if meter.name == "upload_bytes":
        monkeypatch.setenv("POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY", str(10**9))
        from pocketpaw_ee.cloud.uploads import upload_budget

        return (await upload_budget.try_spend(subject, 0, 1))[0]
    # Every other meter is called exactly the way its production caller calls
    # the primitive: its own cap resolver, subject type and failure mode.
    resolved = {
        "workspace_turns": metering.workspace_turns_cap,
        "file_comprehension": metering.file_comprehension_cap,
        "file_transcription": metering.file_transcription_cap,
        "illustration": metering.illustration_cap,
        "guest_turns": lambda: cap,  # guest_gates passes limits_for(...) as-is
    }[meter.name]()
    return await metering.try_spend(
        subject_type="user" if meter.name == "guest_turns" else "workspace",
        subject_id=subject,
        meter=DailyMeter(meter.name),
        cap=resolved,
        fail_open=meter.name == "workspace_turns",
    )


def _docs() -> list[type]:
    from pocketpaw_ee.cloud.models.daily_usage import DailyUsage

    return [DailyUsage]


# ── the adapter table ends here; nothing below may change in the refactor ──

_METERS = [
    Meter("workspace_turns", "POCKETPAW_WORKSPACE_TURNS_DAILY", True, True, True),
    Meter("upload_files", "POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY", True, True, True),
    Meter("upload_bytes", "POCKETPAW_WORKSPACE_UPLOAD_BYTES_DAILY", True, True, True),
    Meter("file_comprehension", "POCKETPAW_FILE_COMPREHENSION_DAILY", False, False, True),
    Meter("file_transcription", "POCKETPAW_FILE_TRANSCRIPTION_DAILY", False, False, True),
    Meter("illustration", "POCKETPAW_OTHER_HAND_DAILY_ILLUSTRATIONS", False, False, True),
    Meter("guest_turns", None, False, False, False),
]
_IDS = [m.name for m in _METERS]


@pytest.fixture
async def usage_db():
    from beanie import init_beanie

    client = AsyncMongoMockClient()
    db = client[f"test_daily_caps_{uuid.uuid4().hex[:8]}"]
    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]
    docs = _docs()
    await init_beanie(database=db, document_models=docs)
    try:
        yield db
    finally:
        for model in docs:
            for attr in ("_document_settings", "_settings"):
                if hasattr(model, attr):
                    try:
                        delattr(model, attr)
                    except Exception:
                        pass


def _subject() -> str:
    return f"s-{uuid.uuid4().hex[:8]}"


@pytest.mark.parametrize("meter", _METERS, ids=_IDS)
async def test_the_claim_that_exceeds_the_cap_is_refused(usage_db, monkeypatch, meter):
    s = _subject()
    assert await _spend(meter, s, 2, monkeypatch) is True
    assert await _spend(meter, s, 2, monkeypatch) is True
    assert await _spend(meter, s, 2, monkeypatch) is False


@pytest.mark.parametrize("meter", _METERS, ids=_IDS)
async def test_a_refused_claim_does_not_hold_the_slot(usage_db, monkeypatch, meter):
    s = _subject()
    assert await _spend(meter, s, 1, monkeypatch) is True
    for _ in range(3):
        assert await _spend(meter, s, 1, monkeypatch) is False
    # Three refusals rolled back: raising the cap to 2 leaves exactly one slot.
    assert await _spend(meter, s, 2, monkeypatch) is True
    assert await _spend(meter, s, 2, monkeypatch) is False


@pytest.mark.parametrize("meter", _METERS, ids=_IDS)
async def test_what_a_zero_cap_means(usage_db, monkeypatch, meter):
    s = _subject()
    for _ in range(3):
        assert await _spend(meter, s, 0, monkeypatch) is meter.zero_allows


@pytest.mark.parametrize(
    "meter", [m for m in _METERS if m.name == "workspace_turns"], ids=lambda m: m.name
)
async def test_an_uncapped_meter_allows_even_without_a_subject(usage_db, monkeypatch, meter):
    # Upload is excluded on purpose: with the OTHER dimension still capped, a
    # workspace-less batch is refused (only both caps at 0 skip the check).
    assert await _spend(meter, "", 0, monkeypatch) is True
    assert await _spend(meter, None, 0, monkeypatch) is True


@pytest.mark.parametrize(
    "meter", [m for m in _METERS if m.takes_missing_subject], ids=lambda m: m.name
)
async def test_a_missing_subject_is_refused(usage_db, monkeypatch, meter):
    assert await _spend(meter, "", 5, monkeypatch) is False
    assert await _spend(meter, None, 5, monkeypatch) is False


@pytest.mark.parametrize("meter", _METERS, ids=_IDS)
async def test_a_storage_failure_falls_the_meters_way(monkeypatch, meter):
    def _boom(*_a, **_k):
        raise RuntimeError("collection unavailable")

    for doc in _docs():
        monkeypatch.setattr(doc, "get_pymongo_collection", _boom)
    assert await _spend(meter, _subject(), 5, monkeypatch) is meter.fail_open


@pytest.mark.parametrize("meter", _METERS, ids=_IDS)
async def test_subjects_do_not_share_a_counter(usage_db, monkeypatch, meter):
    a, b = _subject(), _subject()
    assert await _spend(meter, a, 1, monkeypatch) is True
    assert await _spend(meter, a, 1, monkeypatch) is False
    assert await _spend(meter, b, 1, monkeypatch) is True


@pytest.mark.parametrize("meter", _METERS, ids=_IDS)
async def test_two_concurrent_claims_at_cap_minus_one_admit_exactly_one(
    usage_db, monkeypatch, meter
):
    s = _subject()
    for _ in range(2):
        assert await _spend(meter, s, 3, monkeypatch) is True
    results = await asyncio.gather(
        _spend(meter, s, 3, monkeypatch), _spend(meter, s, 3, monkeypatch)
    )
    assert sorted(results) == [False, True]
