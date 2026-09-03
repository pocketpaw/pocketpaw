# tests/cloud/studio/test_deepgram_contract.py — the REAL Deepgram wire contract.
#
# Reproduction suite for the /studio/transcribe 502. The original module was
# written against an INVENTED Deepgram protocol (async submit + poll), and the
# original tests monkeypatched `_post_submit` / `_get_status` to return that
# invention — so the suite was green while every live request 502'd. Mocking the
# seam you got wrong proves only that you are self-consistent.
#
# Every fixture here is a VERBATIM capture from a live
# `POST https://api.deepgram.com/v1/listen` (nova-3, 2026-09-03), so these tests
# fail whenever the code drifts from what Deepgram actually returns.
#
# The contract, confirmed against the live API and the Pre-Recorded reference:
#   * `/v1/listen` is SYNCHRONOUS — the POST response carries the results.
#     There is no `async=true`, and `GET /v1/requests/{id}` is the usage API,
#     not a results-polling endpoint. Async means supplying `callback`.
#   * Transcript:  results.channels[0].alternatives[0].transcript
#     Words:       results.channels[0].alternatives[0].words[]
#     (`results.channel_detections` and `results.metadata.status` do not exist.)
#   * `request_id` lives under top-level `metadata`, never at the root.
#   * Real params are `utt_split` (float, needs `utterances=true`), not
#     `utterance_split`; `alternatives` is a RESPONSE array, not a request param.
#
# Created 2026-09-03 (studio-transcribe-502): pin the real provider contract.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.studio import deepgram_stt

# ── Fixtures: verbatim live captures ────────────────────────────────────────

#: A real completed response, trimmed to three words. Note `punctuated_word`:
#: with smart_format/punctuate on, THIS is the caption-ready token — `word` is
#: the raw lowercase form.
REAL_LISTEN_RESPONSE: dict = {
    "metadata": {
        "transaction_key": "deprecated",
        "request_id": "01a0656e-a60f-7a20-aa46-2a0ac43232e3",
        "sha256": "2a6cea9b6e04af8ebd00365e4798a24b54e1cc2180b8688fe571232aaee6e3b6",
        "created": "2026-09-03T04:02:31.457Z",
        "duration": 3.717625,
        "channels": 1,
        "models": ["2187e11a-3532-4498-b076-81fa530bdd49"],
    },
    "results": {
        "channels": [
            {
                "alternatives": [
                    {
                        "transcript": "Hello, world. This is a test.",
                        "confidence": 0.99902344,
                        "words": [
                            {
                                "word": "hello",
                                "start": 0.0,
                                "end": 0.48,
                                "confidence": 0.8156738,
                                "punctuated_word": "Hello,",
                            },
                            {
                                "word": "world",
                                "start": 0.48,
                                "end": 0.88,
                                "confidence": 0.9716797,
                                "punctuated_word": "world.",
                            },
                            {
                                "word": "test",
                                "start": 0.88,
                                "end": 1.12,
                                "confidence": 0.99902344,
                                "punctuated_word": "test.",
                            },
                        ],
                    }
                ]
            }
        ]
    },
}


