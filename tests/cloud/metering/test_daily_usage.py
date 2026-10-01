# tests/cloud/metering/test_daily_usage.py — the one daily usage primitive.
#
# Created 2026-10-01 (fix/canon-daily-caps, CN-3). The per-meter boundary
# behaviour is pinned in tests/cloud/test_daily_caps_characterization.py; this
# file covers what is the primitive's own: the legacy cap translation, the
# refund (explicit day, clamp at zero, never upserts), multi-unit claims, and
# that an uncapped meter touches no counter.

from __future__ import annotations

import logging
import uuid

import pytest
from mongomock_motor import AsyncMongoMockClient
from pocketpaw_ee.cloud.metering import service as metering
from pocketpaw_ee.cloud.metering.domain import DailyMeter
from pocketpaw_ee.cloud.models.daily_usage import DailyUsage


_W = {"subject_type": "workspace"}


@pytest.fixture
async def usage_db():
    from beanie import init_beanie

    client = AsyncMongoMockClient()
    db = client[f"test_daily_usage_{uuid.uuid4().hex[:8]}"]
    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]
    await init_beanie(database=db, document_models=[DailyUsage])
    try:
        yield db
    finally:
        for attr in ("_document_settings", "_settings"):
            if hasattr(DailyUsage, attr):
                try:
                    delattr(DailyUsage, attr)
                except Exception:
                    pass


@pytest.mark.parametrize(
    ("env", "resolver"),
    [
        ("POCKETPAW_WORKSPACE_TURNS_DAILY", metering.workspace_turns_cap),
        ("POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY", metering.upload_files_cap),
        ("POCKETPAW_WORKSPACE_UPLOAD_BYTES_DAILY", metering.upload_bytes_cap),
    ],
)
def test_a_legacy_zero_means_uncapped_and_says_so_once(monkeypatch, caplog, env, resolver):
    monkeypatch.setattr(metering, "_logged_legacy_zero", set())
    for raw in ("0", "-3"):
        monkeypatch.setenv(env, raw)
        with caplog.at_level(logging.INFO, logger=metering.logger.name):
            assert resolver() is None
    notes = [r for r in caplog.records if env in r.getMessage()]
    assert len(notes) == 1, "the legacy meaning is logged once, not per call"


@pytest.mark.parametrize(
    ("env", "resolver"),
    [
        ("POCKETPAW_FILE_COMPREHENSION_DAILY", metering.file_comprehension_cap),
        ("POCKETPAW_FILE_TRANSCRIPTION_DAILY", metering.file_transcription_cap),
        ("POCKETPAW_OTHER_HAND_DAILY_ILLUSTRATIONS", metering.illustration_cap),
    ],
)
def test_zero_still_disables_the_paid_extras(monkeypatch, env, resolver):
    monkeypatch.setenv(env, "0")
    assert resolver() == 0
    monkeypatch.setenv(env, "-1")
    assert resolver() == 0


def test_the_defaults_are_unchanged(monkeypatch):
    for env in (
        "POCKETPAW_WORKSPACE_TURNS_DAILY",
        "POCKETPAW_WORKSPACE_UPLOAD_FILES_DAILY",
        "POCKETPAW_WORKSPACE_UPLOAD_BYTES_DAILY",
        "POCKETPAW_FILE_COMPREHENSION_DAILY",
        "POCKETPAW_FILE_TRANSCRIPTION_DAILY",
        "POCKETPAW_OTHER_HAND_DAILY_ILLUSTRATIONS",
    ):
        monkeypatch.delenv(env, raising=False)
    assert metering.workspace_turns_cap() == 500
    assert metering.upload_files_cap() == 2000
    assert metering.upload_bytes_cap() == 20_000_000_000
    assert metering.file_comprehension_cap() == 500
    assert metering.file_transcription_cap() == 100
    assert metering.illustration_cap() == 20


async def test_an_uncapped_meter_touches_no_counter(usage_db):
    for _ in range(3):
        assert await metering.try_spend(
            **_W, subject_id="w1", meter=DailyMeter.WORKSPACE_TURNS, cap=None
        )
    assert await DailyUsage.get_pymongo_collection().count_documents({}) == 0


async def test_a_multi_unit_claim_is_all_or_nothing(usage_db):
    kw = {**_W, "subject_id": "w1", "meter": DailyMeter.UPLOAD_BYTES, "cap": 1000}
    assert await metering.try_spend(**kw, amount=900) is True
    assert await metering.try_spend(**kw, amount=200) is False
    assert await metering.used(**_W, subject_id="w1", meter=DailyMeter.UPLOAD_BYTES) == 900


async def test_meters_and_subject_types_do_not_share_a_row(usage_db):
    assert await metering.try_spend(**_W, subject_id="x", meter=DailyMeter.ILLUSTRATION, cap=1)
    assert await metering.try_spend(
        **_W, subject_id="x", meter=DailyMeter.FILE_COMPREHENSION, cap=1
    )
    assert await metering.try_spend(
        subject_type="user", subject_id="x", meter=DailyMeter.ILLUSTRATION, cap=1
    )


async def test_refund_targets_the_day_it_is_given(usage_db):
    kw = {**_W, "subject_id": "w1", "meter": DailyMeter.UPLOAD_FILES}
    assert await metering.try_spend(**kw, amount=3, cap=10)
    await metering.refund(**kw, amount=2, day="1999-01-01")
    assert await metering.used(**kw) == 3, "a refund for another day touched today"
    await metering.refund(**kw, amount=2, day=metering.today())
    assert await metering.used(**kw) == 1


async def test_refund_clamps_at_zero_and_never_upserts(usage_db):
    kw = {**_W, "subject_id": "w1", "meter": DailyMeter.UPLOAD_FILES}
    await metering.refund(**kw, amount=5, day=metering.today())
    assert await DailyUsage.get_pymongo_collection().count_documents({}) == 0
    assert await metering.try_spend(**kw, amount=1, cap=10)
    await metering.refund(**kw, amount=5, day=metering.today())
    await metering.refund(**kw, amount=5, day=metering.today())
    assert await metering.used(**kw) == 0, "a double refund minted quota"


async def test_refund_swallows_a_storage_error(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("mongo is down")

    monkeypatch.setattr(DailyUsage, "get_pymongo_collection", _boom)
    await metering.refund(
        **_W, subject_id="w1", meter=DailyMeter.UPLOAD_FILES, amount=1, day="2026-10-01"
    )
