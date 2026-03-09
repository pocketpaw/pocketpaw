# PawKit configuration schema v0.1 — Data models for Command Center definitions.
# Created: 2026-02-26
#
# Defines the PawKit YAML config format as Pydantic v2 models. A PawKit is a
# publishable Command Center template that describes layout (panels, sections),
# automated workflows (scheduled or trigger-based), user-configurable fields,
# bundled skills, and integration requirements.
#
# See idocs/agentic-os/blueprints-strategy.md for the full design spec.

import logging
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, model_validator

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ============================================================================
# Enums
# ============================================================================


class PawKitCategory(StrEnum):
    """Domain category for a PawKit."""

    general = "general"
    code = "code"
    business = "business"
    creative = "creative"
    education = "education"
    content = "content"
    marketing = "marketing"
    devops = "devops"
    finance = "finance"
    health = "health"
    realestate = "realestate"


class PanelType(StrEnum):
    """Visual panel widget type."""

    metrics_row = "metrics_row"
    table = "table"
    kanban = "kanban"
    chart = "chart"
    feed = "feed"
    calendar = "calendar"
    pipeline = "pipeline"
    markdown = "markdown"
    status_list = "status_list"
    progress_tracker = "progress_tracker"


class ChartType(StrEnum):
    """Chart visualization style."""

    line = "line"
    bar = "bar"
    pie = "pie"
    area = "area"


class SpanType(StrEnum):
    """Section width within the grid layout."""

    full = "full"
    left = "left"
    right = "right"


class WorkflowOutputType(StrEnum):
    """What a workflow produces."""

    structured = "structured"
    task_list = "task_list"
    feed = "feed"
    document = "document"


class TriggerType(StrEnum):
    """How a workflow trigger fires."""

    threshold = "threshold"
    event = "event"
    cascade = "cascade"


class UserConfigFieldType(StrEnum):
    """Input type for user-configurable fields shown during PawKit install."""

    text = "text"
    secret = "secret"
    select = "select"
    number = "number"
    boolean = "boolean"


# ============================================================================
# Panel Models
# ============================================================================


class PanelAction(BaseModel):
    """An interactive action button attached to a panel.

    Actions let the user trigger agent tasks, dismiss items, navigate,
    or call external APIs directly from a panel card.
    """

    label: str
    action_type: str  # agent_action, dismiss, navigate, api_call
    instruction: str | None = None


class PanelConfig(BaseModel):
    """Configuration for a single dashboard panel.

    A panel is a visual widget — a table, chart, kanban board, feed, etc.
    The ``source`` field binds the panel to a workflow output or data store.
    """

    id: str
    panel_type: PanelType
    source: str | None = None
    config: dict[str, Any] = {}
    actions: list[PanelAction] = []
    items: list[dict[str, Any]] = []  # metrics-row items
    columns: list[dict[str, Any]] = []  # kanban columns
    chart_type: ChartType | None = None
    max_items: int | None = None
    filter: dict[str, Any] | None = None
    card_fields: list[str] = []
    period: str | None = None
    label: str | None = None


# ============================================================================
# Layout Models
# ============================================================================


class SectionConfig(BaseModel):
    """A titled section of the dashboard containing one or more panels."""

    title: str
    span: SpanType = SpanType.full
    panels: list[PanelConfig]


class LayoutConfig(BaseModel):
    """Top-level layout definition — grid columns and ordered sections."""

    columns: int = 2
    sections: list[SectionConfig] = []


# ============================================================================
# Workflow Models
# ============================================================================


class WorkflowTrigger(BaseModel):
    """A reactive trigger that starts a workflow when a condition is met."""

    trigger_type: TriggerType
    source: str | None = None
    condition: str | None = None


