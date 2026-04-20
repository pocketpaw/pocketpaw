import pytest

from ee.cloud.files.errors import MountNotFound
from ee.cloud.files.registry import ProviderRegistry
from ee.cloud.files.schemas import MountConfig


def test_register_and_get(ctx):
    from tests.cloud.files.conftest import FakeProvider

    reg = ProviderRegistry()
    p = FakeProvider("uploads")
    reg.register(p)
    assert reg.get("uploads") is p


def test_register_duplicate_raises():
    from tests.cloud.files.conftest import FakeProvider

    reg = ProviderRegistry()
    reg.register(FakeProvider("uploads"))
    with pytest.raises(ValueError):
        reg.register(FakeProvider("uploads"))


def test_resolve_mount_longest_prefix():
    reg = ProviderRegistry(
        configs=[
            MountConfig(provider_id="a", mount_template="/X", writable=False, order=1),
            MountConfig(provider_id="b", mount_template="/X/Y", writable=False, order=2),
        ]
    )
    got = reg.resolve_mount(path="/X/Y/inside", variables={})
    assert got.provider_id == "b"


def test_resolve_mount_missing_raises():
    reg = ProviderRegistry(configs=[])
    with pytest.raises(MountNotFound):
        reg.resolve_mount(path="/nope", variables={})


def test_resolve_mount_substitutes_variables():
    reg = ProviderRegistry(
        configs=[
            MountConfig(
                provider_id="kb",
                mount_template="/Workspaces/{workspace_id}/KB",
                writable=True,
                order=1,
            )
        ]
    )
    got = reg.resolve_mount(path="/Workspaces/ws_1/KB/doc", variables={"workspace_id": "ws_1"})
    assert got.provider_id == "kb"
    assert got.path == "/Workspaces/ws_1/KB"
