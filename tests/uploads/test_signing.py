"""Unit tests for upload grant signing."""

from __future__ import annotations

import time

from pocketpaw.uploads.signing import sign_grant, verify_grant


def test_sign_and_verify_roundtrip():
    token, exp = sign_grant("file-1", "secret")
    assert verify_grant("file-1", token, "secret")
    assert exp > int(time.time())


def test_reject_wrong_file_id():
    token, _ = sign_grant("file-1", "secret")
    assert not verify_grant("file-2", token, "secret")


def test_reject_wrong_secret():
    token, _ = sign_grant("file-1", "secret")
    assert not verify_grant("file-1", token, "other")


def test_reject_malformed_token():
    assert not verify_grant("file-1", "not-a-token", "secret")
    assert not verify_grant("file-1", "", "secret")
    assert not verify_grant("file-1", "abc.def.ghi", "secret")
    assert not verify_grant("file-1", "notanumber.deadbeef", "secret")


def test_reject_expired():
    token, _ = sign_grant("file-1", "secret", ttl_seconds=-1)
    assert not verify_grant("file-1", token, "secret")


def test_sig_not_stripped_across_file_ids():
    # Two files share the same exp but produce distinct signatures.
    t1, _ = sign_grant("file-1", "secret", ttl_seconds=60)
    t2, _ = sign_grant("file-2", "secret", ttl_seconds=60)
    assert t1 != t2
