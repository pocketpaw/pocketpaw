# tests/test_step_composer.py — instinct-guardrail-ux Criterion 2: the fixed
# 3-step LLM pipeline (schema validator, executor typed handoff + lenient
# parsing, tool wrapper registration + no-backend error). No real model calls —
# the runner is always a scripted fake.
# Created: 2026-07-11 (feat/guardrail-c2-composer).
from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from pocketpaw.bundled_templates.schema import LlmStepPipeline
from pocketpaw.bundled_templates.step_composer import run_step_pipeline
from pocketpaw.tools.builtin.step_pipeline_tool import StepPipelineTool

# NOTE: the suite runs pytest-asyncio in AUTO mode — async tests need no marks.


def _pipeline(*kinds: str, **extras) -> dict:
    steps = []
    for k in kinds:
        step: dict = {"step": k, "instruction": f"do {k}"}
        step.update(extras.get(k, {}))
        steps.append(step)
    return {"steps": steps}


# ---------------------------------------------------------------------------
# Validator: fixed order, no dups, 1-3 steps, input_from resolution
# ---------------------------------------------------------------------------


def test_validator_accepts_canonical_order():
    p = LlmStepPipeline.model_validate(
        _pipeline("extract", "classify", "recommend", extract={"fields": ["amount"]})
    )
    assert [s.step for s in p.steps] == ["extract", "classify", "recommend"]


def test_validator_rejects_wrong_order_and_dups():
    with pytest.raises(ValidationError, match="order"):
        LlmStepPipeline.model_validate(_pipeline("classify", "extract"))
    with pytest.raises(ValidationError, match="repeat"):
        LlmStepPipeline.model_validate(_pipeline("extract", "extract"))
    with pytest.raises(ValidationError, match="1-3"):
        LlmStepPipeline.model_validate({"steps": []})


def test_validator_rejects_forward_input_from():
    bad = _pipeline("extract", "recommend")
    bad["steps"][0]["input_from"] = "recommend"  # forward reference
    with pytest.raises(ValidationError, match="input_from"):
        LlmStepPipeline.model_validate(bad)


# ---------------------------------------------------------------------------
# Executor: typed handoff + lenient parsing
# ---------------------------------------------------------------------------


def _scripted_runner(replies: list[str]):
    calls: list[str] = []

    async def run(prompt: str) -> str:
        calls.append(prompt)
        return replies[len(calls) - 1]

    run.calls = calls  # type: ignore[attr-defined]
    return run


async def test_executor_typed_handoff():
    """extract's JSON fields feed classify; classify's label feeds recommend."""
    pipeline = LlmStepPipeline.model_validate(
        _pipeline(
            "extract",
            "classify",
            "recommend",
            extract={"fields": ["amount", "vendor"]},
            classify={"labels": ["approve", "review"]},
        )
    )
    runner = _scripted_runner(
        ['{"amount": 900, "vendor": "Acme"}', "REVIEW", "Escalate to a human."]
    )
    result = await run_step_pipeline(pipeline, "Invoice from Acme for $900", runner)

    assert result["result"] == "Escalate to a human."
    assert [s["step"] for s in result["steps"]] == ["extract", "classify", "recommend"]
    # extract parsed to the named fields
    assert result["steps"][0]["output"] == {"amount": 900, "vendor": "Acme"}
    # classify received extract's output as its input (typed handoff)
    assert "Acme" in result["steps"][1]["input"]
    # classify's reply matched the closed label set case-insensitively
    assert result["steps"][1]["output"] == "review"
    # recommend received the label
    assert result["steps"][2]["input"] == "review"


async def test_executor_lenient_on_weird_replies():
    """A non-JSON extract reply and an off-label classify reply degrade to text."""
    pipeline = LlmStepPipeline.model_validate(
        _pipeline("extract", "classify", extract={"fields": ["x"]}, classify={"labels": ["a"]})
    )
    runner = _scripted_runner(["not json at all", "something else entirely"])
    result = await run_step_pipeline(pipeline, "input", runner)
    assert result["steps"][0]["output"] == "not json at all"
    assert result["result"] == "something else entirely"


async def test_executor_input_from_initial_input():
    """input_from='input' rewires a later step back to the pipeline input."""
    pipeline = LlmStepPipeline.model_validate(
        {
            "steps": [
                {"step": "extract", "instruction": "pull", "fields": ["a"]},
                {"step": "recommend", "instruction": "advise", "input_from": "input"},
            ]
        }
    )
    runner = _scripted_runner(['{"a": 1}', "ok"])
    await run_step_pipeline(pipeline, "THE-ORIGINAL", runner)
    assert "THE-ORIGINAL" in runner.calls[1]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Tool wrapper: registration + no-backend error + happy path
# ---------------------------------------------------------------------------


def test_tool_registered_in_builtin_map():
    import pocketpaw.tools.builtin as builtin

    tool_cls = getattr(builtin, "StepPipelineTool")
    assert tool_cls is StepPipelineTool


async def test_tool_without_runner_returns_clear_error():
    out = json.loads(await StepPipelineTool().execute(pipeline=_pipeline("recommend"), input="x"))
    assert "requires an agent backend" in out["error"]


async def test_tool_happy_path_and_invalid_spec():
    async def runner(prompt: str) -> str:
        return "fine"

    tool = StepPipelineTool(agent_runner=runner)
    ok = json.loads(await tool.execute(pipeline=_pipeline("recommend"), input="x"))
    assert ok["result"] == "fine"
    bad = json.loads(await tool.execute(pipeline=_pipeline("extract", "extract"), input="x"))
    assert "invalid pipeline" in bad["error"]
