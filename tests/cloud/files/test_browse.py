import pytest
from pocketpaw_ee.cloud.files.abac_config import AbacRule, AbacRuleSet
from pocketpaw_ee.cloud.files.browse import browse_mount
from pocketpaw_ee.cloud.files.dto import MountConfig
from pocketpaw_ee.cloud.files.errors import MountNotFound
from pocketpaw_ee.cloud.files.registry import ProviderRegistry

from tests.cloud.files.conftest import FakeProvider


@pytest.mark.asyncio
async def test_browse_mount_returns_entries(ctx, make_entry):
    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="uploads", mount_template="/My Files", writable=True, order=10)
        ]
    )
    entry = make_entry("uploads", "a", "/My Files/a")
    reg.register(FakeProvider("uploads", entries=[entry]))
    page = await browse_mount(
        ctx=ctx,
        registry=reg,
        rules=AbacRuleSet(),
        mount_path="/My Files",
        variables={},
        cursor=None,
        limit=50,
        filters={},
    )
    assert len(page.items) == 1
    assert "read" in page.items[0].capabilities


@pytest.mark.asyncio
async def test_browse_mount_abac_filters_tagged(ctx, make_entry):
    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="uploads", mount_template="/My Files", writable=True, order=10)
        ]
    )
    a = make_entry("uploads", "a", "/My Files/a")
    b = make_entry("uploads", "b", "/My Files/b", tags=["confidential"])
    reg.register(FakeProvider("uploads", entries=[a, b]))
    rules = AbacRuleSet(rules=[AbacRule(tag="confidential", require={"role": ["admin"]})])
    page = await browse_mount(
        ctx=ctx,
        registry=reg,
        rules=rules,
        mount_path="/My Files",
        variables={},
        cursor=None,
        limit=50,
        filters={},
    )
    assert [e.id for e in page.items] == ["uploads:a"]


@pytest.mark.asyncio
async def test_browse_mount_unregistered_provider_raises_mount_not_found(ctx):
    # Mount config references a provider the registry does not know about —
    # mounts.yaml may list providers whose implementations aren't wired yet.
    reg = ProviderRegistry(
        configs=[
            MountConfig(
                provider_id="drive", mount_template="/Connected/Drive", writable=False, order=80
            )
        ]
    )
    with pytest.raises(MountNotFound):
        await browse_mount(
            ctx=ctx,
            registry=reg,
            rules=AbacRuleSet(),
            mount_path="/Connected/Drive",
            variables={},
            cursor=None,
            limit=50,
            filters={},
        )
