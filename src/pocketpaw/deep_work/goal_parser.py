# Deep Work Goal Parser — structured goal analysis via LLM.
# Created: 2026-02-18
# Updated: 2026-05-21 (feat/deep-work-intake) — issue #1161: added the
#   interactive intake loop. GoalParser already produced a
#   `clarifications_needed` list but nothing asked the questions. The new
#   GoalIntake runs that conversation: it asks each clarification, takes an
#   answer via an injected async callback, folds the Q&A back into the goal
#   text, and re-parses so planning starts from a well-formed goal. A
#   well-formed goal (no clarifications) skips the loop entirely.
#
# First primitive in the Deep Work pipeline. Takes messy human input
# and produces a structured GoalAnalysis: domain detection, complexity
# estimation, AI/human role identification, and clarification questions.
#
# Public API:
#   GoalAnalysis — dataclass with parsed goal structure
#   GoalParser.parse(user_input) -> GoalAnalysis
#   GoalParser.parse_raw(raw_json) -> GoalAnalysis (for testing)
#   IntakeResult — dataclass: enriched goal text + Q&A transcript + analysis
#   GoalIntake.run(user_input, answer_provider) -> IntakeResult

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Max clarification rounds before the intake loop gives up. The parser
# already caps `clarifications_needed` at 4, but a defensive bound here
# keeps a misbehaving answer provider (one that keeps surfacing new
# ambiguity) from looping forever.
MAX_INTAKE_ROUNDS = 3

# Regex to strip markdown code fences (```json ... ``` or ``` ... ```)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)

# Valid domain values
VALID_DOMAINS = frozenset({"code", "business", "creative", "education", "events", "home", "hybrid"})

# Valid complexity values
VALID_COMPLEXITIES = frozenset({"S", "M", "L", "XL"})

# Valid research depth values
VALID_RESEARCH_DEPTHS = frozenset({"none", "quick", "standard", "deep"})


@dataclass
class GoalAnalysis:
    """Structured analysis of a user's project goal.

    Produced by GoalParser as the first step in the Deep Work pipeline.
    Informs research depth, planner context, and frontend display.

    Attributes:
        goal: Clear one-sentence restatement of the user's goal.
        domain: Primary domain (code, business, creative, education, events, home, hybrid).
        sub_domains: Specific sub-domains (e.g. "web-development", "react").
        complexity: Estimated complexity (S, M, L, XL).
        estimated_phases: Number of expected project phases (1-10).
        ai_capabilities: What AI can do for this project.
        human_requirements: What the human must do (AI cannot).
        constraints_detected: Budget, timeline, or technical constraints found in input.
        clarifications_needed: Questions to ask before planning.
        suggested_research_depth: Recommended research depth (none/quick/standard/deep).
        confidence: Parser confidence in the analysis (0.0 to 1.0).
    """

    goal: str = ""
    domain: str = "code"
    sub_domains: list[str] = field(default_factory=list)
    complexity: str = "M"
    estimated_phases: int = 1
    ai_capabilities: list[str] = field(default_factory=list)
    human_requirements: list[str] = field(default_factory=list)
    constraints_detected: list[str] = field(default_factory=list)
    clarifications_needed: list[str] = field(default_factory=list)
    suggested_research_depth: str = "standard"
    confidence: float = 0.7

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "goal": self.goal,
            "domain": self.domain,
            "sub_domains": self.sub_domains,
            "complexity": self.complexity,
            "estimated_phases": self.estimated_phases,
            "ai_capabilities": self.ai_capabilities,
            "human_requirements": self.human_requirements,
            "constraints_detected": self.constraints_detected,
            "clarifications_needed": self.clarifications_needed,
            "suggested_research_depth": self.suggested_research_depth,
            "confidence": self.confidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoalAnalysis":
        """Create from dictionary."""
        raw_clarifications = data.get("clarifications_needed", [])
        if len(raw_clarifications) > 4:
            logger.warning("Clarifications truncated from %d to 4", len(raw_clarifications))

        complexity = _validate_complexity(data.get("complexity", "M"))
        estimated_phases = int(_clamp(data.get("estimated_phases", 1), 1, 10))
        # Enforce minimum phases for high complexity
        min_phases = {"S": 1, "M": 1, "L": 2, "XL": 3}
        estimated_phases = max(estimated_phases, min_phases.get(complexity, 1))

        return cls(
            goal=data.get("goal", ""),
            domain=_validate_domain(data.get("domain", "code")),
            sub_domains=_sanitize_str_list(data.get("sub_domains", []))[:6],
            complexity=complexity,
            estimated_phases=estimated_phases,
            ai_capabilities=_sanitize_str_list(data.get("ai_capabilities", [])),
            human_requirements=_sanitize_str_list(data.get("human_requirements", [])),
            constraints_detected=_sanitize_str_list(data.get("constraints_detected", [])),
            clarifications_needed=_sanitize_str_list(raw_clarifications)[:4],
            suggested_research_depth=_validate_research_depth(
                data.get("suggested_research_depth", "standard")
            ),
            confidence=_clamp(data.get("confidence", 0.7), 0.0, 1.0),
        )

    @property
    def needs_clarification(self) -> bool:
        """Whether the goal needs clarification before planning."""
        return len(self.clarifications_needed) > 0

    @property
    def domain_label(self) -> str:
        """Human-readable domain label."""
        labels = {
            "code": "Software & Code",
            "business": "Business & Strategy",
            "creative": "Creative & Content",
            "education": "Learning & Education",
            "events": "Events & Logistics",
            "home": "Home & Physical",
            "hybrid": "Multi-Domain",
        }
        return labels.get(self.domain, self.domain.title())


