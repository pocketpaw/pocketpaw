"""Regression test: disable-thinking must actually reach the model.

Past bug: we set ``kwargs["extra_body"]`` and ``kwargs["reasoning_effort"]``
at the top level. ChatLiteLLM's Pydantic constructor silently drops any
kwarg not in its declared field set, so neither value ever made it to
DeepSeek and thinking stayed on regardless of the env var.

Fix: nest both under ``model_kwargs`` for the litellm provider. This
test pins that contract.
"""

from __future__ import annotations

from pocketpaw.agents.deep_agents import DeepAgentsBackend
from pocketpaw.config import Settings


def _build(disable: bool) -> object:
    s = Settings(
        agent_backend="deep_agents",
        deep_agents_model="litellm:litellm_proxy/deepseek-v4-flash",
        deep_agents_disable_thinking=disable,
        litellm_api_base="http://example.local",
        litellm_api_key="not-needed",
    )
    b = DeepAgentsBackend(s)
    b._sdk_available = True
    return b._build_model()


def test_disable_thinking_reaches_chat_model_via_model_kwargs() -> None:
    model = _build(disable=True)
    mk = getattr(model, "model_kwargs", {}) or {}
    assert mk.get("extra_body") == {"thinking": {"type": "disabled"}}
    # reasoning_effort must NOT be set alongside disable — they are
    # contradictory params and at least one provider was observed
    # keeping thinking on when both were sent.
    assert "reasoning_effort" not in mk


def test_thinking_kwargs_absent_when_flag_false() -> None:
    model = _build(disable=False)
    mk = getattr(model, "model_kwargs", {}) or {}
    assert "extra_body" not in mk
    assert "reasoning_effort" not in mk


def test_disable_thinking_does_not_leak_into_top_level_kwargs() -> None:
    """Defensive: ChatLiteLLM drops unknown top-level kwargs silently,
    so setting them at the top level would be a no-op. Verify the
    model exposes neither attribute at the top level — they must live
    inside model_kwargs."""
    model = _build(disable=True)
    assert not hasattr(model, "extra_body") or getattr(model, "extra_body", None) is None
    assert (
        not hasattr(model, "reasoning_effort") or getattr(model, "reasoning_effort", None) is None
    )
