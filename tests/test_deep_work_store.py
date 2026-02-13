# Tests for Deep Work models and store integration.
# Created: 2026-02-12
# Tests Project, TaskSpec, AgentSpec, PlannerResult models and
# FileMissionControlStore project CRUD operations.

import tempfile
from pathlib import Path

import pytest

from pocketclaw.deep_work.models import (
    AgentSpec,
    PlannerResult,
    Project,
    ProjectStatus,
    TaskSpec,
)
from pocketclaw.mission_control.store import (
    FileMissionControlStore,
    reset_mission_control_store,
)

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_store_path():
    """Create a temporary directory for test storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def store(temp_store_path):
    """Create a fresh store for each test."""
    reset_mission_control_store()
    return FileMissionControlStore(temp_store_path)


# ============================================================================
# Model Tests
# ============================================================================


class TestProjectModel:
    """Tests for Project dataclass."""

    def test_project_defaults(self):
        """Test Project default values."""
        project = Project()
        assert project.id is not None
        assert project.title == ""
        assert project.status == ProjectStatus.DRAFT
        assert project.creator_id == "human"
        assert project.team_agent_ids == []
        assert project.task_ids == []
        assert project.created_at is not None

    def test_project_to_dict(self):
        """Test Project serialization."""
        project = Project(
            title="Build Dashboard",
            description="Create a web dashboard for monitoring",
            status=ProjectStatus.PLANNING,
            tags=["frontend", "dashboard"],
            creator_id="agent-planner",
        )
        data = project.to_dict()
        assert data["title"] == "Build Dashboard"
        assert data["description"] == "Create a web dashboard for monitoring"
        assert data["status"] == "planning"
        assert data["tags"] == ["frontend", "dashboard"]
        assert data["creator_id"] == "agent-planner"

    def test_project_from_dict(self):
        """Test Project deserialization."""
        data = {
            "id": "proj-123",
            "title": "API Refactor",
            "status": "executing",
            "planner_agent_id": "agent-1",
            "team_agent_ids": ["agent-2", "agent-3"],
            "task_ids": ["task-a", "task-b"],
            "tags": ["backend"],
        }
        project = Project.from_dict(data)
        assert project.id == "proj-123"
        assert project.title == "API Refactor"
        assert project.status == ProjectStatus.EXECUTING
        assert project.planner_agent_id == "agent-1"
        assert project.team_agent_ids == ["agent-2", "agent-3"]
        assert project.task_ids == ["task-a", "task-b"]

    def test_project_to_dict_from_dict_roundtrip(self):
        """Test Project round-trip serialization."""
        original = Project(
            title="Round Trip",
            description="Testing serialization",
            status=ProjectStatus.AWAITING_APPROVAL,
            planner_agent_id="planner-1",
            team_agent_ids=["a1", "a2"],
            task_ids=["t1", "t2"],
            prd_document_id="doc-1",
            creator_id="human",
            tags=["test", "roundtrip"],
            started_at="2026-01-01T00:00:00+00:00",
            metadata={"key": "value"},
        )
        data = original.to_dict()
        restored = Project.from_dict(data)

        assert restored.id == original.id
        assert restored.title == original.title
        assert restored.description == original.description
        assert restored.status == original.status
        assert restored.planner_agent_id == original.planner_agent_id
        assert restored.team_agent_ids == original.team_agent_ids
        assert restored.task_ids == original.task_ids
        assert restored.prd_document_id == original.prd_document_id
        assert restored.creator_id == original.creator_id
        assert restored.tags == original.tags
        assert restored.started_at == original.started_at
        assert restored.metadata == original.metadata


class TestTaskSpecModel:
    """Tests for TaskSpec dataclass."""

    def test_task_spec_defaults(self):
        """Test TaskSpec default values."""
        spec = TaskSpec()
        assert spec.key == ""
        assert spec.task_type == "agent"
        assert spec.priority == "medium"
        assert spec.estimated_minutes == 30

    def test_task_spec_to_dict_from_dict_roundtrip(self):
        """Test TaskSpec round-trip serialization."""
        original = TaskSpec(
            key="research-api",
            title="Research API Options",
            description="Evaluate REST vs GraphQL",
            task_type="agent",
            priority="high",
            tags=["research"],
            estimated_minutes=60,
            required_specialties=["backend", "api-design"],
            blocked_by_keys=["gather-requirements"],
        )
        data = original.to_dict()
        restored = TaskSpec.from_dict(data)

        assert restored.key == original.key
        assert restored.title == original.title
        assert restored.description == original.description
        assert restored.task_type == original.task_type
        assert restored.priority == original.priority
        assert restored.tags == original.tags
        assert restored.estimated_minutes == original.estimated_minutes
        assert restored.required_specialties == original.required_specialties
        assert restored.blocked_by_keys == original.blocked_by_keys


class TestAgentSpecModel:
    """Tests for AgentSpec dataclass."""

    def test_agent_spec_defaults(self):
        """Test AgentSpec default values."""
        spec = AgentSpec()
        assert spec.name == ""
        assert spec.backend == "claude_agent_sdk"

    def test_agent_spec_to_dict_from_dict_roundtrip(self):
        """Test AgentSpec round-trip serialization."""
        original = AgentSpec(
            name="Researcher",
            role="Research Analyst",
            description="Gathers and synthesizes information",
            specialties=["research", "analysis"],
            backend="claude_agent_sdk",
        )
        data = original.to_dict()
        restored = AgentSpec.from_dict(data)

        assert restored.name == original.name
        assert restored.role == original.role
        assert restored.description == original.description
        assert restored.specialties == original.specialties
        assert restored.backend == original.backend


class TestPlannerResultModel:
    """Tests for PlannerResult dataclass."""

    def test_planner_result_defaults(self):
        """Test PlannerResult default values."""
        result = PlannerResult()
        assert result.project_id == ""
        assert result.tasks == []
        assert result.team_recommendation == []
        assert result.estimated_total_minutes == 0

    def test_planner_result_to_dict_from_dict_roundtrip(self):
        """Test PlannerResult round-trip serialization with nested objects."""
        original = PlannerResult(
            project_id="proj-1",
            prd_content="# Project PRD\n\nGoals...",
            tasks=[
                TaskSpec(key="t1", title="Task One", priority="high"),
                TaskSpec(key="t2", title="Task Two", blocked_by_keys=["t1"]),
            ],
            team_recommendation=[
                AgentSpec(name="Dev", role="Developer", specialties=["python"]),
            ],
            human_tasks=[
                TaskSpec(key="h1", title="Review PR", task_type="human"),
            ],
            dependency_graph={"t2": ["t1"]},
            estimated_total_minutes=120,
            research_notes="Findings from research phase",
        )
        data = original.to_dict()
        restored = PlannerResult.from_dict(data)

        assert restored.project_id == original.project_id
        assert restored.prd_content == original.prd_content
        assert len(restored.tasks) == 2
        assert restored.tasks[0].key == "t1"
        assert restored.tasks[1].blocked_by_keys == ["t1"]
        assert len(restored.team_recommendation) == 1
        assert restored.team_recommendation[0].name == "Dev"
        assert len(restored.human_tasks) == 1
        assert restored.human_tasks[0].task_type == "human"
        assert restored.dependency_graph == {"t2": ["t1"]}
        assert restored.estimated_total_minutes == 120
        assert restored.research_notes == "Findings from research phase"


# ============================================================================
# Store Tests
# ============================================================================


class TestStoreProjectOperations:
    """Tests for FileMissionControlStore project CRUD."""

    @pytest.mark.asyncio
    async def test_save_and_get_project(self, store):
        """Test saving and retrieving a project."""
        project = Project(
            title="Test Project",
            description="A test project",
            status=ProjectStatus.DRAFT,
        )
        await store.save_project(project)

        retrieved = await store.get_project(project.id)
        assert retrieved is not None
        assert retrieved.title == "Test Project"
        assert retrieved.description == "A test project"
        assert retrieved.status == ProjectStatus.DRAFT

    @pytest.mark.asyncio
    async def test_get_nonexistent_project(self, store):
        """Test getting a project that doesn't exist."""
        result = await store.get_project("nonexistent-id")
        assert result is None

    @pytest.mark.asyncio
    async def test_list_projects(self, store):
        """Test listing all projects."""
        p1 = Project(title="Project A", status=ProjectStatus.DRAFT)
        p2 = Project(title="Project B", status=ProjectStatus.EXECUTING)
        await store.save_project(p1)
        await store.save_project(p2)

        projects = await store.list_projects()
        assert len(projects) == 2

    @pytest.mark.asyncio
    async def test_list_projects_with_status_filter(self, store):
        """Test listing projects filtered by status."""
        p1 = Project(title="Draft Project", status=ProjectStatus.DRAFT)
        p2 = Project(title="Active Project", status=ProjectStatus.EXECUTING)
        p3 = Project(title="Another Draft", status=ProjectStatus.DRAFT)
        await store.save_project(p1)
        await store.save_project(p2)
        await store.save_project(p3)

        drafts = await store.list_projects(status="draft")
        assert len(drafts) == 2
        assert all(p.status == ProjectStatus.DRAFT for p in drafts)

        executing = await store.list_projects(status="executing")
        assert len(executing) == 1
        assert executing[0].title == "Active Project"

    @pytest.mark.asyncio
    async def test_delete_project(self, store):
        """Test deleting a project."""
        project = Project(title="To Delete")
        await store.save_project(project)

        deleted = await store.delete_project(project.id)
        assert deleted is True

        retrieved = await store.get_project(project.id)
        assert retrieved is None

    @pytest.mark.asyncio
    async def test_delete_nonexistent_project(self, store):
        """Test deleting a project that doesn't exist."""
        deleted = await store.delete_project("nonexistent-id")
        assert deleted is False

    @pytest.mark.asyncio
    async def test_save_project_updates_timestamp(self, store):
        """Test that saving updates updated_at."""
        project = Project(title="Timestamped")
        await store.save_project(project)

        original_updated = project.updated_at

        # Modify and re-save
        project.title = "Timestamped (edited)"
        await store.save_project(project)

        retrieved = await store.get_project(project.id)
        assert retrieved.title == "Timestamped (edited)"
        assert retrieved.updated_at >= original_updated

    @pytest.mark.asyncio
    async def test_project_persistence(self, temp_store_path):
        """Test that projects persist across store instances."""
        store1 = FileMissionControlStore(temp_store_path)
        project = Project(
            title="Persistent Project",
            status=ProjectStatus.APPROVED,
            tags=["persist-test"],
        )
        await store1.save_project(project)

        # Create new store instance from same path
        store2 = FileMissionControlStore(temp_store_path)
        retrieved = await store2.get_project(project.id)

        assert retrieved is not None
        assert retrieved.title == "Persistent Project"
        assert retrieved.status == ProjectStatus.APPROVED
        assert retrieved.tags == ["persist-test"]

    @pytest.mark.asyncio
    async def test_stats_include_projects(self, store):
        """Test that get_stats includes project counts."""
        await store.save_project(Project(title="P1", status=ProjectStatus.DRAFT))
        await store.save_project(Project(title="P2", status=ProjectStatus.EXECUTING))

        stats = await store.get_stats()
        assert "projects" in stats
        assert stats["projects"]["total"] == 2
        assert stats["projects"]["by_status"]["draft"] == 1
        assert stats["projects"]["by_status"]["executing"] == 1

    @pytest.mark.asyncio
    async def test_clear_all_includes_projects(self, store):
        """Test that clear_all removes projects too."""
        await store.save_project(Project(title="To Clear"))
        projects = await store.list_projects()
        assert len(projects) == 1

        await store.clear_all()
        projects = await store.list_projects()
        assert len(projects) == 0
