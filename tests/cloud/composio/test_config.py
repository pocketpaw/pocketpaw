"""Composio config wiring — env-var parsing and required-fields validation."""

from __future__ import annotations

import pytest

from pocketpaw.config import Settings


def test_composio_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POCKETPAW_COMPOSIO_API_KEY", raising=False)
    monkeypatch.delenv("POCKETPAW_COMPOSIO_ENTERPRISE_ID", raising=False)
    monkeypatch.delenv("POCKETPAW_COMPOSIO_TOOLKITS", raising=False)
    s = Settings(_env_file=None)
    assert s.composio_api_key is None
    assert s.composio_enterprise_id is None
    assert s.composio_toolkits == []
    assert s.composio_base_url is None
    assert s.composio_connect_link_inline is True


def test_composio_enabled_requires_enterprise_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POCKETPAW_COMPOSIO_API_KEY", "ck_xxx")
    monkeypatch.delenv("POCKETPAW_COMPOSIO_ENTERPRISE_ID", raising=False)
    with pytest.raises(ValueError, match="composio_enterprise_id"):
        Settings(_env_file=None)


def test_composio_toolkits_csv_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POCKETPAW_COMPOSIO_API_KEY", "ck_xxx")
    monkeypatch.setenv("POCKETPAW_COMPOSIO_ENTERPRISE_ID", "ent_acme")
    monkeypatch.setenv("POCKETPAW_COMPOSIO_TOOLKITS", "gmail, slack ,github")
    s = Settings(_env_file=None)
    assert s.composio_toolkits == ["gmail", "slack", "github"]


def test_composio_toolkits_blank_entries_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POCKETPAW_COMPOSIO_API_KEY", "ck_xxx")
    monkeypatch.setenv("POCKETPAW_COMPOSIO_ENTERPRISE_ID", "ent_acme")
    monkeypatch.setenv("POCKETPAW_COMPOSIO_TOOLKITS", " , gmail, ,")
    s = Settings(_env_file=None)
    assert s.composio_toolkits == ["gmail"]


def test_composio_base_url_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("POCKETPAW_COMPOSIO_API_KEY", "ck_xxx")
    monkeypatch.setenv("POCKETPAW_COMPOSIO_ENTERPRISE_ID", "ent_acme")
    monkeypatch.setenv("POCKETPAW_COMPOSIO_BASE_URL", "https://composio.internal.example")
    s = Settings(_env_file=None)
    assert s.composio_base_url == "https://composio.internal.example"
