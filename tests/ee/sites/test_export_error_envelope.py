# tests/ee/sites/test_export_error_envelope.py — a failed export must reach the
# client as a renderable envelope, not a bare 500.
#
# Found by the frontend agent building the export panel: ``ExportUnavailable`` is a
# plain ``Exception``, and the cloud error handler maps ``CloudError`` only, so the
# POST answered with an unhandled 500 and the safe sentence the same failure had
# just written to the export row never reached the caller. The panel could not tell
# "your site's data is unreachable" from "the backend fell over", and had to reload
# the list to find out what happened.
#
# This is the same shape as the ``InvalidId`` cast ``sites/service.py`` already
# guards, and the module header there says why it matters: an ordinary exception
# escaping a CloudError-only handler surfaces as a 500 with nothing useful in it.

from __future__ import annotations

import pytest
from pocketpaw_ee.cloud._core.errors import CloudError, NotFound, ValidationError
from pocketpaw_ee.sites.export import ExportUnavailable
from pocketpaw_ee.sites.service import _export_error


def test_export_unavailable_is_not_renderable_on_its_own() -> None:
    """The premise. If this ever becomes a CloudError subclass, the converter can go
    — but silently leaving both in place would be worse than either."""
    assert not issubclass(ExportUnavailable, CloudError)


def test_a_plain_export_failure_is_promoted_to_a_renderable_error() -> None:
    err = _export_error(ExportUnavailable("This site's live data cannot be read."))

    assert isinstance(err, CloudError)
    assert err.code == "sites.export_unavailable"
    # Our own sentence survives — it is the whole reason the export said anything.
    assert "live data cannot be read" in err.message


def test_an_unexpected_failure_does_not_leak_provider_text() -> None:
    """A driver or Cloudflare string here would reach every site reader in the
    workspace. Only ``ExportUnavailable`` messages are ours to show."""
    err = _export_error(RuntimeError("mongo: connection refused to 10.0.0.4:27017"))

    assert isinstance(err, ValidationError)
    assert err.code == "sites.export_unavailable"
    assert "10.0.0.4" not in err.message
    assert "mongo" not in err.message.lower()
    assert err.message == "This site's data could not be exported."


@pytest.mark.parametrize(
    "original",
    [
        NotFound("site", "s1"),
        ValidationError("sites.not_dynamic", "not a dynamic site"),
    ],
)
def test_an_error_that_was_already_renderable_passes_through_untouched(original) -> None:
    """Promoting these would flatten a 404 into a 422 and lose the code the client
    branches on."""
    assert _export_error(original) is original
