# Bridged tools must receive their arguments in the TYPE their schema declares.
# Created: 2026-08-15 — regression tests for the pydantic-ai bridge flattening
# every parameter to ``str``/``str | None``.
#
# ``_signature_from_json_schema`` built one ``str`` parameter per declared
# property regardless of the schema's ``type``, and defaulted every optional one
# to ``None``. pydantic-ai derives the tool schema it advertises from that
# signature, so a tool declaring ``num_results: {"type": "integer", "default": 5}``
# was offered to the model as a nullable STRING. ``execute()`` then received
# ``None`` (omitted) or ``"5"`` (provided) where it had annotated ``int = 5``.
#
# It surfaced as ``web_search`` dying on ``min(max(num_results, 1), 10)`` with
# "'>' not supported between instances of 'int' and 'NoneType'" and the ``str``
# variant. That line is not the bug — it is just the first place a wrong type
# gets compared. Every bridged tool with a non-string parameter had it.
#
# The bug was INVISIBLE until the pydantic-ai argument drop was fixed: while the
# backend delivered ``{}`` for every call, optional arguments never arrived and
# the tool's own Python defaults always applied.
#
# These tests exercise the real bridge output (``build_pydantic_ai_tools``), not
# a hand-built signature — a hand-built one would test the helper rather than
# the surface the model actually calls.

from __future__ import annotations

import inspect

import pytest

from pocketpaw.config import Settings


def _settings(**overrides) -> Settings:
    base = {
        "pydantic_ai_model": "litellm:test-model",
        "litellm_api_base": "http://localhost:4000",
        "litellm_api_key": "sk-test",
    }
    base.update(overrides)
    return Settings(**base)


def _bridged(name: str):
    from pocketpaw.agents.tool_bridge import build_pydantic_ai_tools

    for tool in build_pydantic_ai_tools(_settings(), backend="pydantic_ai"):
        if tool.name == name:
            return tool
    pytest.skip(f"{name} is not bridged in this configuration")


def test_an_integer_property_is_not_advertised_as_a_string():
    """The declared ``type`` reaches the signature pydantic-ai reads.

    Asserted on the annotation rather than on a call, because this is what
    pydantic-ai turns into the JSON schema the MODEL sees. Get it wrong and the
    model is actively instructed to send "5".
    """
    sig = inspect.signature(_bridged("web_search").function)
    annotation = sig.parameters["num_results"].annotation

    assert annotation is not str, "an integer property was advertised as a string"
    assert str not in getattr(annotation, "__args__", (annotation,)), (
        f"num_results advertised as {annotation!r}; the schema declares an integer"
    )


def test_an_optional_property_keeps_its_schema_default():
    """``num_results`` declares ``"default": 5``.

    Defaulting it to ``None`` instead is the omitted-argument half of the bug:
    the model leaves it out, the bridge passes ``num_results=None`` explicitly,
    and that OVERRIDES the tool's own ``= 5``. The tool never sees its default.
    """
    sig = inspect.signature(_bridged("web_search").function)
    assert sig.parameters["num_results"].default == 5


def test_the_schema_the_model_is_shown_declares_an_integer():
    """The end of the chain: what pydantic-ai actually advertises.

    The signature is an implementation detail; THIS is the artifact that tells
    the model what to send. It read ``{"type": "string"}`` with a null default,
    which is why models sent ``"5"`` — they were asked for a string.
    """
    props = _bridged("web_search").function_schema.json_schema["properties"]
    num = props["num_results"]
    types = {opt.get("type") for opt in num.get("anyOf", [num])}

    assert "integer" in types, num
    assert "string" not in types, num
    assert num.get("default") == 5, num


@pytest.mark.parametrize(
    "supplied",
    [
        pytest.param({}, id="model-omitted-it"),
        pytest.param({"num_results": None}, id="model-sent-null"),
        pytest.param({"num_results": "5"}, id="model-sent-a-string"),
        pytest.param({"num_results": 5}, id="model-sent-an-int"),
    ],
)
async def test_web_search_survives_the_argument_the_model_actually_sends(supplied):
    """The crashing variants from the 2026-08-15 report, plus the good cases.

    Routed through ``function_schema.validator`` because that is the layer
    pydantic-ai puts in front of the tool: it coerces ``"5"`` to ``5`` against
    the advertised schema. Calling ``.function`` directly would BYPASS it and
    test a path production never takes.

    No network: with no provider key configured the tool returns its "not
    configured" string, and that return happens AFTER
    ``min(max(num_results, 1), 10)`` — so reaching it at all is the proof the
    clamp no longer explodes.

    ``_run`` swallows exceptions into ``"Error executing <tool>: ..."``, so a
    regression here is a wrong STRING rather than a raised error — assert on the
    message, or this passes while the tool is broken.
    """
    tool = _bridged("web_search")
    kwargs = tool.function_schema.validator.validate_python(
        {"query": "quarterly filings", **supplied}
    )

    result = await tool.function(**kwargs)

    assert "not supported between instances" not in result, result
    assert not result.startswith("Error executing web_search"), result


def test_no_optional_argument_reaches_a_tool_as_none():
    """The wider half of the bug, swept across the whole bridged surface.

    ``web_search`` is where this crashed loudly. It is not where it was rare:
    26 parameters across 13 tools declare no JSON-schema ``default`` while their
    ``execute()`` declares a real one — ``connector_*`` has
    ``pocket_id: str = "default"``, ``drive_list`` has ``max_results: int = 20``.
    The synthesized signature defaulted those to ``None``, pydantic-ai filled the
    ``None`` in when the model omitted the argument, and it overrode the tool's
    own default.

    The integer ones crash the same way ``web_search`` did. The string ones are
    worse: ``pocket_id=None`` instead of ``"default"`` is a silently wrong scope,
    not an error anyone sees.

    Asserted over EVERY bridged tool rather than a sample, because the failure is
    per-parameter and a sample is how 25 of the 26 stay hidden.
    """
    import inspect

    from pocketpaw.agents.tool_bridge import _instantiate_all_tools, build_pydantic_ai_tools

    wrappers = {t.name: t for t in build_pydantic_ai_tools(_settings(), backend="pydantic_ai")}
    if not wrappers:
        pytest.skip("pydantic-ai not installed; nothing bridged")

    offenders: list[str] = []
    for tool in _instantiate_all_tools("pydantic_ai"):
        wrapper = wrappers.get(getattr(tool, "name", ""))
        if wrapper is None:
            continue
        try:
            spec = tool.definition.parameters or {}
            execute_sig = inspect.signature(tool.execute)
            bridged_sig = inspect.signature(wrapper.function)
        except (TypeError, ValueError):  # pragma: no cover - defensive
            continue

        required = set(spec.get("required", []) or [])
        for pname in spec.get("properties", {}) or {}:
            if pname in required:
                continue
            own = execute_sig.parameters.get(pname)
            bridged = bridged_sig.parameters.get(pname)
            if own is None or bridged is None:
                continue
            if own.default is inspect.Parameter.empty or own.default is None:
                continue
            # The tool has a real default. Omitting the argument must NOT
            # deliver None over the top of it.
            if bridged.default is None:
                offenders.append(f"{tool.name}.{pname} (execute default {own.default!r})")

    assert not offenders, (
        f"{len(offenders)} optional argument(s) would arrive as None and override "
        f"the tool's own default: {offenders[:8]}"
    )
