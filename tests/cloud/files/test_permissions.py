from datetime import UTC, datetime

from ee.cloud.files.abac_config import AbacRule, AbacRuleSet
from ee.cloud.files.permissions import (
    PermissionsEvaluator,
    apply_abac,
    derive_capabilities,
)
from ee.cloud.files.schemas import FileEntry, Permission, RequestContext


def _entry(tags=None, caps=("read", "download")):
    return FileEntry(
        id="uploads:x",
        provider_id="uploads",
        mount_path="/My Files/x",
        name="x",
        mime="text/plain",
        size=1,
        owner_id="u",
        workspace_id="ws",
        scope="personal",
        tags=list(tags or []),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        source_ref={},
        capabilities=list(caps),
    )


def _ctx(**attrs):
    return RequestContext(user_id="u", workspace_id="ws", attributes=attrs)


def test_apply_abac_passes_untagged():
    rs = AbacRuleSet(rules=[AbacRule(tag="confidential", require={"role": ["admin"]})])
    entries = [_entry(tags=[]), _entry(tags=["confidential"])]
    out = apply_abac(entries, ctx=_ctx(role="member"), rules=rs)
    assert [e.id for e in out] == ["uploads:x"]  # only untagged survives


def test_apply_abac_allows_when_attr_matches():
    rs = AbacRuleSet(rules=[AbacRule(tag="confidential", require={"role": ["admin"]})])
    out = apply_abac([_entry(tags=["confidential"])], ctx=_ctx(role="admin"), rules=rs)
    assert len(out) == 1


def test_derive_capabilities_intersects_rbac_and_mount_writable():
    e = _entry(caps=("read", "download", "rename", "delete"))
    rbac = Permission(read=True, write=False, manage=False)
    caps = derive_capabilities(
        entry=e, rbac=rbac, mount_writable=False, abac_allowed=True
    )
    assert set(caps) == {"read", "download"}


def test_derive_capabilities_strips_all_when_abac_denies():
    e = _entry(caps=("read", "download"))
    rbac = Permission(read=True, write=True, manage=True)
    caps = derive_capabilities(
        entry=e, rbac=rbac, mount_writable=True, abac_allowed=False
    )
    assert caps == []


def test_derive_capabilities_requires_manage_for_delete():
    e = _entry(caps=("read", "delete", "rename"))
    rbac = Permission(read=True, write=True, manage=False)
    caps = derive_capabilities(
        entry=e, rbac=rbac, mount_writable=True, abac_allowed=True
    )
    assert "delete" not in caps
    assert "rename" in caps


def test_evaluator_filters_and_annotates():
    rs = AbacRuleSet(rules=[AbacRule(tag="pii", require={"clearance": ["high"]})])
    ev = PermissionsEvaluator(rules=rs)
    entries = [_entry(tags=[]), _entry(tags=["pii"])]
    out = ev.filter(entries=entries, ctx=_ctx(clearance="low"))
    assert len(out) == 1 and out[0].tags == []
