# test_transcription.py — T2 "Audio/video transcription at ingest".
# Created: 2026-08-29 (T2).
#
# Three failure shapes are being defended against here, and they are the ones
# this codebase keeps shipping — a feature that reads as SWITCHED OFF rather
# than broken:
#
#   (1) a fail-closed budget that refuses everything because of a wrong API
#       name. ``TestTheDailyCap`` reaches the degraded-database case honestly
#       (no Beanie binding at all) instead of simulating it, and
#       ``test_the_counter_uses_the_beanie_2x_collection_api`` asserts the
#       exact attribute — ``get_pymongo_collection``, never
#       ``get_motor_collection`` — that turned an identical budget into a
#       silent refusal of every claim;
#   (2) a wrong or defaulted model parameter. ``test_the_language_is_sent_as_
#       an_explicit_null`` is the load-bearing one: wizper's ``language``
#       defaults to ``"en"``, and with the default a Spanish recording comes
#       back as fluent English that is then summarised, tagged and indexed as
#       if it were true. Nothing raises. Nothing looks wrong;
#   (3) a ceiling that never fires, so a three-hour file is discovered
#       mid-bill. ``TestTheCeiling`` puts every fixture OVER the limit it is
#       named for, so the branch it exists to prove actually runs.
#
# The seam tests (``TestTheFalSeam``) exist because every other test here uses
# a double. A double proves the code calls what we told it to; it cannot prove
# the SDK still has those methods, so those are asserted against the real
# ``fal_client``.
#
# NOT covered by any test: the live fal network call. It was verified by hand
# on 2026-08-29 — ``fal-ai/wizper`` transcribed a 21.6 s clip, an mp4 VIDEO
# uploaded through ``fal_client.upload_file``, and a 15.8-minute mp3 through
# ``subscribe()`` in 43 s with segments covering the full duration. Measured
# prices are in the module header of ``transcription.py``.
#
# Mutation-tested: ``tests/mutations/transcription.json``.
"""T2: a recording becomes a document, and the bill stays bounded."""

from __future__ import annotations

import asyncio
import inspect
import struct
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pocketpaw_ee.cloud._core.realtime.events import FileReady
from pocketpaw_ee.cloud.extraction.adapter import ExtractionResult
from pocketpaw_ee.cloud.uploads import transcription, transcription_budget
from pocketpaw_ee.cloud.uploads.extracted_text import blob_key, load_extracted_text

from pocketpaw.uploads.file_store import FileRecord
from pocketpaw.uploads.local import LocalStorageAdapter

# ``asyncio_mode = "auto"`` is set repo-wide, so async tests need no marker —
# and this file has sync ones (the argument and seam assertions), which an
# explicit ``pytestmark`` would warn on for every single test.

_STORAGE_KEY = "w1/u1/talk.mp3"

#: Captured at IMPORT time, before the autouse ``no_real_transcription``
#: fixture replaces them. The seam tests read these functions' source, and a
#: monkeypatched attribute would hand them the stub's source instead — a
#: source assertion that silently measures the test double is worse than none.
_REAL_CALL_FAL = transcription._call_fal
_REAL_API_KEY = transcription._api_key


# ---------------------------------------------------------------------------
# Media builders — real container headers, so the ceiling reads a real length
# ---------------------------------------------------------------------------


def _mp3_bytes(seconds: float) -> bytes:
    """A 128 kbps MPEG-1 Layer III stream of ``seconds`` duration."""
    return bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * (int(16_000 * seconds) - 4)


def _mp4_bytes(seconds: float, *, pad: int = 1024) -> bytes:
    def box(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload) + 8) + kind + payload

    mvhd = box(
        b"mvhd",
        b"\x00" * 12 + struct.pack(">II", 1000, int(seconds * 1000)) + b"\x00" * 80,
    )
    return box(b"ftyp", b"isom" + b"\x00" * 8) + box(b"moov", mvhd) + box(b"mdat", b"\x00" * pad)


