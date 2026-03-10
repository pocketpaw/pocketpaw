# Tests for Deep Work v2 features.
# Created: 2026-02-26
#
# Covers:
#   1. Task model v2 fields: output, retry_count, max_retries, timeout_minutes, error_message
#   2. TaskSpec model v2 fields: max_retries, timeout_minutes
#   3. ProjectStatus.CANCELLED enum value
#   4. PawKitConfig schema: meta, panels, sections, layouts, workflows, user_config, integrations
#   5. PawKit YAML round-trips: load_pawkit_from_string, save_pawkit, load_pawkit
#   6. MCTaskExecutor: output storage, timeout, auto-retry, retry exhaustion, stop_all_project_tasks
#   7. DeepWorkSession.cancel(): status, task skipping, executor.stop_all_project_tasks,
#      rejection of terminal states
#   8. Deep Work API: POST /projects/{id}/cancel and POST /projects/{id}/tasks/{tid}/retry

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pocketpaw.deep_work.models import Project, ProjectStatus, TaskSpec
from pocketpaw.deep_work.pawkit import (
    IntegrationRequirements,
    LayoutConfig,
    PanelConfig,
    PanelType,
    PawKitCategory,
    PawKitConfig,
    PawKitMeta,
    SectionConfig,
    SpanType,
    TriggerType,
    UserConfigField,
    UserConfigFieldType,
    WorkflowConfig,
    WorkflowTrigger,
    load_pawkit,
    load_pawkit_from_string,
    save_pawkit,
)
from pocketpaw.deep_work.session import DeepWorkSession
from pocketpaw.mission_control.manager import (
    MissionControlManager,
    reset_mission_control_manager,
)
from pocketpaw.mission_control.models import Task, TaskStatus
from pocketpaw.mission_control.store import (
    FileMissionControlStore,
    reset_mission_control_store,
)

# ============================================================================
# Shared fixtures
# ============================================================================


def _make_uuid() -> str:
    """Generate a valid UUID string."""
    return str(uuid.uuid4())


@pytest.fixture
def temp_store_path(tmp_path):
    """Temporary directory for file-based store."""
    return tmp_path


@pytest.fixture
def store(temp_store_path):
    """Fresh file store for each test."""
    reset_mission_control_store()
    return FileMissionControlStore(temp_store_path)


@pytest.fixture
def manager(store):
    """Manager backed by the temp store."""
    reset_mission_control_manager()
    return MissionControlManager(store)


@pytest.fixture
def mock_executor():
    """Mock MCTaskExecutor."""
    executor = MagicMock()
    executor.is_task_running = MagicMock(return_value=False)
    executor.stop_task = AsyncMock(return_value=True)
    executor.stop_all_project_tasks = AsyncMock(return_value=0)
    executor.execute_task_background = AsyncMock()
    executor._on_task_done_callback = None
    return executor


@pytest.fixture
def mock_human_router():
    """Mock HumanTaskRouter."""
    router = MagicMock()
    router.notify_human_task = AsyncMock()
    router.notify_review_task = AsyncMock()
    router.notify_plan_ready = AsyncMock()
    router.notify_project_completed = AsyncMock()
    return router


# ============================================================================
# 1. Task model — v2 fields
# ============================================================================


class TestTaskV2Fields:
    """v2 fields: output, retry_count, max_retries, timeout_minutes, error_message."""

    def test_default_values(self):
        """New v2 fields should have correct defaults."""
        task = Task()
        assert task.output is None
        assert task.retry_count == 0
        assert task.max_retries == 1
        assert task.timeout_minutes is None
        assert task.error_message is None

    def test_settable_via_constructor(self):
        """v2 fields must be settable at construction time."""
        task = Task(
            title="Retry task",
            output="done result",
            retry_count=1,
            max_retries=3,
            timeout_minutes=10,
            error_message="Something went wrong",
        )
        assert task.output == "done result"
        assert task.retry_count == 1
        assert task.max_retries == 3
        assert task.timeout_minutes == 10
        assert task.error_message == "Something went wrong"

    def test_to_dict_includes_v2_fields(self):
        """to_dict must include all five new v2 fields."""
        task = Task(
            title="Export test",
            output="agent output here",
            retry_count=2,
            max_retries=3,
            timeout_minutes=5,
            error_message="timed out",
        )
        data = task.to_dict()
        assert data["output"] == "agent output here"
        assert data["retry_count"] == 2
        assert data["max_retries"] == 3
        assert data["timeout_minutes"] == 5
        assert data["error_message"] == "timed out"

    def test_to_dict_v2_defaults_serialize_correctly(self):
        """to_dict with default v2 values should serialize None/zero correctly."""
        task = Task(title="Minimal")
        data = task.to_dict()
        assert data["output"] is None
        assert data["retry_count"] == 0
        assert data["max_retries"] == 1
        assert data["timeout_minutes"] is None
        assert data["error_message"] is None

    def test_from_dict_with_v2_fields(self):
        """from_dict should deserialize all v2 fields correctly."""
        data = {
            "id": "task-v2-001",
            "title": "v2 task",
            "status": "blocked",
            "priority": "high",
            "output": "all done",
            "retry_count": 1,
            "max_retries": 2,
            "timeout_minutes": 15,
            "error_message": "first attempt failed",
        }
        task = Task.from_dict(data)
        assert task.id == "task-v2-001"
        assert task.output == "all done"
        assert task.retry_count == 1
        assert task.max_retries == 2
        assert task.timeout_minutes == 15
        assert task.error_message == "first attempt failed"

    def test_from_dict_backward_compat(self):
        """from_dict with old data (missing v2 fields) should use defaults."""
        old_data = {
            "id": "legacy-001",
            "title": "Old task",
            "status": "inbox",
            "priority": "medium",
        }
        task = Task.from_dict(old_data)
        assert task.output is None
        assert task.retry_count == 0
        assert task.max_retries == 1
        assert task.timeout_minutes is None
        assert task.error_message is None

    def test_round_trip(self):
        """to_dict -> from_dict preserves all v2 fields."""
        original = Task(
            title="Round trip v2",
            output="final output",
            retry_count=1,
            max_retries=3,
            timeout_minutes=20,
            error_message="attempt 1 failed",
        )
        restored = Task.from_dict(original.to_dict())
        assert restored.output == original.output
        assert restored.retry_count == original.retry_count
        assert restored.max_retries == original.max_retries
        assert restored.timeout_minutes == original.timeout_minutes
        assert restored.error_message == original.error_message


