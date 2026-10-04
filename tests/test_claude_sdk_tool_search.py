# tests/test_claude_sdk_tool_search.py
# Pins how the Claude SDK backend hands ENABLE_TOOL_SEARCH to the Claude Code CLI.
# The CLI turns MCP tool search off when ANTHROPIC_BASE_URL is not api.anthropic.com,
# so tenants on a gateway provider (litellm, openrouter, openai_compatible, ollama,
# gemini) get every MCP tool schema upfront. ``claude_sdk_tool_search`` is an
# operator opt-in: unset adds no ENABLE_TOOL_SEARCH key (the CLI default), a
# documented value passes through, anything else is ignored with a warning, and a
# non-empty ENABLE_TOOL_SEARCH in the process env or the per-run extras wins. With
# no choice made behind a gateway, one INFO line per process says tool search is off.
# A JSON true/false (config.json, PUT /api/v1/settings) means "true"/"false": a value
# the field rejected used to knock Settings.load() back to defaults, dropping the
# whole saved config. Reuses the ``_build_options`` harness from
# test_claude_sdk_model_override.py.

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from pocketpaw.agents import claude_sdk
from pocketpaw.agents.claude_sdk import ClaudeSDKBackend
from pocketpaw.llm.providers.base import ProviderConfig
from pocketpaw.llm.providers.litellm import LiteLLMAdapter
from tests.test_claude_sdk_model_override import _make_sdk, _make_settings

_LOGGER = "pocketpaw.agents.claude_sdk"
_FIRST_PARTY = {"ANTHROPIC_API_KEY": "sk-test"}
_UNSET = object()


def _litellm_env(base_url: str = "http://localhost:4000") -> dict[str, str]:
    """The env the real litellm adapter hands the CLI."""
    config = ProviderConfig(provider="litellm", model="m", api_key="sk-gw", base_url=base_url)
    return LiteLLMAdapter().build_env_dict(config)


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    # pytest may run inside a Claude Code session that exports these.
    monkeypatch.delenv("ENABLE_TOOL_SEARCH", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.setattr(claude_sdk, "_logged_once", set())


async def _env(setting=_UNSET, provider_env=_FIRST_PARTY, *, provider="anthropic", extras=None):
    """Run ``_build_options`` and return the subprocess env it assembled."""
    overrides = {"claude_sdk_provider": provider}
    if setting is not _UNSET:
        overrides["claude_sdk_tool_search"] = setting
    sdk = _make_sdk(_make_settings(**overrides))
    if extras:
        sdk.attach_subprocess_env(extras)
    llm = MagicMock()
    llm.is_ollama = llm.is_openai_compatible = llm.is_gemini = llm.is_openrouter = False
    llm.is_litellm = provider == "litellm"
    llm.model = "claude-sonnet-4-5"
    llm.to_sdk_env.return_value = dict(provider_env)
    with (
        patch("pocketpaw.llm.client.resolve_llm_client", return_value=llm),
        patch.object(ClaudeSDKBackend, "_get_mcp_servers", return_value={}),
    ):
        built = await sdk._build_options(
            "hello",
            system_prompt="identity",
            session_key="s1",
            deny_mcp_tool_ids=frozenset(),
            allow_sdk_tools=frozenset(),
            allow_mcp_tool_ids=None,
            skill_names=frozenset(),
            stderr_sink=[],
        )
    return built.options_kwargs["env"]


def _messages(caplog, level):
    return [r.getMessage() for r in caplog.records if r.name == _LOGGER and r.levelno == level]


def _notices(caplog):
    return [m for m in _messages(caplog, logging.INFO) if "tool search" in m.lower()]


# -- pass-through ------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [None, "", "   "])
async def test_unset_leaves_the_cli_default(setting):
    env = await _env(setting)
    assert "ENABLE_TOOL_SEARCH" not in env


@pytest.mark.asyncio
async def test_a_mocked_setting_reads_as_unset_without_a_warning(caplog):
    """Most claude_sdk tests build settings as MagicMocks."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    env = await _env()
    assert "ENABLE_TOOL_SEARCH" not in env
    assert _messages(caplog, logging.WARNING) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["true", "false", "auto", "auto:5", "auto:0", "auto:100"])
async def test_documented_values_pass_through(value):
    env = await _env(value)
    assert env["ENABLE_TOOL_SEARCH"] == value


@pytest.mark.asyncio
async def test_surrounding_whitespace_is_trimmed():
    env = await _env(" auto:5 ")
    assert env["ENABLE_TOOL_SEARCH"] == "auto:5"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value", ["yes", "1", "on", "TRUE", "force", "auto:", "auto:x", "auto:-1", "auto:101"]
)
async def test_other_values_are_ignored_with_a_warning(value, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    env = await _env(value)
    assert "ENABLE_TOOL_SEARCH" not in env
    warnings = _messages(caplog, logging.WARNING)
    assert len(warnings) == 1 and repr(value) in warnings[0]


@pytest.mark.asyncio
async def test_the_invalid_value_warning_is_logged_once(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    await _env("yes")
    await _env("yes")
    assert len(_messages(caplog, logging.WARNING)) == 1


@pytest.mark.asyncio
async def test_litellm_env_unset_has_no_key():
    env = await _env(None, _litellm_env(), provider="litellm")
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:4000"
    assert "ENABLE_TOOL_SEARCH" not in env


@pytest.mark.asyncio
async def test_litellm_env_set_passes_through():
    env = await _env("true", _litellm_env(), provider="litellm")
    assert env["ANTHROPIC_BASE_URL"] == "http://localhost:4000"
    assert env["ENABLE_TOOL_SEARCH"] == "true"


# -- precedence --------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_process_env_value_wins_over_the_setting(monkeypatch, caplog):
    """The SDK layers ``options.env`` over ``os.environ``; writing nothing lets the
    CLI inherit the operator's own value."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "false")
    env = await _env("true", _litellm_env(), provider="litellm")
    assert "ENABLE_TOOL_SEARCH" not in env
    warnings = _messages(caplog, logging.WARNING)
    assert len(warnings) == 1 and "ENABLE_TOOL_SEARCH" in warnings[0]


@pytest.mark.asyncio
async def test_an_empty_process_env_value_does_not_block_the_setting(monkeypatch):
    """The CLI reads an empty ENABLE_TOOL_SEARCH as unset."""
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "")
    env = await _env("auto")
    assert env["ENABLE_TOOL_SEARCH"] == "auto"


