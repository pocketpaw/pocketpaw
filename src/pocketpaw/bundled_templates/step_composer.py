# pocketpaw/bundled_templates/step_composer.py — the fixed 3-step LLM pipeline
# executor (instinct-guardrail-ux Criterion 2).
#
# Created: 2026-07-11 (feat/guardrail-c2-composer).
#
# The ``start_flow`` discipline applied to an LLM-in-the-loop pipeline:
# deterministic Python owns the STRUCTURE and SEQUENCING (which step runs,
# what feeds what); the model is invoked exactly once per step through an
# INJECTED ``agent_runner`` callable. This module never instantiates an LLM
# client — the caller resolves the backend (this deployment may run the
# Claude Code agent backend with no ANTHROPIC_API_KEY, so any direct client
# here would be a bug, not a missing key).
#
# Leniency contract: a step's weird model reply degrades to raw text (the
# pipeline keeps going and the transcript shows what happened); a MALFORMED
# PIPELINE raises (the ``LlmStepPipeline`` validator owns most of that).

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from pocketpaw.bundled_templates.schema import LlmStep, LlmStepPipeline

logger = logging.getLogger(__name__)

AgentRunner = Callable[[str], Awaitable[str]]


def _step_prompt(step: LlmStep, step_input: str) -> str:
    """Build the one typed prompt for a step (instruction + typed ask + input)."""
    if step.step == "extract":
        fields = ", ".join(step.fields or []) or "the relevant fields"
        ask = (
            f"Extract these fields as a flat JSON object: {fields}. "
            "Reply with ONLY the JSON object."
        )
    elif step.step == "classify":
        labels = ", ".join(step.labels or []) or "an appropriate label"
        ask = f"Classify the input as exactly ONE of: {labels}. Reply with ONLY the label."
    else:  # recommend
        ask = "Recommend the next action, concisely."
    return f"{step.instruction}\n\n{ask}\n\nInput:\n{step_input}"


def _parse_output(step: LlmStep, reply: str) -> Any:
    """Parse a step's reply into its typed output — leniently, never raising.

    extract → a dict of the named fields when the reply parses as JSON (a
    fenced JSON block is unwrapped), else the raw text. classify → the matching
    label (case-insensitive) when one of ``labels`` appears, else the trimmed
    raw reply. recommend → free text.
    """
    text = reply.strip()
    if step.step == "extract":
        candidate = text
        if candidate.startswith("```"):
            candidate = candidate.strip("`")
            candidate = candidate.split("\n", 1)[1] if "\n" in candidate else candidate
            candidate = candidate.rsplit("```", 1)[0] if "```" in candidate else candidate
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            return text
        if isinstance(parsed, dict) and step.fields:
            return {k: parsed.get(k) for k in step.fields}
        return parsed if isinstance(parsed, dict) else text
    if step.step == "classify":
        lowered = text.lower()
        for label in step.labels or []:
            if label.lower() == lowered or label.lower() in lowered:
                return label
        return text
    return text


def _as_text(value: Any) -> str:
    """Render a prior step's typed output as the next step's text input."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


async def run_step_pipeline(
    pipeline: LlmStepPipeline,
    initial_input: str,
    agent_runner: AgentRunner,
) -> dict[str, Any]:
    """Run the pipeline sequentially with typed handoff between steps.

    Each step's input resolves from ``input_from`` ("input" = the initial
    input; a kind name = that earlier step's output; None = the previous
    step's output, or the initial input for the first step). Returns
    ``{"steps": [{step, input, output}, ...], "result": <final output>}``.
    """
    outputs: dict[str, Any] = {"input": initial_input}
    transcript: list[dict[str, Any]] = []
    previous: Any = initial_input

    for step in pipeline.steps:
        source = step.input_from if step.input_from is not None else None
        step_input = _as_text(outputs[source]) if source is not None else _as_text(previous)
        reply = await agent_runner(_step_prompt(step, step_input))
        output = _parse_output(step, reply)
        outputs[step.step] = output
        previous = output
        transcript.append({"step": step.step, "input": step_input, "output": output})
        logger.debug("step_composer: %s -> %.120r", step.step, output)

    return {"steps": transcript, "result": previous}


__all__ = ["AgentRunner", "run_step_pipeline"]
