"""Tests for the /tree 30s in-process cache."""
from __future__ import annotations

import pytest

from ee.cloud.files.abac_config import AbacRuleSet
from ee.cloud.files.registry import ProviderRegistry
from ee.cloud.files.schemas import (
    FileEntry,
    MountConfig,
    Page,
    Permission,
    RequestContext,
    ResolvedMount,
)
from ee.cloud.files.tree import CachedTreeBuilder, invalidate_tree_cache


class CountingProvider:
    def __init__(self, provider_id: str, mounts: list[ResolvedMount]) -> None:
        self.provider_id = provider_id
        self._mounts = mounts
        self.list_mounts_calls = 0

    async def list_mounts(self, ctx: RequestContext) -> list[ResolvedMount]:
        self.list_mounts_calls += 1
        return list(self._mounts)

    async def list_entries(self, ctx, mount_path, cursor, limit, filters):  # noqa: ANN001
        return Page(items=[])

    async def get_entry(self, ctx: RequestContext, entry_id: str) -> FileEntry:
        raise KeyError(entry_id)

    async def open_stream(self, ctx, entry_id):  # noqa: ANN001
        async def _g():
            yield b""
        return _g()

    async def upload(self, ctx, mount_path, upload):  # noqa: ANN001
        raise NotImplementedError

    async def rename(self, ctx, entry_id, new_name):  # noqa: ANN001
        raise NotImplementedError

    async def move(self, ctx, entry_id, dest_mount_path):  # noqa: ANN001
        raise NotImplementedError

    async def delete(self, ctx, entry_id):  # noqa: ANN001
        raise NotImplementedError

    async def search(self, ctx, query):  # noqa: ANN001
        return Page(items=[])

    def baseline_rbac(self, ctx, entry):  # noqa: ANN001
        return Permission(read=True, write=False, manage=False)


def _registry_with_counting(prov: CountingProvider) -> ProviderRegistry:
    reg = ProviderRegistry(
        configs=[
            MountConfig(
                provider_id=prov.provider_id,
                mount_template="/My Files",
                writable=True,
                order=10,
            )
        ]
    )
    reg.register(prov)
    return reg


@pytest.fixture(autouse=True)
def _clear_cache():
    invalidate_tree_cache()
    yield
    invalidate_tree_cache()


def _ctx() -> RequestContext:
    return RequestContext(user_id="u1", workspace_id="ws_1", attributes={})


def _mount(prov_id: str) -> ResolvedMount:
    return ResolvedMount(
        provider_id=prov_id, path="/My Files", writable=True, order=10, variables={}
    )


@pytest.mark.asyncio
async def test_cache_hits_within_ttl_do_not_refanout():
    now = [1000.0]
    prov = CountingProvider("uploads", mounts=[_mount("uploads")])
    reg = _registry_with_counting(prov)
    builder = CachedTreeBuilder(
        registry=reg, rules=AbacRuleSet(), ttl_seconds=30.0, clock=lambda: now[0]
    )

    await builder.build(ctx=_ctx())
    await builder.build(ctx=_ctx())
    now[0] += 10  # still inside 30s window
    await builder.build(ctx=_ctx())

    assert prov.list_mounts_calls == 1


@pytest.mark.asyncio
async def test_invalidate_forces_refanout():
    now = [1000.0]
    prov = CountingProvider("uploads", mounts=[_mount("uploads")])
    reg = _registry_with_counting(prov)
    builder = CachedTreeBuilder(
        registry=reg, rules=AbacRuleSet(), ttl_seconds=30.0, clock=lambda: now[0]
    )

    await builder.build(ctx=_ctx())
    assert prov.list_mounts_calls == 1

    invalidate_tree_cache(user_id="u1", workspace_id="ws_1")
    await builder.build(ctx=_ctx())
    assert prov.list_mounts_calls == 2


@pytest.mark.asyncio
async def test_ttl_expiry_forces_refanout():
    now = [1000.0]
    prov = CountingProvider("uploads", mounts=[_mount("uploads")])
    reg = _registry_with_counting(prov)
    builder = CachedTreeBuilder(
        registry=reg, rules=AbacRuleSet(), ttl_seconds=30.0, clock=lambda: now[0]
    )

    await builder.build(ctx=_ctx())
    now[0] += 31.0  # past TTL
    await builder.build(ctx=_ctx())

    assert prov.list_mounts_calls == 2


@pytest.mark.asyncio
async def test_invalidate_all_clears_every_key():
    now = [1000.0]
    prov = CountingProvider("uploads", mounts=[_mount("uploads")])
    reg = _registry_with_counting(prov)
    builder = CachedTreeBuilder(
        registry=reg, rules=AbacRuleSet(), ttl_seconds=30.0, clock=lambda: now[0]
    )

    ctx_a = RequestContext(user_id="u1", workspace_id="ws_1", attributes={})
    ctx_b = RequestContext(user_id="u2", workspace_id="ws_2", attributes={})
    await builder.build(ctx=ctx_a)
    await builder.build(ctx=ctx_b)
    assert prov.list_mounts_calls == 2

    invalidate_tree_cache()
    await builder.build(ctx=ctx_a)
    await builder.build(ctx=ctx_b)
    assert prov.list_mounts_calls == 4
