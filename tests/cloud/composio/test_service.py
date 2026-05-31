"""Composio service tests — namespacing, client cache, disabled gate."""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from pocketpaw_ee.cloud._core.context import RequestContext, ScopeKind
from pocketpaw_ee.cloud._core.errors import Internal, ValidationError
from pocketpaw_ee.cloud.composio import service as composio_service
from pocketpaw_ee.cloud.composio.domain import ComposioUserId

from pocketpaw.config import Settings


def _ctx(user_id: str = "user_123", workspace_id: str | None = "ws_acme") -> RequestContext:
    return RequestContext(
        user_id=user_id,
        workspace_id=workspace_id,
        request_id="req_test",
        scope=ScopeKind.NONE,
        started_at=datetime.now(UTC),
    )


def _enabled_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "composio_api_key": "ck_test",
        "composio_enterprise_id": "ent_acme",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _reset_client_cache() -> None:
    composio_service.reset_client_cache_for_tests()
    yield
    composio_service.reset_client_cache_for_tests()


@pytest.fixture
def stub_composio_module(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Install a fake ``composio`` module so ``service._get_client`` can
    import it without the real SDK being present. Returns the ``Composio``
    class mock so tests can assert on construction args.
    """
    fake_module = types.ModuleType("composio")
    composio_cls = MagicMock(name="Composio")
    # Each call returns a fresh client mock so caching is observable.
    composio_cls.side_effect = lambda *a, **kw: MagicMock(name="client", init_args=(a, kw))
    fake_module.Composio = composio_cls  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "composio", fake_module)
    return composio_cls


# ---------------------------------------------------------------------------
# Domain — ComposioUserId
# ---------------------------------------------------------------------------


def test_composio_user_id_str_format() -> None:
    uid = ComposioUserId(enterprise_id="ent_acme", user_id="user_42")
    assert str(uid) == "ent_acme:user_42"


def test_composio_user_id_requires_enterprise() -> None:
    with pytest.raises(ValueError, match="enterprise_id"):
        ComposioUserId(enterprise_id="", user_id="user_42")


def test_composio_user_id_requires_user() -> None:
    with pytest.raises(ValueError, match="user_id"):
        ComposioUserId(enterprise_id="ent_acme", user_id="")


def test_composio_user_id_rejects_separator_in_enterprise() -> None:
    with pytest.raises(ValueError, match="separator"):
        ComposioUserId(enterprise_id="ent:bad", user_id="user_42")


# ---------------------------------------------------------------------------
# is_enabled
# ---------------------------------------------------------------------------


def test_is_enabled_false_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POCKETPAW_COMPOSIO_API_KEY", raising=False)
    monkeypatch.delenv("POCKETPAW_COMPOSIO_ENTERPRISE_ID", raising=False)
    s = Settings(_env_file=None)
    assert composio_service.is_enabled(s) is False


def test_is_enabled_true_when_both_set() -> None:
    s = _enabled_settings()
    assert composio_service.is_enabled(s) is True


# ---------------------------------------------------------------------------
# composio_user_id (namespacing)
# ---------------------------------------------------------------------------


def test_composio_user_id_namespacing() -> None:
    s = _enabled_settings()
    ctx = _ctx(user_id="user_42")
    uid = composio_service.composio_user_id(ctx, s)
    assert uid.enterprise_id == "ent_acme"
    assert uid.user_id == "user_42"
    assert str(uid) == "ent_acme:user_42"


def test_composio_user_id_raises_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POCKETPAW_COMPOSIO_API_KEY", raising=False)
    monkeypatch.delenv("POCKETPAW_COMPOSIO_ENTERPRISE_ID", raising=False)
    s = Settings(_env_file=None)
    with pytest.raises(ValidationError, match="composio.disabled"):
        composio_service.composio_user_id(_ctx(), s)


# ---------------------------------------------------------------------------
# Client caching
# ---------------------------------------------------------------------------


async def test_get_client_caches_singleton(
    stub_composio_module: MagicMock,
) -> None:
    s = _enabled_settings()
    c1 = await composio_service._get_client(s)
    c2 = await composio_service._get_client(s)
    assert c1 is c2
    stub_composio_module.assert_called_once()


async def test_get_client_raises_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POCKETPAW_COMPOSIO_API_KEY", raising=False)
    monkeypatch.delenv("POCKETPAW_COMPOSIO_ENTERPRISE_ID", raising=False)
    s = Settings(_env_file=None)
    with pytest.raises(ValidationError, match="composio.disabled"):
        await composio_service._get_client(s)


async def test_get_client_passes_base_url_when_set(
    stub_composio_module: MagicMock,
) -> None:
    s = _enabled_settings(composio_base_url="https://composio.internal")
    await composio_service._get_client(s)
    _, kwargs = stub_composio_module.call_args
    assert kwargs.get("base_url") == "https://composio.internal"
    assert kwargs.get("api_key") == "ck_test"


async def test_get_client_omits_base_url_when_unset(
    stub_composio_module: MagicMock,
) -> None:
    s = _enabled_settings()
    await composio_service._get_client(s)
    _, kwargs = stub_composio_module.call_args
    assert "base_url" not in kwargs


async def test_get_client_raises_when_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure ``composio`` is not importable
    monkeypatch.delitem(sys.modules, "composio", raising=False)

    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "composio":
            raise ImportError("composio not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", fake_import)

    s = _enabled_settings()
    with pytest.raises(Internal, match="composio.sdk_missing"):
        await composio_service._get_client(s)


# ---------------------------------------------------------------------------
# list_available_toolkits  (Connectors-panel admin discovery helper)
# ---------------------------------------------------------------------------


async def test_list_available_toolkits_returns_slugs(
    stub_composio_module: MagicMock,
) -> None:
    s = _enabled_settings()
    client = await composio_service._get_client(s)
    tk_gmail = MagicMock(slug="gmail")
    tk_slack = MagicMock(slug="slack")
    # Real SDK returns a ToolkitListResponse with ``.items``; mirror that.
    response = MagicMock()
    response.items = [tk_gmail, tk_slack]
    client.toolkits.list.return_value = response

    slugs = await composio_service.list_available_toolkits(s)
    assert slugs == ["gmail", "slack"]


async def test_list_available_toolkits_wraps_errors(
    stub_composio_module: MagicMock,
) -> None:
    s = _enabled_settings()
    client = await composio_service._get_client(s)
    client.toolkits.list.side_effect = RuntimeError("upstream")

    with pytest.raises(Internal, match="composio.toolkit_list_failed"):
        await composio_service.list_available_toolkits(s)