class GoalParser:
    """Parses user goals into structured GoalAnalysis via LLM.

    Uses AgentRouter to run the GOAL_PARSE_PROMPT and parse the
    structured JSON response into a GoalAnalysis dataclass.
    """

    async def parse(self, user_input: str) -> GoalAnalysis:
        """Parse a user's goal description into structured analysis.

        Args:
            user_input: Natural language goal description.

        Returns:
            GoalAnalysis with domain, complexity, roles, and clarifications.

        Raises:
            RuntimeError: If the LLM fails to produce valid output.
        """
        from pocketpaw.deep_work.prompts import GOAL_PARSE_PROMPT

        # Escape curly braces in user input to prevent format string injection
        safe_input = user_input.replace("{", "{{").replace("}", "}}")
        prompt = GOAL_PARSE_PROMPT.format(user_input=safe_input)
        raw_output = await self._run_prompt(prompt)

        analysis = self.parse_raw(raw_output)
        if not analysis.goal:
            # Fallback: use input as goal if LLM didn't restate it
            analysis.goal = user_input[:200]

        logger.info(
            "Goal parsed for '%.50s': domain=%s complexity=%s confidence=%.2f clarifications=%d",
            user_input,
            analysis.domain,
            analysis.complexity,
            analysis.confidence,
            len(analysis.clarifications_needed),
        )
        return analysis

    def parse_raw(self, raw: str) -> GoalAnalysis:
        """Parse raw LLM JSON output into a GoalAnalysis.

        Handles markdown code fences and returns a default GoalAnalysis
        on parse failure.

        Args:
            raw: Raw JSON string (possibly with markdown code fences).

        Returns:
            Parsed GoalAnalysis, or default analysis on failure.
        """
        cleaned = self._strip_code_fences(raw)
        try:
            data = json.loads(cleaned)
            if isinstance(data, dict):
                return GoalAnalysis.from_dict(data)
            logger.warning("Goal parse JSON is not an object: %s", type(data).__name__)
            return GoalAnalysis()
        except (json.JSONDecodeError, TypeError) as e:
            logger.warning("Failed to parse goal analysis JSON: %s\nRaw: %s", e, raw[:200])
            return GoalAnalysis()

    async def _run_prompt(self, prompt: str) -> str:
        """Run a prompt through AgentRouter and collect message chunks.

        Raises RuntimeError if the router yields only error events.
        """
        from pocketpaw.agents.router import AgentRouter
        from pocketpaw.config import get_settings

        router = AgentRouter(get_settings())
        output_parts: list[str] = []
        errors: list[str] = []

        async for event in router.run(prompt):
            if event.type == "message":
                content = event.content or ""
                if content:
                    output_parts.append(content)
            elif event.type == "error":
                error_content = event.content or "Unknown error"
                errors.append(error_content)
                logger.error("LLM error during goal parsing: %s", error_content)

        if not output_parts:
            if errors:
                raise RuntimeError(f"LLM error during goal parsing: {errors[0]}")
            raise RuntimeError("LLM produced empty response during goal parsing")

        return "".join(output_parts)

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """Remove markdown code fences from LLM output."""
        match = _CODE_FENCE_RE.search(text)
        if match:
            return match.group(1).strip()
        return text.strip()


