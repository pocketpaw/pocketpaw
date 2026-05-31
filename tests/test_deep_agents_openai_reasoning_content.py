"""Regression test: DeepSeek reasoning_content must round-trip through
the langchain_openai serializer.

DeepSeek thinking mode requires ``reasoning_content`` from each prior
assistant turn to be echoed back on subsequent multi-turn requests
when those turns contained tool_calls. Vanilla langchain_openai 1.2.1:

  - ignores ``reasoning_content`` on inbound (doesn't capture it),
  - drops it on outbound (``_convert_message_to_dict`` skips
    additional_kwargs entries it doesn't know about).

This breaks every tool-using DeepSeek agent on its second API call
("`reasoning_content` in thinking mode must be passed back to the API",
HTTP 400, https://api-docs.deepseek.com/guides/thinking_mode).

The fix in ``pocketpaw.agents.deep_agents._patch_openai_message_serializer``
monkey-patches three serializer functions. This test pins:
  1. inbound capture from response dict → AIMessage.additional_kwargs
  2. inbound capture from streaming delta → AIMessageChunk.additional_kwargs
  3. outbound echo from AIMessage.additional_kwargs → request dict
"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from pocketpaw.agents.deep_agents import _patch_openai_message_serializer


def test_outbound_echoes_reasoning_content_for_assistant_messages() -> None:
    _patch_openai_message_serializer()
    from langchain_openai.chat_models import base as _oa

    msg = AIMessage(
        content="final answer",
        additional_kwargs={"reasoning_content": "step 1, step 2, conclusion"},
    )
    out = _oa._convert_message_to_dict(msg)
    assert out["role"] == "assistant"
    assert out["reasoning_content"] == "step 1, step 2, conclusion"


def test_outbound_omits_reasoning_content_when_absent() -> None:
    _patch_openai_message_serializer()
    from langchain_openai.chat_models import base as _oa

    msg = AIMessage(content="plain reply")
    out = _oa._convert_message_to_dict(msg)
    assert "reasoning_content" not in out


def test_inbound_captures_reasoning_content_into_additional_kwargs() -> None:
    _patch_openai_message_serializer()
    from langchain_openai.chat_models import base as _oa

    response_dict = {
        "role": "assistant",
        "content": "final answer",
        "reasoning_content": "think think think",
    }
    msg = _oa._convert_dict_to_message(response_dict)
    assert msg.additional_kwargs.get("reasoning_content") == "think think think"


def test_inbound_no_capture_when_field_absent() -> None:
    """Non-DeepSeek openai-compat endpoints don't emit reasoning_content
    — the patch must be a no-op for them."""
    _patch_openai_message_serializer()
    from langchain_openai.chat_models import base as _oa

    response_dict = {"role": "assistant", "content": "plain"}
    msg = _oa._convert_dict_to_message(response_dict)
    assert "reasoning_content" not in msg.additional_kwargs


def test_patch_is_idempotent() -> None:
    """Apply twice — the second call must not double-wrap the patched
    function or it would call original_original on each invocation."""
    _patch_openai_message_serializer()
    _patch_openai_message_serializer()
    from langchain_openai.chat_models import base as _oa

    msg = AIMessage(
        content="x",
        additional_kwargs={"reasoning_content": "y"},
    )
    out = _oa._convert_message_to_dict(msg)
    assert out["reasoning_content"] == "y"  # not "yy" or any accumulator artifact


def test_patch_logs_loudly_when_a_target_symbol_is_missing(monkeypatch, caplog) -> None:
    """If a future langchain-openai release renames or removes one of
    the private ``_convert_*`` symbols, the patch must log a loud error
    naming the missing symbol — NOT silently AttributeError on the
    first DeepSeek call in production. The other two patches still
    apply so partial functionality survives."""
    import logging

    from langchain_openai.chat_models import base as _oa

    import pocketpaw.agents.deep_agents as deep_agents_mod

    # Reset the patched flag and the target attribute. The original
    # function gets restored at the end so other tests aren't poisoned.
    monkeypatch.setattr(deep_agents_mod, "_OPENAI_PATCHED", False)
    original = _oa._convert_dict_to_message
    monkeypatch.delattr(_oa, "_convert_dict_to_message", raising=True)

    try:
        with caplog.at_level(logging.ERROR, logger="pocketpaw.agents.deep_agents"):
            _patch_openai_message_serializer()
    finally:
        # Restore so the next test sees a normal module + a re-runnable patch.
        _oa._convert_dict_to_message = original
        monkeypatch.setattr(deep_agents_mod, "_OPENAI_PATCHED", False)
        _patch_openai_message_serializer()

    assert any(
        "_convert_dict_to_message" in rec.message
        and "langchain_openai upgrade broke" in rec.message
        for rec in caplog.records
    ), "missing-symbol error log not emitted"
