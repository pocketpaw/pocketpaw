# Tests for Deep Work Task model extensions
# Created: 2026-02-12
# Tests the new Task fields: project_id, task_type, blocks,
# active_description, estimated_minutes

from pocketclaw.mission_control.models import Task, TaskPriority, TaskStatus


class TestTaskDeepWorkFields:
    """Tests for the new Deep Work fields on Task."""

    def test_new_field_defaults(self):
        """New fields should have sensible defaults."""
        task = Task()
        assert task.project_id is None
        assert task.task_type == "agent"
        assert task.blocks == []
        assert task.active_description == ""
        assert task.estimated_minutes is None

    def test_new_fields_settable(self):
        """New fields should be settable via constructor."""
        task = Task(
            title="Design API",
            project_id="proj-123",
            task_type="review",
            blocks=["task-a", "task-b"],
            active_description="Designing the API schema",
            estimated_minutes=30,
        )
        assert task.project_id == "proj-123"
        assert task.task_type == "review"
        assert task.blocks == ["task-a", "task-b"]
        assert task.active_description == "Designing the API schema"
        assert task.estimated_minutes == 30

    def test_to_dict_includes_new_fields(self):
        """to_dict should include all new Deep Work fields."""
        task = Task(
            title="Build feature",
            project_id="proj-abc",
            task_type="human",
            blocks=["task-x"],
            active_description="Building the feature",
            estimated_minutes=60,
        )
        data = task.to_dict()
        assert data["project_id"] == "proj-abc"
        assert data["task_type"] == "human"
        assert data["blocks"] == ["task-x"]
        assert data["active_description"] == "Building the feature"
        assert data["estimated_minutes"] == 60

    def test_to_dict_new_fields_defaults(self):
        """to_dict with default new fields should serialize correctly."""
        task = Task(title="Minimal task")
        data = task.to_dict()
        assert data["project_id"] is None
        assert data["task_type"] == "agent"
        assert data["blocks"] == []
        assert data["active_description"] == ""
        assert data["estimated_minutes"] is None

    def test_from_dict_with_new_fields(self):
        """from_dict should correctly deserialize new fields."""
        data = {
            "id": "task-001",
            "title": "Review PR",
            "status": "review",
            "priority": "high",
            "project_id": "proj-999",
            "task_type": "review",
            "blocks": ["task-002", "task-003"],
            "active_description": "Reviewing the pull request",
            "estimated_minutes": 15,
        }
        task = Task.from_dict(data)
        assert task.id == "task-001"
        assert task.title == "Review PR"
        assert task.status == TaskStatus.REVIEW
        assert task.priority == TaskPriority.HIGH
        assert task.project_id == "proj-999"
        assert task.task_type == "review"
        assert task.blocks == ["task-002", "task-003"]
        assert task.active_description == "Reviewing the pull request"
        assert task.estimated_minutes == 15

    def test_from_dict_backward_compat(self):
        """from_dict with old data (missing new fields) should use defaults."""
        old_data = {
            "id": "legacy-task",
            "title": "Old task",
            "status": "inbox",
            "priority": "medium",
            "assignee_ids": ["agent-1"],
            "blocked_by": [],
            "tags": ["legacy"],
        }
        task = Task.from_dict(old_data)
        assert task.id == "legacy-task"
        assert task.title == "Old task"
        assert task.assignee_ids == ["agent-1"]
        assert task.tags == ["legacy"]
        # New fields should get their defaults
        assert task.project_id is None
        assert task.task_type == "agent"
        assert task.blocks == []
        assert task.active_description == ""
        assert task.estimated_minutes is None

    def test_round_trip(self):
        """to_dict -> from_dict should preserve all fields."""
        original = Task(
            title="Round trip test",
            description="Testing serialization round trip",
            status=TaskStatus.IN_PROGRESS,
            priority=TaskPriority.URGENT,
            assignee_ids=["agent-1", "agent-2"],
            creator_id="agent-0",
            parent_task_id="parent-1",
            blocked_by=["dep-1"],
            tags=["test", "deep-work"],
            project_id="proj-rt",
            task_type="human",
            blocks=["blocked-1", "blocked-2"],
            active_description="Running round trip test",
            estimated_minutes=45,
        )
        data = original.to_dict()
        restored = Task.from_dict(data)

        assert restored.id == original.id
        assert restored.title == original.title
        assert restored.description == original.description
        assert restored.status == original.status
        assert restored.priority == original.priority
        assert restored.assignee_ids == original.assignee_ids
        assert restored.creator_id == original.creator_id
        assert restored.parent_task_id == original.parent_task_id
        assert restored.blocked_by == original.blocked_by
        assert restored.tags == original.tags
        assert restored.created_at == original.created_at
        assert restored.updated_at == original.updated_at
        assert restored.metadata == original.metadata
        assert restored.project_id == original.project_id
        assert restored.task_type == original.task_type
        assert restored.blocks == original.blocks
        assert restored.active_description == original.active_description
        assert restored.estimated_minutes == original.estimated_minutes

    def test_blocks_list_is_independent(self):
        """Each Task instance should have its own blocks list (no shared mutable default)."""
        task1 = Task(title="Task 1")
        task2 = Task(title="Task 2")
        task1.blocks.append("task-x")
        assert task2.blocks == []
