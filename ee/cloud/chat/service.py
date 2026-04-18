# Refactored: Now a thin re-export module for backward compatibility.
# GroupService and helpers moved to group_service.py (with N+1 fix).
# MessageService and helpers moved to message_service.py (with new create_agent_message).

"""Chat domain — re-exports for backward compatibility."""

from ee.cloud.chat.group_service import GroupService, _group_response  # noqa: F401
from ee.cloud.chat.message_service import MessageService, _message_response  # noqa: F401
