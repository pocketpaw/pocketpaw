"""FileVersion domain value object
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

EditorKind = Literal["human", "agent"]


@dataclass(frozen=True)
class FileVersion:
    id: str
    file_id: str
    workspace_id: str
    version_number: int
    content: str
    content_hash: str
    size_bytes: int
    editor_kind: EditorKind
    editor_id: str  # user_id or agent_id
    created_at: datetime


__all__ = ["EditorKind", "FileVersion"]
