"""Tests for ee.cloud._core.errors — Phase 0 additions to the hierarchy."""

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core import errors as core_errors
from pocketpaw_ee.cloud.shared.errors import (
    CloudError as SharedCloudError,
)
from pocketpaw_ee.cloud.shared.errors import (
    ConflictError as SharedConflictError,
)
from pocketpaw_ee.cloud.shared.errors import (
    Forbidden as SharedForbidden,
)
from pocketpaw_ee.cloud.shared.errors import (
    NotFound as SharedNotFound,
)
from pocketpaw_ee.cloud.shared.errors import (
    SeatLimitError as SharedSeatLimit,
)
from pocketpaw_ee.cloud.shared.errors import (
    ValidationError as SharedValidation,
)


class TestReExports:
    """Re-exporting from shared/errors keeps a single class identity per error."""

    def test_cloud_error_is_same_class(self) -> None:
        assert core_errors.CloudError is SharedCloudError

    def test_not_found_is_same_class(self) -> None:
        assert core_errors.NotFound is SharedNotFound

    def test_forbidden_is_same_class(self) -> None:
        assert core_errors.Forbidden is SharedForbidden

    def test_conflict_is_same_class(self) -> None:
        assert core_errors.ConflictError is SharedConflictError

    def test_validation_is_same_class(self) -> None:
        assert core_errors.ValidationError is SharedValidation

    def test_seat_limit_is_same_class(self) -> None:
        assert core_errors.SeatLimitError is SharedSeatLimit


class TestRateLimited:
    def test_default_message(self) -> None:
        err = core_errors.RateLimited("api.rate_limited")
        assert err.status_code == 429
        assert err.code == "api.rate_limited"
        assert err.message == "Rate limit exceeded"

    def test_custom_message(self) -> None:
        err = core_errors.RateLimited("login.rate_limited", "Too many login attempts")
        assert err.message == "Too many login attempts"

    def test_to_dict(self) -> None:
        err = core_errors.RateLimited("api.rate_limited")
        assert err.to_dict() == {
            "error": {"code": "api.rate_limited", "message": "Rate limit exceeded"}
        }


class TestInternal:
    def test_default(self) -> None:
        err = core_errors.Internal()
        assert err.status_code == 500
        assert err.code == "internal"
        assert err.message == "Internal server error"

    def test_custom_code_and_message(self) -> None:
        err = core_errors.Internal("repo.unavailable", "Datastore offline")
        assert err.code == "repo.unavailable"
        assert err.message == "Datastore offline"


class TestWithCause:
    def test_attaches_cause_and_returns_same_error(self) -> None:
        original = ValueError("bad json")
        err = core_errors.NotFound("workspace", "abc")
        returned = core_errors.with_cause(err, original)
        assert returned is err  # same instance, allows fluent raising
        assert err.__cause__ is original

    def test_raise_with_cause_preserves_cause(self) -> None:
        try:
            try:
                raise ValueError("inner")
            except ValueError as inner:
                raise core_errors.with_cause(core_errors.Internal(), inner)
        except core_errors.CloudError as outer:
            assert isinstance(outer.__cause__, ValueError)
            assert str(outer.__cause__) == "inner"
        else:
            pytest.fail("expected CloudError")
