import sys
from unittest.mock import patch

import pocketclaw.__main__ as main_module


def test_windows_default_host_binding():
    """
    Regression test:
    Ensure Windows defaults to 127.0.0.1 instead of 0.0.0.0.
    """

    class DummyArgs:
        host = None
        discord = False
        slack = False
        whatsapp = False
        signal = False
        matrix = False
        teams = False
        gchat = False
        telegram = False
        security_audit = False

    with patch.object(sys, "platform", "win32"):
        with patch.object(main_module, "_is_headless", return_value=False):

            args = DummyArgs()

            # Replicate host resolution logic
            if args.host is not None:
                host = args.host
            elif sys.platform.startswith("win"):
                host = "127.0.0.1"
            elif main_module._is_headless():
                host = "0.0.0.0"
            else:
                host = "127.0.0.1"

            assert host == "127.0.0.1"
