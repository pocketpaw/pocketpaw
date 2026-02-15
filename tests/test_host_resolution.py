import sys
from unittest.mock import patch

import pytest

from pocketclaw.__main__ import _is_headless


# -------------------------------------------
# Helper: Host resolution logic replica
# (matches logic inside main())
# -------------------------------------------

def resolve_host(args_host, settings_host, platform, headless):
    """
    Mimics the host resolution logic inside main().
    """

    if args_host is not None:
        return args_host

    if settings_host != "127.0.0.1":
        return settings_host

    if headless:
        return "0.0.0.0"

    return "127.0.0.1"


# -------------------------------------------
# TESTS
# -------------------------------------------

def test_windows_defaults_to_localhost():
    """
    On Windows (non-headless), host should default to 127.0.0.1
    """

    with patch.object(sys, "platform", "win32"):
        host = resolve_host(
            args_host=None,
            settings_host="127.0.0.1",
            platform="win32",
            headless=False,
        )

        assert host == "127.0.0.1"


def test_headless_linux_binds_all_interfaces():
    """
    On headless Linux, host should be 0.0.0.0
    """

    with patch.object(sys, "platform", "linux"):
        host = resolve_host(
            args_host=None,
            settings_host="127.0.0.1",
            platform="linux",
            headless=True,
        )

        assert host == "0.0.0.0"


def test_explicit_host_overrides_all():
    """
    If --host is provided, it must override everything.
    """

    host = resolve_host(
        args_host="192.168.1.10",
        settings_host="127.0.0.1",
        platform="win32",
        headless=False,
    )

    assert host == "192.168.1.10"
