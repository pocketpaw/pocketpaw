"""Chat domain — re-exports for backward compatibility.

The chat module is split across ``message_service.py``, ``group_service.py``,
``unread_service.py``, and ``agent_service.py`` (one module per entity).
This file keeps the wire-helper re-exports (``_message_response``,
``_group_response``) that a few callers still import directly.
"""

from pocketpaw_ee.cloud.chat.group_service import _group_response  # noqa: F401
from pocketpaw_ee.cloud.chat.message_service import _message_response  # noqa: F401
