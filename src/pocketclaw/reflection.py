"""Self-reflection module for agent response validation and auto-correction.

This module implements a secondary LLM pass to evaluate and optionally correct
agent responses before they are stored in memory. This is a quality assurance
mechanism that improves response accuracy and reduces hallucinations.

ARCHITECTURE:
  1. Reflection Evaluation — Structured prompt asking: Is response COMPLETE, ACCURATE, etc?
  2. Pass/Fail Decision — Simple PASS vs FAIL parsing for deterministic results
  3. Auto-Correction — If failed, send feedback to LLM for improvement
  4. Improvement Tracking — Prevent over-correction and oscillation
  5. Confidence Scoring — Optional: extract 0-1 confidence metric for smart retry logic

PRODUCTION SAFEGUARDS:
  ✓ Robust PASS/FAIL parsing (case-insensitive, regex-safe)
  ✓ Improvement tracking prevents worse answers
  ✓ Artifact prevention in correction prompts
  ✓ Metrics returned for observability
  ✓ Graceful fallback on backend failures

Created: 2026-02-14
Updated: 2026-02-14 (production hardening)
"""

import difflib
import logging
import re
from typing import Tuple

from pocketclaw.llm.router import LLMRouter

logger = logging.getLogger(__name__)


def _is_reflection_pass(result: str) -> bool:
    """Robust PASS detection (handles variations).

    Args:
        result: Reflection result string

    Returns:
        True if reflection passed, False otherwise

    Examples:
        _is_reflection_pass("PASS")      -> True
        _is_reflection_pass("pass:")     -> True
        _is_reflection_pass("PASS.")     -> True
        _is_reflection_pass("FAIL: ...")  -> False
    """
    result_upper = result.upper().strip()
    # Match: PASS, PASS:, PASS., PASS! etc.
    # But NOT FAIL variants
    return result_upper.startswith("PASS") and not result_upper.startswith("FAIL")


def _is_improvement(original: str, corrected: str) -> bool:
    """Check if corrected response is a meaningful improvement.

    Prevents over-correction and oscillation by ensuring:
    - Response wasn't truncated (min 50% of original length)
    - Response wasn't completely rewritten (>85% similar)
    - Semantic content preserved with additions (>90% similar if same length)

    Args:
        original: Original agent response
        corrected: Corrected response to evaluate

    Returns:
        True if correction is accepted, False if rejected
    """
    original_len = len(original)
    corrected_len = len(corrected)

    # GUARD 1: Reject truncation (less than 50% of original)
    if corrected_len < original_len * 0.5:
        logger.debug(f"Correction rejected: Too short ({corrected_len} < {original_len * 0.5})")
        return False

    # GUARD 2: Similarity check (prevent complete rewrites)
    similarity = difflib.SequenceMatcher(None, original, corrected).ratio()
    logger.debug(f"Correction similarity: {similarity:.2f}")

    # Accept if: longer AND similar (added content, not replacement)
    if corrected_len > original_len * 0.95 and similarity > 0.85:
        return True

    # Accept if: similar length AND very similar (refactored/improved)
    length_diff = abs(corrected_len - original_len)
    if length_diff < 100 and similarity > 0.90:
        return True

    logger.debug(
        f"Correction rejected: insufficient improvement (sim={similarity:.2f}, len_delta={length_diff})"
    )
    return False


def build_reflection_prompt(user_input: str, agent_output: str) -> str:
    """Build a reflection prompt to evaluate the agent's response.

    Args:
        user_input: The original user question
        agent_output: The agent's response to evaluate

    Returns:
        A structured prompt for the reflection LLM
    """
    return f"""You are a critical evaluator of AI agent responses.

USER QUESTION:
{user_input}

AGENT RESPONSE:
{agent_output}

[REFLECTION TASK]

Evaluate the response carefully on these criteria:

1. **Completeness** — Does it fully answer the question?
2. **Accuracy** — Is any information hallucinated or incorrect?
3. **Tool Usage** — Did the agent misuse any tools?
4. **Reasoning** — Is the logic sound and transparent?

[RESPONSE FORMAT]

Respond with EXACTLY ONE of:

PASS

or

FAIL: <short explanation (1-2 sentences)>

Do not include confidence scores or any additional text.
"""


def build_correction_prompt(user_input: str, reflection_feedback: str) -> str:
    """Build correction prompt with artifact prevention.

    Explicitly forbids meta-commentary and apologies to ensure clean corrections.

    Args:
        user_input: The original user question
        reflection_feedback: The reflection evaluation feedback

    Returns:
        A structured prompt for correction
    """
    return f"""The previous answer had issues.

FEEDBACK:
{reflection_feedback}

ORIGINAL QUESTION:
{user_input}

[CORRECTION TASK]

Provide an improved answer that addresses the feedback above.

CRITICAL REQUIREMENTS:
- Provide ONLY the corrected answer
- Do NOT include meta-commentary ("I apologize", "I made an error", etc.)
- Do NOT repeat the problematic answer
- Do NOT add disclaimers or hedging language
- Do NOT say "Here is the corrected response:" or similar preamble
- Be direct and concise

OUTPUT THE ANSWER ONLY.
"""