def _media_file(tmp_path: Path, data: bytes, name: str = "talk.mp3") -> Path:
    f = tmp_path / name
    f.write_bytes(data)
    return f


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _FakeFal:
    """Stands in for ``transcription._call_fal``. Records every call."""

    def __init__(self, reply=None, raises: BaseException | None = None, delay: float = 0.0):
        self.calls: list[tuple[Path, str, str]] = []
        self._reply = (
            reply
            if reply is not None
            else {
                "text": "  So the plan for Q3 is to ship the thing.  ",
                "chunks": [{"timestamp": [0.7, 4.2], "text": "So the plan for Q3"}],
                "languages": ["en"],
            }
        )
        self._raises = raises
        self._delay = delay

    async def __call__(self, path, model, key):
        self.calls.append((path, model, key))
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises is not None:
            raise self._raises
        return self._reply


class _FakeChain:
    """The extraction chain, which media must never reach."""

    def __init__(self, result: ExtractionResult | None = None):
        self.runs: list[tuple[Path, str]] = []
        self._result = result or ExtractionResult(text="chain text", backend="local")

    async def run(self, path: Path, mime: str) -> ExtractionResult:
        self.runs.append((path, mime))
        return self._result


class _FakeIngest:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, *, scope: str, text: str, source: str) -> dict:
        self.calls.append({"scope": scope, "text": text, "source": source})
        return {"article": "art-1"}


class _NoopComprehension:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def __call__(self, **kwargs) -> None:
        self.calls.append(kwargs)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def beanie_with_budget():
    """Beanie bound to the upload docs AND the transcription counter.

    The package conftest deliberately does not register
    ``FileTranscriptionUsage``, so every OTHER uploads test runs with an
    unreadable counter and therefore a fail-CLOSED budget — belt and braces
    with the conftest's raising fal stub. Tests that need a transcription to
    actually happen ask for this fixture.
    """
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient
    from pocketpaw_ee.cloud.models.file_transcription_usage import FileTranscriptionUsage
    from pocketpaw_ee.cloud.uploads.models import FileFolder, FileUpload
    from pocketpaw_ee.cloud.uploads.share_models import ShareLink

    client = AsyncMongoMockClient()
    db = client[f"test_tx_{uuid.uuid4().hex[:8]}"]
    original = db.list_collection_names

    async def _safe(*_a, **_kw):
        return await original()

    db.list_collection_names = _safe  # type: ignore[method-assign]

    models = [FileUpload, FileFolder, ShareLink, FileTranscriptionUsage]
    await init_beanie(database=db, document_models=models)
    try:
        yield db
    finally:
        for model in models:
            for attr in ("_document_settings", "_settings"):
                if hasattr(model, attr):
                    try:
                        setattr(model, attr, None)
                    except Exception:  # pragma: no cover — defensive
                        pass


@pytest.fixture()
async def budget_store(beanie_with_budget):
    from pocketpaw_ee.cloud.uploads.mongo_store import MongoFileStore

    return MongoFileStore()


@pytest.fixture()
def adapter(tmp_path) -> LocalStorageAdapter:
    """A real local storage adapter holding a real (short) mp3."""
    a = LocalStorageAdapter(tmp_path / "storage")
    target = (tmp_path / "storage" / _STORAGE_KEY).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(_mp3_bytes(90))
    return a


def _record(**overrides) -> FileRecord:
    defaults = {
        "id": "f1",
        "storage_key": _STORAGE_KEY,
        "filename": "talk.mp3",
        "mime": "audio/mpeg",
        "size": 1,
        "owner_id": "u1",
        "chat_id": None,
        "created": datetime.now(UTC),
    }
    defaults.update(overrides)
    return FileRecord(**defaults)


def _event(**overrides) -> FileReady:
    data = {
        "workspace_id": "w1",
        "file_id": "f1",
        "filename": "talk.mp3",
        "mime": "audio/mpeg",
        "storage_key": _STORAGE_KEY,
    }
    data.update(overrides)
    return FileReady(data=data)