@pytest.mark.asyncio
async def test_a_per_run_extra_wins_over_the_setting():
    env = await _env("true", extras={"ENABLE_TOOL_SEARCH": "false"})
    assert env["ENABLE_TOOL_SEARCH"] == "false"


# -- the "tool search is off" notice ------------------------------------------


@pytest.mark.asyncio
async def test_a_gateway_with_no_choice_logs_once_per_process(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    await _env(None, _litellm_env(), provider="litellm")
    await _env(None, _litellm_env(), provider="litellm")
    notices = _notices(caplog)
    assert len(notices) == 1
    assert "litellm" in notices[0] and "localhost" in notices[0]
    assert "POCKETPAW_CLAUDE_SDK_TOOL_SEARCH" in notices[0]


@pytest.mark.asyncio
async def test_a_process_env_base_url_counts(monkeypatch, caplog):
    """The CLI sees the process env when the provider env sets no base URL."""
    caplog.set_level(logging.INFO, logger=_LOGGER)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gw.example.com")
    await _env(None)
    assert len(_notices(caplog)) == 1


@pytest.mark.asyncio
async def test_the_notice_names_the_host_not_the_credentials(caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    await _env(None, _litellm_env("http://user:s3cret@gw.internal:4000"), provider="litellm")
    (notice,) = _notices(caplog)
    assert "gw.internal" in notice and "s3cret" not in notice


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_env",
    [_FIRST_PARTY, {**_FIRST_PARTY, "ANTHROPIC_BASE_URL": "https://api.anthropic.com"}],
)
async def test_no_notice_on_the_first_party_api(provider_env, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    await _env(None, provider_env)
    assert _notices(caplog) == []


@pytest.mark.asyncio
async def test_no_notice_once_the_operator_chose(monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=_LOGGER)
    await _env("false", _litellm_env(), provider="litellm")
    monkeypatch.setenv("ENABLE_TOOL_SEARCH", "true")
    await _env(None, _litellm_env(), provider="litellm")
    assert _notices(caplog) == []


# -- the setting -------------------------------------------------------------


def test_the_setting_defaults_to_unset_and_reads_from_the_environment(monkeypatch):
    from pocketpaw.config import Settings

    monkeypatch.delenv("POCKETPAW_CLAUDE_SDK_TOOL_SEARCH", raising=False)
    assert Settings().claude_sdk_tool_search == ""
    monkeypatch.setenv("POCKETPAW_CLAUDE_SDK_TOOL_SEARCH", "auto:5")
    assert Settings().claude_sdk_tool_search == "auto:5"


_JSON_VALUES = [(True, "true"), (False, "false"), (None, "")]


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """A throwaway ~/.pocketpaw: config.json and the credential store live in tmp_path."""
    import pocketpaw.config as cfg
    import pocketpaw.credentials as creds

    # Settings.load() drops any config.json key that has a POCKETPAW_ env var.
    for var in (
        "POCKETPAW_CLAUDE_SDK_TOOL_SEARCH",
        "POCKETPAW_CLAUDE_SDK_PROVIDER",
        "POCKETPAW_ANTHROPIC_API_KEY",
        "POCKETPAW_IGNORE_CONFIG_JSON",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cfg, "get_config_dir", lambda: tmp_path)
    monkeypatch.setattr(cfg, "_MIGRATION_DONE_PATH", None)
    (tmp_path / ".secrets_migrated").write_text("1")
    store = creds.CredentialStore(config_dir=tmp_path)
    monkeypatch.setattr(creds, "get_credential_store", lambda: store)
    return tmp_path


@pytest.mark.parametrize(("raw", "stored"), _JSON_VALUES)
def test_the_setting_accepts_json_true_false_and_null(raw, stored, monkeypatch):
    from pocketpaw.config import Settings

    monkeypatch.delenv("POCKETPAW_CLAUDE_SDK_TOOL_SEARCH", raising=False)
    assert Settings(claude_sdk_tool_search=raw).claude_sdk_tool_search == stored


@pytest.mark.parametrize(("raw", "stored"), _JSON_VALUES)
def test_a_config_json_boolean_loads_without_dropping_the_rest(raw, stored, config_dir):
    """A value the field rejects makes Settings.load() fall back to defaults,
    which silently drops every config.json value and stored secret."""
    import json

    import pocketpaw.credentials as creds
    from pocketpaw.config import Settings

    (config_dir / "config.json").write_text(
        json.dumps({"claude_sdk_tool_search": raw, "claude_sdk_provider": "litellm"})
    )
    creds.get_credential_store().set("anthropic_api_key", "sk-ant-kept")
    loaded = Settings.load()
    assert loaded.claude_sdk_tool_search == stored
    assert loaded.claude_sdk_provider == "litellm"
    assert loaded.anthropic_api_key == "sk-ant-kept"


@pytest.mark.asyncio
async def test_a_boolean_put_through_the_settings_api_reaches_the_cli(config_dir):
    """PUT setattrs the raw JSON value (no validate_assignment), save() writes it
    as-is, and the next Settings.load() is what the backend runs on."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from pocketpaw.api.v1.settings import router
    from pocketpaw.config import Settings

    Settings(claude_sdk_provider="litellm", anthropic_api_key="sk-ant-kept").save()
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    with patch("pocketpaw.config.get_settings"):
        resp = TestClient(app).put(
            "/api/v1/settings", json={"settings": {"claude_sdk_tool_search": True}}
        )
    assert resp.status_code == 200
    loaded = Settings.load()
    assert loaded.claude_sdk_provider == "litellm"
    assert loaded.anthropic_api_key == "sk-ant-kept"
    env = await _env(loaded.claude_sdk_tool_search, _litellm_env(), provider="litellm")
    assert env["ENABLE_TOOL_SEARCH"] == "true"


@pytest.mark.asyncio
@pytest.mark.parametrize(("raw", "value"), [(True, "true"), (False, "false")])
async def test_a_boolean_on_a_live_settings_object_passes_through(raw, value):
    """An in-memory setattr skips the field validator."""
    env = await _env(raw)
    assert env["ENABLE_TOOL_SEARCH"] == value
