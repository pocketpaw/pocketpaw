"""Public schemas for the files module.

2026-09-05 (files vault, feat/files-links): response models for the two link
reads. ``FileLinksResponse`` (GET /files/{id}/links) carries the file's outgoing
wikilink targets, resolved to a file where one exists, plus the files that
link back to it. ``FilesGraphResponse`` (GET /files/graph) is the whole library
as nodes + edges, with unresolved names as ``ghosts`` and ``truncated`` set
when the 2000-file cap was hit.

2026-08-29 (T3 "Files content search"): added ``ContentSearchRequest``, the body
of ``POST /files/search``. It carries no ``scope`` field on purpose — unlike
``/kb/search``, which takes a client scope and binds it to the caller through an
allowlist, this surface derives its scopes from the caller, so there is nothing
to validate and nothing to get wrong. See the class docstring.

2026-08-28 (FC-1 "File comprehension"): ``FileEntry`` grew ``summary``
(``str | None``, default ``None``) — one or two sentences saying what the file
IS, written by the comprehension pass on ingest. Only the uploads provider
sets it; every other provider keeps the default, so no provider had to change.
``None`` reads as "nothing understood about this file yet", which is also what
a legacy row, a hidden file, or a file that arrived after the daily cap looks
like — the UI does not need to tell those apart.

2026-08-29 (BA-1 "Make an agent of this book"): ``FileEntry`` grew
``agent_id`` (``str | None``) — the dedicated co-reader agent made from this
file. Only the uploads provider sets it (it is the only provider whose rows
can be turned into an agent); every other provider leaves the ``None``
default. The listing needs it to decide between "Make an agent of this book"
and "Open the agent" without a per-row round trip.

2026-07-03 (FL-1 "Library metadata"): ``FileEntry`` grew ``collections``
(list[str]) and ``hide_from_ai`` (bool) alongside the existing ``tags`` so a
file's library metadata surfaces in the unified /files listing. Both default
empty/False, so providers that don't set them (kb, drive, etc.) keep their
current shape.
"""

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
    # FL-1 library metadata. Only the uploads provider sets these today;
    # other providers leave the defaults (empty list / False).
    collections: list[str] = Field(default_factory=list)
    hide_from_ai: bool = False
    # FC-1: what the file IS, in a sentence or two. ``None`` = not comprehended.
    summary: str | None = None
    # BA-1 book-agent bind. ``None`` means no agent has been made from this
    # file yet. Uploads-provider only; other providers keep the default.
    agent_id: str | None = None
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


class ContentSearchRequest(BaseModel):
    """Body of ``POST /files/search`` (T3 "Files content search").

    POST, not GET, for the same reason ``/kb/search`` is: the query is the
    member's own words about their own documents, and a GET would write it
    into every access log and proxy trace along the way.

    There is deliberately NO ``scope`` field. ``/kb/search`` accepts one and
    binds it to the caller through an allowlist; this surface derives its
    scopes from the caller instead (``content_search.resolve_kb_scopes``), so
    there is nothing to validate and nothing to get wrong. ``pocket_id`` is
    the only partition the client may ask for, and it is gated by the same
    ``pockets_service.is_member`` check the listing uses.
    """

    query: str
    limit: int = Field(20, ge=1, le=50)
    pocket_id: str | None = None


# ---------------------------------------------------------------------------
# Files vault: links + graph (2026-09-05)
# ---------------------------------------------------------------------------


class LinkTarget(BaseModel):
    name: str
    file_id: str | None = None
    filename: str | None = None


class Backlink(BaseModel):
    file_id: str
    filename: str
    mime: str


class FileLinksResponse(BaseModel):
    outgoing: list[LinkTarget] = Field(default_factory=list)
    backlinks: list[Backlink] = Field(default_factory=list)


class GraphNode(BaseModel):
    id: str
    filename: str
    mime: str
    tags: list[str] = Field(default_factory=list)
    collections: list[str] = Field(default_factory=list)


class GraphEdge(BaseModel):
    source: str
    target: str


class FilesGraphResponse(BaseModel):
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    ghosts: list[str] = Field(default_factory=list)
    truncated: bool = False