# ============================================================================
# Validation helpers
# ============================================================================


def _validate_domain(value: str) -> str:
    """Validate and normalize domain value."""
    normalized = value.lower().strip()
    if normalized in VALID_DOMAINS:
        return normalized
    return "hybrid"


def _validate_complexity(value: str) -> str:
    """Validate and normalize complexity value."""
    normalized = value.upper().strip()
    if normalized in VALID_COMPLEXITIES:
        return normalized
    return "M"


def _validate_research_depth(value: str) -> str:
    """Validate and normalize research depth value."""
    normalized = value.lower().strip()
    if normalized in VALID_RESEARCH_DEPTHS:
        return normalized
    return "standard"


def _sanitize_str_list(items: Any) -> list[str]:
    """Filter a list to only non-empty string items."""
    if not isinstance(items, list):
        return []
    return [str(item) for item in items if item is not None and str(item).strip()]


def _clamp(value, minimum, maximum):
    """Clamp a numeric value between min and max."""
    try:
        return max(minimum, min(maximum, float(value)))
    except (TypeError, ValueError):
        return minimum


# ============================================================================
# Interactive intake (issue #1161)
# ============================================================================
#
# `clarifications_needed` is the half-built intake: it holds the exact
# questions you would ask a human to disambiguate a vague goal, but nothing
# asks them. GoalIntake closes that loop.
#
# An "answer provider" is any async callable `(question: str) -> str`. The
# dashboard wires it to a chat turn; tests wire it to a dict lookup; a CLI
# could wire it to `input()`. The intake layer doesn't care where answers
# come from — it just asks, collects, and folds.


# An answer provider takes a clarification question and returns the human's
# answer. Async so a real implementation can await a chat round-trip.
AnswerProvider = Callable[[str], Awaitable[str]]


@dataclass
class QAPair:
    """A single clarification question and the answer the human gave."""

    question: str
    answer: str

    def to_dict(self) -> dict[str, str]:
        return {"question": self.question, "answer": self.answer}