def _wire_listener(monkeypatch, *, chain, adapter, ingest):
    from pocketpaw_ee.cloud.agents import knowledge as kn
    from pocketpaw_ee.cloud.uploads import listeners

    monkeypatch.setattr("pocketpaw_ee.cloud.extraction.build_chain", lambda settings: chain)
    monkeypatch.setattr(listeners, "_resolve_adapter", lambda: adapter)
    monkeypatch.setattr(kn.KnowledgeService, "ingest_text_to_scope", ingest)
    monkeypatch.setattr(listeners, "_write_comprehension", _NoopComprehension())


async def _transcribe(path: Path, mime: str = "audio/mpeg", workspace: str = "w1"):
    return await transcription.transcribe_media(
        path=path, mime=mime, file_id="f1", workspace_id=workspace, filename=path.name
    )


# ---------------------------------------------------------------------------
# What we send fal — the parameter that silently lies
# ---------------------------------------------------------------------------


class TestTheArguments:
    def test_the_language_is_sent_as_an_explicit_null(self):
        """THE load-bearing assertion in this file.

        wizper's ``language`` defaults to ``"en"`` — not to auto-detect. Probed
        2026-08-29: with the default, a Spanish clip came back as fluent
        English ("Maria, what are we having for dinner today?") and reported
        ``languages: ["en"]``. Nothing errors. The transcript is confident,
        readable, and of something nobody said — and it then gets summarised,
        tagged and indexed as the content of that file.

        So the key must be PRESENT and ``None``. Absent is not equivalent.

        Mutation: delete the ``"language": None`` entry — red here, and green
        everywhere else in this file.
        """
        args = transcription.build_arguments("https://fal.media/x.mp3")

        assert "language" in args, (
            "language was omitted — wizper then defaults it to English and "
            "transcribes every other language into confident nonsense"
        )
        assert args["language"] is None, (
            f"language must be null for auto-detect, got {args['language']!r}"
        )

    def test_it_transcribes_rather_than_translates(self):
        """``task: translate`` returns English for everything, which is the
        same silent corruption by a different route."""
        assert transcription.build_arguments("u")["task"] == "transcribe"

    def test_the_audio_url_is_passed_through(self):
        assert transcription.build_arguments("https://x/y.mp4")["audio_url"] == "https://x/y.mp4"

    def test_the_default_model_is_the_one_that_was_priced(self):
        """Pinned so a swap has to be deliberate. The comparison that chose it
        is in the module header; ``fal-ai/whisper`` measured 3.6x the price for
        the same job."""
        assert transcription.DEFAULT_MODEL == "fal-ai/wizper"
        assert transcription.configured_model() == "fal-ai/wizper"

    def test_a_deployment_can_override_the_model(self, monkeypatch):
        monkeypatch.setenv("POCKETPAW_FILE_TRANSCRIPTION_MODEL", transcription.FULL_WHISPER_MODEL)
        assert transcription.configured_model() == "fal-ai/whisper"


# ---------------------------------------------------------------------------
# The seam — asserted against the real SDK, not a double
# ---------------------------------------------------------------------------


class TestTheFalSeam:
    def test_the_sdk_still_has_the_methods_we_call(self):
        """Every other test here uses a double, which cannot notice an SDK
        rename. A missing method would land in ``_call_fal``'s caller's broad
        except and read as "transcription is off"."""
        import fal_client

        client = fal_client.AsyncClient
        for method in ("upload_file", "subscribe"):
            assert hasattr(client, method), f"fal_client.AsyncClient lost .{method}()"
            assert inspect.iscoroutinefunction(getattr(client, method))

    def test_it_uses_the_queue_path_not_the_synchronous_one(self):
        """A 30-minute file is ~90 s of inference. ``run()`` is the sync
        endpoint; the meetings and podcasts this feature exists for are exactly
        the files that would time out on it.

        Asserted by reading the source, because the alternative is a live call.
        """
        src = inspect.getsource(_REAL_CALL_FAL)
        assert "client.subscribe(" in src
        assert "client.run(" not in src

    def test_the_key_resolves_through_the_one_rule_the_repo_has(self):
        """No second copy of the FAL_AI_API_KEY / FAL_KEY precedence."""
        src = inspect.getsource(_REAL_API_KEY)
        assert "fal_api_key" in src and "studio.fal_edit" in src


