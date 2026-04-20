"""Schema validation tests."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ee.cloud.files.schemas import (
    FileEntry,
    FolderNode,
    MountConfig,
    Page,
    Permission,
    RequestContext,
)


def _entry(**overrides):
    base = dict(
        id="uploads:abc",
        provider_id="uploads",
        mount_path="/My Files/report.pdf",
        name="report.pdf",
        mime="application/pdf",
        size=1024,
        owner_id="user_1",
        workspace_id="ws_1",
        scope="personal",
        tags=[],
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        source_ref={},
        capabilities=["read", "download"],
    )
    base.update(overrides)
    return FileEntry(**base)


def test_file_entry_id_must_be_namespaced():
    with pytest.raises(ValidationError):
        _entry(id="no-colon")


def test_file_entry_id_prefix_matches_provider():
    with pytest.raises(ValidationError):
        _entry(id="kb:abc", provider_id="uploads")


def test_file_entry_scope_is_enum():
    with pytest.raises(ValidationError):
        _entry(scope="nope")


def test_file_entry_capabilities_subset():
    with pytest.raises(ValidationError):
        _entry(capabilities=["read", "teleport"])


def test_folder_node_children_are_folder_nodes():
    n = FolderNode(
        path="/Workspaces/Acme",
        name="Acme",
        provider_id="kb",
        children=[
            FolderNode(
                path="/Workspaces/Acme/KB",
                name="Knowledge Base",
                provider_id="kb",
                children=[],
                capabilities=["read"],
            )
        ],
        capabilities=["read"],
    )
    assert n.children[0].provider_id == "kb"


def test_mount_config_rejects_non_absolute():
    with pytest.raises(ValidationError):
        MountConfig(
            provider_id="uploads",
            mount_template="My Files",
            writable=True,
            order=10,
        )


def test_permission_merge_is_intersection():
    a = Permission(read=True, write=True, manage=False)
    b = Permission(read=True, write=False, manage=False)
    assert (a & b) == Permission(read=True, write=False, manage=False)


def test_page_carries_cursor_and_items():
    p: Page[int] = Page(items=[1, 2], next_cursor="x")
    assert p.next_cursor == "x" and p.items == [1, 2]


def test_request_context_requires_user_id():
    with pytest.raises(ValidationError):
        RequestContext(workspace_id="ws", session_id="s", attributes={})
