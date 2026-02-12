"""Statistics manager for tracking agent performance metrics."""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class AgentCallStats:
    """Statistics for a single agent call."""

    timestamp: datetime
    session_key: str
    backend: str
    response_time_ms: float
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    model: str = ""
    success: bool = True
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "timestamp": self.timestamp.isoformat(),
            "session_key": self.session_key,
            "backend": self.backend,
            "response_time_ms": self.response_time_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "model": self.model,
            "success": self.success,
            "error": self.error,
        }


class StatsManager:
    """Manages agent performance statistics."""

    def __init__(self, max_history: int = 1000):
        """
        Initialize the stats manager.

        Args:
            max_history: Maximum number of calls to keep in history
        """
        self._max_history = max_history
        self._call_history: deque[AgentCallStats] = deque(maxlen=max_history)
        self._active_calls: Dict[str, Dict[str, Any]] = {}
        logger.info(f"📊 StatsManager initialized (max_history={max_history})")

    def start_call(self, session_key: str, backend: str, model: str = "") -> str:
        """
        Start tracking a new agent call.

        Args:
            session_key: Session identifier
            backend: Agent backend being used
            model: Model name (optional)

        Returns:
            call_id: Unique identifier for this call
        """
        call_id = f"{session_key}_{int(time.time() * 1000)}"
        self._active_calls[call_id] = {
            "session_key": session_key,
            "backend": backend,
            "model": model,
            "start_time": time.time(),
        }
        logger.debug(f"📊 Started tracking call: {call_id}")
        return call_id

    def end_call(
        self,
        call_id: str,
        input_tokens: int = 0,
        output_tokens: int = 0,
        model: str = "",
        success: bool = True,
        error: str = "",
    ) -> Optional[AgentCallStats]:
        """
        End tracking an agent call and record statistics.

        Args:
            call_id: Call identifier from start_call
            input_tokens: Number of input tokens used
            output_tokens: Number of output tokens generated
            model: Model name (overrides start_call if provided)
            success: Whether the call succeeded
            error: Error message if call failed

        Returns:
            AgentCallStats object or None if call_id not found
        """
        if call_id not in self._active_calls:
            logger.warning(f"📊 Call ID not found: {call_id}")
            return None

        call_data = self._active_calls.pop(call_id)
        end_time = time.time()
        response_time_ms = (end_time - call_data["start_time"]) * 1000

        stats = AgentCallStats(
            timestamp=datetime.now(),
            session_key=call_data["session_key"],
            backend=call_data["backend"],
            response_time_ms=response_time_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=input_tokens + output_tokens,
            model=model or call_data.get("model", ""),
            success=success,
            error=error,
        )

        self._call_history.append(stats)

        logger.info(
            f"📊 Call completed: {call_id} | "
            f"Time: {response_time_ms:.0f}ms | "
            f"Tokens: {stats.total_tokens} | "
            f"Success: {success}"
        )

        return stats

    def get_summary(self) -> Dict[str, Any]:
        """
        Get summary statistics across all recorded calls.

        Returns:
            Dictionary with summary metrics
        """
        if not self._call_history:
            return {
                "total_calls": 0,
                "successful_calls": 0,
                "failed_calls": 0,
                "success_rate": 0.0,
                "avg_response_time_ms": 0.0,
                "total_tokens": 0,
                "total_input_tokens": 0,
                "total_output_tokens": 0,
                "avg_tokens_per_call": 0.0,
            }

        total_calls = len(self._call_history)
        successful_calls = sum(1 for call in self._call_history if call.success)
        failed_calls = total_calls - successful_calls

        total_response_time = sum(call.response_time_ms for call in self._call_history)
        total_tokens = sum(call.total_tokens for call in self._call_history)
        total_input_tokens = sum(call.input_tokens for call in self._call_history)
        total_output_tokens = sum(call.output_tokens for call in self._call_history)

        return {
            "total_calls": total_calls,
            "successful_calls": successful_calls,
            "failed_calls": failed_calls,
            "success_rate": (successful_calls / total_calls * 100) if total_calls > 0 else 0.0,
            "avg_response_time_ms": total_response_time / total_calls if total_calls > 0 else 0.0,
            "total_tokens": total_tokens,
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "avg_tokens_per_call": total_tokens / total_calls if total_calls > 0 else 0.0,
        }

    def get_recent_calls(self, limit: int = 20) -> List[Dict[str, Any]]:
        """
        Get recent agent calls.

        Args:
            limit: Maximum number of calls to return

        Returns:
            List of call statistics as dictionaries
        """
        recent = list(self._call_history)[-limit:]
        return [call.to_dict() for call in reversed(recent)]

    def clear_history(self) -> None:
        """Clear all statistics history."""
        self._call_history.clear()
        self._active_calls.clear()
        logger.info("📊 Statistics history cleared")

    def get_call_count(self) -> int:
        """Get total number of calls in history."""
        return len(self._call_history)


# Global singleton instance
_stats_manager: Optional[StatsManager] = None


def get_stats_manager() -> StatsManager:
    """
    Get the global StatsManager instance.

    Returns:
        StatsManager singleton instance
    """
    global _stats_manager
    if _stats_manager is None:
        _stats_manager = StatsManager()
    return _stats_manager
