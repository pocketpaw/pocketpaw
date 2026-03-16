"""File browser tool."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None


class FetchRequest(BaseModel):
    path_str: str = Field(..., description="The path to explore. Cannot be empty.")
    jail_str: str | None = Field(None, description="The strictly enforced jail directory. If None, falls back to home.")
    limit: int = Field(20, ge=1, le=100, description="Number of items to return.")

    @field_validator("path_str", mode="before")
    @classmethod
    def prevent_empty(cls, v: Any) -> str:
        target = str(v) if v is not None else ""
        if not target.strip():
            raise ValueError("Path string cannot be empty or whitespace.")
        return target


def is_safe_path(path: Path, jail: Path) -> bool:
    """Check if path is strictly within the jail directory."""
    try:
        resolved_path = path.resolve()
        resolved_jail = jail.resolve()
        return resolved_path.is_relative_to(resolved_jail)
    except (ValueError, FileNotFoundError):
        return False


def get_directory_keyboard(
    path: Path | str, jail: Path | str | None = None, limit: int = 20
) -> "InlineKeyboardMarkup | None":
    """Generate inline keyboard for directory contents."""
    if InlineKeyboardMarkup is None:
        return None

    try:
        req = FetchRequest(path_str=path, jail_str=jail, limit=limit)
    except ValidationError:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("⛔ Invalid path or jail", callback_data="noop")]]
        )

    path_obj = Path(req.path_str).resolve()
    jail_target = req.jail_str if req.jail_str is not None else str(Path.home())
    jail_obj = Path(jail_target).resolve()

    if not is_safe_path(path_obj, jail_obj):
        path_obj = jail_obj

    buttons = []

    # Parent directory button (if not at jail root)
    if path_obj != jail_obj:
        parent = path_obj.parent
        buttons.append([InlineKeyboardButton("📁 ..", callback_data=f"fetch:{parent}")])

    try:
        items = sorted(
            (i for i in path_obj.iterdir() if not i.name.startswith(".")),
            key=lambda x: (not x.is_dir(), x.name.lower()),
        )

        for item in items[: req.limit]:
            if item.is_dir():
                buttons.append(
                    [InlineKeyboardButton(f"📁 {item.name}/", callback_data=f"fetch:{item}")]
                )
            else:
                # Show file size
                try:
                    size = item.stat().st_size
                    if size < 1024:
                        size_str = f"{size} B"
                    elif size < 1024 * 1024:
                        size_str = f"{size / 1024:.1f} KB"
                    else:
                        size_str = f"{size / (1024 * 1024):.1f} MB"
                except Exception:
                    size_str = "?"

                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"📄 {item.name} ({size_str})", callback_data=f"fetch:{item}"
                        )
                    ]
                )
    except PermissionError:
        buttons.append([InlineKeyboardButton("⛔ Permission denied", callback_data="noop")])

    return InlineKeyboardMarkup(buttons)


async def handle_path(path_str: str | Path, jail: str | Path | None = None, limit: int = 20) -> dict:
    """Handle a path selection - return directory listing or file."""
    try:
        req = FetchRequest(path_str=path_str, jail_str=jail, limit=limit)
    except ValidationError as e:
        return {"type": "error", "message": f"Validation Error: {e.errors()[0]['msg']}"}

    path_obj = Path(req.path_str).resolve()
    jail_target = req.jail_str if req.jail_str is not None else str(Path.home())
    jail_obj = Path(jail_target).resolve()

    if not is_safe_path(path_obj, jail_obj):
        return {
            "type": "error",
            "message": "Access denied: path outside allowed directory or does not exist",
        }

    if path_obj.is_dir():
        return {
            "type": "directory",
            "keyboard": get_directory_keyboard(path_obj, jail_obj, limit=req.limit),
        }
    elif path_obj.is_file():
        return {"type": "file", "path": path_obj, "filename": path_obj.name}
    else:
        return {"type": "error", "message": "Path does not exist"}


def list_directory(path_str: str | Path, jail_str: str | Path | None = None, limit: int = 30) -> str:
    """List directory contents as formatted string for web dashboard."""
    try:
        req = FetchRequest(path_str=path_str, jail_str=jail_str, limit=limit)
    except ValidationError as e:
        return f"⛔ Validation Error: {e.errors()[0]['msg']}"

    path_obj = Path(req.path_str).resolve()
    jail_target = req.jail_str if req.jail_str is not None else str(Path.home())
    jail_obj = Path(jail_target).resolve()

    if not is_safe_path(path_obj, jail_obj):
        return "⛔ Access denied: path outside allowed directory or does not exist"

    if not path_obj.is_dir():
        return f"📄 {path_obj.name} - File selected"

    lines = [f"📂 **{path_obj}**\n"]

    try:
        visible = [i for i in path_obj.iterdir() if not i.name.startswith(".")]
        items = sorted(visible, key=lambda x: (not x.is_dir(), x.name.lower()))

        for item in items[: req.limit]:
            if item.is_dir():
                lines.append(f"📁 {item.name}/")
            else:
                try:
                    size = item.stat().st_size
                    if size < 1024:
                        size_str = f"{size} B"
                    elif size < 1024 * 1024:
                        size_str = f"{size / 1024:.1f} KB"
                    else:
                        size_str = f"{size / (1024 * 1024):.1f} MB"
                except Exception:
                    size_str = "?"
                lines.append(f"📄 {item.name} ({size_str})")

        if len(items) > req.limit:
            lines.append(f"\n... and {len(items) - req.limit} more items")

    except PermissionError:
        lines.append("⛔ Permission denied")

    return "\n".join(lines)
