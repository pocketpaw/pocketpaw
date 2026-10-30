# tests/test_claude_sdk_mcp_output_cap.py
# Pins the MCP tool-output cap handed to the Claude Code CLI subprocess.
# Claude Code truncates (or diverts to a file) any MCP tool result above
# ``MAX_MCP_OUTPUT_TOKENS`` (its default is 25,000 tokens). On /sites the agent
# cannot read that file back, so ``read_site_source`` on a larger site came back
# incomplete and the agent re-called it in a loop. The backend always sets the
# variable from ``claude_sdk_max_mcp_output_tokens`` unless the parent process or
# the per-run extra env already carries one. Reuses the ``_build_options`` harness
# from test_claude_sdk_model_override.py.

from __future__ import annotations

import pytest

from tests.test_claude_sdk_model_override import _build, _make_sdk, _make_settings


@pytest.mark.asyncio
async def test_the_cap_is_always_in_the_subprocess_env(monkeypatch):
    monkeypatch.delenv("MAX_MCP_OUTPUT_TOKENS", raising=False)
    sdk = _make_sdk(_make_settings(claude_sdk_max_mcp_output_tokens=200_000))
    kwargs = await _build(sdk)
    assert kwargs["env"]["MAX_MCP_OUTPUT_TOKENS"] == "200000"


@pytest.mark.asyncio
async def test_a_mocked_setting_falls_back_to_the_default(monkeypatch):
    """Settings are sometimes MagicMocks; a non-int value must not leak into env."""
    monkeypatch.delenv("MAX_MCP_OUTPUT_TOKENS", raising=False)
    sdk = _make_sdk()
    kwargs = await _build(sdk)
    assert kwargs["env"]["MAX_MCP_OUTPUT_TOKENS"] == "200000"


@pytest.mark.asyncio
async def test_the_configured_value_is_used(monkeypatch):
    monkeypatch.delenv("MAX_MCP_OUTPUT_TOKENS", raising=False)
    sdk = _make_sdk(_make_settings(claude_sdk_max_mcp_output_tokens=90_000))
    kwargs = await _build(sdk)
    assert kwargs["env"]["MAX_MCP_OUTPUT_TOKENS"] == "90000"


@pytest.mark.asyncio
async def test_a_parent_env_value_is_not_overridden(monkeypatch):
    """The SDK layers ``options.env`` over ``os.environ``; writing our default
    would clobber an operator's explicit value."""
    monkeypatch.setenv("MAX_MCP_OUTPUT_TOKENS", "12345")
    sdk = _make_sdk(_make_settings(claude_sdk_max_mcp_output_tokens=200_000))
    kwargs = await _build(sdk)
    assert kwargs.get("env", {}).get("MAX_MCP_OUTPUT_TOKENS") in (None, "12345")


@pytest.mark.asyncio
async def test_an_extra_subprocess_env_value_is_not_overridden(monkeypatch):
    monkeypatch.delenv("MAX_MCP_OUTPUT_TOKENS", raising=False)
    sdk = _make_sdk(_make_settings(claude_sdk_max_mcp_output_tokens=200_000))
    sdk.attach_subprocess_env({"MAX_MCP_OUTPUT_TOKENS": "5000"})
    kwargs = await _build(sdk)
    assert kwargs["env"]["MAX_MCP_OUTPUT_TOKENS"] == "5000"


def test_the_setting_reads_from_the_environment(monkeypatch):
    from pocketpaw.config import Settings

    monkeypatch.setenv("POCKETPAW_CLAUDE_SDK_MAX_MCP_OUTPUT_TOKENS", "150000")
    assert Settings().claude_sdk_max_mcp_output_tokens == 150_000