# ---------------------------------------------------------------------------
# The ceiling — before the spend, and before a budget slot
# ---------------------------------------------------------------------------


class TestTheCeiling:
    async def test_a_three_hour_recording_is_refused_from_its_own_header(
        self, tmp_path, beanie_with_budget, monkeypatch
    ):
        """The requirement, stated exactly: skipped with a recorded reason,
        not discovered mid-bill.

        The fixture is a real 3-hour mp4 header (a few KB on disk, so the BYTE
        ceiling cannot be what refuses it) — the duration probe has to be the
        thing that fires, or this test is measuring the wrong gate.

        Mutation: change the duration check to ``if False`` — red.
        """
        fal = _FakeFal()
        monkeypatch.setattr(transcription, "_call_fal", fal)
        media = _media_file(tmp_path, _mp4_bytes(3 * 3600), "long.mp4")
        assert media.stat().st_size < transcription.max_bytes(), (
            "fixture is over the byte ceiling, so this test would pass without "
            "the duration probe ever running"
        )

        result = await _transcribe(media, mime="video/mp4")

        assert fal.calls == [], "a three-hour video reached the paid endpoint"
        assert result is not None, "the reason must be recorded, not thrown away"
        assert result.text == ""
        skipped = result.metadata["transcription"]
        assert skipped["skipped"] == "too_long"
        assert skipped["duration_seconds"] == pytest.approx(10_800.0)
        assert skipped["limit_minutes"] == 30.0

    async def test_the_ceiling_is_checked_before_a_budget_slot_is_claimed(
        self, tmp_path, beanie_with_budget, monkeypatch
    ):
        """An over-long file must not burn a slot a transcribable one could
        have used.

        Mutation: move the ``try_spend`` call above the duration check — the
        counter row appears and this goes red.
        """
        from pocketpaw_ee.cloud.models.file_transcription_usage import FileTranscriptionUsage

        monkeypatch.setattr(transcription, "_call_fal", _FakeFal())
        await _transcribe(_media_file(tmp_path, _mp4_bytes(4 * 3600), "long.mp4"), "video/mp4")

        day = datetime.now(UTC).strftime("%Y-%m-%d")
        row = await FileTranscriptionUsage.find_one(FileTranscriptionUsage.key == f"w1:{day}")
        assert row is None, "a refused file consumed a transcription slot"

    async def test_an_oversized_file_is_refused_when_the_length_is_unreadable(
        self, tmp_path, beanie_with_budget, monkeypatch
    ):
        """Ogg/webm/flac have no duration probe, so bytes are the only gate
        left. It has to actually fire.

        Mutation: delete the ``size > limit_bytes`` check — red.
        """
        from pocketpaw_ee.cloud.uploads.media_duration import probe_duration_seconds

        fal = _FakeFal()
        monkeypatch.setattr(transcription, "_call_fal", fal)
        monkeypatch.setenv("POCKETPAW_FILE_TRANSCRIPTION_MAX_MB", "1")
        media = _media_file(tmp_path, b"OggS\x00\x02" + b"\x00" * (2 * 1024 * 1024), "voice.ogg")
        assert probe_duration_seconds(media) is None, (
            "the fixture's length IS readable, so this test would be measuring "
            "the duration ceiling and not the byte one"
        )

        result = await _transcribe(media, mime="audio/ogg")

        assert fal.calls == []
        assert result.metadata["transcription"]["skipped"] == "too_large"
        assert result.metadata["transcription"]["duration_seconds"] is None

    async def test_a_file_under_both_ceilings_is_transcribed(
        self, tmp_path, beanie_with_budget, monkeypatch
    ):
        """The gate must not be a wall. A ceiling that refuses everything is
        the same outcome as a feature that is switched off."""
        fal = _FakeFal()
        monkeypatch.setattr(transcription, "_call_fal", fal)

        result = await _transcribe(_media_file(tmp_path, _mp3_bytes(600)))

        assert len(fal.calls) == 1
        assert result.text == "So the plan for Q3 is to ship the thing."
        assert result.backend == transcription.BACKEND
        assert result.metadata["transcription"]["languages"] == ["en"]
        assert result.metadata["transcription"]["duration_seconds"] == pytest.approx(600, rel=0.01)
        assert result.metadata["transcription"]["segments"][0]["start"] == 0.7

    async def test_a_nonsense_ceiling_falls_back_to_the_default(self, monkeypatch):
        """ "thirty" must not read as 0 and switch transcription off for a whole
        deployment."""
        monkeypatch.setenv("POCKETPAW_FILE_TRANSCRIPTION_MAX_MINUTES", "thirty")
        assert transcription.max_minutes() == 30.0
        monkeypatch.setenv("POCKETPAW_FILE_TRANSCRIPTION_MAX_MINUTES", "-5")
        assert transcription.max_minutes() == 30.0


