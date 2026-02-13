# Plan Mode — approval flow for tool execution.
# Created: 2026-02-07
# Part of Phase 2 Integration Ecosystem

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class PlanStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTING = "executing"
    COMPLETED = "completed"


@dataclass
class PlanStep:
    """A single tool call in an execution plan."""

    tool_name: str
    tool_input: dict[str, Any]
    preview: str = ""  # Human-readable preview

    def generate_preview(self) -> str:
        """Generate a human-readable preview of this step."""
        if self.tool_name in ("shell", "Bash"):
            cmd = self.tool_input.get("command", "")
            return f"$ {cmd}"
        elif self.tool_name in ("write_file", "Write"):
            path = self.tool_input.get("path", self.tool_input.get("file_path", ""))
            content = self.tool_input.get("content", "")
            preview = content[:200] + "..." if len(content) > 200 else content
            return f"Write to {path}:\n{preview}"
        elif self.tool_name in ("edit_file", "Edit"):
            path = self.tool_input.get("path", self.tool_input.get("file_path", ""))
            return f"Edit {path}"
        elif self.tool_name in ("read_file", "Read"):
            path = self.tool_input.get("path", self.tool_input.get("file_path", ""))
            return f"Read {path}"
        else:
            params_str = ", ".join(f"{k}={v!r}" for k, v in self.tool_input.items())
            return f"{self.tool_name}({params_str[:200]})"


@dataclass
class ExecutionPlan:
    """A collection of tool calls awaiting approval."""

    session_key: str
    steps: list[PlanStep] = field(default_factory=list)
    status: PlanStatus = PlanStatus.PROPOSED
    created_at: float = field(default_factory=time.time)

    def add_step(self, tool_name: str, tool_input: dict[str, Any]) -> PlanStep:
        """Add a step to the plan."""
        step = PlanStep(tool_name=tool_name, tool_input=tool_input)
        step.preview = step.generate_preview()
        self.steps.append(step)
        return step

    def to_preview(self) -> str:
        """Generate a full preview of the plan for display."""
        if not self.steps:
            return "Empty plan"
        lines = [f"Execution Plan ({len(self.steps)} steps):\n"]
        for i, step in enumerate(self.steps, 1):
            lines.append(f"  {i}. {step.preview}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        """Serialize for WebSocket/API transport."""
        return {
            "session_key": self.session_key,
            "status": self.status.value,
            "steps": [
                {
                    "tool_name": s.tool_name,
                    "tool_input": s.tool_input,
                    "preview": s.preview,
                }
                for s in self.steps
            ],
            "created_at": self.created_at,
        }


class PlanManager:
    """Manages execution plans awaiting user approval.

    One active plan per session. Plans expire after 5 minutes.
    """

    PLAN_TIMEOUT = 300  # 5 minutes

    def __init__(self):
        self._plans: dict[str, ExecutionPlan] = {}
        self._approval_events: dict[str, asyncio.Event] = {}

    def create_plan(self, session_key: str, steps: list[PlanStep] | None = None) -> ExecutionPlan:
        """Create a new plan for a session (replaces any existing)."""
        plan = ExecutionPlan(session_key=session_key)
        if steps:
            plan.steps = steps
        self._plans[session_key] = plan
        self._approval_events[session_key] = asyncio.Event()
        return plan

    def add_step_to_plan(
        self, session_key: str, tool_name: str, tool_input: dict[str, Any]
    ) -> PlanStep | None:
        """Add a step to the active plan. Creates plan if needed."""
        plan = self._plans.get(session_key)
        if plan is None:
            plan = self.create_plan(session_key)
        return plan.add_step(tool_name, tool_input)

    def approve_plan(self, session_key: str) -> ExecutionPlan | None:
        """Approve the active plan for a session."""
        plan = self._plans.get(session_key)
        if plan and plan.status == PlanStatus.PROPOSED:
            plan.status = PlanStatus.APPROVED
            event = self._approval_events.get(session_key)
            if event:
                event.set()
            return plan
        return None

    def reject_plan(self, session_key: str) -> ExecutionPlan | None:
        """Reject the active plan for a session."""
        plan = self._plans.get(session_key)
        if plan and plan.status == PlanStatus.PROPOSED:
            plan.status = PlanStatus.REJECTED
            event = self._approval_events.get(session_key)
            if event:
                event.set()
            return plan
        return None

    def get_active_plan(self, session_key: str) -> ExecutionPlan | None:
        """Get the active plan for a session, if any."""
        plan = self._plans.get(session_key)
        if plan and (time.time() - plan.created_at) > self.PLAN_TIMEOUT:
            # Plan expired
            self._plans.pop(session_key, None)
            self._approval_events.pop(session_key, None)
            return None
        return plan

    async def wait_for_approval(self, session_key: str, timeout: float = 300) -> PlanStatus:
        """Wait for the plan to be approved or rejected.

        Returns the final status (APPROVED or REJECTED).
        Raises asyncio.TimeoutError if timeout expires.
        """
        event = self._approval_events.get(session_key)
        if not event:
            return PlanStatus.REJECTED

        await asyncio.wait_for(event.wait(), timeout=timeout)

        plan = self._plans.get(session_key)
        return plan.status if plan else PlanStatus.REJECTED

    def clear_plan(self, session_key: str) -> None:
        """Remove the plan for a session."""
        self._plans.pop(session_key, None)
        self._approval_events.pop(session_key, None)


# Singleton
_plan_manager: PlanManager | None = None


def get_plan_manager() -> PlanManager:
    """Get the singleton plan manager."""
    global _plan_manager
    if _plan_manager is None:
        _plan_manager = PlanManager()
    return _plan_manager
