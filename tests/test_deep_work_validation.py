"""Tests for Deep Work input validation.

This test suite validates the input validation added to the
plan_existing_project function to prevent silent failures.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from pocketpaw.deep_work.session import VALID_RESEARCH_DEPTHS, DeepWorkSession
from pocketpaw.mission_control import (
    FileMissionControlStore,
    MissionControlManager,
)
from pocketpaw.deep_work.models import Project, ProjectStatus


@pytest.fixture
async def session(tmp_path):
    """Create a DeepWorkSession with a temporary store for testing."""
    store = FileMissionControlStore(tmp_path)
    manager = MissionControlManager(store)

    # Create mock executor
    executor = MagicMock()
    executor.stop_task = AsyncMock()
    executor.is_task_running = MagicMock(return_value=False)

    session = DeepWorkSession(manager=manager, executor=executor)
    
    # Create a test project
    project = await manager.create_project(
        title="Test Project",
        description="Test project for validation tests",
    )
    
    # Store project_id for tests to use
    session._test_project_id = project.id
    
    return session


class TestResearchDepthValidation:
    """Test validation of research_depth parameter."""

    @pytest.mark.asyncio
    async def test_valid_research_depths_constant_exists(self):
        """The VALID_RESEARCH_DEPTHS constant should be defined."""
        assert VALID_RESEARCH_DEPTHS is not None
        assert len(VALID_RESEARCH_DEPTHS) == 4
        assert "none" in VALID_RESEARCH_DEPTHS
        assert "quick" in VALID_RESEARCH_DEPTHS
        assert "standard" in VALID_RESEARCH_DEPTHS
        assert "deep" in VALID_RESEARCH_DEPTHS

    @pytest.mark.asyncio
    async def test_invalid_research_depth(self, session):
        """Invalid research_depth should raise ValueError with clear message."""
        with pytest.raises(ValueError) as exc_info:
            await session.plan_existing_project(
                session._test_project_id,
                "Build a todo app",
                "invalid_depth"
            )
        
        error_msg = str(exc_info.value)
        assert "Invalid research_depth" in error_msg
        assert "invalid_depth" in error_msg
        assert "none" in error_msg or "quick" in error_msg  # Shows valid options

    @pytest.mark.asyncio
    async def test_empty_research_depth(self, session):
        """Empty string research_depth should raise ValueError."""
        with pytest.raises(ValueError) as exc_info:
            await session.plan_existing_project(
                session._test_project_id,
                "Build a todo app",
                ""
            )
        
        assert "Invalid research_depth" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_case_sensitive_research_depth(self, session):
        """research_depth should be case-sensitive (STANDARD != standard)."""
        with pytest.raises(ValueError) as exc_info:
            await session.plan_existing_project(
                session._test_project_id,
                "Build a todo app",
                "STANDARD"  # uppercase
            )
        
        assert "Invalid research_depth" in str(exc_info.value)


class TestUserInputValidation:
    """Test validation of user_input parameter."""

    @pytest.mark.asyncio
    async def test_empty_user_input(self, session):
        """Empty user_input should raise ValueError."""
        with pytest.raises(ValueError) as exc_info:
            await session.plan_existing_project(
                session._test_project_id,
                "",
                "standard"
            )
        
        assert "cannot be empty" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_whitespace_only_user_input(self, session):
        """Whitespace-only user_input should raise ValueError."""
        test_cases = [
            "   ",
            "\n",
            "\t",
            "  \n  \t  ",
        ]
        
        for whitespace_input in test_cases:
            with pytest.raises(ValueError) as exc_info:
                await session.plan_existing_project(
                    session._test_project_id,
                    whitespace_input,
                    "standard"
                )
            
            assert "cannot be empty" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_user_input_too_long(self, session):
        """User input over 5000 chars should raise ValueError."""
        long_input = "a" * 5001
        
        with pytest.raises(ValueError) as exc_info:
            await session.plan_existing_project(
                session._test_project_id,
                long_input,
                "standard"
            )
        
        error_msg = str(exc_info.value)
        assert "too long" in error_msg
        assert "5000" in error_msg

    @pytest.mark.asyncio
    async def test_user_input_exactly_5000_chars(self, session):
        """User input of exactly 5000 chars should be accepted."""
        # This test will fail during planning phase (no LLM configured)
        # but should pass validation
        exact_input = "a" * 5000
        
        # We expect this to fail at the planning stage, NOT at validation
        # So we check it doesn't raise a "too long" error
        try:
            await session.plan_existing_project(
                session._test_project_id,
                exact_input,
                "standard"
            )
        except ValueError as e:
            # If it fails, it should NOT be because of length
            assert "too long" not in str(e)


class TestValidInputAcceptance:
    """Test that valid inputs are accepted (don't raise validation errors)."""

    @pytest.mark.asyncio
    async def test_minimal_valid_input(self, session):
        """Minimal valid input should pass validation."""
        # This will fail at planning (no LLM), but should pass validation
        try:
            await session.plan_existing_project(
                session._test_project_id,
                "a",  # single character
                "none"  # no research needed
            )
        except ValueError as e:
            # Should not fail on validation
            assert "Invalid research_depth" not in str(e)
            assert "cannot be empty" not in str(e)
            assert "too long" not in str(e)

    @pytest.mark.asyncio
    async def test_all_valid_research_depths_pass_validation(self, session):
        """All valid research_depth values should pass validation."""
        for depth in VALID_RESEARCH_DEPTHS:
            # Each should pass validation (may fail at planning stage)
            try:
                await session.plan_existing_project(
                    session._test_project_id,
                    "Build a simple app",
                    depth
                )
            except ValueError as e:
                # Should not fail on research_depth validation
                error_msg = str(e)
                assert "Invalid research_depth" not in error_msg, \
                    f"Valid depth '{depth}' was rejected: {error_msg}"