# ============================================================================
# 2. TaskSpec model — v2 fields
# ============================================================================


class TestTaskSpecV2Fields:
    """max_retries and timeout_minutes on TaskSpec."""

    def test_default_values(self):
        """TaskSpec v2 fields should have correct defaults."""
        spec = TaskSpec(key="my-task")
        assert spec.max_retries == 1
        assert spec.timeout_minutes is None

    def test_settable_via_constructor(self):
        """v2 fields must be settable at construction time."""
        spec = TaskSpec(key="retry-task", max_retries=3, timeout_minutes=30)
        assert spec.max_retries == 3
        assert spec.timeout_minutes == 30

    def test_to_dict_includes_v2_fields(self):
        """to_dict must include max_retries and timeout_minutes."""
        spec = TaskSpec(key="t", max_retries=2, timeout_minutes=45)
        data = spec.to_dict()
        assert data["max_retries"] == 2
        assert data["timeout_minutes"] == 45

    def test_from_dict_with_v2_fields(self):
        """from_dict correctly deserializes v2 fields."""
        data = {
            "key": "spec-001",
            "title": "Research API",
            "max_retries": 3,
            "timeout_minutes": 60,
        }
        spec = TaskSpec.from_dict(data)
        assert spec.max_retries == 3
        assert spec.timeout_minutes == 60

    def test_from_dict_backward_compat(self):
        """from_dict with old data (missing v2 fields) should use defaults."""
        old_data = {
            "key": "old-spec",
            "title": "Legacy task",
            "task_type": "agent",
            "priority": "medium",
        }
        spec = TaskSpec.from_dict(old_data)
        assert spec.max_retries == 1
        assert spec.timeout_minutes is None


# ============================================================================
# 3. ProjectStatus.CANCELLED
# ============================================================================


class TestProjectStatusCancelled:
    """ProjectStatus.CANCELLED enum value."""

    def test_cancelled_exists(self):
        """CANCELLED must be a member of ProjectStatus."""
        assert hasattr(ProjectStatus, "CANCELLED")

    def test_cancelled_value(self):
        """CANCELLED must have string value 'cancelled'."""
        assert ProjectStatus.CANCELLED == "cancelled"
        assert ProjectStatus.CANCELLED.value == "cancelled"

    def test_cancelled_from_string(self):
        """ProjectStatus('cancelled') must resolve to CANCELLED."""
        assert ProjectStatus("cancelled") == ProjectStatus.CANCELLED

    def test_cancelled_is_distinct_from_completed(self):
        """CANCELLED and COMPLETED are separate states."""
        assert ProjectStatus.CANCELLED != ProjectStatus.COMPLETED

    def test_cancelled_is_terminal_alongside_completed(self):
        """CANCELLED should be detectable as a terminal state."""
        terminal = {ProjectStatus.COMPLETED, ProjectStatus.CANCELLED}
        assert ProjectStatus.CANCELLED in terminal
        assert ProjectStatus.EXECUTING not in terminal


# ============================================================================
# 4. PawKit Config Schema
# ============================================================================


class TestPawKitMeta:
    """PawKitMeta creation and defaults."""

    def test_minimal_meta(self):
        """Only 'name' is required; defaults are sensible."""
        meta = PawKitMeta(name="My Kit")
        assert meta.name == "My Kit"
        assert meta.author == ""
        assert meta.version == "0.1.0"
        assert meta.category == PawKitCategory.general
        assert meta.built_in is False
        assert meta.icon is None
        assert meta.preview_image is None

    def test_full_meta(self):
        """All fields settable."""
        meta = PawKitMeta(
            name="Creator Studio",
            author="pocketpaw",
            version="1.0.0",
            description="YouTube creator workspace",
            category=PawKitCategory.content,
            tags=["youtube", "creator"],
            icon="video",
            preview_image="https://example.com/preview.png",
            built_in=True,
        )
        assert meta.name == "Creator Studio"
        assert meta.category == PawKitCategory.content
        assert meta.built_in is True
        assert "youtube" in meta.tags


class TestPanelConfig:
    """PanelConfig with different panel types."""

    def test_table_panel(self):
        """Create a table panel."""
        panel = PanelConfig(
            id="videos-table",
            panel_type=PanelType.table,
            source="workflow_youtube_stats",
        )
        assert panel.id == "videos-table"
        assert panel.panel_type == PanelType.table
        assert panel.source == "workflow_youtube_stats"

    def test_kanban_panel_with_columns(self):
        """Create a kanban panel with columns."""
        panel = PanelConfig(
            id="content-board",
            panel_type=PanelType.kanban,
            columns=[{"id": "ideas"}, {"id": "filming"}, {"id": "published"}],
        )
        assert panel.panel_type == PanelType.kanban
        assert len(panel.columns) == 3

    def test_metrics_row_panel(self):
        """Create a metrics row panel."""
        panel = PanelConfig(
            id="kpis",
            panel_type=PanelType.metrics_row,
            items=[
                {"label": "Views", "value": "{{total_views}}"},
                {"label": "Subs", "value": "{{subscribers}}"},
            ],
        )
        assert panel.panel_type == PanelType.metrics_row
        assert len(panel.items) == 2

    def test_chart_panel_with_type_and_period(self):
        """Create a chart panel with chart_type and period."""
        from pocketpaw.deep_work.pawkit import ChartType

        panel = PanelConfig(
            id="views-chart",
            panel_type=PanelType.chart,
            chart_type=ChartType.line,
            period="30d",
        )
        assert panel.chart_type == ChartType.line
        assert panel.period == "30d"

    def test_feed_panel_with_max_items(self):
        """Create a feed panel with max_items."""
        panel = PanelConfig(
            id="activity-feed",
            panel_type=PanelType.feed,
            source="workflow_social_monitor",
            max_items=20,
        )
        assert panel.panel_type == PanelType.feed
        assert panel.max_items == 20


