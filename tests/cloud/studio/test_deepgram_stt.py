# tests/cloud/studio/test_deepgram_stt.py — the direct Deepgram STT client.
#
# deepgram_stt is the seam behind the /studio editor's "Generate transcript"
# button: it POSTs audio to Deepgram's synchronous Prerecorded endpoint and
# converts the result into running text plus word-level timings. Like the fal_*
# modules these tests keep the HTTP layer OUT of the picture — ``_listen`` is
# monkeypatched — so query building, the seconds→milliseconds conversion, and
# error mapping are asserted precisely.
#
# Created 2026-09-02 (studio-transcribe): Deepgram speech-to-text tests.
# Updated 2026-09-03 (studio-transcribe-502): retargeted at the real provider
#   contract. These tests previously mocked an invented async submit+poll
#   protocol, so they passed against a module that 502'd on every live call —
#   a mock of the seam you got wrong proves only self-consistency. The wire
#   shape itself is now pinned by verbatim captures in
#   ``test_deepgram_contract.py``; this file covers the surrounding logic.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.studio import deepgram_stt, schemas, service

# ── API key resolution ──────────────────────────────────────────────────────


def test_resolve_api_key_prefers_namespaced_setting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A POCKETPAW_-prefixed key must win over a bare DEEPGRAM_API_KEY.

    Regression guard: this once read a module-level ``settings`` object that
    does not exist in config.py, so the ImportError was swallowed by a broad
    except and the namespaced key was silently ignored forever.
    """
    monkeypatch.setenv("DEEPGRAM_API_KEY", "plain-key")
    monkeypatch.setattr(deepgram_stt, "_settings_key", lambda: "prefixed-key")
    assert deepgram_stt.resolve_api_key() == "prefixed-key"


def test_resolve_api_key_falls_back_to_bare_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The unprefixed name is what already ships in .env, so it must still work."""
    monkeypatch.setenv("DEEPGRAM_API_KEY", "plain-key")
    monkeypatch.setattr(deepgram_stt, "_settings_key", lambda: None)
    assert deepgram_stt.resolve_api_key() == "plain-key"


def test_resolve_api_key_blank_values_are_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setattr(deepgram_stt, "_settings_key", lambda: "   ")
    assert deepgram_stt.resolve_api_key() is None


@pytest.mark.asyncio
async def test_transcribe_without_key_raises_clearly(monkeypatch: pytest.MonkeyPatch) -> None:
    """No silent empty transcript — the message names both env vars."""
    monkeypatch.setattr(deepgram_stt, "resolve_api_key", lambda: None)
    with pytest.raises(deepgram_stt.DeepgramError, match="POCKETPAW_DEEPGRAM_API_KEY"):
        await deepgram_stt.transcribe_bytes(audio_bytes=b"abc")


# ── Query building ──────────────────────────────────────────────────────────


def test_build_query_requests_word_timings_and_punctuation() -> None:
    query = deepgram_stt._build_query(model="nova-3", language=None)
    assert query["model"] == "nova-3"
    assert query["punctuate"] == "true"
    assert query["smart_format"] == "true"
    # No diarize: speaker labels have nowhere to go in the caption model yet.
    assert "diarize" not in query
    assert "language" not in query


def test_build_query_passes_language_through() -> None:
    assert deepgram_stt._build_query(model="nova-2", language="en-US")["language"] == "en-US"


# ── Response extraction ─────────────────────────────────────────────────────


def _completed(transcript: str, words: list[dict]) -> dict:
    """A Deepgram listen response.

    The real envelope: ``results.channels[0].alternatives[0]``. This helper used
    to build ``results.channel_detections`` with a ``metadata.status`` — a shape
    Deepgram never returns — which is precisely why the suite stayed green while
    every live call 502'd. The full verbatim capture lives in
    ``test_deepgram_contract.py``; this is its minimal form.
    """
    return {
        "metadata": {"request_id": "req-1", "duration": 1.0},
        "results": {"channels": [{"alternatives": [{"transcript": transcript, "words": words}]}]},
    }


def test_extract_converts_seconds_to_milliseconds() -> None:
    body = _completed(
        "hello world",
        [
            {"word": "hello", "start": 0.0, "end": 0.5, "confidence": 0.99},
            {"word": "world", "start": 0.5, "end": 1.234, "confidence": 0.9},
        ],
    )
    text, words = deepgram_stt._extract_transcript(body)
    assert text == "hello world"
    assert words[0] == {"text": "hello", "startMs": 0, "endMs": 500, "confidence": 0.99}
    assert words[1]["startMs"] == 500
    assert words[1]["endMs"] == 1234


def test_extract_rounds_instead_of_truncating() -> None:
    """0.9999s must become 1000ms, not 999ms — forty truncated words drift."""
    body = _completed("x", [{"word": "x", "start": 0.9999, "end": 1.5}])
    _, words = deepgram_stt._extract_transcript(body)
    assert words[0]["startMs"] == 1000


def test_extract_guarantees_nonzero_word_duration() -> None:
    body = _completed("x", [{"word": "x", "start": 2.0, "end": 2.0}])
    _, words = deepgram_stt._extract_transcript(body)
    assert words[0]["endMs"] > words[0]["startMs"]


def test_extract_skips_blank_and_untimed_words() -> None:
    body = _completed(
        "keep",
        # The blank entry carries a real timing so the test isolates ONE rule —
        # whitespace rejection. If it were also untimed, a broken timing filter
        # could mask a broken whitespace filter (and vice versa).
        [
            {"word": "  ", "start": 0.0, "end": 1.0},
            {"word": "keep", "start": 1.0, "end": 2.0},
            {"word": "dropped", "start": None},
        ],
    )
    _, words = deepgram_stt._extract_transcript(body)
    assert [w["text"] for w in words] == ["keep"]