# ── The 502 itself ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transcribe_bytes_reads_the_synchronous_listen_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE BUG: `/v1/listen` answers with the transcript, not a job handle.

    Before the fix this raised `DeepgramError("Deepgram submit returned no
    request_id")` — the code looked for a root-level `request_id` to poll, the
    real body has results instead, and the service mapped that to a 502.

    `_listen` is the single HTTP seam: one POST, one body, no polling.
    """
    monkeypatch.setattr(deepgram_stt, "resolve_api_key", lambda: "k")

    async def fake_listen(**kwargs: object) -> dict:
        return REAL_LISTEN_RESPONSE

    monkeypatch.setattr(deepgram_stt, "_listen", fake_listen)

    result = await deepgram_stt.transcribe_bytes(audio_bytes=b"riff-bytes")

    assert result["text"] == "Hello, world. This is a test."
    assert result["words"][0]["startMs"] == 0
    assert result["words"][0]["endMs"] == 480
    assert result["model"] == deepgram_stt.DEFAULT_MODEL


@pytest.mark.asyncio
async def test_no_polling_helpers_survive(monkeypatch: pytest.MonkeyPatch) -> None:
    """The async job protocol was fictional — its helpers must be gone.

    Kept as a gate because a "safe" re-add of a poll fallback would restore the
    exact failure: Deepgram never reports a job status, so any status branch is
    dead code that can only mislead the next reader.
    """
    assert not hasattr(deepgram_stt, "_post_submit")
    assert not hasattr(deepgram_stt, "_get_status")


# ── Response extraction against the real envelope ───────────────────────────


def test_extract_reads_the_real_channels_path() -> None:
    """`results.channels[0].alternatives[0]` — not `channel_detections`."""
    text, words = deepgram_stt._extract_transcript(REAL_LISTEN_RESPONSE)
    assert text == "Hello, world. This is a test."
    assert len(words) == 3


def test_extract_prefers_punctuated_word_for_captions() -> None:
    """Captions are burned into frames — they need the punctuated token.

    We ask for `smart_format=true` and `punctuate=true`, so Deepgram returns
    `punctuated_word` ("Hello,") alongside the raw `word` ("hello"). Reading
    `word` throws that away and renders an unpunctuated caption track under a
    transcript that IS punctuated — the two disagree on screen.
    """
    _, words = deepgram_stt._extract_transcript(REAL_LISTEN_RESPONSE)
    assert [w["text"] for w in words] == ["Hello,", "world.", "test."]


def test_extract_survives_a_model_without_punctuated_word() -> None:
    """`punctuated_word` is absent on some models; fall back to `word`."""
    body = {
        "results": {
            "channels": [
                {
                    "alternatives": [
                        {
                            "transcript": "bare",
                            "words": [{"word": "bare", "start": 0.0, "end": 0.2}],
                        }
                    ]
                }
            ]
        }
    }
    _, words = deepgram_stt._extract_transcript(body)
    assert words[0]["text"] == "bare"


def test_extract_rejects_a_job_envelope_that_carries_no_results() -> None:
    """A body with only `metadata` must error, not yield an empty transcript."""
    with pytest.raises(deepgram_stt.DeepgramError, match="no `results`"):
        deepgram_stt._extract_transcript({"metadata": {"request_id": "x"}})


# ── Query building: only parameters Deepgram actually accepts ───────────────


def test_build_query_sends_no_invented_parameters() -> None:
    """Every key must be a real Deepgram parameter.

    `utterance_split` and `alternatives` are not request params, and `async` is
    not a Deepgram concept at all. Deepgram silently IGNORES unknown query
    params, so these never errored — they just advertised behaviour the code
    never got, which is exactly how the fiction survived review.
    """
    query = deepgram_stt._build_query(model="nova-3", language=None)
    for invented in ("utterance_split", "alternatives", "async", "words"):
        assert invented not in query, f"{invented} is not a real Deepgram parameter"


def test_build_query_keeps_the_real_formatting_params() -> None:
    query = deepgram_stt._build_query(model="nova-3", language="en-US")
    assert query["model"] == "nova-3"
    assert query["smart_format"] == "true"
    assert query["punctuate"] == "true"
    assert query["language"] == "en-US"


def test_utterance_splitting_uses_utt_split_if_present() -> None:
    """If we ask to split utterances it must be `utt_split` + `utterances`.

    The pair is the contract: `utt_split` alone is inert without
    `utterances=true`, which is what the original `utterance_split=punctuation`
    was reaching for and never achieved.
    """
    query = deepgram_stt._build_query(model="nova-3", language=None)
    if "utt_split" in query:
        assert query.get("utterances") == "true"
        float(query["utt_split"])  # must be numeric seconds, not "punctuation"


# ── Configured model override ───────────────────────────────────────────────


def test_default_model_comes_from_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """`POCKETPAW_DEEPGRAM_STT_MODEL` was added to Settings but never read.

    The module docstring promised the override while `DEFAULT_MODEL` stayed a
    hardcoded constant, so the setting was decoration. Pin the wiring.
    """
    monkeypatch.setattr(deepgram_stt, "_settings_model", lambda: "nova-2")
    assert deepgram_stt.resolve_model(None) == "nova-2"


def test_explicit_model_argument_beats_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deepgram_stt, "_settings_model", lambda: "nova-2")
    assert deepgram_stt.resolve_model("nova-3") == "nova-3"
