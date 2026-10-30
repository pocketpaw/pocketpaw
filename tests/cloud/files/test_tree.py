import pytest
from pocketpaw_ee.cloud.files.abac_config import AbacRuleSet
from pocketpaw_ee.cloud.files.dto import MountConfig
from pocketpaw_ee.cloud.files.registry import ProviderRegistry
from pocketpaw_ee.cloud.files.tree import build_tree

from tests.cloud.files.conftest import FakeProvider


@pytest.mark.asyncio
async def test_build_tree_merges_mounts_sorted_by_order(ctx, make_mount):
    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="uploads", mount_template="/My Files", writable=True, order=10),
            MountConfig(
                provider_id="kb",
                mount_template="/Workspaces/ws_1/KB",
                writable=True,
                order=20,
            ),
        ]
    )
    reg.register(FakeProvider("uploads", mounts=[make_mount("uploads", "/My Files", True, 10)]))
    reg.register(FakeProvider("kb", mounts=[make_mount("kb", "/Workspaces/ws_1/KB", True, 20)]))

    tree = await build_tree(ctx=ctx, registry=reg, rules=AbacRuleSet())
    # top-level children sorted by order: My Files (10) before Workspaces (20)
    assert [c.name for c in tree.children] == ["My Files", "Workspaces"]


@pytest.mark.asyncio
async def test_build_tree_nests_segments(ctx, make_mount):
    reg = ProviderRegistry(
        configs=[
            MountConfig(
                provider_id="kb",
                mount_template="/Workspaces/ws_1/KB",
                writable=False,
                order=10,
            )
        ]
    )
    reg.register(FakeProvider("kb", mounts=[make_mount("kb", "/Workspaces/ws_1/KB")]))

    tree = await build_tree(ctx=ctx, registry=reg, rules=AbacRuleSet())
    assert tree.children[0].name == "Workspaces"
    assert tree.children[0].children[0].name == "ws_1"
    assert tree.children[0].children[0].children[0].name == "KB"


@pytest.mark.asyncio
async def test_build_tree_returns_warnings_on_provider_failure(ctx, make_mount):
    class FailingProvider(FakeProvider):
        async def list_mounts(self, ctx):
            raise RuntimeError("boom")

    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="uploads", mount_template="/My Files", writable=True, order=10),
            MountConfig(provider_id="kb", mount_template="/KB", writable=False, order=20),
        ]
    )
    reg.register(FakeProvider("uploads", mounts=[make_mount("uploads", "/My Files")]))
    reg.register(FailingProvider("kb"))

    tree, warnings = await build_tree(
        ctx=ctx, registry=reg, rules=AbacRuleSet(), collect_warnings=True
    )
    assert [c.name for c in tree.children] == ["My Files"]
    assert warnings == [{"provider_id": "kb", "code": "files.provider_error"}]
