# An agent run must send an explicit max_tokens.
# Created: 2026-08-16 (feat/agent-max-output-tokens).
#
# No agent backend sent one. That is not a neutral omission: OpenRouter prices
# its PRE-FLIGHT credit check against max_tokens and substitutes the model's own
# ceiling when none is given, so a reply that would run a few hundred tokens was
# refused with
#
#   402 - You requested up to 65536 tokens, but can only afford 6627.
#
# The two halves are tested separately on purpose: the resolver is pure and gets
# its branches pinned directly, and one surface test proves the number actually
# reaches ``run_stream_events`` — a resolver returning the right value into a
# run that never sends it is the failure this whole change exists to avoid.

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from pocketpaw.agents.model_limits import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    model_output_ceiling,
    resolve_max_output_tokens,
)
from pocketpaw.config import Settings


def _settings(**overrides) -> Settings:
    base: dict[str, Any] = {
        "pydantic_ai_model": "litellm:deepseek/deepseek-v4-flash",
        "litellm_api_base": "http://localhost:4000",
        "litellm_api_key": "sk-test",
    }
    base.update(overrides)
    return Settings(**base)


# ---------------------------------------------------------------------------
# the resolver
# ---------------------------------------------------------------------------


def test_a_run_sends_a_cap_by_default():
    """The whole point: with no configuration, something is sent.

    Before this, the answer was None on every model and every backend.
    """
    assert resolve_max_output_tokens("litellm", "deepseek/deepseek-v4-flash", _settings()) == (
        DEFAULT_MAX_OUTPUT_TOKENS
    )


def test_an_operator_value_replaces_the_default():
    got = resolve_max_output_tokens(
        "litellm", "deepseek/deepseek-v4-flash", _settings(agent_max_output_tokens=4096)
    )
    assert got == 4096


def test_a_negative_value_opts_out_entirely():
    """The escape hatch back to the old behaviour, for a gateway that dislikes
    an explicit cap. It must send NOTHING, not zero — ``max_tokens=0`` is a
    request for an empty completion on several providers."""
    got = resolve_max_output_tokens(
        "litellm", "deepseek/deepseek-v4-flash", _settings(agent_max_output_tokens=-1)
    )
    assert got is None


def test_an_unknown_model_still_gets_the_default():
    """The pinned metadata trails new releases by design, so "not in the map" is
    ordinary rather than exceptional. It must not mean "send no cap" — that is
    the broken state, and brand-new models are exactly where it bites."""
    assert resolve_max_output_tokens("litellm", "no-such-model-xyz", _settings()) == (
        DEFAULT_MAX_OUTPUT_TOKENS
    )


def test_a_smaller_model_ceiling_lowers_the_cap():
    """The clamp's real job: don't ask a 4k model for 8k.

    Skipped rather than asserted blind if the pinned map doesn't carry the
    model — the point is the CLAMP, and a test that silently stops exercising it
    is worse than one that says so.
    """
    ceiling = model_output_ceiling("openai", "gpt-3.5-turbo")
    if not ceiling or ceiling >= DEFAULT_MAX_OUTPUT_TOKENS:
        pytest.skip("pinned metadata has no smaller-than-default ceiling for gpt-3.5-turbo")

    assert resolve_max_output_tokens("openai", "gpt-3.5-turbo", _settings()) == ceiling


def test_the_model_ceiling_can_never_raise_the_cap():
    """The finding that shaped this design, pinned so it cannot regress.

    On 2026-08-16 ``deepseek/deepseek-v4-flash`` advertised max_output_tokens
    8192 in the morning and 393216 the same afternoon, with max_input_tokens at
    1000000 — a context window leaking into the output field. Resolving the
    ceiling and SENDING it would have taken the credit reservation from 65536 to
    393216 and made the 402 six times worse while looking like a fix.

    So a ceiling above the target must be ignored, whatever the metadata says.
    """
    with patch("pocketpaw.agents.model_limits.model_output_ceiling", return_value=393_216):
        got = resolve_max_output_tokens("litellm", "deepseek/deepseek-v4-flash", _settings())

    assert got == DEFAULT_MAX_OUTPUT_TOKENS
    assert got < 393_216


def test_a_junk_setting_does_not_break_a_run():
    """Settings can arrive from a UI, a YAML file, or an env var."""
    assert (
        resolve_max_output_tokens(
            "litellm", "deepseek/deepseek-v4-flash", _settings(agent_max_output_tokens=0)
        )
        == DEFAULT_MAX_OUTPUT_TOKENS
    )


def test_a_resolver_failure_never_breaks_a_run():
    """A token cap is a cost optimisation, not a feature. If anything in the
    lookup throws, the run proceeds uncapped rather than dying."""
    from pocketpaw.agents.pydantic_ai import PydanticAIBackend

    backend = PydanticAIBackend(_settings())
    with patch(
        "pocketpaw.agents.model_limits.resolve_max_output_tokens",
        side_effect=RuntimeError("metadata exploded"),
    ):
        assert backend._resolve_max_output_tokens() is None


# ---------------------------------------------------------------------------
# the surface — the number reaches the run
# ---------------------------------------------------------------------------


async def _captured_run_kwargs(backend) -> dict:
    """Drive a real ``backend.run`` and return the kwargs it handed pydantic-ai.

    Patches ``run_stream_events`` rather than the resolver, so this fails if the
    wiring is dropped even while every resolver test above still passes. That is
    the failure mode worth guarding: a correct number that never leaves the
    process changes nothing about the 402.
    """
    captured: dict = {}

    class _Stream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def __aiter__(self):
            async def _empty():
                return
                yield  # pragma: no cover - never reached, makes this a generator

            return _empty()

    def _fake(self, message, **kwargs):  # noqa: ARG001 - signature mirrors pydantic-ai
        captured.update(kwargs)
        return _Stream()

    from pydantic_ai import Agent

    with patch.object(Agent, "run_stream_events", _fake):
        async for _ in backend.run("hello"):
            pass
    return captured


@pytest.mark.asyncio
async def test_the_cap_reaches_run_stream_events():
    """The number is on the kwargs pydantic-ai receives."""
    from pydantic_ai.models.test import TestModel

    from pocketpaw.agents.pydantic_ai import PydanticAIBackend

    backend = PydanticAIBackend(_settings())
    with patch.object(backend, "_build_model", return_value=TestModel(custom_output_text="ok")):
        kwargs = await _captured_run_kwargs(backend)

    assert "model_settings" in kwargs, f"no model_settings sent; got {sorted(kwargs)}"
    assert kwargs["model_settings"].get("max_tokens") == DEFAULT_MAX_OUTPUT_TOKENS


@pytest.mark.asyncio
async def test_opting_out_sends_no_model_settings_at_all():
    """The escape hatch must omit the key, not send ``max_tokens=0`` — which is
    a request for an empty completion on several providers."""
    from pydantic_ai.models.test import TestModel

    from pocketpaw.agents.pydantic_ai import PydanticAIBackend

    backend = PydanticAIBackend(_settings(agent_max_output_tokens=-1))
    with patch.object(backend, "_build_model", return_value=TestModel(custom_output_text="ok")):
        kwargs = await _captured_run_kwargs(backend)

    assert "model_settings" not in kwargs
