"""Public schemas for the files module."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Scope = Literal["personal", "shared", "workspace"]
Capability = Literal["read", "download", "rename", "delete", "move", "replace", "upload"]

T = TypeVar("T")


class Permission(BaseModel):
    read: bool = False
    write: bool = False
    manage: bool = False

    def __and__(self, other: Permission) -> Permission:
        return Permission(
            read=self.read and other.read,
            write=self.write and other.write,
            manage=self.manage and other.manage,
        )


class RequestContext(BaseModel):
    user_id: str
    workspace_id: str | None = None
    session_id: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class FileEntry(BaseModel):
    model_config = ConfigDict(frozen=False)

    id: str
    provider_id: str
    mount_path: str
    name: str
    mime: str
    size: int
    owner_id: str | None = None
    workspace_id: str | None = None
    scope: Scope
    tags: list[str] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    source_ref: dict[str, Any] = Field(default_factory=dict)
    capabilities: list[Capability] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_id_namespace(self) -> FileEntry:
        if ":" not in self.id:
            raise ValueError("FileEntry.id must be namespaced as '<provider_id>:<native_id>'")
        prefix, _, _ = self.id.partition(":")
        if prefix != self.provider_id:
            raise ValueError(
                f"FileEntry.id prefix {prefix!r} must match provider_id {self.provider_id!r}"
            )
        return self

    @field_validator("mount_path")
    @classmethod
    def _mount_path_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("mount_path must start with '/'")
        return v


class FolderNode(BaseModel):
    path: str
    name: str
    provider_id: str
    children: list[FolderNode] = Field(default_factory=list)
    capabilities: list[Capability] = Field(default_factory=list)

    @field_validator("path")
    @classmethod
    def _path_absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("FolderNode.path must start with '/'")
        return v


FolderNode.model_rebuild()


class MountConfig(BaseModel):
    provider_id: str
    mount_template: str
    writable: bool = False
    order: int = 100

    @field_validator("mount_template")
    @classmethod
    def _absolute(cls, v: str) -> str:
        if not v.startswith("/"):
            raise ValueError("mount_template must start with '/'")
        return v


class ResolvedMount(BaseModel):
    provider_id: str
    path: str
    writable: bool
    order: int
    variables: dict[str, str] = Field(default_factory=dict)


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: str | None = None


class SearchQuery(BaseModel):
    query: str
    mount: str | None = None
    mimes: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    limit: int = 50