# ---------------------------------------------------------------------------
# The daily cap
# ---------------------------------------------------------------------------


class TestTheDailyCap:
    def test_the_counter_uses_the_beanie_2x_collection_api(self):
        """The exact mistake that shipped a budget refusing every claim.

        ``get_motor_collection`` is beanie 1.x. On 2.1.0 it does not exist, the
        AttributeError lands in the fail-closed except, and the feature reads
        as "transcription is off" — not as a bug. Asserted on the class, not
        on the source text, so a rename in beanie itself also fails here.
        """
        from pocketpaw_ee.cloud.models.file_transcription_usage import FileTranscriptionUsage

        assert hasattr(FileTranscriptionUsage, "get_pymongo_collection")
        assert not hasattr(FileTranscriptionUsage, "get_motor_collection")
        assert "get_pymongo_collection" in inspect.getsource(transcription_budget.try_spend)

    def test_the_document_is_registered_for_beanie_init(self):
        """An unregistered document raises at claim time, inside the
        fail-closed except. Same silent refusal, one layer up."""
        from pocketpaw_ee.cloud.models import get_all_documents
        from pocketpaw_ee.cloud.models.file_transcription_usage import FileTranscriptionUsage

        assert FileTranscriptionUsage in get_all_documents()

    async def test_the_cap_refuses_the_next_claim(self, beanie_with_budget, monkeypatch):
        monkeypatch.setenv("POCKETPAW_FILE_TRANSCRIPTION_DAILY", "2")

        first = await transcription_budget.try_spend("w1")
        second = await transcription_budget.try_spend("w1")
        third = await transcription_budget.try_spend("w1")

        assert first[0] is True
        assert second[0] is True
        assert third[0] is False, "the third claim on a cap of 2 must be refused"
        assert third[1:] == (2, 2)

    async def test_a_refused_claim_does_not_consume_a_slot(self, beanie_with_budget, monkeypatch):
        """An over-cap claim is rolled back, so the counter cannot run away to
        thousands and leave the workspace refused long after midnight."""
        from pocketpaw_ee.cloud.models.file_transcription_usage import FileTranscriptionUsage

        monkeypatch.setenv("POCKETPAW_FILE_TRANSCRIPTION_DAILY", "1")

        await transcription_budget.try_spend("w1")
        for _ in range(5):
            await transcription_budget.try_spend("w1")

        day = datetime.now(UTC).strftime("%Y-%m-%d")
        row = await FileTranscriptionUsage.find_one(FileTranscriptionUsage.key == f"w1:{day}")
        assert row is not None
        assert row.used == 1

    async def test_one_workspace_cannot_spend_anothers_budget(
        self, beanie_with_budget, monkeypatch
    ):
        monkeypatch.setenv("POCKETPAW_FILE_TRANSCRIPTION_DAILY", "1")

        await transcription_budget.try_spend("w1")
        assert (await transcription_budget.try_spend("w2"))[0] is True

    async def test_an_unreadable_counter_fails_CLOSED(self, monkeypatch):
        """No Beanie binding in this test, so the collection genuinely cannot
        be read — the degraded-database case reached honestly rather than
        simulated. Skipping a transcript costs a transcript; an ungated ingest
        costs money."""
        monkeypatch.setenv("POCKETPAW_FILE_TRANSCRIPTION_DAILY", "5")

        assert (await transcription_budget.try_spend("w1"))[0] is False

    async def test_no_workspace_is_refused(self, beanie_with_budget, monkeypatch):
        monkeypatch.setenv("POCKETPAW_FILE_TRANSCRIPTION_DAILY", "5")
        assert (await transcription_budget.try_spend(""))[0] is False
        assert (await transcription_budget.try_spend(None))[0] is False

    async def test_a_zero_cap_disables_the_feature(self, beanie_with_budget, monkeypatch):
        monkeypatch.setenv("POCKETPAW_FILE_TRANSCRIPTION_DAILY", "0")
        allowed, _spent, cap = await transcription_budget.try_spend("w1")
        assert allowed is False
        assert cap == 0

    async def test_a_nonsense_cap_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("POCKETPAW_FILE_TRANSCRIPTION_DAILY", "one hundred")
        assert transcription_budget.daily_cap() == 100

    async def test_an_exhausted_budget_stops_the_spend(
        self, tmp_path, beanie_with_budget, monkeypatch
    ):
        """The gate is only worth anything if it stands between the caller and
        the paid call.

        The first file spends the only slot, so the SECOND one is the one
        exercising the refusal — a fixture that never reaches the cap would
        never run this branch.

        Mutation: ignore ``allowed`` and transcribe anyway — red.
        """
        fal = _FakeFal()
        monkeypatch.setattr(transcription, "_call_fal", fal)
        monkeypatch.setenv("POCKETPAW_FILE_TRANSCRIPTION_DAILY", "1")
        media = _media_file(tmp_path, _mp3_bytes(60))

        first = await _transcribe(media)
        second = await _transcribe(media)

        assert first is not None and first.text
        assert len(fal.calls) == 1, "the capped file still reached the paid endpoint"
        assert second is None, (
            "a budget refusal must persist NOTHING — it is a fact about today, "
            "not about the file, so the next ingest should retry"
        )


