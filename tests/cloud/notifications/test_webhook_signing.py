# tests/cloud/notifications/test_webhook_signing.py
# The outbound webhook signature contract (X-Paw-Timestamp / X-Paw-Signature
# v1=HMAC-SHA256(secret, "{ts}.{body}")). The known vector was cross-checked with
# ``openssl dgst -sha256 -hmac``, so a receiver in any language can test against
# the same numbers the docs quote.

from __future__ import annotations

from pocketpaw_ee.cloud.notifications import webhook_signing as ws

SECRET = "whsec_test"
TS = "1700000000"
BODY = '{"id":"evt_1","type":"lead.captured"}'
EXPECTED = "35aea954dafaba38bb223bdd493656857236540655f31dc96809ce7e59e0abe9"


def test_known_vector() -> None:
    assert ws.compute_signature(SECRET, TS, BODY) == EXPECTED
    assert ws.signature_header(SECRET, TS, BODY) == f"v1={EXPECTED}"


def test_sign_headers_carry_timestamp_and_signature() -> None:
    headers = ws.sign_headers(SECRET, BODY, now=1700000000.9)
    assert headers[ws.TIMESTAMP_HEADER] == TS
    assert headers[ws.SIGNATURE_HEADER] == f"v1={EXPECTED}"
    assert headers["Content-Type"] == "application/json"


def test_verify_accepts_a_fresh_valid_signature() -> None:
    assert ws.verify_signature(SECRET, TS, BODY, f"v1={EXPECTED}", now=1700000060)
    # bytes body works too (what a receiver usually has)
    assert ws.verify_signature(SECRET, TS, BODY.encode(), f"v1={EXPECTED}", now=1700000000)


def test_verify_rejects_tampering_wrong_secret_and_replay() -> None:
    assert not ws.verify_signature(SECRET, TS, BODY + " ", f"v1={EXPECTED}", now=1700000000)
    assert not ws.verify_signature("other", TS, BODY, f"v1={EXPECTED}", now=1700000000)
    assert not ws.verify_signature(SECRET, TS, BODY, EXPECTED, now=1700000000)  # no v1=
    # Outside the 5-minute window: a captured delivery can't be replayed later.
    assert not ws.verify_signature(SECRET, TS, BODY, f"v1={EXPECTED}", now=1700000301)
    assert not ws.verify_signature(SECRET, "not-a-number", BODY, f"v1={EXPECTED}")


def test_encode_event_is_the_signed_body() -> None:
    event = ws.build_event(event_id="evt_1", event_type="lead.captured", created_at="t", data={})
    assert (
        ws.encode_event(event) == '{"id":"evt_1","type":"lead.captured","created_at":"t","data":{}}'
    )