@dataclass
class IntakeResult:
    """Outcome of an interactive goal-intake conversation.

    Attributes:
        original_input: The raw goal the user first submitted.
        enriched_goal: The goal text after folding in clarification answers.
            This is what gets handed to the planner. When no clarification
            was needed it equals ``original_input``.
        transcript: Ordered Q&A pairs collected during intake (empty when
            the goal was well-formed and intake was skipped).
        analysis: The final GoalAnalysis (re-parsed after enrichment when
            clarifications were folded in).
        clarified: True if at least one clarification question was answered.
    """

    original_input: str = ""
    enriched_goal: str = ""
    transcript: list[QAPair] = field(default_factory=list)
    analysis: GoalAnalysis = field(default_factory=GoalAnalysis)
    clarified: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization / API output."""
        return {
            "original_input": self.original_input,
            "enriched_goal": self.enriched_goal,
            "transcript": [qa.to_dict() for qa in self.transcript],
            "analysis": self.analysis.to_dict(),
            "clarified": self.clarified,
        }


class GoalIntake:
    """Runs the interactive clarification conversation for a vague goal.

    Wraps :class:`GoalParser`. The flow:

      1. Parse the raw input into a GoalAnalysis.
      2. If ``clarifications_needed`` is empty, the goal is well-formed —
         return immediately with the input unchanged (one-shot path).
      3. Otherwise, ask each question via the injected answer provider,
         collect the answers, and fold the Q&A into an enriched goal text.
      4. Re-parse the enriched goal so planning starts from a clean
         analysis. If the re-parse still surfaces clarifications, loop —
         bounded by ``MAX_INTAKE_ROUNDS``.

    GoalIntake never blocks on a real human directly; the caller supplies
    the answer provider. That keeps this class testable and channel-agnostic.
    """

    def __init__(self, parser: GoalParser | None = None) -> None:
        self.parser = parser or GoalParser()

    async def run(
        self,
        user_input: str,
        answer_provider: AnswerProvider,
    ) -> IntakeResult:
        """Run intake for ``user_input``, asking questions as needed.

        Args:
            user_input: The raw goal description from the user.
            answer_provider: Async callable that, given a clarification
                question, returns the human's answer. Called once per
                question. An empty-string answer is treated as "skip this
                question" and is not folded into the goal.

        Returns:
            An :class:`IntakeResult`. ``enriched_goal`` is what the caller
            should hand to the planner.
        """
        analysis = await self.parser.parse(user_input)

        # Fast path: a well-formed goal needs no conversation. This is the
        # existing one-shot behaviour — start_deep_work(good_goal) still
        # runs straight through.
        if not analysis.needs_clarification:
            logger.info("Goal intake: no clarifications needed, skipping intake loop")
            return IntakeResult(
                original_input=user_input,
                enriched_goal=user_input,
                transcript=[],
                analysis=analysis,
                clarified=False,
            )

        transcript: list[QAPair] = []
        enriched_goal = user_input

        for round_num in range(1, MAX_INTAKE_ROUNDS + 1):
            questions = list(analysis.clarifications_needed)
            if not questions:
                break

            logger.info(
                "Goal intake round %d: asking %d clarification(s)",
                round_num,
                len(questions),
            )

            asked_this_round = 0
            for question in questions:
                answer = await answer_provider(question)
                answer = (answer or "").strip()
                if not answer:
                    # Treat a blank answer as "no further detail" — record
                    # nothing and move on rather than polluting the goal.
                    continue
                transcript.append(QAPair(question=question, answer=answer))
                asked_this_round += 1

            if asked_this_round == 0:
                # The human skipped every question this round. Folding
                # nothing in and re-parsing would just resurface the same
                # questions, so stop here with what we have.
                logger.info("Goal intake: all questions skipped, ending intake")
                break

            # Fold the full Q&A transcript into the goal and re-parse so
            # the planner sees a single coherent, enriched goal.
            enriched_goal = _fold_transcript(user_input, transcript)
            analysis = await self.parser.parse(enriched_goal)

        clarified = len(transcript) > 0
        # When the goal was clarified, the enriched text is the real goal —
        # make sure the analysis.goal reflects it rather than the vague input.
        if clarified and not analysis.goal:
            analysis.goal = enriched_goal[:200]

        return IntakeResult(
            original_input=user_input,
            enriched_goal=enriched_goal,
            transcript=transcript,
            analysis=analysis,
            clarified=clarified,
        )


def _fold_transcript(original_input: str, transcript: list[QAPair]) -> str:
    """Build an enriched goal string from the original input + Q&A.

    The result is plain text the planner's prompts can consume directly:
    the original goal followed by a "Clarifications" block. Keeping it as
    readable prose (not JSON) means every downstream prompt — research,
    PRD, task breakdown — gets the extra context for free.
    """
    if not transcript:
        return original_input
    lines = [original_input.strip(), "", "Clarifications gathered during intake:"]
    for qa in transcript:
        lines.append(f"- {qa.question.strip()} → {qa.answer.strip()}")
    return "\n".join(lines)
