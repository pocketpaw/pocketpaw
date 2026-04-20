from ee.cloud.files.errors import (
    CrossScopeMove,
    EntryNotFound,
    FilesForbidden,
    MountReadonly,
    ProviderUnsupported,
)
from ee.cloud.files.events import FileAdded, FileMoved, FileRemoved, FileUpdated


def test_errors_have_codes():
    assert ProviderUnsupported.code == "files.operation_unsupported"
    assert CrossScopeMove.http_status == 409
    assert EntryNotFound.http_status == 404
    assert MountReadonly.http_status == 403
    assert FilesForbidden.code == "files.forbidden"
    assert FilesForbidden.http_status == 403


def test_events_importable():
    assert FileAdded and FileUpdated and FileRemoved and FileUpdated and FileMoved
