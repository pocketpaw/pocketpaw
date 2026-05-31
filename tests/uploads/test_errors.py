from pocketpaw.uploads.errors import (
    AccessDenied,
    EmptyFile,
    NotFound,
    StorageFailure,
    TooLarge,
    UnsupportedMime,
    UploadError,
)


def test_all_errors_inherit_upload_error():
    for cls in (TooLarge, UnsupportedMime, EmptyFile, NotFound, AccessDenied, StorageFailure):
        assert issubclass(cls, UploadError)


def test_errors_carry_code_attribute():
    assert TooLarge("25mb").code == "too_large"
    assert UnsupportedMime("image/tiff").code == "unsupported_mime"
    assert EmptyFile().code == "empty"
    assert NotFound().code == "not_found"
    assert AccessDenied().code == "access_denied"
    assert StorageFailure("disk full").code == "storage_error"


def test_upload_error_preserves_message():
    err = TooLarge("file is 40MB")
    assert str(err) == "file is 40MB"
