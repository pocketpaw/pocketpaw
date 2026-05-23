"""Meeting events → notification fan-out — source-agnostic.

Subscribes to ``MeetingScheduled``, ``MeetingReminder``, ``MeetingStarted``,
``MeetingCancelled`` and emits in-app notifications via the existing
``notifications.service.create``. The notification ``type`` strings
(``meeting_scheduled`` / ``meeting_reminder`` / ``meeting_started`` /
``meeting_cancelled``) match what paw-enterprise #235 listens for —
those names are the existing contract between the two repos.

Phase 1 ships a stub registration function — actual subscribers land in
Phase 3 alongside the LiveKit rebase, which is the first PR that produces
``MeetingScheduled`` events at scale. Recall-source meetings don't go
through the scheduling path today; they're sent-bot-on-demand and skip
the ``meeting.scheduled`` notification entirely.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def register_meeting_notification_listeners() -> None:
    """Called from ``mount_cloud()`` after ``init_realtime``. Phase 1 stub.

    When Phase 3 lands, this subscribes to the four meeting.* events and
    calls ``notifications.service.create`` for each, with the audience
    resolved from the meeting's participant_user_ids.
    """
    logger.info(
        "meetings notification bridge is a Phase 1 stub — no fan-out "
        "subscribers registered yet. Lands with #1178's rebase."
    )
