"""Unit tests for the StatsManager."""

import pytest
from datetime import datetime
from pocketclaw.stats.manager import AgentCallStats, StatsManager, get_stats_manager


def test_stats_manager_singleton():
    """Test that get_stats_manager returns the same instance."""
    manager1 = get_stats_manager()
    manager2 = get_stats_manager()
    assert manager1 is manager2


def test_start_end_call():
    """Test starting and ending a call."""
    # Use a fresh instance for this test
    manager = StatsManager(max_history=100)
    
    # Start a call
    call_id = manager.start_call("test_session", "claude_agent_sdk", "claude-3-sonnet")
    assert call_id is not None
    assert call_id.startswith("test_session_")
    
    # End the call
    stats = manager.end_call(
        call_id=call_id,
        input_tokens=100,
        output_tokens=200,
        model="claude-3-sonnet-20240229",
        success=True
    )
    
    assert stats is not None
    assert stats.session_key == "test_session"
    assert stats.backend == "claude_agent_sdk"
    assert stats.input_tokens == 100
    assert stats.output_tokens == 200
    assert stats.total_tokens == 300
    assert stats.model == "claude-3-sonnet-20240229"
    assert stats.success is True
    assert stats.response_time_ms > 0


def test_get_summary():
    """Test getting summary statistics."""
    # Use a fresh instance for this test
    manager = StatsManager(max_history=100)
    
    # Initially empty
    summary = manager.get_summary()
    assert summary["total_calls"] == 0
    assert summary["avg_response_time_ms"] == 0.0
    
    # Add some calls
    for i in range(5):
        call_id = manager.start_call(f"session_{i}", "claude_agent_sdk")
        manager.end_call(
            call_id=call_id,
            input_tokens=100 + i * 10,
            output_tokens=200 + i * 20,
            success=True
        )
    
    # Check summary
    summary = manager.get_summary()
    assert summary["total_calls"] == 5
    assert summary["successful_calls"] == 5
    assert summary["failed_calls"] == 0
    assert summary["success_rate"] == 100.0
    assert summary["total_tokens"] > 0
    assert summary["avg_tokens_per_call"] > 0
    assert summary["avg_response_time_ms"] > 0


def test_get_recent_calls():
    """Test getting recent calls."""
    # Use a fresh instance for this test
    manager = StatsManager(max_history=100)
    
    # Add some calls
    for i in range(10):
        call_id = manager.start_call(f"session_{i}", "claude_agent_sdk")
        manager.end_call(
            call_id=call_id,
            input_tokens=100,
            output_tokens=200,
            success=True
        )
    
    # Get recent calls
    recent = manager.get_recent_calls(limit=5)
    assert len(recent) == 5
    
    # Should be in reverse chronological order (most recent first)
    assert all(isinstance(call, dict) for call in recent)
    assert all("timestamp" in call for call in recent)
    assert all("response_time_ms" in call for call in recent)


def test_clear_history():
    """Test clearing statistics history."""
    # Use a fresh instance for this test
    manager = StatsManager(max_history=100)
    
    # Add some calls
    for i in range(3):
        call_id = manager.start_call(f"session_{i}", "claude_agent_sdk")
        manager.end_call(call_id=call_id, input_tokens=100, output_tokens=200)
    
    assert manager.get_call_count() == 3
    
    # Clear history
    manager.clear_history()
    assert manager.get_call_count() == 0
    
    summary = manager.get_summary()
    assert summary["total_calls"] == 0


def test_max_history_limit():
    """Test that history respects max_history limit."""
    # Use a fresh instance with small limit
    manager = StatsManager(max_history=5)
    
    # Add more calls than the limit
    for i in range(10):
        call_id = manager.start_call(f"session_{i}", "claude_agent_sdk")
        manager.end_call(call_id=call_id, input_tokens=100, output_tokens=200)
    
    # Should only keep the last 5
    assert manager.get_call_count() == 5


def test_failed_call():
    """Test tracking a failed call."""
    # Use a fresh instance for this test
    manager = StatsManager(max_history=100)
    
    call_id = manager.start_call("test_session", "claude_agent_sdk")
    stats = manager.end_call(
        call_id=call_id,
        input_tokens=50,
        output_tokens=0,
        success=False,
        error="API error"
    )
    
    assert stats.success is False
    assert stats.error == "API error"
    
    summary = manager.get_summary()
    assert summary["failed_calls"] == 1
    assert summary["success_rate"] == 0.0


def test_stats_to_dict():
    """Test AgentCallStats to_dict conversion."""
    stats = AgentCallStats(
        timestamp=datetime.now(),
        session_key="test",
        backend="claude_agent_sdk",
        response_time_ms=1234.56,
        input_tokens=100,
        output_tokens=200,
        total_tokens=300,
        model="claude-3-sonnet",
        success=True,
        error=""
    )
    
    data = stats.to_dict()
    assert isinstance(data, dict)
    assert data["session_key"] == "test"
    assert data["backend"] == "claude_agent_sdk"
    assert data["response_time_ms"] == 1234.56
    assert data["input_tokens"] == 100
    assert data["output_tokens"] == 200
    assert data["total_tokens"] == 300
    assert data["model"] == "claude-3-sonnet"
    assert data["success"] is True
    assert "timestamp" in data


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
