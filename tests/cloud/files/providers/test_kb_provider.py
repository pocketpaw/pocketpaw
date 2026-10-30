from datetime import UTC, datetime

import pytest
from pocketpaw_ee.cloud.files.dto import RequestContext
from pocketpaw_ee.cloud.files.providers.kb import KbProvider

from tests.cloud.files.test_provider_contract import ProviderContract


class _FakeKbService:
    def __init__(self, docs):
        self._docs = docs

    async def list_documents(self, workspace_id: str, *, limit: int = 500):
        return list(self._docs)

    async def get_document(self, doc_id: str, *, workspace_id: str):
        for d in self._docs:
            if d["id"] == doc_id:
                return d
        raise KeyError(doc_id)


def _doc(**overrides):
    base = dict(
        id="doc1",
        title="handbook.pdf",
        mime="application/pdf",
        size=512,
        owner_id="u1",
        workspace_id="ws_1",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        visibility="workspace",
        tags=[],
    )
    base.update(overrides)
    return base


class TestKbProviderContract(ProviderContract):
    def build_provider(self):
        return KbProvider(service=_FakeKbService([_doc()]))


@pytest.mark.asyncio
async def test_kb_list_entries_scoped_to_workspace():
    svc = _FakeKbService([_doc(id="a"), _doc(id="b", title="spec.md", mime="text/markdown")])
    p = KbProvider(service=svc)
    ctx = RequestContext(user_id="u1", workspace_id="ws_1", attributes={})
    page = await p.list_entries(ctx, "/Workspaces/ws_1/Knowledge Base", None, 50, {})
    assert {e.id for e in page.items} == {"kb:a", "kb:b"}


@pytest.mark.asyncio
async def test_kb_baseline_rbac_workspace_member_reads():
    svc = _FakeKbService([])
    p = KbProvider(service=svc)
    ctx = RequestContext(user_id="u2", workspace_id="ws_1", attributes={"role": "member"})
    from pocketpaw_ee.cloud.files.dto import FileEntry

    e = FileEntry(
        id="kb:a",
        provider_id="kb",
        mount_path="/Workspaces/ws_1/Knowledge Base/a",
        name="a",
        mime="text/plain",
        size=1,
        owner_id="u1",
        workspace_id="ws_1",
        scope="workspace",
        tags=[],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        source_ref={},
        capabilities=["read", "download"],
    )
    perm = p.baseline_rbac(ctx, e)
    assert perm.read and not perm.write and not perm.manage


@pytest.mark.asyncio
async def test_kb_baseline_rbac_admin_manages():
    svc = _FakeKbService([])
    p = KbProvider(service=svc)
    ctx = RequestContext(user_id="u1", workspace_id="ws_1", attributes={"role": "admin"})
    from pocketpaw_ee.cloud.files.dto import FileEntry

    e = FileEntry(
        id="kb:a",
        provider_id="kb",
        mount_path="/Workspaces/ws_1/Knowledge Base/a",
        name="a",
        mime="text/plain",
        size=1,
        owner_id="u1",
        workspace_id="ws_1",
        scope="workspace",
        tags=[],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        source_ref={},
        capabilities=["read", "download", "rename", "delete"],
    )
    perm = p.baseline_rbac(ctx, e)
    assert perm.read and perm.write and perm.manage


@pytest.mark.asyncio
async def test_kb_mount_template_resolves_workspace_id():
    svc = _FakeKbService([])
    p = KbProvider(service=svc)
    ctx = RequestContext(user_id="u", workspace_id="ws_77", attributes={})
    mounts = await p.list_mounts(ctx)
    assert mounts[0].path == "/Workspaces/ws_77/Knowledge Base"
