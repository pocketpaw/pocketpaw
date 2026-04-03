import sys
from io import StringIO
from unittest.mock import patch

from pocketpaw.__main__ import check_python_version


def test_no_warning_on_313():
    """Python 3.13 is supported — no warning should be emitted."""
    buf = StringIO()
    with patch.object(sys, "version_info", (3, 13, 0, "final", 0)):
        with patch("sys.stderr", buf):
            check_python_version()
    assert buf.getvalue() == ""


def test_warning_on_314():
    """Python 3.14 is the threshold — warning must be emitted."""
    buf = StringIO()
    with patch.object(sys, "version_info", (3, 14, 0, "final", 0)):
        with patch("sys.stderr", buf):
            check_python_version()
    assert "3.14+" in buf.getvalue()
    assert "3.11 or 3.12" in buf.getvalue()


def test_warning_on_315():
    """Python 3.15+ should also trigger the warning."""
    buf = StringIO()
    with patch.object(sys, "version_info", (3, 15, 0, "final", 0)):
        with patch("sys.stderr", buf):
            check_python_version()
    assert "3.14+" in buf.getvalue()