async def reflect_and_correct(
    llm_backend: LLMRouter,
    user_input: str,
    agent_output: str,
    max_retries: int = 1,
) -> Tuple[str, bool, dict]:
    """Reflect on agent output and optionally request corrections.

    Uses a secondary LLM pass to validate the agent response.
    If issues are found, requests corrections up to max_retries times.

    FLOW:
      1. Run reflection prompt on initial response
      2. If PASS → return response immediately (no corrections needed)
      3. If FAIL → send feedback to LLM for correction attempt
      4. Check if correction is improvement; if yes, accept; if no, reject
      5. Repeat up to max_retries times
      6. Return final response, correction flag, and metrics

    SAFEGUARDS:
      - Robust PASS/FAIL parsing (case-insensitive)
      - Improvement tracking prevents over-correction
      - Errors don't block response (graceful fallback)
      - All activity logged for observability

    Args:
        llm_backend: LLMRouter instance for reflection completions
        user_input: The original user question
        agent_output: The agent's response to reflect on
        max_retries: Maximum number of correction attempts (default: 1)
                    Set to 0 for "validation only" mode (no corrections)

    Returns:
        Tuple of (final_response, was_corrected_flag, metrics_dict)
            - final_response: The response (possibly corrected)
            - was_corrected_flag: True if any corrections were accepted
            - metrics_dict: {
                "reflection_passed": bool,
                "total_attempts": int,
                "corrections_attempted": int,
                "corrections_accepted": int,
              }

    Raises:
        No exceptions — logs warnings and returns original on any failure
    """
    logger.info(f"🔍 Reflection: evaluating {len(agent_output)} char response")

    corrected = False
    metrics = {
        "reflection_passed": False,
        "total_attempts": 0,
        "corrections_attempted": 0,
        "corrections_accepted": 0,
    }

    current_response = agent_output

    for attempt in range(max_retries + 1):
        metrics["total_attempts"] += 1
        reflection_prompt = build_reflection_prompt(user_input, current_response)

        try:
            reflection_result = await llm_backend.chat(reflection_prompt)
            reflection_result = reflection_result.strip()
        except Exception as e:
            logger.warning(f"⚠️ Reflection backend error: {type(e).__name__} — using original")
            return current_response, corrected, metrics

        logger.debug(f"📋 Reflection result: {reflection_result[:100]}...")

        # Check if reflection passed (robust parsing)
        if _is_reflection_pass(reflection_result):
            logger.info("✅ Reflection PASSED — response quality verified")
            metrics["reflection_passed"] = True
            return current_response, corrected, metrics

        # Response failed — request correction if we have retries left
        if attempt < max_retries:
            logger.warning(f"⚠️ Reflection FAILED (attempt {attempt + 1}/{max_retries + 1})")
            logger.info(f"   Feedback: {reflection_result[:80]}...")
            metrics["corrections_attempted"] += 1

            correction_prompt = build_correction_prompt(user_input, reflection_result)

            try:
                logger.info("🔄 Requesting correction from LLM...")
                corrected_response = await llm_backend.chat(correction_prompt)
                corrected_response = corrected_response.strip()

                # IMPROVEMENT TRACKING: Only accept if better
                if _is_improvement(current_response, corrected_response):
                    current_response = corrected_response
                    corrected = True
                    metrics["corrections_accepted"] += 1
                    logger.info(f"✨ Correction {attempt + 1} ACCEPTED ({len(corrected_response)} chars)")
                else:
                    logger.info(f"⏭️  Correction {attempt + 1} REJECTED (not a meaningful improvement)")
                    # Don't use corrected_response, stay with current
            except Exception as e:
                logger.warning(f"❌ Correction request failed: {type(e).__name__}")
                # Stay with current best response
        else:
            # No more retries
            logger.warning(f"⚠️ Final reflection FAILED (reached max retries={max_retries})")

    logger.info(
        f"Reflection complete: {metrics['corrections_accepted']}/{metrics['corrections_attempted']} accepted"
    )
    return current_response, corrected, metrics


async def reflect_only(
    llm_backend: LLMRouter,
    user_input: str,
    agent_output: str,
) -> Tuple[str, float]:
    """Reflect on response and extract confidence score (advanced mode).

    This is a more advanced variant that extracts a numeric confidence metric
    from the reflection. Can be used for smart retry logic:

    Example:
        feedback, confidence = await reflect_only(...)
        if confidence < 0.7:
            # Trigger auto-correction

    Args:
        llm_backend: LLMRouter instance
        user_input: Original user question
        agent_output: Agent response to evaluate

    Returns:
        Tuple of (feedback_string, confidence_0_to_1)
            - feedback_string: The full reflection feedback
            - confidence: Float between 0.0 and 1.0
    """
    prompt = f"""{build_reflection_prompt(user_input, agent_output)}

Additionally, assign a confidence score (0.0 to 1.0) that this response is correct:

CONFIDENCE: <number between 0.0 and 1.0>
"""

    try:
        logger.info("🔍 Reflection with confidence scoring...")
        result = await llm_backend.chat(prompt)
        feedback = result.strip()

        # Try to extract numeric confidence if provided
        match = re.search(r"CONFIDENCE:\s*([0-9.]+)", feedback)
        confidence = float(match.group(1)) if match else 0.5

        logger.debug(f"Confidence score: {confidence:.2f}")
        return feedback, confidence
    except Exception as e:
        logger.warning(f"⚠️ Reflection failed: {type(e).__name__}")
        return "Unable to reflect", 0.5
