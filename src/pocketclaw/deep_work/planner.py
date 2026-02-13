# Deep Work Planner — orchestrates 4-phase project planning via LLM.
# Created: 2026-02-12
# Updated: 2026-02-12 — Added research_depth parameter (none/quick/standard/deep).
#   'none' skips research entirely (no LLM call), passing empty notes to PRD.
#
# PlannerAgent runs research, PRD generation, task breakdown, and team
# assembly through AgentRouter, producing a PlannerResult that can be
# materialized into Mission Control objects.

import json
import logging
import re

from pocketclaw.deep_work.models import AgentSpec, PlannerResult, TaskSpec
from pocketclaw.deep_work.prompts import (
    PRD_PROMPT,
    RESEARCH_PROMPT,
    RESEARCH_PROMPT_DEEP,
    RESEARCH_PROMPT_QUICK,
    TASK_BREAKDOWN_PROMPT,
    TEAM_ASSEMBLY_PROMPT,
)
from pocketclaw.mission_control.manager import MissionControlManager
from pocketclaw.mission_control.models import AgentProfile

logger = logging.getLogger(__name__)

# Regex to strip markdown code fences (```json ... ``` or ``` ... ```)
_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


class PlannerAgent:
    """Orchestrates multi-phase project planning through LLM calls.

    Phases:
      1. Research — gather domain knowledge
      2. PRD — generate a product requirements document
      3. Task breakdown — decompose into atomic tasks (JSON)
      4. Team assembly — recommend agents for the project (JSON)

    Each phase runs a formatted prompt through AgentRouter and collects
    the streamed text output.
    """

    def __init__(self, manager: MissionControlManager):
        self.manager = manager

    async def ensure_profile(self) -> AgentProfile:
        """Get or create the 'deep-work-planner' agent in Mission Control."""
        existing = await self.manager.get_agent_by_name("deep-work-planner")
        if existing:
            return existing
        return await self.manager.create_agent(
            name="deep-work-planner",
            role="Project Planner & Architect",
            description=(
                "Researches domains, generates PRDs, breaks projects "
                "into executable tasks, and recommends team composition"
            ),
            specialties=["planning", "research", "architecture", "task-decomposition"],
            backend="claude_agent_sdk",
        )

    async def plan(
        self,
        project_description: str,
        project_id: str = "",
        research_depth: str = "standard",
    ) -> PlannerResult:
        """Run all 4 planning phases and return a structured PlannerResult.

        Args:
            project_description: Natural language project description.
            project_id: ID of the project being planned.
            research_depth: How thorough to research — "none" (skip entirely),
                "quick", "standard", or "deep". None skips the research
                LLM call entirely, passing empty notes to subsequent phases.

        Broadcasts SystemEvents for each phase so the frontend can show
        progress (e.g. spinner text).
        """
        # Create a single AgentRouter for all phases (avoids 4x SDK init)
        from pocketclaw.agents.router import AgentRouter
        from pocketclaw.config import get_settings

        router = AgentRouter(get_settings())

        # Phase 1: Research (depth controls prompt and thoroughness)
        if research_depth == "none":
            # Skip research entirely — no LLM call
            research = ""
        else:
            self._broadcast_phase(project_id, "research")
            research_prompts = {
                "quick": RESEARCH_PROMPT_QUICK,
                "standard": RESEARCH_PROMPT,
                "deep": RESEARCH_PROMPT_DEEP,
            }
            prompt_template = research_prompts.get(research_depth, RESEARCH_PROMPT)
            research = await self._run_prompt(
                prompt_template.format(project_description=project_description),
                router=router,
            )

        # Phase 2: PRD
        self._broadcast_phase(project_id, "prd")
        prd = await self._run_prompt(
            PRD_PROMPT.format(
                project_description=project_description,
                research_notes=research,
            ),
            router=router,
        )

        # Phase 3: Task breakdown
        self._broadcast_phase(project_id, "tasks")
        tasks_raw = await self._run_prompt(
            TASK_BREAKDOWN_PROMPT.format(
                project_description=project_description,
                prd_content=prd,
                research_notes=research,
            ),
            router=router,
        )
        tasks = self._parse_tasks(tasks_raw)

        # Retry once if task breakdown failed to parse
        if not tasks:
            logger.info("Retrying task breakdown with explicit JSON instruction")
            tasks_raw = await self._run_prompt(
                "Your previous response was not valid JSON. "
                "Return ONLY a JSON array of task objects, no markdown, "
                "no explanation — just the raw JSON array.\n\n"
                + TASK_BREAKDOWN_PROMPT.format(
                    project_description=project_description,
                    prd_content=prd,
                    research_notes=research,
                ),
                router=router,
            )
            tasks = self._parse_tasks(tasks_raw)

        # Phase 4: Team assembly
        self._broadcast_phase(project_id, "team")
        tasks_json_str = json.dumps([t.to_dict() for t in tasks], indent=2)
        team_raw = await self._run_prompt(
            TEAM_ASSEMBLY_PROMPT.format(tasks_json=tasks_json_str),
            router=router,
        )
        team = self._parse_team(team_raw)

        # Split human tasks out for the result
        human_tasks = [t for t in tasks if t.task_type == "human"]
        agent_tasks = [t for t in tasks if t.task_type != "human"]

        # Build dependency graph: key -> [keys it depends on]
        dep_graph: dict[str, list[str]] = {}
        for t in tasks:
            if t.blocked_by_keys:
                dep_graph[t.key] = list(t.blocked_by_keys)

        total_minutes = sum(t.estimated_minutes for t in tasks)

        return PlannerResult(
            project_id=project_id,
            prd_content=prd,
            tasks=agent_tasks,
            team_recommendation=team,
            human_tasks=human_tasks,
            dependency_graph=dep_graph,
            estimated_total_minutes=total_minutes,
            research_notes=research,
        )

    async def _run_prompt(self, prompt: str, router=None) -> str:
        """Run a prompt through AgentRouter and collect all message chunks.

        Args:
            prompt: The prompt to send to the LLM.
            router: Optional pre-created AgentRouter (avoids re-initialization).
        """
        if router is None:
            from pocketclaw.agents.router import AgentRouter
            from pocketclaw.config import get_settings

            router = AgentRouter(get_settings())

        output_parts: list[str] = []

        async for chunk in router.run(prompt):
            if chunk.get("type") == "message":
                content = chunk.get("content", "")
                if content:
                    output_parts.append(content)

        return "".join(output_parts)

    def _parse_tasks(self, raw: str) -> list[TaskSpec]:
        """Parse LLM JSON output into a list of TaskSpec objects.

        Handles markdown code fences (```json ... ```) and returns an
        empty list on parse failure.
        """
        data = self._parse_json_list(raw, "task breakdown")
        if data is None:
            return []
        return [TaskSpec.from_dict(item) for item in data if isinstance(item, dict)]

    def _parse_team(self, raw: str) -> list[AgentSpec]:
        """Parse LLM JSON output into a list of AgentSpec objects.

        Handles markdown code fences and returns an empty list on failure.
        """
        data = self._parse_json_list(raw, "team assembly")
        if data is None:
            return []
        return [AgentSpec.from_dict(item) for item in data if isinstance(item, dict)]

    def _parse_json_list(self, raw: str, label: str) -> list[dict] | None:
        """Parse raw LLM output as a JSON list, with one retry on failure.

        Returns None if parsing fails after retry.
        """
        cleaned = self._strip_code_fences(raw)
        try:
            data = json.loads(cleaned)
            if isinstance(data, list):
                return data
            logger.warning(f"{label} JSON is not a list")
            return None
        except (json.JSONDecodeError, TypeError):
            logger.warning("Failed to parse %s JSON (will retry):\n%s", label, raw[:200])
            return None

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """Remove markdown code fences from LLM output.

        Extracts content from ```json ... ``` or ``` ... ``` blocks.
        If no fences found, returns the original text stripped.
        """
        match = _CODE_FENCE_RE.search(text)
        if match:
            return match.group(1).strip()
        return text.strip()

    def _broadcast_phase(self, project_id: str, phase: str) -> None:
        """Publish a SystemEvent for frontend progress tracking.

        This is best-effort — if the bus is not running (e.g. in tests),
        the error is silently ignored.
        """
        phase_messages = {
            "research": "Researching domain knowledge...",
            "prd": "Writing product requirements...",
            "tasks": "Breaking down into tasks...",
            "team": "Assembling agent team...",
        }
        message = phase_messages.get(phase, f"Planning phase: {phase}")

        try:
            from pocketclaw.bus import get_message_bus
            from pocketclaw.bus.events import SystemEvent

            bus = get_message_bus()
            import asyncio

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(
                    bus.publish_system(
                        SystemEvent(
                            event_type="dw_planning_phase",
                            data={
                                "project_id": project_id,
                                "phase": phase,
                                "message": message,
                            },
                        )
                    )
                )
            except RuntimeError:
                pass  # No event loop running
        except Exception:
            pass  # Bus may not be available in tests