def test_extract_falls_back_to_joined_words_without_transcript() -> None:
    body = _completed("", [{"word": "one", "start": 0.0, "end": 0.5, "confidence": 0.5}])
    text, words = deepgram_stt._extract_transcript(body)
    assert text == "one"
    assert len(words) == 1


def test_extract_rejects_missing_results() -> None:
    with pytest.raises(deepgram_stt.DeepgramError, match="no `results`"):
        deepgram_stt._extract_transcript({"metadata": {}})


def test_extract_rejects_empty_transcript() -> None:
    """An empty result must be an error, not a caption track with zero cues."""
    with pytest.raises(deepgram_stt.DeepgramError, match="empty transcript"):
        deepgram_stt._extract_transcript(_completed("", []))


def test_extract_rejects_non_numeric_timing() -> None:
    body = _completed("x", [{"word": "x", "start": "not-a-number", "end": 1.0}])
    with pytest.raises(deepgram_stt.DeepgramError, match="non-numeric"):
        deepgram_stt._extract_transcript(body)


# ── The listen call ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transcribe_bytes_makes_exactly_one_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One POST, results inline. Deepgram's prerecorded API is synchronous.

    The call count is the assertion that matters: the module previously issued a
    submit and then polled a status endpoint that does not exist.
    """
    monkeypatch.setattr(deepgram_stt, "resolve_api_key", lambda: "k")

    calls: list[dict] = []

    async def fake_listen(**kwargs):
        calls.append(kwargs)
        return _completed("hi", [{"word": "hi", "start": 0.0, "end": 0.4}])

    monkeypatch.setattr(deepgram_stt, "_listen", fake_listen)

    result = await deepgram_stt.transcribe_bytes(audio_bytes=b"abc")

    assert len(calls) == 1
    assert calls[0]["query"]["model"] == deepgram_stt.DEFAULT_MODEL
    assert result["text"] == "hi"
    assert result["words"][0]["endMs"] == 400
    assert result["model"] == deepgram_stt.DEFAULT_MODEL


@pytest.mark.asyncio
async def test_transcribe_bytes_passes_content_type_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The uploaded media type reaches Deepgram — audio goes up as raw bytes.

    A hardcoded ``audio/wav`` would mislabel the mp3/m4a the editor can also
    hand us.
    """
    monkeypatch.setattr(deepgram_stt, "resolve_api_key", lambda: "k")

    seen: dict = {}

    async def fake_listen(**kwargs):
        seen.update(kwargs)
        return _completed("hi", [{"word": "hi", "start": 0.0, "end": 0.4}])

    monkeypatch.setattr(deepgram_stt, "_listen", fake_listen)
    await deepgram_stt.transcribe_bytes(audio_bytes=b"abc", content_type="audio/mpeg")

    assert seen["content_type"] == "audio/mpeg"


@pytest.mark.asyncio
async def test_transcribe_bytes_surfaces_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-2xx from Deepgram must reach the caller as a DeepgramError."""
    monkeypatch.setattr(deepgram_stt, "resolve_api_key", lambda: "k")

    async def fake_listen(**kwargs):
        raise deepgram_stt.DeepgramError("Deepgram request failed (401): bad key")

    monkeypatch.setattr(deepgram_stt, "_listen", fake_listen)

    with pytest.raises(deepgram_stt.DeepgramError, match="401"):
        await deepgram_stt.transcribe_bytes(audio_bytes=b"abc")


@pytest.mark.asyncio
async def test_transcribe_bytes_rejects_empty_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deepgram_stt, "resolve_api_key", lambda: "k")
    with pytest.raises(deepgram_stt.DeepgramError, match="No audio data"):
        await deepgram_stt.transcribe_bytes(audio_bytes=b"")


@pytest.mark.asyncio
async def test_transcribe_bytes_rejects_oversized_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deepgram_stt, "resolve_api_key", lambda: "k")
    monkeypatch.setattr(deepgram_stt, "MAX_AUDIO_BYTES", 10)
    with pytest.raises(deepgram_stt.DeepgramError, match="too large"):
        await deepgram_stt.transcribe_bytes(audio_bytes=b"x" * 11)


# ── Service wrapper ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_service_transcribe_maps_words_to_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_transcribe(**kwargs):
        return {
            "text": "hello there",
            "words": [{"text": "hello", "startMs": 0, "endMs": 500, "confidence": 0.9}],
            "model": "nova-3",
        }

    monkeypatch.setattr(deepgram_stt, "transcribe_bytes", fake_transcribe)
    response = await service.transcribe(b"audio-bytes")

    assert isinstance(response, schemas.TranscriptResponse)
    assert response.text == "hello there"
    assert response.words[0].startMs == 0
    assert response.words[0].confidence == 0.9


@pytest.mark.asyncio
async def test_service_transcribe_wraps_provider_error_as_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(**kwargs):
        raise deepgram_stt.DeepgramError("upstream said no")

    monkeypatch.setattr(deepgram_stt, "transcribe_bytes", boom)
    with pytest.raises(service.StudioUpstreamError, match="upstream said no"):
        await service.transcribe(b"audio-bytes")


@pytest.mark.asyncio
async def test_service_transcribe_rejects_empty_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty bytes are caller error (→400), never a wasted provider call."""

    async def unused(**kwargs):
        raise AssertionError("must not reach the provider")

    monkeypatch.setattr(deepgram_stt, "transcribe_bytes", unused)
    with pytest.raises(ValueError, match="empty"):
        await service.transcribe(b"")
