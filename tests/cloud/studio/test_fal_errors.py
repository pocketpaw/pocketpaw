# tests/cloud/studio/test_fal_errors.py — what a fal rejection tells the user.
#
# Created 2026-09-05 (studio-surface-fal-errors).
#
# A content-policy rejection used to reach the user as a 502 whose detail was
# `str(exception)` — fal's whole record list, base64 payload and all. Nobody
# reads that, so the generation appeared to fail for no reason while the reason
# sat in a log behind a traceback.
#
# Two properties are load-bearing and neither is obvious from a passing request:
#
#   * The message must NEVER carry the request payload. It is base64 images and
#     audio; it makes the toast unreadable and the log useless.
#   * A refusal must NOT be a 502. The user can fix it by changing an input, and
#     calling it an outage both misleads them and tells our monitoring that fal
#     is down while it is working correctly.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud.studio import fal_errors


class _FalErr(Exception):
    """Stands in for fal_client.FalClientHTTPError, which carries the parsed
    body as args[0]."""


def _policy_error(field: str = "image_urls") -> _FalErr:
    return _FalErr(
        [
            {
                "loc": ["body", field],
                "msg": (
                    "The images or videos provided may contain likenesses of real "
                    "people or other private information that cannot be processed."
                ),
                "type": "content_policy_violation",
                "ctx": {"extra_info": {"reason": "partner_validation_failed"}},
                "input": {
                    "prompt": "@Image1 stands at the cliff edge",
                    "image_urls": ["data:image/jpeg;base64," + "QUJD" * 2000],
                },
            }
        ]
    )


class TestThePayloadNeverEscapes:
    def test_the_message_carries_no_base64(self) -> None:
        """The whole point. Mutation that must break this: include `input`."""
        failure = fal_errors.parse_fal_error(_policy_error(), action="video generation")
        assert "base64" not in failure.message
        assert "QUJD" not in failure.message

    def test_the_log_line_carries_no_base64_either(self) -> None:
        """The log was the other half of the problem — a traceback with the
        request body in it, on every rejection."""
        failure = fal_errors.parse_fal_error(_policy_error(), action="video generation")
        assert "QUJD" not in failure.log_line
        assert "base64" not in failure.log_line

    def test_an_unparseable_body_is_truncated_rather_than_dumped(self) -> None:
        """A body we cannot read can still be a whole base64 payload."""
        failure = fal_errors.parse_fal_error(_FalErr("x" * 5000), action="video generation")
        assert len(failure.message) < 400
        assert failure.message.endswith("…")


class TestTheMessageIsActionable:
    def test_it_names_what_was_rejected(self) -> None:
        failure = fal_errors.parse_fal_error(_policy_error(), action="video generation")
        assert "reference image" in failure.message

    def test_it_keeps_fals_own_explanation(self) -> None:
        """Our framing helps; replacing the provider's reason would not."""
        failure = fal_errors.parse_fal_error(_policy_error(), action="video generation")
        assert "likenesses of real people" in failure.message

    def test_the_field_label_is_the_users_word_not_the_wire_name(self) -> None:
        failure = fal_errors.parse_fal_error(_policy_error("audio_urls"), action="video generation")
        assert "music track" in failure.message
        assert "audio_urls" not in failure.message

    def test_a_self_describing_error_does_not_stutter(self) -> None:
        """ "That audio track is too short in the music track" says it twice."""
        exc = _FalErr(
            [
                {
                    "loc": ["body", "audio_urls", 0],
                    "msg": "Audio duration is too short. Minimum is 1.8 seconds.",
                    "type": "audio_duration_too_short",
                }
            ]
        )
        failure = fal_errors.parse_fal_error(exc, action="video generation")
        assert "in the music track" not in failure.message
        assert "too short" in failure.message

    def test_extra_records_are_counted_not_concatenated(self) -> None:
        exc = _FalErr(
            [
                {"loc": ["body", "image_urls"], "msg": "First problem.", "type": "invalid_image"},
                {"loc": ["body", "audio_urls"], "msg": "Second problem.", "type": "invalid_audio"},
            ]
        )
        failure = fal_errors.parse_fal_error(exc, action="video generation")
        assert "First problem." in failure.message
        assert "+1 more" in failure.message


class TestFaultClassification:
    @pytest.mark.parametrize(
        "code",
        [
            "content_policy_violation",
            "audio_duration_too_short",
            "file_too_large",
            "unsupported_format",
        ],
    )
    def test_a_refusal_is_the_users_to_fix(self, code: str) -> None:
        """These become a 4xx. Mutation that must break this: drop the code from
        _CLIENT_FAULT_TYPES, and a policy hit is reported as an outage again."""
        exc = _FalErr([{"loc": ["body", "image_urls"], "msg": "no", "type": code}])
        assert fal_errors.parse_fal_error(exc).client_fault is True

    def test_an_unrecognised_failure_stays_an_upstream_error(self) -> None:
        """Erring toward 502 keeps a real fal incident visible instead of
        blaming the user for it."""
        exc = _FalErr([{"loc": ["body"], "msg": "internal", "type": "internal_server_error"}])
        assert fal_errors.parse_fal_error(exc).client_fault is False

    def test_an_unstructured_exception_is_not_blamed_on_the_user(self) -> None:
        assert fal_errors.parse_fal_error(_FalErr("connection reset")).client_fault is False


class TestItNeverMakesThingsWorse:
    """This runs inside an exception handler. A formatter that throws would
    replace a useful error with a confusing one."""

    @pytest.mark.parametrize(
        "exc",
        [
            _FalErr(),
            _FalErr(None),
            _FalErr([]),
            _FalErr([{"no": "recognised keys"}]),
            _FalErr([{"loc": "not-a-list", "msg": None, "type": 42}]),
            _FalErr('{"detail": [{"msg": "from json", "type": "value_error"}]}'),
        ],
    )
    def test_every_shape_produces_a_message(self, exc: Exception) -> None:
        failure = fal_errors.parse_fal_error(exc, action="video generation")
        assert isinstance(failure.message, str)
        assert failure.message.strip()

    def test_a_json_string_body_is_still_parsed(self) -> None:
        exc = _FalErr('[{"loc": ["body", "prompt"], "msg": "Too long.", "type": "value_error"}]')
        failure = fal_errors.parse_fal_error(exc, action="video generation")
        assert "Too long." in failure.message
        assert failure.client_fault is True
