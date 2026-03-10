"""PawKit Catalog — curated registry of installable command center kits.

Provides a catalog of pre-configured dashboard kits that users can browse
and install from the Kit Store in the desktop client.

Pattern mirrors src/pocketpaw/mcp/presets.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class KitCatalogEntry:
    """A kit available in the catalog."""

    id: str  # e.g. "project-orchestrator"
    name: str  # e.g. "Project Orchestrator"
    description: str
    icon: str  # lucide icon name
    category: str  # "general" | "research" | "engineering" | "content"
    author: str
    tags: list[str] = field(default_factory=list)
    preview: str = ""  # short preview blurb for the store card


# ---------------------------------------------------------------------------
# Catalog Registry
# ---------------------------------------------------------------------------

_CATALOG: list[KitCatalogEntry] = [
    KitCatalogEntry(
        id="project-orchestrator",
        name="Project Orchestrator",
        description="Track tasks, monitor agent progress, and manage your projects",
        icon="layout-dashboard",
        category="general",
        author="PocketPaw",
        tags=["tasks", "projects", "orchestration"],
        preview="Metrics row, Kanban board, activity feed, documents table",
    ),
    KitCatalogEntry(
        id="research-hub",
        name="Research Hub",
        description="Organize research documents, track reading progress, and collect insights",
        icon="book-open",
        category="research",
        author="PocketPaw",
        tags=["research", "documents", "reading"],
        preview="Document metrics, research docs table, activity feed, reading kanban",
    ),
    KitCatalogEntry(
        id="sprint-tracker",
        name="Sprint Tracker",
        description="Plan sprints, track velocity, and manage engineering deliverables",
        icon="gauge",
        category="engineering",
        author="PocketPaw",
        tags=["sprints", "engineering", "agile"],
        preview="Sprint metrics, 5-column kanban, activity log, deliverables table",
    ),
    KitCatalogEntry(
        id="content-pipeline",
        name="Content Pipeline",
        description="Manage your content workflow from ideation to publication",
        icon="pen-tool",
        category="content",
        author="PocketPaw",
        tags=["content", "writing", "publishing"],
        preview="Content metrics, 4-stage board, docs table, activity feed",
    ),
]

# Build lookup dicts once
_CATALOG_BY_ID: dict[str, KitCatalogEntry] = {e.id: e for e in _CATALOG}

# Map catalog kit IDs to their bundled YAML filenames
_BUILTIN_YAMLS: dict[str, str] = {
    "project-orchestrator": "project_orchestrator.yaml",
    "research-hub": "research_hub.yaml",
    "sprint-tracker": "sprint_tracker.yaml",
    "content-pipeline": "content_pipeline.yaml",
}


def get_all_catalog_kits() -> list[KitCatalogEntry]:
    """Return all kits in the catalog."""
    return list(_CATALOG)


def get_catalog_kit(kit_id: str) -> KitCatalogEntry | None:
    """Return a catalog entry by ID, or None if not found."""
    return _CATALOG_BY_ID.get(kit_id)


def get_builtin_yaml(kit_id: str) -> str | None:
    """Read the bundled YAML for a catalog kit, or None if not found."""
    filename = _BUILTIN_YAMLS.get(kit_id)
    if not filename:
        return None
    yaml_path = Path(__file__).parent / "builtins" / filename
    if not yaml_path.exists():
        return None
    return yaml_path.read_text(encoding="utf-8")
