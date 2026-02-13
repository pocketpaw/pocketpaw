# Tests for Mission Control API endpoints
# Created: 2026-02-05
# Tests the FastAPI router for Mission Control

import tempfile
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pocketclaw.mission_control import (
    FileMissionControlStore,
    MissionControlManager,
    reset_mission_control_manager,
    reset_mission_control_store,
)
from pocketclaw.mission_control.api import router

# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def temp_store_path():
    """Create a temporary directory for test storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def test_app(temp_store_path, monkeypatch):
    """Create a test FastAPI app with Mission Control router."""
    # Reset singletons
    reset_mission_control_store()
    reset_mission_control_manager()

    # Create store and manager with temp path
    store = FileMissionControlStore(temp_store_path)
    manager = MissionControlManager(store)

    # Patch the get functions to return our test instances
    import pocketclaw.mission_control.manager as manager_module
    import pocketclaw.mission_control.store as store_module

    monkeypatch.setattr(store_module, "_store_instance", store)
    monkeypatch.setattr(manager_module, "_manager_instance", manager)

    # Create test app
    app = FastAPI()
    app.include_router(router, prefix="/api/mission-control")

    return app


@pytest.fixture
def client(test_app):
    """Create a test client."""
    return TestClient(test_app)


# ============================================================================
# Agent API Tests
# ============================================================================


class TestAgentAPI:
    """Tests for agent endpoints."""

    def test_list_agents_empty(self, client):
        """Test listing agents when none exist."""
        response = client.get("/api/mission-control/agents")
        assert response.status_code == 200
        data = response.json()
        assert data["agents"] == []
        assert data["count"] == 0

    def test_create_agent(self, client):
        """Test creating an agent."""
        response = client.post(
            "/api/mission-control/agents",
            json={
                "name": "Jarvis",
                "role": "Squad Lead",
                "description": "Team coordinator",
                "specialties": ["coordination", "planning"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["agent"]["name"] == "Jarvis"
        assert data["agent"]["role"] == "Squad Lead"
        assert "id" in data["agent"]

    def test_get_agent(self, client):
        """Test getting a specific agent."""
        # Create agent first
        create_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Shuri", "role": "Analyst"},
        )
        agent_id = create_response.json()["agent"]["id"]

        # Get agent
        response = client.get(f"/api/mission-control/agents/{agent_id}")
        assert response.status_code == 200
        assert response.json()["agent"]["name"] == "Shuri"

    def test_get_agent_not_found(self, client):
        """Test getting a non-existent agent."""
        response = client.get("/api/mission-control/agents/nonexistent")
        assert response.status_code == 404

    def test_update_agent(self, client):
        """Test updating an agent."""
        # Create agent
        create_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Friday", "role": "Developer"},
        )
        agent_id = create_response.json()["agent"]["id"]

        # Update agent
        response = client.patch(
            f"/api/mission-control/agents/{agent_id}",
            json={"role": "Senior Developer", "status": "active"},
        )
        assert response.status_code == 200
        assert response.json()["agent"]["role"] == "Senior Developer"
        assert response.json()["agent"]["status"] == "active"

    def test_delete_agent(self, client):
        """Test deleting an agent."""
        # Create agent
        create_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Temp", "role": "Test"},
        )
        agent_id = create_response.json()["agent"]["id"]

        # Delete agent
        response = client.delete(f"/api/mission-control/agents/{agent_id}")
        assert response.status_code == 200

        # Verify deleted
        get_response = client.get(f"/api/mission-control/agents/{agent_id}")
        assert get_response.status_code == 404

    def test_record_heartbeat(self, client):
        """Test recording an agent heartbeat."""
        # Create agent
        create_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Vision", "role": "SEO"},
        )
        agent_id = create_response.json()["agent"]["id"]

        # Record heartbeat
        response = client.post(f"/api/mission-control/agents/{agent_id}/heartbeat")
        assert response.status_code == 200
        assert response.json()["agent"]["last_heartbeat"] is not None


# ============================================================================
# Task API Tests
# ============================================================================


class TestTaskAPI:
    """Tests for task endpoints."""

    def test_list_tasks_empty(self, client):
        """Test listing tasks when none exist."""
        response = client.get("/api/mission-control/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["tasks"] == []

    def test_create_task(self, client):
        """Test creating a task."""
        response = client.post(
            "/api/mission-control/tasks",
            json={
                "title": "Research competitors",
                "description": "Analyze top 5 competitors",
                "priority": "high",
                "tags": ["research", "marketing"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["task"]["title"] == "Research competitors"
        assert data["task"]["priority"] == "high"
        assert data["task"]["status"] == "inbox"

    def test_create_task_with_assignees(self, client):
        """Test creating a task with assignees."""
        # Create agent first
        agent_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Loki", "role": "Writer"},
        )
        agent_id = agent_response.json()["agent"]["id"]

        # Create task with assignment
        response = client.post(
            "/api/mission-control/tasks",
            json={
                "title": "Write blog post",
                "assignee_ids": [agent_id],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["task"]["status"] == "assigned"
        assert agent_id in data["task"]["assignee_ids"]

    def test_get_task_with_messages(self, client):
        """Test getting a task includes its messages."""
        # Create task
        task_response = client.post(
            "/api/mission-control/tasks",
            json={"title": "Test task"},
        )
        task_id = task_response.json()["task"]["id"]

        # Create agent
        agent_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Agent", "role": "Role"},
        )
        agent_id = agent_response.json()["agent"]["id"]

        # Post message
        client.post(
            f"/api/mission-control/tasks/{task_id}/messages",
            json={"from_agent_id": agent_id, "content": "Test message"},
        )

        # Get task
        response = client.get(f"/api/mission-control/tasks/{task_id}")
        assert response.status_code == 200
        assert len(response.json()["messages"]) == 1

    def test_update_task_status(self, client):
        """Test updating task status."""
        # Create task
        task_response = client.post(
            "/api/mission-control/tasks",
            json={"title": "Status test"},
        )
        task_id = task_response.json()["task"]["id"]

        # Update status via JSON body (matches how the frontend sends it)
        response = client.post(
            f"/api/mission-control/tasks/{task_id}/status",
            json={"status": "in_progress"},
        )
        assert response.status_code == 200
        assert response.json()["task"]["status"] == "in_progress"
        assert response.json()["task"]["started_at"] is not None

    def test_assign_task(self, client):
        """Test assigning agents to a task."""
        # Create task
        task_response = client.post(
            "/api/mission-control/tasks",
            json={"title": "Assign test"},
        )
        task_id = task_response.json()["task"]["id"]

        # Create agent
        agent_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Pepper", "role": "Email"},
        )
        agent_id = agent_response.json()["agent"]["id"]

        # Assign
        response = client.post(
            f"/api/mission-control/tasks/{task_id}/assign",
            json={"agent_ids": [agent_id]},
        )
        assert response.status_code == 200
        assert agent_id in response.json()["task"]["assignee_ids"]

    def test_filter_tasks_by_status(self, client):
        """Test filtering tasks by status."""
        # Create tasks
        client.post("/api/mission-control/tasks", json={"title": "Task 1"})
        task2_response = client.post("/api/mission-control/tasks", json={"title": "Task 2"})
        task2_id = task2_response.json()["task"]["id"]

        # Mark one as in_progress
        client.post(
            f"/api/mission-control/tasks/{task2_id}/status",
            params={"status": "in_progress"},
        )

        # Filter by status
        response = client.get("/api/mission-control/tasks", params={"status": "inbox"})
        assert response.status_code == 200
        # Only inbox tasks
        for task in response.json()["tasks"]:
            assert task["status"] == "inbox"


# ============================================================================
# Message API Tests
# ============================================================================


class TestMessageAPI:
    """Tests for message endpoints."""

    def test_post_message(self, client):
        """Test posting a message to a task."""
        # Create task and agent
        task_response = client.post("/api/mission-control/tasks", json={"title": "Message test"})
        task_id = task_response.json()["task"]["id"]

        agent_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Quill", "role": "Social"},
        )
        agent_id = agent_response.json()["agent"]["id"]

        # Post message
        response = client.post(
            f"/api/mission-control/tasks/{task_id}/messages",
            json={
                "from_agent_id": agent_id,
                "content": "Here's my update on the task.",
            },
        )
        assert response.status_code == 200
        assert "update" in response.json()["message"]["content"]

    def test_post_message_with_mentions(self, client):
        """Test that @mentions are extracted."""
        # Create task and agents
        task_response = client.post("/api/mission-control/tasks", json={"title": "Mention test"})
        task_id = task_response.json()["task"]["id"]

        agent1_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Sender", "role": "Role1"},
        )
        agent1_id = agent1_response.json()["agent"]["id"]

        client.post(
            "/api/mission-control/agents",
            json={"name": "Target", "role": "Role2"},
        )

        # Post message with mention
        response = client.post(
            f"/api/mission-control/tasks/{task_id}/messages",
            json={
                "from_agent_id": agent1_id,
                "content": "Hey @Target, please review this!",
            },
        )
        assert response.status_code == 200
        assert "target" in response.json()["message"]["mentions"]

    def test_get_messages_for_task(self, client):
        """Test getting all messages for a task."""
        # Create task and agent
        task_response = client.post("/api/mission-control/tasks", json={"title": "Messages test"})
        task_id = task_response.json()["task"]["id"]

        agent_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Agent", "role": "Role"},
        )
        agent_id = agent_response.json()["agent"]["id"]

        # Post multiple messages
        client.post(
            f"/api/mission-control/tasks/{task_id}/messages",
            json={"from_agent_id": agent_id, "content": "First"},
        )
        client.post(
            f"/api/mission-control/tasks/{task_id}/messages",
            json={"from_agent_id": agent_id, "content": "Second"},
        )

        # Get messages
        response = client.get(f"/api/mission-control/tasks/{task_id}/messages")
        assert response.status_code == 200
        assert response.json()["count"] == 2


# ============================================================================
# Document API Tests
# ============================================================================


class TestDocumentAPI:
    """Tests for document endpoints."""

    def test_create_document(self, client):
        """Test creating a document."""
        response = client.post(
            "/api/mission-control/documents",
            json={
                "title": "Research Report",
                "content": "# Research\n\nFindings here...",
                "type": "research",
                "tags": ["research", "competitors"],
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["document"]["title"] == "Research Report"
        assert data["document"]["type"] == "research"
        assert data["document"]["version"] == 1

    def test_update_document(self, client):
        """Test updating a document increases version."""
        # Create document
        create_response = client.post(
            "/api/mission-control/documents",
            json={"title": "Draft", "content": "Initial"},
        )
        doc_id = create_response.json()["document"]["id"]

        # Update document
        response = client.patch(
            f"/api/mission-control/documents/{doc_id}",
            json={"content": "Updated content"},
        )
        assert response.status_code == 200
        assert response.json()["document"]["version"] == 2
        assert "Updated" in response.json()["document"]["content"]

    def test_list_documents_filtered(self, client):
        """Test filtering documents by type."""
        # Create documents
        client.post(
            "/api/mission-control/documents",
            json={"title": "Doc1", "content": "...", "type": "research"},
        )
        client.post(
            "/api/mission-control/documents",
            json={"title": "Doc2", "content": "...", "type": "deliverable"},
        )

        # Filter
        response = client.get("/api/mission-control/documents", params={"type": "research"})
        assert response.status_code == 200
        for doc in response.json()["documents"]:
            assert doc["type"] == "research"


# ============================================================================
# Activity & Stats API Tests
# ============================================================================


class TestActivityStatsAPI:
    """Tests for activity and stats endpoints."""

    def test_activity_feed(self, client):
        """Test getting the activity feed."""
        # Create some activity
        client.post(
            "/api/mission-control/agents",
            json={"name": "Agent", "role": "Role"},
        )
        client.post(
            "/api/mission-control/tasks",
            json={"title": "Task"},
        )

        # Get activity
        response = client.get("/api/mission-control/activity")
        assert response.status_code == 200
        assert response.json()["count"] > 0

    def test_stats(self, client):
        """Test getting stats."""
        # Create some data
        client.post(
            "/api/mission-control/agents",
            json={"name": "Agent", "role": "Role"},
        )
        client.post(
            "/api/mission-control/tasks",
            json={"title": "Task"},
        )

        # Get stats
        response = client.get("/api/mission-control/stats")
        assert response.status_code == 200
        stats = response.json()["stats"]
        assert stats["agents"]["total"] == 1
        assert stats["tasks"]["total"] == 1

    def test_standup(self, client):
        """Test generating a standup."""
        # Create some data
        agent_response = client.post(
            "/api/mission-control/agents",
            json={"name": "TestAgent", "role": "Role"},
        )
        agent_id = agent_response.json()["agent"]["id"]

        task_response = client.post(
            "/api/mission-control/tasks",
            json={"title": "Standup task", "assignee_ids": [agent_id]},
        )
        task_id = task_response.json()["task"]["id"]

        client.post(
            f"/api/mission-control/tasks/{task_id}/status",
            params={"status": "done"},
        )

        # Get standup
        response = client.get("/api/mission-control/standup")
        assert response.status_code == 200
        assert "Daily Standup" in response.json()["standup"]


# ============================================================================
# Notification API Tests
# ============================================================================


class TestNotificationAPI:
    """Tests for notification endpoints."""

    def test_list_notifications(self, client):
        """Test listing notifications."""
        response = client.get("/api/mission-control/notifications")
        assert response.status_code == 200

    def test_notification_from_mention(self, client):
        """Test that @mentions create notifications."""
        # Create task and agents
        task_response = client.post(
            "/api/mission-control/tasks", json={"title": "Notification test"}
        )
        task_id = task_response.json()["task"]["id"]

        sender_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Sender", "role": "Role1"},
        )
        sender_id = sender_response.json()["agent"]["id"]

        target_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Target", "role": "Role2"},
        )
        target_id = target_response.json()["agent"]["id"]

        # Post message with mention
        client.post(
            f"/api/mission-control/tasks/{task_id}/messages",
            json={
                "from_agent_id": sender_id,
                "content": "@Target please check this",
            },
        )

        # Check notifications for target
        response = client.get(
            "/api/mission-control/notifications",
            params={"agent_id": target_id},
        )
        assert response.status_code == 200
        notifications = response.json()["notifications"]
        assert len(notifications) > 0
        assert any("mentioned" in n["content"].lower() for n in notifications)

    def test_mark_notification_read(self, client):
        """Test marking a notification as read."""
        # Create setup for notification
        task_response = client.post("/api/mission-control/tasks", json={"title": "Read test"})
        task_id = task_response.json()["task"]["id"]

        sender_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Sender2", "role": "Role1"},
        )
        sender_id = sender_response.json()["agent"]["id"]

        target_response = client.post(
            "/api/mission-control/agents",
            json={"name": "Target2", "role": "Role2"},
        )
        target_id = target_response.json()["agent"]["id"]

        client.post(
            f"/api/mission-control/tasks/{task_id}/messages",
            json={
                "from_agent_id": sender_id,
                "content": "@Target2 read this",
            },
        )

        # Get notification
        notif_response = client.get(
            "/api/mission-control/notifications",
            params={"agent_id": target_id},
        )
        notification_id = notif_response.json()["notifications"][0]["id"]

        # Mark as read
        response = client.post(f"/api/mission-control/notifications/{notification_id}/read")
        assert response.status_code == 200
