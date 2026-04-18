from __future__ import annotations

from ee.cloud.shared.errors import (
    CloudError,
    ConflictError,
    Forbidden,
    NotFound,
    SeatLimitError,
    ValidationError,
)


def test_cloud_error_base():
    err = CloudError(404, "test.not_found", "Thing not found")
    assert err.status_code == 404
    assert err.code == "test.not_found"
    assert err.message == "Thing not found"


def test_not_found():
    err = NotFound("group", "abc123")
    assert err.status_code == 404
    assert err.code == "group.not_found"
    assert "abc123" in err.message


def test_not_found_without_id():
    err = NotFound("workspace")
    assert err.status_code == 404
    assert err.code == "workspace.not_found"
    assert "workspace" in err.message.lower()


def test_forbidden():
    err = Forbidden("workspace.not_member")
    assert err.status_code == 403
    assert err.code == "workspace.not_member"
    assert err.message == "Access denied"


def test_forbidden_custom_message():
    err = Forbidden("workspace.not_member", "You are not a member")
    assert err.status_code == 403
    assert err.message == "You are not a member"


def test_conflict():
    err = ConflictError("workspace.slug_taken", "Slug already in use")
    assert err.status_code == 409
    assert err.code == "workspace.slug_taken"
    assert err.message == "Slug already in use"


def test_validation_error():
    err = ValidationError("message.too_long", "Max 10000 chars")
    assert err.status_code == 422
    assert err.code == "message.too_long"
    assert err.message == "Max 10000 chars"


def test_seat_limit():
    err = SeatLimitError(seats=5)
    assert err.status_code == 402
    assert "5" in err.message


def test_cloud_error_to_dict():
    err = NotFound("group", "abc123")
    d = err.to_dict()
    assert d == {"error": {"code": "group.not_found", "message": err.message}}


def test_cloud_error_is_exception():
    err = CloudError(500, "internal", "Something broke")
    assert isinstance(err, Exception)


def test_cloud_error_str():
    err = CloudError(404, "test.not_found", "Thing not found")
    assert "test.not_found" in str(err)
    assert "Thing not found" in str(err)
