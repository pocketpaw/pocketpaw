"""PawKits — configurable command center dashboards.

Provides models, file-based storage, and built-in kit definitions.
"""

from pocketpaw.kits.catalog import (
    KitCatalogEntry,
    get_all_catalog_kits,
    get_builtin_yaml,
    get_catalog_kit,
)
from pocketpaw.kits.models import (
    InstalledKit,
    LayoutConfig,
    MetricItem,
    PanelConfig,
    PawKitConfig,
    PawKitMeta,
    SectionConfig,
    UserConfigField,
    WorkflowConfig,
)
from pocketpaw.kits.store import FileKitStore, get_kit_store

__all__ = [
    "FileKitStore",
    "InstalledKit",
    "KitCatalogEntry",
    "LayoutConfig",
    "MetricItem",
    "PanelConfig",
    "PawKitConfig",
    "PawKitMeta",
    "SectionConfig",
    "UserConfigField",
    "WorkflowConfig",
    "get_all_catalog_kits",
    "get_builtin_yaml",
    "get_catalog_kit",
    "get_kit_store",
]
