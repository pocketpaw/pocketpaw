# Tests for Mission Control
# Created: 2026-02-05
# Tests data models, store, and manager for multi-agent orchestration

import tempfile
from pathlib import Path

import pytest

from pocketpaw.mission_control import (
    Activity,
    ActivityType,
    AgentLevel,
    AgentProfile,
    AgentStatus,
    DocumentType,
    FileMissionControlStore,
    Message,
    MissionControlManager,
    Notification,
    Task,
    TaskPriority,
    TaskStatus,
    reset_mission_control_manager,
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


@pytest.fixture
def manager(store):
    """Create a manager with the test store."""
    reset_mission_control_manager()
    return MissionControlManager(store)


# ============================================================================
# Model Tests
# ============================================================================


class TestModels:
    """Tests for data models."""

    def test_agent_profile_defaults(self):
        """Test AgentProfile default values."""
        agent = AgentProfile()
        assert agent.id is not None
        assert agent.name == ""
        assert agent.status == AgentStatus.IDLE
        assert agent.level == AgentLevel.SPECIALIST
        assert agent.created_at is not None

    def test_agent_profile_to_dict(self):
        """Test AgentProfile serialization."""
        agent = AgentProfile(
            name="Jarvis",
            role="Squad Lead",
            specialties=["coordination", "planning"],
        )
        data = agent.to_dict()
        assert data["name"] == "Jarvis"
        assert data["role"] == "Squad Lead"
        assert data["specialties"] == ["coordination", "planning"]
        assert data["status"] == "idle"

    def test_agent_profile_from_dict(self):
        """Test AgentProfile deserialization."""
        data = {
            "id": "test-id",
            "name": "Shuri",
            "role": "Analyst",
            "status": "active",
            "level": "lead",
        }
        agent = AgentProfile.from_dict(data)
        assert agent.id == "test-id"
        assert agent.name == "Shuri"
        assert agent.status == AgentStatus.ACTIVE
        assert agent.level == AgentLevel.LEAD

    def test_task_defaults(self):
        """Test Task default values."""
        task = Task()
        assert task.id is not None
        assert task.status == TaskStatus.INBOX
        assert task.priority == TaskPriority.MEDIUM
        assert task.assignee_ids == []

    def test_task_to_dict(self):
        """Test Task serialization."""
        task = Task(
            title="Research competitors",
            description="Analyze top 5 competitors",
            priority=TaskPriority.HIGH,
            tags=["research", "marketing"],
        )
        data = task.to_dict()
        assert data["title"] == "Research competitors"
        assert data["priority"] == "high"
        assert data["tags"] == ["research", "marketing"]

    def test_message_with_mentions(self):
        """Test Message with mentions."""
        message = Message(
            task_id="task-1",
            from_agent_id="agent-1",
            content="Hey @Jarvis, please review this. @all FYI.",
            mentions=["jarvis", "all"],
        )
        assert message.mentions == ["jarvis", "all"]

    def test_activity_types(self):
        """Test Activity with different types."""
        activity = Activity(
            type=ActivityType.TASK_CREATED,
            message="Created task: Research",
        )
        assert activity.type == ActivityType.TASK_CREATED
        assert activity.to_dict()["type"] == "task_created"


# ============================================================================
# Store Tests
# ============================================================================


class TestFileMissionControlStore:
    """Tests for file-based storage."""

    @pytest.mark.asyncio
    async def test_save_and_get_agent(self, store):
        """Test saving and retrieving an agent."""
        agent = AgentProfile(
            name="Jarvis",
            role="Squad Lead",
        )
        await store.save_agent(agent)

        retrieved = await store.get_agent(agent.id)
        assert retrieved is not None
        assert retrieved.name == "Jarvis"
        assert retrieved.role == "Squad Lead"

    @pytest.mark.asyncio
    async def test_get_agent_by_name(self, store):
        """Test finding agent by name."""
        agent = AgentProfile(name="Shuri", role="Analyst")
        await store.save_agent(agent)

        found = await store.get_agent_by_name("shuri")  # Case insensitive
        assert found is not None
        assert found.id == agent.id

    @pytest.mark.asyncio
    async def test_list_agents_filtered(self, store):
        """Test listing agents with status filter."""
        agent1 = AgentProfile(name="Agent1", status=AgentStatus.IDLE)
        agent2 = AgentProfile(name="Agent2", status=AgentStatus.ACTIVE)
        await store.save_agent(agent1)
        await store.save_agent(agent2)

        idle = await store.list_agents(status="idle")
        assert len(idle) == 1
        assert idle[0].name == "Agent1"

    @pytest.mark.asyncio
    async def test_save_and_get_task(self, store):
        """Test saving and retrieving a task."""
        task = Task(
            title="Test Task",
            description="Testing",
            priority=TaskPriority.HIGH,
        )
        await store.save_task(task)

        retrieved = await store.get_task(task.id)
        assert retrieved is not None
        assert retrieved.title == "Test Task"
        assert retrieved.priority == TaskPriority.HIGH

    @pytest.mark.asyncio
    async def test_list_tasks_by_status(self, store):
        """Test filtering tasks by status."""
        task1 = Task(title="Task1", status=TaskStatus.INBOX)
        task2 = Task(title="Task2", status=TaskStatus.IN_PROGRESS)
        await store.save_task(task1)
        await store.save_task(task2)

        in_progress = await store.list_tasks(status=TaskStatus.IN_PROGRESS)
        assert len(in_progress) == 1
        assert in_progress[0].title == "Task2"

    @pytest.mark.asyncio
    async def test_messages_for_task(self, store):
        """Test getting messages for a task."""
        task = Task(title="Task")
        await store.save_task(task)

        msg1 = Message(task_id=task.id, content="First")
        msg2 = Message(task_id=task.id, content="Second")
        await store.save_message(msg1)
        await store.save_message(msg2)

        messages = await store.get_messages_for_task(task.id)
        assert len(messages) == 2

    @pytest.mark.asyncio
    async def test_activity_feed(self, store):
        """Test activity feed ordering."""
        a1 = Activity(message="First")
        a2 = Activity(message="Second")
        await store.save_activity(a1)
        await store.save_activity(a2)

        feed = await store.get_activity_feed()
        assert len(feed) == 2
        # Most recent first
        assert feed[0].message == "Second"

    @pytest.mark.asyncio
    async def test_undelivered_notifications(self, store):
        """Test getting undelivered notifications."""
        n1 = Notification(agent_id="agent-1", content="Test1", delivered=False)
        n2 = Notification(agent_id="agent-1", content="Test2", delivered=True)
        await store.save_notification(n1)
        await store.save_notification(n2)

        undelivered = await store.get_undelivered_notifications()
        assert len(undelivered) == 1
        assert undelivered[0].content == "Test1"

    @pytest.mark.asyncio
    async def test_stats(self, store):
        """Test statistics generation."""
        await store.save_agent(AgentProfile(name="Agent"))
        await store.save_task(Task(title="Task", status=TaskStatus.INBOX))
        await store.save_task(Task(title="Task2", status=TaskStatus.DONE))

        stats = await store.get_stats()
        assert stats["agents"]["total"] == 1
        assert stats["tasks"]["total"] == 2
        assert stats["tasks"]["by_status"]["inbox"] == 1
        assert stats["tasks"]["by_status"]["done"] == 1

    @pytest.mark.asyncio
    async def test_persistence(self, temp_store_path):
        """Test that data persists across store instances."""
        # Create store and save data
        store1 = FileMissionControlStore(temp_store_path)
        agent = AgentProfile(name="Persistent")
        await store1.save_agent(agent)

        # Create new store instance
        store2 = FileMissionControlStore(temp_store_path)
        retrieved = await store2.get_agent(agent.id)

        assert retrieved is not None
        assert retrieved.name == "Persistent"


# ============================================================================
# Manager Tests
# ============================================================================


class TestMissionControlManager:
    """Tests for high-level manager operations."""

    @pytest.mark.asyncio
    async def test_create_agent(self, manager):
        """Test agent creation with activity logging."""
        agent = await manager.create_agent(
            name="Jarvis",
            role="Squad Lead",
            specialties=["coordination"],
        )

        assert agent.name == "Jarvis"
        assert agent.session_key == "agent:jarvis:main"

        # Check activity was logged
        activities = await manager.get_activity_feed()
        assert len(activities) == 1
        assert "joined the team" in activities[0].message

    @pytest.mark.asyncio
    async def test_create_task_with_assignment(self, manager):
        """Test task creation with automatic assignment."""
        agent = await manager.create_agent(name="Shuri", role="Analyst")

        task = await manager.create_task(
            title="Research competitors",
            description="Full analysis",
            assignee_ids=[agent.id],
        )

        assert task.status == TaskStatus.ASSIGNED
        assert agent.id in task.assignee_ids

        # Check notification was created
        notifications = await manager.get_notifications_for_agent(agent.id)
        assert len(notifications) == 1
        assert "assigned" in notifications[0].content.lower()

    @pytest.mark.asyncio
    async def test_update_task_status(self, manager):
        """Test task status updates with timestamps."""
        task = await manager.create_task(title="Test Task")

        # Start work
        await manager.update_task_status(task.id, TaskStatus.IN_PROGRESS)
        task = await manager.get_task(task.id)
        assert task.status == TaskStatus.IN_PROGRESS
        assert task.started_at is not None

        # Complete
        await manager.update_task_status(task.id, TaskStatus.DONE)
        task = await manager.get_task(task.id)
        assert task.status == TaskStatus.DONE
        assert task.completed_at is not None

    @pytest.mark.asyncio
    async def test_post_message_with_mentions(self, manager):
        """Test posting message with @mention notifications."""
        jarvis = await manager.create_agent(name="Jarvis", role="Lead")
        shuri = await manager.create_agent(name="Shuri", role="Analyst")

        task = await manager.create_task(title="Test")

        # Jarvis mentions Shuri
        message = await manager.post_message(
            task_id=task.id,
            from_agent_id=jarvis.id,
            content="Hey @Shuri, please review this.",
        )

        assert "shuri" in message.mentions

        # Shuri should have notification
        notifications = await manager.get_notifications_for_agent(shuri.id)
        mention_notifs = [n for n in notifications if "mentioned" in n.content.lower()]
        assert len(mention_notifs) == 1

    @pytest.mark.asyncio
    async def test_post_message_mention_all(self, manager):
        """Test @all mention notifies everyone."""
        agent1 = await manager.create_agent(name="Agent1", role="Role1")
        agent2 = await manager.create_agent(name="Agent2", role="Role2")

        task = await manager.create_task(title="Test")

        # Agent1 mentions @all
        await manager.post_message(
            task_id=task.id,
            from_agent_id=agent1.id,
            content="@all please check this out",
        )

        # Agent2 should have notification (Agent1 shouldn't notify themselves)
        notifs = await manager.get_notifications_for_agent(agent2.id)
        mention_notifs = [n for n in notifs if "mentioned" in n.content.lower()]
        assert len(mention_notifs) == 1

    @pytest.mark.asyncio
    async def test_create_and_update_document(self, manager):
        """Test document creation and versioning."""
        agent = await manager.create_agent(name="Loki", role="Writer")

        doc = await manager.create_document(
            title="Research Report",
            content="# Initial Draft",
            doc_type=DocumentType.DRAFT,
            author_id=agent.id,
        )

        assert doc.version == 1

        # Update
        updated = await manager.update_document(
            document_id=doc.id,
            content="# Revised Draft",
            editor_id=agent.id,
        )

        assert updated.version == 2
        assert "Revised" in updated.content

    @pytest.mark.asyncio
    async def test_record_heartbeat(self, manager):
        """Test agent heartbeat recording."""
        agent = await manager.create_agent(name="Friday", role="Dev")

        assert agent.last_heartbeat is None

        success = await manager.record_heartbeat(agent.id)
        assert success

        updated = await manager.get_agent(agent.id)
        assert updated.last_heartbeat is not None

    @pytest.mark.asyncio
    async def test_generate_standup(self, manager):
        """Test standup report generation."""
        agent = await manager.create_agent(name="Jarvis", role="Lead")

        # Create tasks in different states
        await manager.create_task(title="Done Task")
        done_task = (await manager.list_tasks())[0]
        await manager.update_task_status(done_task.id, TaskStatus.DONE)

        await manager.create_task(
            title="In Progress",
            assignee_ids=[agent.id],
        )
        ip_task = (await manager.list_tasks(status=TaskStatus.ASSIGNED))[0]
        await manager.update_task_status(ip_task.id, TaskStatus.IN_PROGRESS)

        standup = await manager.generate_standup()

        assert "Daily Standup" in standup
        assert "Done Task" in standup or "Completed" in standup
        assert "In Progress" in standup
        assert "Jarvis" in standup

    @pytest.mark.asyncio
    async def test_get_stats(self, manager):
        """Test stats generation through manager."""
        await manager.create_agent(name="Agent", role="Role")
        await manager.create_task(title="Task")

        stats = await manager.get_stats()
        assert stats["agents"]["total"] == 1
        assert stats["tasks"]["total"] == 1