# ---------------------------------------------------------------------------
# Refusals and containment
# ---------------------------------------------------------------------------


class TestRefusals:
    @pytest.mark.parametrize("mime", ["application/pdf", "text/vtt", "image/png", "text/plain", ""])
    async def test_non_media_is_not_ours(self, tmp_path, mime, monkeypatch):
        monkeypatch.setattr(transcription, "_call_fal", _FakeFal())
        assert await _transcribe(_media_file(tmp_path, b"hello"), mime) is None

    async def test_no_key_configured_persists_nothing(
        self, tmp_path, beanie_with_budget, monkeypatch
    ):
        """A deployment fact, not a fact about the file: configure a key and a
        re-ingest should work, so nothing durable is written."""
        fal = _FakeFal()
        monkeypatch.setattr(transcription, "_call_fal", fal)
        monkeypatch.setattr(transcription, "_api_key", lambda: None)

        assert await _transcribe(_media_file(tmp_path, _mp3_bytes(60))) is None
        assert fal.calls == []

    async def test_a_fal_outage_persists_nothing(self, tmp_path, beanie_with_budget, monkeypatch):
        monkeypatch.setattr(
            transcription, "_call_fal", _FakeFal(raises=RuntimeError("fal is down"))
        )
        assert await _transcribe(_media_file(tmp_path, _mp3_bytes(60))) is None

    async def test_a_hung_endpoint_gives_up_rather_than_pinning_the_listener(
        self, tmp_path, beanie_with_budget, monkeypatch
    ):
        """Mutation: remove the ``asyncio.wait_for`` — the test hangs."""
        monkeypatch.setenv("POCKETPAW_FILE_TRANSCRIPTION_TIMEOUT_S", "0.05")
        monkeypatch.setattr(transcription, "_call_fal", _FakeFal(delay=5))

        result = await asyncio.wait_for(
            _transcribe(_media_file(tmp_path, _mp3_bytes(60))), timeout=3
        )
        assert result is None

    async def test_silence_is_recorded_rather_than_retried(
        self, tmp_path, beanie_with_budget, monkeypatch
    ):
        """We paid and there was no speech. That IS a fact about the file:
        re-running buys the same silence again."""
        monkeypatch.setattr(transcription, "_call_fal", _FakeFal(reply={"text": "   "}))

        result = await _transcribe(_media_file(tmp_path, _mp3_bytes(60)))

        assert result is not None
        assert result.text == ""
        assert result.metadata["transcription"]["skipped"] == "no_speech"

    async def test_an_unexpected_response_shape_is_not_a_crash(
        self, tmp_path, beanie_with_budget, monkeypatch
    ):
        """An override may point at a sibling endpoint with a different shape.
        A listener must not die of it."""
        monkeypatch.setattr(transcription, "_call_fal", _FakeFal(reply=["not", "a", "dict"]))
        result = await _transcribe(_media_file(tmp_path, _mp3_bytes(60)))
        assert result.metadata["transcription"]["skipped"] == "no_speech"