class TestSectionConfig:
    """SectionConfig grouping panels."""

    def test_section_defaults(self):
        """Section defaults to full span."""
        panel = PanelConfig(id="p1", panel_type=PanelType.markdown)
        section = SectionConfig(title="Overview", panels=[panel])
        assert section.title == "Overview"
        assert section.span == SpanType.full

    def test_section_with_half_span(self):
        """Section can specify left or right span."""
        panel = PanelConfig(id="p2", panel_type=PanelType.table)
        section = SectionConfig(title="Left Side", span=SpanType.left, panels=[panel])
        assert section.span == SpanType.left


class TestLayoutConfig:
    """LayoutConfig with columns and sections."""

    def test_default_layout(self):
        """Default layout has 2 columns and no sections."""
        layout = LayoutConfig()
        assert layout.columns == 2
        assert layout.sections == []

    def test_layout_with_sections(self):
        """Layout can contain multiple sections."""
        s1 = SectionConfig(
            title="KPIs",
            panels=[PanelConfig(id="kpis", panel_type=PanelType.metrics_row)],
        )
        s2 = SectionConfig(
            title="Feed",
            panels=[PanelConfig(id="feed", panel_type=PanelType.feed)],
        )
        layout = LayoutConfig(columns=3, sections=[s1, s2])
        assert layout.columns == 3
        assert len(layout.sections) == 2
        assert layout.sections[0].title == "KPIs"


class TestWorkflowConfig:
    """WorkflowConfig — exactly one of schedule or trigger required."""

    def test_with_schedule_valid(self):
        """schedule-only workflow is valid."""
        wf = WorkflowConfig(
            schedule="daily 8am",
            instruction="Fetch YouTube stats",
        )
        assert wf.schedule == "daily 8am"
        assert wf.trigger is None

    def test_with_trigger_valid(self):
        """trigger-only workflow is valid."""
        trigger = WorkflowTrigger(trigger_type=TriggerType.threshold, condition="views < 1000")
        wf = WorkflowConfig(
            trigger=trigger,
            instruction="Alert on low views",
        )
        assert wf.trigger is not None
        assert wf.schedule is None

    def test_both_schedule_and_trigger_raises(self):
        """Having both schedule and trigger must raise a validation error."""
        from pydantic import ValidationError

        trigger = WorkflowTrigger(trigger_type=TriggerType.event)
        with pytest.raises(ValidationError):
            WorkflowConfig(
                schedule="daily 8am",
                trigger=trigger,
                instruction="Ambiguous",
            )

    def test_neither_schedule_nor_trigger_raises(self):
        """Having neither schedule nor trigger must raise a validation error."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            WorkflowConfig(instruction="No source")

    def test_workflow_retry_default(self):
        """Default retry count is 2."""
        wf = WorkflowConfig(schedule="weekly", instruction="Send report")
        assert wf.retry == 2

    def test_workflow_with_channels(self):
        """Workflow can specify output channels."""
        wf = WorkflowConfig(
            schedule="every monday 9am",
            instruction="Send weekly report",
            channels=["telegram", "slack"],
        )
        assert "telegram" in wf.channels
        assert "slack" in wf.channels


class TestUserConfigField:
    """UserConfigField with all supported field types."""

    def test_text_field(self):
        """Text field type is the default."""
        field = UserConfigField(
            key="youtube_channel",
            label="YouTube Channel URL",
            field_type=UserConfigFieldType.text,
            placeholder="https://youtube.com/@channel",
        )
        assert field.field_type == UserConfigFieldType.text
        assert field.placeholder == "https://youtube.com/@channel"

    def test_secret_field(self):
        """Secret field type for sensitive credentials."""
        field = UserConfigField(
            key="api_key",
            label="YouTube API Key",
            field_type=UserConfigFieldType.secret,
        )
        assert field.field_type == UserConfigFieldType.secret
        assert field.options is None

    def test_select_field_with_options(self):
        """Select field carries an options list and an optional default."""
        field = UserConfigField(
            key="post_frequency",
            label="Post Frequency",
            field_type=UserConfigFieldType.select,
            options=["daily", "weekly", "monthly"],
            default="weekly",
        )
        assert field.options == ["daily", "weekly", "monthly"]
        assert field.default == "weekly"

    def test_number_field(self):
        """Number field type with a numeric default."""
        field = UserConfigField(
            key="max_videos",
            label="Max videos to track",
            field_type=UserConfigFieldType.number,
            default=50,
        )
        assert field.field_type == UserConfigFieldType.number
        assert field.default == 50

    def test_boolean_field(self):
        """Boolean field type with a boolean default."""
        field = UserConfigField(
            key="notify_comments",
            label="Notify on new comments",
            field_type=UserConfigFieldType.boolean,
            default=True,
        )
        assert field.field_type == UserConfigFieldType.boolean
        assert field.default is True


class TestIntegrationRequirements:
    """IntegrationRequirements model."""

    def test_empty_defaults(self):
        """Both required and optional default to empty lists."""
        req = IntegrationRequirements()
        assert req.required == []
        assert req.optional == []

    def test_with_integrations(self):
        """Required and optional integrations can be set."""
        req = IntegrationRequirements(
            required=["youtube", "google_analytics"],
            optional=["slack"],
        )
        assert "youtube" in req.required
        assert "slack" in req.optional


class TestPawKitConfig:
    """PawKitConfig full round-trip and minimal creation."""

    def test_minimal_config(self):
        """Config with only meta is valid."""
        config = PawKitConfig(meta=PawKitMeta(name="Minimal Kit"))
        assert config.meta.name == "Minimal Kit"
        assert config.workflows == {}
        assert config.user_config == []
        assert config.skills == []

    def test_full_round_trip(self):
        """create -> model_dump -> model_validate produces an equivalent config."""
        config = PawKitConfig(
            meta=PawKitMeta(
                name="Creator Studio",
                author="pocketpaw",
                version="1.0.0",
                category=PawKitCategory.content,
            ),
            layout=LayoutConfig(
                columns=2,
                sections=[
                    SectionConfig(
                        title="Overview",
                        panels=[
                            PanelConfig(
                                id="kpis",
                                panel_type=PanelType.metrics_row,
                                items=[{"label": "Views", "value": "100"}],
                            )
                        ],
                    )
                ],
            ),
            workflows={
                "daily_stats": WorkflowConfig(
                    schedule="daily 9am",
                    instruction="Fetch YouTube stats and update dashboard",
                )
            },
            user_config=[
                UserConfigField(
                    key="channel_url",
                    label="YouTube Channel",
                    field_type=UserConfigFieldType.text,
                )
            ],
            skills=["youtube-analytics"],
            integrations=IntegrationRequirements(required=["youtube"]),
        )

        data = config.model_dump(mode="json")
        restored = PawKitConfig.model_validate(data)

        assert restored.meta.name == config.meta.name
        assert restored.meta.category == config.meta.category
        assert "daily_stats" in restored.workflows
        assert restored.workflows["daily_stats"].schedule == "daily 9am"
        assert len(restored.layout.sections) == 1
        assert restored.layout.sections[0].title == "Overview"
        assert len(restored.user_config) == 1
        assert restored.user_config[0].key == "channel_url"
        assert "youtube" in restored.integrations.required


# ============================================================================
# 5. PawKit YAML utilities
# ============================================================================


YOUTUBE_CREATOR_YAML = """
meta:
  name: YouTube Creator Studio
  author: pocketpaw
  version: 1.0.0
  description: Manage your YouTube channel with AI automation
  category: content
  tags:
    - youtube
    - creator
  built_in: true

