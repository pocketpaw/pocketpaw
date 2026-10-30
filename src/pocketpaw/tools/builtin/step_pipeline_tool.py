# pocketpaw/tools/builtin/step_pipeline_tool.py — `run_step_pipeline` builtin
# (instinct-guardrail-ux Criterion 2).
#
# Created: 2026-07-11 (feat/guardrail-c2-composer).
#
# The tool wrapper over ``bundled_templates.step_composer``: an agent (or an
# action) invokes it with a fixed-order pipeline spec (extract → classify →
# recommend) + an input string; deterministic Python sequences the steps and
# the model is called once per step through an INJECTED runner.
#
# Runner resolution (the risky seam, kept honest): no builtin currently
# invokes the model mid-tool, so this tool does NOT construct an LLM client.
# The hosting layer that registers the tool passes ``agent_runner=`` (an async
# ``(prompt) -> str``); without one the tool returns a clear
# "requires an agent backend" error instead of guessing at credentials —
# this deployment may run the Claude Code backend with no ANTHROPIC_API_KEY.

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from pocketpaw.bundled_templates.schema import LlmStepPipeline
from pocketpaw.bundled_templates.step_composer import AgentRunner, run_step_pipeline
from pocketpaw.tools.protocol import BaseTool

_NO_RUNNER_ERROR = (
    "run_step_pipeline requires an agent backend: the hosting layer did not "
    "provide an agent_runner, so no model call can be made. Register the tool "
    "with agent_runner=<async (prompt) -> str> to enable it."
)


class StepPipelineTool(BaseTool):
    """Run a fixed 3-step LLM pipeline (extract → classify → recommend)."""

    def __init__(self, agent_runner: AgentRunner | None = None) -> None:
        self._agent_runner = agent_runner

    @property
    def name(self) -> str:
        return "run_step_pipeline"

    @property
    def description(self) -> str:
        return (
            "Run a small fixed-order LLM pipeline over an input text: "
            "extract (pull named fields) -> classify (pick one label) -> "
            "recommend (suggest the next action). Steps are optional but must "
            "keep that order, each at most once. Deterministic code sequences "
            "the steps; each step is one model call. Returns a JSON transcript "
            "{steps: [{step, input, output}], result}."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pipeline": {
                    "type": "object",
                    "description": (
                        "The pipeline spec: {steps: [{step: extract|classify|"
                        "recommend, instruction, input_from?, fields?, labels?}]}"
                        " — 1-3 steps in the fixed order, each kind at most once."
                    ),
                },
                "input": {
                    "type": "string",
                    "description": "The input text the pipeline runs over.",
                },
            },
            "required": ["pipeline", "input"],
        }

    async def execute(
        self,
        pipeline: dict[str, Any] | str | None = None,
        input: str = "",  # noqa: A002 — tool-schema param name
        **_: Any,
    ) -> str:
        if self._agent_runner is None:
            return json.dumps({"error": _NO_RUNNER_ERROR})
        if isinstance(pipeline, str):
            try:
                pipeline = json.loads(pipeline)
            except (json.JSONDecodeError, ValueError):
                return json.dumps({"error": "pipeline must be a JSON object"})
        try:
            spec = LlmStepPipeline.model_validate(pipeline or {})
        except ValidationError as exc:
            # Surface the validator's precise message so the model can fix the
            # spec and retry (the flow_tool forgiving-author loop).
            return json.dumps({"error": f"invalid pipeline: {exc.errors()[0]['msg']}"})
        result = await run_step_pipeline(spec, input, self._agent_runner)
        return json.dumps(result, ensure_ascii=False)


__all__ = ["StepPipelineTool"]
