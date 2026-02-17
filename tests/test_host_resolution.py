import sys
from types import SimpleNamespace

import pocketclaw.__main__ as main_module


class DummySettings:
    def __init__(self, web_host="127.0.0.1"):
        self.web_host = web_host


def test_explicit_host_overrides_all():
    args = SimpleNamespace(host="192.168.1.10")
    settings = DummySettings()

    host = main_module._resolve_host(args, settings)

    assert host == "192.168.1.10"


def test_config_host_used():
    args = SimpleNamespace(host=None)
    settings = DummySettings(web_host="192.168.1.20")

    host = main_module._resolve_host(args, settings)

    assert host == "192.168.1.20"


def test_windows_defaults_to_localhost(monkeypatch):
    args = SimpleNamespace(host=None)
    settings = DummySettings()

    monkeypatch.setattr(sys, "platform", "win32")

    host = main_module._resolve_host(args, settings)

    assert host == "127.0.0.1"


def test_headless_linux_binds_all_interfaces(monkeypatch):
    args = SimpleNamespace(host=None)
    settings = DummySettings()

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(main_module, "_is_headless", lambda: True)

    host = main_module._resolve_host(args, settings)

    assert host == "0.0.0.0"
