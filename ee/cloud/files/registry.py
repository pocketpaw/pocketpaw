"""Provider registry + mount resolution.

Providers implement the `FolderProvider` protocol (duck-typed; any class with
matching methods works). The registry owns a list of MountConfig and routes
incoming paths to providers via longest-prefix match.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from ee.cloud.files.errors import MountNotFound
from ee.cloud.files.mounts_config import resolve_template
from ee.cloud.files.schemas import (
    FileEntry,
    MountConfig,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
    SearchQuery,
)


@runtime_checkable
class FolderProvider(Protocol):
    provider_id: str

    async def list_mounts(self, ctx: RequestContext) -> list[ResolvedMount]: ...
    async def list_entries(
        self,
        ctx: RequestContext,
        mount_path: str,
        cursor: str | None,
        limit: int,
        filters: dict,
    ) -> Page[FileEntry]: ...
    async def get_entry(self, ctx: RequestContext, entry_id: str) -> FileEntry: ...
    async def open_stream(
        self, ctx: RequestContext, entry_id: str
    ) -> AsyncIterator[bytes]: ...
    async def upload(self, ctx: RequestContext, mount_path: str, upload: object) -> FileEntry: ...
    async def rename(self, ctx: RequestContext, entry_id: str, new_name: str) -> FileEntry: ...
    async def move(self, ctx: RequestContext, entry_id: str, dest_mount_path: str) -> FileEntry: ...
    async def delete(self, ctx: RequestContext, entry_id: str) -> None: ...
    async def search(self, ctx: RequestContext, query: SearchQuery) -> Page[FileEntry]: ...
    def baseline_rbac(self, ctx: RequestContext, entry: FileEntry) -> Permission: ...


class ProviderRegistry:
    def __init__(self, configs: list[MountConfig] | None = None) -> None:
        self._providers: dict[str, FolderProvider] = {}
        self._configs: list[MountConfig] = list(configs or [])

    def register(self, provider: FolderProvider) -> None:
        if provider.provider_id in self._providers:
            raise ValueError(f"provider {provider.provider_id!r} already registered")
        self._providers[provider.provider_id] = provider

    def get(self, provider_id: str) -> FolderProvider:
        return self._providers[provider_id]

    def all(self) -> list[FolderProvider]:
        return list(self._providers.values())

    @property
    def configs(self) -> list[MountConfig]:
        return list(self._configs)

    def resolve_mount(self, *, path: str, variables: dict[str, str]) -> ResolvedMount:
        """Return the mount whose resolved template is the longest prefix of `path`."""
        best: tuple[int, MountConfig, str] | None = None
        for cfg in self._configs:
            try:
                resolved = resolve_template(cfg.mount_template, variables)
            except KeyError:
                continue
            if path == resolved or path.startswith(resolved + "/"):
                length = len(resolved)
                if best is None or length > best[0]:
                    best = (length, cfg, resolved)
        if best is None:
            raise MountNotFound(path)
        _, cfg, resolved = best
        return ResolvedMount(
            provider_id=cfg.provider_id,
            path=resolved,
            writable=cfg.writable,
            order=cfg.order,
            variables=variables,
        )