layout:
  columns: 2
  sections:
    - title: Channel KPIs
      span: full
      panels:
        - id: kpis-row
          panel_type: metrics_row
          items:
            - label: Total Views
              value: "{{total_views}}"
            - label: Subscribers
              value: "{{subscribers}}"
    - title: Content Pipeline
      span: full
      panels:
        - id: content-kanban
          panel_type: kanban
          columns:
            - id: ideas
              title: Ideas
            - id: filming
              title: Filming
            - id: published
              title: Published

workflows:
  daily_stats:
    schedule: daily 9am
    instruction: Fetch YouTube channel statistics and update the dashboard metrics
    output_type: structured
    retry: 3

user_config:
  - key: youtube_channel_url
    label: YouTube Channel URL
    field_type: text
    placeholder: https://youtube.com/@yourchannel
  - key: youtube_api_key
    label: YouTube Data API Key
    field_type: secret
    help_url: https://console.cloud.google.com/

integrations:
  required:
    - youtube
  optional:
    - google_analytics
"""

MINIMAL_YAML = """
meta:
  name: Bare Kit
"""


class TestPawKitYaml:
    """YAML loading and saving utilities."""

    def test_load_from_string_meta(self):
        """Meta fields parsed correctly from YAML."""
        config = load_pawkit_from_string(YOUTUBE_CREATOR_YAML)
        assert config.meta.name == "YouTube Creator Studio"
        assert config.meta.category == PawKitCategory.content
        assert config.meta.built_in is True
        assert "youtube" in config.meta.tags

    def test_load_from_string_layout_sections(self):
        """Layout sections parsed from YAML."""
        config = load_pawkit_from_string(YOUTUBE_CREATOR_YAML)
        assert config.layout.columns == 2
        assert len(config.layout.sections) == 2
        titles = [s.title for s in config.layout.sections]
        assert "Channel KPIs" in titles
        assert "Content Pipeline" in titles

    def test_load_from_string_panels(self):
        """Panels within sections parsed correctly."""
        config = load_pawkit_from_string(YOUTUBE_CREATOR_YAML)
        kpis_section = next(s for s in config.layout.sections if s.title == "Channel KPIs")
        assert len(kpis_section.panels) == 1
        assert kpis_section.panels[0].panel_type == PanelType.metrics_row
        assert len(kpis_section.panels[0].items) == 2

    def test_load_from_string_workflow(self):
        """Workflow fields parsed correctly from YAML."""
        config = load_pawkit_from_string(YOUTUBE_CREATOR_YAML)
        assert "daily_stats" in config.workflows
        wf = config.workflows["daily_stats"]
        assert wf.schedule == "daily 9am"
        assert wf.trigger is None
        assert wf.retry == 3

    def test_load_from_string_user_config(self):
        """User config fields parsed from YAML."""
        config = load_pawkit_from_string(YOUTUBE_CREATOR_YAML)
        assert len(config.user_config) == 2
        keys = [f.key for f in config.user_config]
        assert "youtube_channel_url" in keys
        assert "youtube_api_key" in keys

        secret_field = next(f for f in config.user_config if f.key == "youtube_api_key")
        assert secret_field.field_type == UserConfigFieldType.secret

    def test_load_from_string_integrations(self):
        """Integrations parsed from YAML."""
        config = load_pawkit_from_string(YOUTUBE_CREATOR_YAML)
        assert "youtube" in config.integrations.required
        assert "google_analytics" in config.integrations.optional

    def test_load_from_string_minimal(self):
        """Minimal YAML with only meta works — all other fields default."""
        config = load_pawkit_from_string(MINIMAL_YAML)
        assert config.meta.name == "Bare Kit"
        assert config.workflows == {}
        assert config.user_config == []
        assert config.layout.sections == []

    def test_save_and_load_round_trip(self, tmp_path):
        """save_pawkit then load_pawkit produces an equivalent config."""
        original = PawKitConfig(
            meta=PawKitMeta(
                name="Round Trip Kit",
                version="2.0.0",
                category=PawKitCategory.code,
            ),
            workflows={
                "check_prs": WorkflowConfig(
                    schedule="hourly",
                    instruction="Check open pull requests",
                )
            },
            user_config=[
                UserConfigField(
                    key="github_token",
                    label="GitHub Token",
                    field_type=UserConfigFieldType.secret,
                )
            ],
            integrations=IntegrationRequirements(required=["github"]),
        )

        yaml_path = tmp_path / "kit.yaml"
        save_pawkit(original, yaml_path)
        assert yaml_path.exists()

        loaded = load_pawkit(yaml_path)
        assert loaded.meta.name == original.meta.name
        assert loaded.meta.version == original.meta.version
        assert loaded.meta.category == original.meta.category
        assert "check_prs" in loaded.workflows
        assert loaded.workflows["check_prs"].schedule == "hourly"
        assert len(loaded.user_config) == 1
        assert loaded.user_config[0].field_type == UserConfigFieldType.secret
        assert "github" in loaded.integrations.required


# ============================================================================
# 6. MCTaskExecutor enhancements
# ============================================================================


def _build_mock_task(
    task_id: str | None = None,
    retry_count: int = 0,
    max_retries: int = 1,
    timeout_minutes: int | None = None,
    project_id: str | None = None,
) -> MagicMock:
    """Build a lightweight mock Task for executor tests."""
    task = MagicMock()
    task.id = task_id or _make_uuid()
    task.title = "Test task"
    task.description = "Do something"
    task.priority = MagicMock()
    task.priority.value = "medium"
    task.project_id = project_id
    task.blocked_by = []
    task.retry_count = retry_count
    task.max_retries = max_retries
    task.timeout_minutes = timeout_minutes
    task.output = None
    task.error_message = None
    task.status = TaskStatus.INBOX
    return task


def _build_mock_agent(agent_id: str | None = None) -> MagicMock:
    """Build a lightweight mock AgentProfile for executor tests."""
    agent = MagicMock()
    agent.id = agent_id or _make_uuid()
    agent.name = "Test Agent"
    agent.role = "Worker"
    agent.description = ""
    agent.specialties = []
    agent.backend = "claude_agent_sdk"
    return agent


def _make_mock_settings():
    """Minimal settings mock for executor."""
    settings = MagicMock()
    settings.agent_backend = "claude_agent_sdk"
    settings.anthropic_api_key = None
    settings.anthropic_model = "claude-3-5-sonnet-20241022"
    settings.openai_api_key = None
    settings.openai_model = "gpt-4o"
    settings.ollama_host = "http://localhost:11434"
    settings.ollama_model = "llama3"
    settings.llm_provider = "anthropic"
    settings.bypass_permissions = True
    return settings


class TestExecutorOutputStorage:
    """After successful completion, task.output holds the full output."""

    @pytest.mark.asyncio
    async def test_output_stored_on_task_after_completion(self):
        """task.output should contain the concatenated output chunks."""
        from pocketpaw.mission_control.executor import MCTaskExecutor

        tid = _make_uuid()
        aid = _make_uuid()
        task = _build_mock_task(task_id=tid)
        task_fresh = _build_mock_task(task_id=tid, retry_count=0, max_retries=1)

        agent = _build_mock_agent(agent_id=aid)
        executor = MCTaskExecutor()

        async def fake_stream(router, prompt, task_id, output_chunks):
            output_chunks.append("Hello ")
            output_chunks.append("World")

        mock_manager = AsyncMock()
        mock_manager.get_task = AsyncMock(side_effect=[task, task_fresh])
        mock_manager.get_agent = AsyncMock(return_value=agent)
        mock_manager.get_project = AsyncMock(return_value=None)
        mock_manager.get_task_documents = AsyncMock(return_value=[])
        mock_manager.update_task_status = AsyncMock()
        mock_manager.set_agent_status = AsyncMock()
        mock_manager.save_task = AsyncMock()
        mock_manager.save_activity = AsyncMock()
        mock_manager.save_document = AsyncMock()
        mock_manager.get_project_tasks = AsyncMock(return_value=[])

        with (
            patch(
                "pocketpaw.mission_control.executor.get_mission_control_manager",
                return_value=mock_manager,
            ),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=_make_mock_settings(),
            ),
            patch("pocketpaw.mission_control.executor.AgentRouter", return_value=MagicMock()),
            patch.object(executor, "_stream_task", side_effect=fake_stream),
            patch.object(executor, "_broadcast_event", new_callable=AsyncMock),
            patch.object(executor, "_log_activity", new_callable=AsyncMock),
            patch.object(executor, "_save_task_deliverable", new_callable=AsyncMock),
        ):
            result = await executor.execute_task(tid, aid)

        assert result["status"] == "completed"
        assert result["output"] == "Hello World"
        assert task_fresh.output == "Hello World"


class TestExecutorTimeout:
    """Tasks with timeout_minutes that expire become BLOCKED with error_message."""

    @pytest.mark.asyncio
    async def test_timeout_sets_blocked_with_error_message(self):
        """When asyncio.TimeoutError fires, status is 'timeout' and task becomes BLOCKED."""
        from pocketpaw.mission_control.executor import MCTaskExecutor

        tid = _make_uuid()
        aid = _make_uuid()
        task = _build_mock_task(task_id=tid, timeout_minutes=1, retry_count=0, max_retries=0)
        task_fresh = _build_mock_task(task_id=tid, timeout_minutes=1, retry_count=0, max_retries=0)

        agent = _build_mock_agent(agent_id=aid)
        executor = MCTaskExecutor()

        mock_manager = AsyncMock()
        mock_manager.get_task = AsyncMock(side_effect=[task, task_fresh])
        mock_manager.get_agent = AsyncMock(return_value=agent)
        mock_manager.get_project = AsyncMock(return_value=None)
        mock_manager.get_task_documents = AsyncMock(return_value=[])
        mock_manager.update_task_status = AsyncMock()
        mock_manager.set_agent_status = AsyncMock()
        mock_manager.save_task = AsyncMock()
        mock_manager.save_activity = AsyncMock()
        mock_manager.save_document = AsyncMock()
        mock_manager.get_project_tasks = AsyncMock(return_value=[])

        with (
            patch(
                "pocketpaw.mission_control.executor.get_mission_control_manager",
                return_value=mock_manager,
            ),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=_make_mock_settings(),
            ),
            patch("pocketpaw.mission_control.executor.AgentRouter", return_value=MagicMock()),
            patch.object(executor, "_broadcast_event", new_callable=AsyncMock),
            patch.object(executor, "_log_activity", new_callable=AsyncMock),
            patch(
                "pocketpaw.mission_control.executor.asyncio.wait_for",
                side_effect=TimeoutError(),
            ),
        ):
            result = await executor.execute_task(tid, aid)

        assert result["status"] == "timeout"
        assert result["error"] is not None
        assert "1 minutes" in result["error"] or "timeout" in result["error"].lower()
        assert task_fresh.status == TaskStatus.BLOCKED
        assert task_fresh.error_message is not None


class TestExecutorRetry:
    """Auto-retry: on failure with retries remaining, increment count and re-dispatch."""

    @pytest.mark.asyncio
    async def test_retry_increments_count_and_resets_to_assigned(self):
        """On error with retries left, retry_count increases and status becomes ASSIGNED."""
        from pocketpaw.mission_control.executor import MCTaskExecutor

        tid = _make_uuid()
        aid = _make_uuid()
        task = _build_mock_task(task_id=tid, retry_count=0, max_retries=2)
        task_fresh = _build_mock_task(task_id=tid, retry_count=0, max_retries=2)

        agent = _build_mock_agent(agent_id=aid)
        executor = MCTaskExecutor()

        async def failing_stream(router, prompt, task_id, output_chunks):
            raise RuntimeError("LLM error")

        mock_manager = AsyncMock()
        mock_manager.get_task = AsyncMock(side_effect=[task, task_fresh])
        mock_manager.get_agent = AsyncMock(return_value=agent)
        mock_manager.get_project = AsyncMock(return_value=None)
        mock_manager.get_task_documents = AsyncMock(return_value=[])
        mock_manager.update_task_status = AsyncMock()
        mock_manager.set_agent_status = AsyncMock()
        mock_manager.save_task = AsyncMock()
        mock_manager.save_activity = AsyncMock()
        mock_manager.save_document = AsyncMock()
        mock_manager.get_project_tasks = AsyncMock(return_value=[])

        with (
            patch(
                "pocketpaw.mission_control.executor.get_mission_control_manager",
                return_value=mock_manager,
            ),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=_make_mock_settings(),
            ),
            patch("pocketpaw.mission_control.executor.AgentRouter", return_value=MagicMock()),
            patch.object(executor, "_stream_task", side_effect=failing_stream),
            patch.object(executor, "_broadcast_event", new_callable=AsyncMock),
            patch.object(executor, "_log_activity", new_callable=AsyncMock),
            patch("pocketpaw.mission_control.executor.asyncio.create_task", MagicMock()),
        ):
            await executor.execute_task(tid, aid)

        # retry_count incremented on the fresh task object
        assert task_fresh.retry_count == 1
        assert task_fresh.status == TaskStatus.ASSIGNED

    @pytest.mark.asyncio
    async def test_no_retry_when_max_retries_exhausted(self):
        """When retry_count >= max_retries, task becomes BLOCKED — no re-dispatch."""
        from pocketpaw.mission_control.executor import MCTaskExecutor

        tid = _make_uuid()
        aid = _make_uuid()
        # Already at max
        task = _build_mock_task(task_id=tid, retry_count=1, max_retries=1)
        task_fresh = _build_mock_task(task_id=tid, retry_count=1, max_retries=1)

        agent = _build_mock_agent(agent_id=aid)
        executor = MCTaskExecutor()

        async def failing_stream(router, prompt, task_id, output_chunks):
            raise RuntimeError("Permanent failure")

        mock_manager = AsyncMock()
        mock_manager.get_task = AsyncMock(side_effect=[task, task_fresh])
        mock_manager.get_agent = AsyncMock(return_value=agent)
        mock_manager.get_project = AsyncMock(return_value=None)
        mock_manager.get_task_documents = AsyncMock(return_value=[])
        mock_manager.update_task_status = AsyncMock()
        mock_manager.set_agent_status = AsyncMock()
        mock_manager.save_task = AsyncMock()
        mock_manager.save_activity = AsyncMock()
        mock_manager.save_document = AsyncMock()
        mock_manager.get_project_tasks = AsyncMock(return_value=[])

        create_task_calls = []

        with (
            patch(
                "pocketpaw.mission_control.executor.get_mission_control_manager",
                return_value=mock_manager,
            ),
            patch(
                "pocketpaw.mission_control.executor.get_settings",
                return_value=_make_mock_settings(),
            ),
            patch("pocketpaw.mission_control.executor.AgentRouter", return_value=MagicMock()),
            patch.object(executor, "_stream_task", side_effect=failing_stream),
            patch.object(executor, "_broadcast_event", new_callable=AsyncMock),
            patch.object(executor, "_log_activity", new_callable=AsyncMock),
            patch(
                "pocketpaw.mission_control.executor.asyncio.create_task",
                side_effect=lambda coro: create_task_calls.append(coro),
            ),
        ):
            result = await executor.execute_task(tid, aid)

        assert result["status"] == "error"
        # retry_count should NOT have been incremented (already at max)
        assert task_fresh.retry_count == 1
        assert task_fresh.status == TaskStatus.BLOCKED
        # No re-dispatch via create_task
        assert len(create_task_calls) == 0


class TestStopAllProjectTasks:
    """stop_all_project_tasks stops running tasks and returns the count."""

    @pytest.mark.asyncio
    async def test_stops_running_tasks_only(self):
        """Only running tasks are stopped; idle tasks are ignored."""
        from pocketpaw.mission_control.executor import MCTaskExecutor

        executor = MCTaskExecutor()

        t1_id = _make_uuid()
        t2_id = _make_uuid()
        t3_id = _make_uuid()

        t1 = MagicMock()
        t1.id = t1_id
        t2 = MagicMock()
        t2.id = t2_id
        t3 = MagicMock()
        t3.id = t3_id

        running_ids = {t1_id, t2_id}
        executor.is_task_running = MagicMock(side_effect=lambda tid: tid in running_ids)
        executor.stop_task = AsyncMock(return_value=True)

        mock_manager = AsyncMock()
        mock_manager.get_project_tasks = AsyncMock(return_value=[t1, t2, t3])

        with patch(
            "pocketpaw.mission_control.executor.get_mission_control_manager",
            return_value=mock_manager,
        ):
            stopped = await executor.stop_all_project_tasks("proj-1")

        assert stopped == 2
        assert executor.stop_task.call_count == 2
        stopped_ids = {call.args[0] for call in executor.stop_task.call_args_list}
        assert t1_id in stopped_ids
        assert t2_id in stopped_ids
        assert t3_id not in stopped_ids

    @pytest.mark.asyncio
    async def test_no_running_tasks_returns_zero(self):
        """Returns 0 and calls stop_task zero times when nothing is running."""
        from pocketpaw.mission_control.executor import MCTaskExecutor

        executor = MCTaskExecutor()
        t1 = MagicMock()
        t1.id = _make_uuid()
        executor.is_task_running = MagicMock(return_value=False)
        executor.stop_task = AsyncMock()

        mock_manager = AsyncMock()
        mock_manager.get_project_tasks = AsyncMock(return_value=[t1])

        with patch(
            "pocketpaw.mission_control.executor.get_mission_control_manager",
            return_value=mock_manager,
        ):
            stopped = await executor.stop_all_project_tasks("proj-empty")

        assert stopped == 0
        executor.stop_task.assert_not_called()


# ============================================================================
# 7. Session.cancel()
# ============================================================================


class TestSessionCancel:
    """DeepWorkSession.cancel() behavior."""

    async def _make_project(self, manager, status: ProjectStatus) -> Project:
        """Helper: persist a project in the given status."""
        project = await manager.create_project(title="Test Project")
        project.status = status
        await manager.update_project(project)
        return project

    def _make_session(self, manager, mock_executor, mock_human_router) -> DeepWorkSession:
        return DeepWorkSession(
            manager=manager,
            executor=mock_executor,
            human_router=mock_human_router,
        )

    @pytest.mark.asyncio
    async def test_cancel_sets_cancelled_status(self, manager, mock_executor, mock_human_router):
        """Cancelling an EXECUTING project sets status to CANCELLED with completed_at."""
        project = await self._make_project(manager, ProjectStatus.EXECUTING)
        session = self._make_session(manager, mock_executor, mock_human_router)

        result = await session.cancel(project.id)

        assert result.status == ProjectStatus.CANCELLED
        assert result.completed_at is not None

    @pytest.mark.asyncio
    async def test_cancel_skips_inbox_tasks(self, manager, mock_executor, mock_human_router):
        """Cancel marks INBOX tasks as SKIPPED but leaves DONE tasks untouched."""
        project = await self._make_project(manager, ProjectStatus.EXECUTING)
        session = self._make_session(manager, mock_executor, mock_human_router)

        done_task = await manager.create_task(title="Already done")
        done_task.project_id = project.id
        done_task.status = TaskStatus.DONE
        await manager.save_task(done_task)

        inbox_task = await manager.create_task(title="Not yet started")
        inbox_task.project_id = project.id
        inbox_task.status = TaskStatus.INBOX
        await manager.save_task(inbox_task)

        await session.cancel(project.id)

        reloaded_done = await manager.get_task(done_task.id)
        reloaded_inbox = await manager.get_task(inbox_task.id)

        assert reloaded_done.status == TaskStatus.DONE
        assert reloaded_inbox.status == TaskStatus.SKIPPED

    @pytest.mark.asyncio
    async def test_cancel_sets_error_message_on_skipped_tasks(
        self, manager, mock_executor, mock_human_router
    ):
        """Skipped tasks should have error_message = 'Project cancelled'."""
        project = await self._make_project(manager, ProjectStatus.EXECUTING)
        session = self._make_session(manager, mock_executor, mock_human_router)

        inbox_task = await manager.create_task(title="Will be skipped")
        inbox_task.project_id = project.id
        inbox_task.status = TaskStatus.INBOX
        await manager.save_task(inbox_task)

        await session.cancel(project.id)

        reloaded = await manager.get_task(inbox_task.id)
        assert reloaded.error_message == "Project cancelled"

    @pytest.mark.asyncio
    async def test_cancel_calls_stop_all_project_tasks(
        self, manager, mock_executor, mock_human_router
    ):
        """cancel() must delegate to executor.stop_all_project_tasks."""
        project = await self._make_project(manager, ProjectStatus.EXECUTING)
        session = self._make_session(manager, mock_executor, mock_human_router)

        await session.cancel(project.id)

        mock_executor.stop_all_project_tasks.assert_called_once_with(project.id)

    @pytest.mark.asyncio
    async def test_cancel_on_completed_project_raises(
        self, manager, mock_executor, mock_human_router
    ):
        """cancel() on a COMPLETED project raises ValueError."""
        project = await self._make_project(manager, ProjectStatus.COMPLETED)
        session = self._make_session(manager, mock_executor, mock_human_router)

        with pytest.raises(ValueError, match="Cannot cancel"):
            await session.cancel(project.id)

    @pytest.mark.asyncio
    async def test_cancel_on_already_cancelled_raises(
        self, manager, mock_executor, mock_human_router
    ):
        """cancel() on an already CANCELLED project raises ValueError."""
        project = await self._make_project(manager, ProjectStatus.CANCELLED)
        session = self._make_session(manager, mock_executor, mock_human_router)

        with pytest.raises(ValueError, match="Cannot cancel"):
            await session.cancel(project.id)

    @pytest.mark.asyncio
    async def test_cancel_works_on_planning_project(
        self, manager, mock_executor, mock_human_router
    ):
        """cancel() should work on non-terminal states like PLANNING."""
        project = await self._make_project(manager, ProjectStatus.PLANNING)
        session = self._make_session(manager, mock_executor, mock_human_router)

        result = await session.cancel(project.id)
        assert result.status == ProjectStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_works_on_paused_project(self, manager, mock_executor, mock_human_router):
        """cancel() should work on a PAUSED project."""
        project = await self._make_project(manager, ProjectStatus.PAUSED)
        session = self._make_session(manager, mock_executor, mock_human_router)

        result = await session.cancel(project.id)
        assert result.status == ProjectStatus.CANCELLED


# ============================================================================
# 8. Deep Work API — cancel and retry endpoints
# ============================================================================


@pytest.fixture
def dw_app_and_manager(temp_store_path, monkeypatch):
    """FastAPI test app with deep_work router and isolated manager."""
    from fastapi import FastAPI

    from pocketpaw.deep_work.api import router as dw_router
    from pocketpaw.mission_control import (
        FileMissionControlStore,
        MissionControlManager,
        reset_mission_control_manager,
        reset_mission_control_store,
    )

    reset_mission_control_store()
    reset_mission_control_manager()

    store = FileMissionControlStore(temp_store_path)
    mgr = MissionControlManager(store)

    import pocketpaw.mission_control.manager as manager_module
    import pocketpaw.mission_control.store as store_module

    monkeypatch.setattr(store_module, "_store_instance", store)
    monkeypatch.setattr(manager_module, "_manager_instance", mgr)

    app = FastAPI()
    app.include_router(dw_router, prefix="/api/deep-work")

    return app, mgr


@pytest.fixture
def dw_client(dw_app_and_manager):
    """Synchronous test client for deep_work endpoints."""
    from fastapi.testclient import TestClient

    app, _ = dw_app_and_manager
    return TestClient(app)


@pytest.fixture
def dw_manager(dw_app_and_manager):
    """Manager bound to the dw test app."""
    _, mgr = dw_app_and_manager
    return mgr


class TestCancelProjectAPI:
    """POST /api/deep-work/projects/{id}/cancel."""

    def test_cancel_success(self, dw_client, dw_manager):
        """Cancelling a non-terminal project returns 200 with 'cancelled' status."""

        async def _setup():
            project = await dw_manager.create_project(title="Cancel Me API")
            project.status = ProjectStatus.EXECUTING
            await dw_manager.update_project(project)
            return project

        project = asyncio.get_event_loop().run_until_complete(_setup())

        # Patch cancel_project to simulate session cancel without full stack
        async def fake_cancel(pid):
            project.status = ProjectStatus.CANCELLED
            project.completed_at = "2026-02-26T12:00:00+00:00"
            return project

        with patch("pocketpaw.deep_work.cancel_project", side_effect=fake_cancel):
            response = dw_client.post(f"/api/deep-work/projects/{project.id}/cancel")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["project"]["status"] == "cancelled"

    def test_cancel_completed_project_returns_400(self, dw_client, dw_manager):
        """Cancelling a COMPLETED project should return HTTP 400."""

        async def raise_value_error(pid):
            raise ValueError("Cannot cancel project with status 'completed'")

        # The route imports cancel_project from pocketpaw.deep_work inside the
        # function body, so we patch at the pocketpaw.deep_work module level.
        with patch("pocketpaw.deep_work.cancel_project", side_effect=raise_value_error):
            response = dw_client.post("/api/deep-work/projects/any-project-id/cancel")

        assert response.status_code == 400
        assert "Cannot cancel" in response.json()["detail"]


class TestRetryTaskAPI:
    """POST /api/deep-work/projects/{id}/tasks/{tid}/retry."""

    def test_retry_blocked_task_success(self, dw_client, dw_manager):
        """Retrying a BLOCKED task returns 200 with ASSIGNED status and incremented retry_count."""

        async def _setup():
            project = await dw_manager.create_project(title="Retry Project API")
            project.status = ProjectStatus.EXECUTING
            await dw_manager.update_project(project)

            task = await dw_manager.create_task(title="Blocked Task")
            task.project_id = project.id
            task.status = TaskStatus.BLOCKED
            task.retry_count = 0
            task.max_retries = 2
            task.assignee_ids = [_make_uuid()]
            await dw_manager.save_task(task)
            return project, task

        project, task = asyncio.get_event_loop().run_until_complete(_setup())

        mock_session = MagicMock()
        mock_session.scheduler = MagicMock()
        mock_session.scheduler._dispatch_task = AsyncMock()

        # The route imports get_deep_work_session from pocketpaw.deep_work inside the
        # function body, so we patch at the pocketpaw.deep_work module level.
        with patch("pocketpaw.deep_work.get_deep_work_session", return_value=mock_session):
            response = dw_client.post(f"/api/deep-work/projects/{project.id}/tasks/{task.id}/retry")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["task"]["status"] == "assigned"
        assert data["task"]["retry_count"] == 1
        assert data["task"]["error_message"] is None

    def test_retry_non_blocked_task_returns_400(self, dw_client, dw_manager):
        """Retrying a task that is not BLOCKED returns 400."""

        async def _setup():
            project = await dw_manager.create_project(title="Non-blocked Retry")
            project.status = ProjectStatus.EXECUTING
            await dw_manager.update_project(project)

            task = await dw_manager.create_task(title="Running Task")
            task.project_id = project.id
            task.status = TaskStatus.IN_PROGRESS
            await dw_manager.save_task(task)
            return project, task

        project, task = asyncio.get_event_loop().run_until_complete(_setup())

        response = dw_client.post(f"/api/deep-work/projects/{project.id}/tasks/{task.id}/retry")

        assert response.status_code == 400
        assert "blocked" in response.json()["detail"].lower()

    def test_retry_nonexistent_task_returns_404(self, dw_client, dw_manager):
        """Retrying a task ID that doesn't exist returns 404."""

        async def _setup():
            return await dw_manager.create_project(title="404 Retry")

        project = asyncio.get_event_loop().run_until_complete(_setup())

        response = dw_client.post(
            f"/api/deep-work/projects/{project.id}/tasks/does-not-exist/retry"
        )

        assert response.status_code == 404

    def test_retry_task_wrong_project_returns_400(self, dw_client, dw_manager):
        """Retrying a task that belongs to a different project returns 400."""

        async def _setup():
            p1 = await dw_manager.create_project(title="Project One")
            p2 = await dw_manager.create_project(title="Project Two")

            task = await dw_manager.create_task(title="P1 Task")
            task.project_id = p1.id
            task.status = TaskStatus.BLOCKED
            await dw_manager.save_task(task)
            return p1, p2, task

        p1, p2, task = asyncio.get_event_loop().run_until_complete(_setup())

        response = dw_client.post(f"/api/deep-work/projects/{p2.id}/tasks/{task.id}/retry")

        assert response.status_code == 400
        assert "does not belong" in response.json()["detail"]
