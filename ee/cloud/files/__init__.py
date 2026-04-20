"""Files aggregation module."""

from ee.cloud.files.bootstrap import build_files_router
from ee.cloud.files.registry import FolderProvider, ProviderRegistry
from ee.cloud.files.router import build_router
from ee.cloud.files.schemas import (
    Capability,
    FileEntry,
    FolderNode,
    MountConfig,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
    Scope,
    SearchQuery,
)

__all__ = [
    "Capability",
    "FileEntry",
    "FolderNode",
    "FolderProvider",
    "MountConfig",
    "Page",
    "Permission",
    "ProviderRegistry",
    "RequestContext",
    "ResolvedMount",
    "Scope",
    "SearchQuery",
    "build_files_router",
    "build_router",
]