class WorkflowConfig(BaseModel):
    """A single automated workflow run by the agent.

    Workflows are either scheduled (cron / human-readable like "daily 8am")
    or trigger-based (threshold, event, cascade). Exactly one of ``schedule``
    or ``trigger`` must be provided.
    """

    schedule: str | None = None
    trigger: WorkflowTrigger | None = None
    instruction: str
    output_type: WorkflowOutputType = WorkflowOutputType.structured
    retry: int = 2
    target_column: str | None = None
    channels: list[str] = []
    message: str | None = None
    format: str | None = None

    @model_validator(mode="after")
    def _require_schedule_or_trigger(self) -> "WorkflowConfig":
        has_schedule = self.schedule is not None
        has_trigger = self.trigger is not None
        if has_schedule == has_trigger:
            raise ValueError(
                "WorkflowConfig must have exactly one of 'schedule' or 'trigger', "
                "not both and not neither."
            )
        return self


# ============================================================================
# User Config & Integration Models
# ============================================================================


class UserConfigField(BaseModel):
    """A field shown to the user during PawKit installation.

    These capture user-specific values (API keys, preferences, domain info)
    that workflows reference via ``{{user_config.<key>}}`` placeholders.
    """

    key: str
    label: str
    field_type: UserConfigFieldType = UserConfigFieldType.text
    placeholder: str | None = None
    options: list[str] | None = None  # for select type
    help_url: str | None = None
    default: Any = None


class IntegrationRequirements(BaseModel):
    """External service dependencies for a PawKit."""

    required: list[str] = []
    optional: list[str] = []


# ============================================================================
# Meta & Root Model
# ============================================================================


class PawKitMeta(BaseModel):
    """Metadata describing a PawKit for display and marketplace indexing."""

    name: str
    author: str = ""
    version: str = "0.1.0"
    description: str = ""
    category: PawKitCategory = PawKitCategory.general
    tags: list[str] = []
    icon: str | None = None
    preview_image: str | None = None
    built_in: bool = False


class PawKitConfig(BaseModel):
    """Root configuration model for a PawKit.

    A PawKit defines everything about a Command Center: what the user sees
    (layout), what the agent does automatically (workflows), what the user
    configures on install (user_config), what domain knowledge is bundled
    (skills), and what integrations are needed.
    """

    meta: PawKitMeta
    layout: LayoutConfig = LayoutConfig()
    workflows: dict[str, WorkflowConfig] = {}
    user_config: list[UserConfigField] = []
    skills: list[str] = []
    integrations: IntegrationRequirements = IntegrationRequirements()


# ============================================================================
# YAML Utilities
# ============================================================================


def _require_yaml() -> None:
    """Raise if PyYAML is not installed."""
    if yaml is None:
        raise RuntimeError(
            "PyYAML is required for PawKit config loading/saving. "
            "Install it with: pip install pyyaml"
        )


def load_pawkit(path: Path) -> PawKitConfig:
    """Load a PawKit config from a YAML file.

    Accepts both ``.yaml`` and ``.yml`` extensions.

    Args:
        path: Filesystem path to the YAML file.

    Returns:
        Parsed and validated PawKitConfig.

    Raises:
        RuntimeError: If PyYAML is not installed.
        FileNotFoundError: If the path does not exist.
        ValueError: If the YAML content fails validation.
    """
    _require_yaml()
    raw = path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    if data is None:
        data = {}
    return PawKitConfig.model_validate(data)


def save_pawkit(config: PawKitConfig, path: Path) -> None:
    """Save a PawKit config to a YAML file.

    Args:
        config: The PawKitConfig to serialize.
        path: Destination file path.

    Raises:
        RuntimeError: If PyYAML is not installed.
    """
    _require_yaml()
    data = config.model_dump(mode="json", exclude_none=True)
    path.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


def load_pawkit_from_string(yaml_string: str) -> PawKitConfig:
    """Load a PawKit config from a YAML string.

    Args:
        yaml_string: Raw YAML content.

    Returns:
        Parsed and validated PawKitConfig.

    Raises:
        RuntimeError: If PyYAML is not installed.
        ValueError: If the YAML content fails validation.
    """
    _require_yaml()
    data = yaml.safe_load(yaml_string)
    if data is None:
        data = {}
    return PawKitConfig.model_validate(data)
