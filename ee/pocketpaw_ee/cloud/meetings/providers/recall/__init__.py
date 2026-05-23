"""Recall.ai meeting provider — external Zoom/Meet/Teams capture.

A Recall.ai bot joins a third-party meeting URL, records the call, and
produces a transcript via Deepgram (or any of Recall's 9 supported
async/streaming providers).

Importing this package has a side effect: it registers
``RecallProvider`` with the meetings provider registry. ``mount_cloud()``
eager-imports this package at startup so ``base.resolve("recall")``
succeeds from the first request.
"""

from pocketpaw_ee.cloud.meetings.providers import base
from pocketpaw_ee.cloud.meetings.providers.recall.provider import RecallProvider

base.register(RecallProvider())

__all__ = ["RecallProvider"]
