# Deep Work Human Task Router
# Created: 2026-02-12
#
# Routes human-required tasks and project notifications to messaging
# channels (Telegram, Discord, Slack, WhatsApp, WebSocket) via the
# MessageBus broadcast mechanism.

import logging

from pocketclaw.mission_control.models import Task

logger = logging.getLogger(__name__)


class HumanTaskRouter:
    """Routes human tasks and notifications to configured channels.

    Uses MessageBus.broadcast_outbound to fan out notifications to all
    active channel adapters (Telegram, Discord, Slack, WhatsApp, etc.).
    """

    async def notify_human_task(self, task: Task) -> None:
        """Push a human-required task to all active channels."""
        message = self._format_task_notification(task)
        await self._publish_outbound(
            message,
            {
                "type": "human_task",
                "task_id": task.id,
                "project_id": task.project_id or "",
            },
        )
        logger.info(f"Human task routed: {task.title}")

    async def notify_review_task(self, task: Task) -> None:
        """Notify user that an agent task is ready for review."""
        message = self._format_review_notification(task)
        await self._publish_outbound(
            message,
            {
                "type": "review_task",
                "task_id": task.id,
                "project_id": task.project_id or "",
            },
        )
        logger.info(f"Review task routed: {task.title}")

    async def notify_plan_ready(
        self, project, task_count: int = 0, estimated_minutes: int = 0
    ) -> None:
        """Notify user that Deep Work plan is ready for approval."""
        message = (
            f"**Deep Work plan ready for review**\n\n"
            f"Project: **{project.title}**\n"
            f"Tasks: {task_count}\n"
            f"Estimated time: ~{estimated_minutes} minutes\n\n"
            f"Review and approve in the dashboard."
        )
        await self._publish_outbound(
            message,
            {
                "type": "plan_ready",
                "project_id": project.id,
            },
        )
        logger.info(f"Plan ready notification sent: {project.title}")

    async def notify_project_completed(self, project, tasks: list[Task] | None = None) -> None:
        """Notify user that all project tasks are done."""
        completed_count = len([t for t in (tasks or []) if t.status.value == "done"])
        total_count = len(tasks or [])
        message = (
            f"**Deep Work project completed!**\n\n"
            f"Project: **{project.title}**\n"
            f"Tasks completed: {completed_count}/{total_count}\n\n"
            f"View deliverables in the dashboard."
        )
        await self._publish_outbound(
            message,
            {
                "type": "project_completed",
                "project_id": project.id,
            },
        )
        logger.info(f"Project completed notification sent: {project.title}")

    def _format_task_notification(self, task: Task) -> str:
        """Format task as channel-friendly message."""
        lines = [
            "**Task needs your help**",
            "",
            f"**{task.title}**",
        ]
        if task.description:
            desc = task.description[:300]
            if len(task.description) > 300:
                desc += "..."
            lines.append(desc)
        lines.append("")
        lines.append(f"Priority: {task.priority.value}")
        if task.tags:
            lines.append(f"Tags: {', '.join(task.tags)}")
        lines.append("")
        lines.append("Mark complete in the dashboard when done.")
        return "\n".join(lines)

    def _format_review_notification(self, task: Task) -> str:
        """Format review notification."""
        return (
            f"**Task ready for review**\n\n"
            f"**{task.title}**\n"
            f"An agent completed this task. Please review in the dashboard."
        )

    async def _publish_outbound(self, content: str, metadata: dict) -> None:
        """Broadcast OutboundMessage to all active channel adapters."""
        try:
            from pocketclaw.bus import get_message_bus
            from pocketclaw.bus.events import Channel, OutboundMessage

            bus = get_message_bus()
            msg = OutboundMessage(
                channel=Channel.SYSTEM,
                chat_id="broadcast",
                content=content,
                metadata=metadata,
            )
            await bus.broadcast_outbound(msg)
        except Exception as e:
            logger.warning(f"Failed to publish notification: {e}")