# ---------------------------------------------------------------------------
# The listener — the seam where the payoff actually happens
# ---------------------------------------------------------------------------


class TestTheListener:
    async def test_a_recording_becomes_a_persisted_searchable_document(
        self, monkeypatch, budget_store, adapter
    ):
        """THE payoff assertion. One ingest, and the transcript is persisted as
        the file's extraction, ingested into kb-go and readable back — with no
        new code in the persist, tag, comprehension or KB paths.

        Mutation: make the listener call ``chain.run`` for media too — the
        stored text becomes the chain's, and this goes red.
        """
        from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

        await budget_store.save_scoped(_record(), "w1")
        fal = _FakeFal()
        monkeypatch.setattr(transcription, "_call_fal", fal)
        chain, ingest = _FakeChain(), _FakeIngest()
        _wire_listener(monkeypatch, chain=chain, adapter=adapter, ingest=ingest)

        await index_uploaded_file(_event())

        assert chain.runs == [], "a media file was read as text by the extraction chain"
        assert len(fal.calls) == 1

        doc = await budget_store.get_doc_scoped("f1", "w1")
        assert doc.extracted_text_key == blob_key("f1"), "the transcript was never persisted"
        stored = await load_extracted_text(doc, adapter=adapter)
        assert stored.text == "So the plan for Q3 is to ship the thing."
        assert stored.backend == transcription.BACKEND

        assert len(ingest.calls) == 1, "the transcript never reached the knowledge base"
        assert ingest.calls[0]["text"] == "So the plan for Q3 is to ship the thing."
        assert ingest.calls[0]["scope"] == "workspace:w1"

    async def test_the_transcript_is_what_comprehension_is_asked_to_summarise(
        self, monkeypatch, budget_store, adapter
    ):
        """Summaries and tags come for free ONLY if the transcript is the text
        handed on. This asserts the hand-off rather than assuming it."""
        from pocketpaw_ee.cloud.uploads import listeners
        from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

        await budget_store.save_scoped(_record(), "w1")
        monkeypatch.setattr(transcription, "_call_fal", _FakeFal())
        comprehension = _NoopComprehension()
        _wire_listener(monkeypatch, chain=_FakeChain(), adapter=adapter, ingest=_FakeIngest())
        monkeypatch.setattr(listeners, "_write_comprehension", comprehension)

        await index_uploaded_file(_event())

        assert len(comprehension.calls) == 1
        assert (
            comprehension.calls[0]["extracted"].text == "So the plan for Q3 is to ship the thing."
        )

    async def test_a_video_never_reaches_the_extraction_chain(
        self, monkeypatch, budget_store, tmp_path
    ):
        """The bug this branch fixes. ``LocalExtractor`` claims every mime and
        ends in ``read_text(errors="replace")``, so a video used to be slurped
        whole into replacement characters and that string was persisted,
        summarised, tagged and indexed.

        Mutation: drop the ``is_transcribable`` branch — ``chain.runs`` fills
        and this goes red.
        """
        from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

        key = "w1/u1/clip.mp4"
        adapter = LocalStorageAdapter(tmp_path / "storage")
        target = (tmp_path / "storage" / key).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_mp4_bytes(120, pad=50_000))

        await budget_store.save_scoped(
            _record(storage_key=key, filename="clip.mp4", mime="video/mp4"), "w1"
        )
        monkeypatch.setattr(transcription, "_call_fal", _FakeFal())
        chain, ingest = _FakeChain(), _FakeIngest()
        _wire_listener(monkeypatch, chain=chain, adapter=adapter, ingest=ingest)

        await index_uploaded_file(_event(storage_key=key, filename="clip.mp4", mime="video/mp4"))

        assert chain.runs == []
        assert ingest.calls[0]["text"] == "So the plan for Q3 is to ship the thing."
        assert "�" not in ingest.calls[0]["text"], "the raw video was indexed as text"

    async def test_a_skipped_file_records_why_and_indexes_nothing(
        self, monkeypatch, budget_store, tmp_path
    ):
        """Over the ceiling: the reason is persisted so it survives, and the
        empty text means the existing paths skip the KB ingest on their own."""
        from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

        key = "w1/u1/long.mp4"
        adapter = LocalStorageAdapter(tmp_path / "storage")
        target = (tmp_path / "storage" / key).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(_mp4_bytes(5 * 3600))

        await budget_store.save_scoped(
            _record(storage_key=key, filename="long.mp4", mime="video/mp4"), "w1"
        )
        fal = _FakeFal()
        monkeypatch.setattr(transcription, "_call_fal", fal)
        ingest = _FakeIngest()
        _wire_listener(monkeypatch, chain=_FakeChain(), adapter=adapter, ingest=ingest)

        await index_uploaded_file(_event(storage_key=key, filename="long.mp4", mime="video/mp4"))

        assert fal.calls == []
        assert ingest.calls == [], "a file with no transcript reached the knowledge base"
        doc = await budget_store.get_doc_scoped("f1", "w1")
        stored = await load_extracted_text(doc, adapter=adapter)
        assert stored is not None, "the skip reason was not recorded anywhere"
        assert stored.metadata["transcription"]["skipped"] == "too_long"

    async def test_a_transcription_failure_loses_nothing_else(
        self, monkeypatch, budget_store, adapter
    ):
        """Containment, requirement 5. A fal outage must not take the upload,
        the library row or anything else with it — and must not persist a
        result we do not have.

        Mutation: let ``_call_fal``'s exception propagate out of
        ``transcribe_media`` — the listener raises and this goes red.
        """
        from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

        await budget_store.save_scoped(_record(), "w1")
        monkeypatch.setattr(transcription, "_call_fal", _FakeFal(raises=RuntimeError("boom")))
        ingest = _FakeIngest()
        _wire_listener(monkeypatch, chain=_FakeChain(), adapter=adapter, ingest=ingest)

        await index_uploaded_file(_event())  # must not raise

        doc = await budget_store.get_doc_scoped("f1", "w1")
        assert doc is not None, "a transcription failure lost the library row"
        assert doc.extracted_text_key is None, "an outage was recorded as a result"
        assert ingest.calls == []

    async def test_a_hidden_file_is_never_uploaded_to_fal(self, monkeypatch, budget_store, adapter):
        """The existing hide-from-AI gate runs before any of this. Nothing in
        the transcription path re-checks it, so this asserts the gate really
        does stand in front — a private recording must never leave the box.

        Mutation: delete the ``hide_from_ai`` early return in the listener —
        red.
        """
        from pocketpaw_ee.cloud.uploads.listeners import index_uploaded_file

        await budget_store.save_scoped(_record(), "w1")
        await budget_store.set_library_metadata("f1", "w1", hide_from_ai=True)
        fal = _FakeFal()
        monkeypatch.setattr(transcription, "_call_fal", fal)
        _wire_listener(monkeypatch, chain=_FakeChain(), adapter=adapter, ingest=_FakeIngest())

        await index_uploaded_file(_event())

        assert fal.calls == [], "a file the user hid from AI was uploaded to fal"
