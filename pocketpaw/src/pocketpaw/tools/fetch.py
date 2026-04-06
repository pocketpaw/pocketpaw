"""File browser tool."""

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, ValidationError, field_validator

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None


def is_safe_path(path: Path, jail: Path) -> bool:
    """Check if path is strictly within the jail directory."""
    try:
        resolved_path = path.resolve()
        resolved_jail = jail.resolve()
        return resolved_path.is_relative_to(resolved_jail)
    except (ValueError, OSError):
        return False


class FetchRequest(BaseModel):
    path_str: str = Field(..., description="The path to explore. Cannot be empty.")
    jail_str: str = Field(..., description="The strictly enforced jail directory.")
    limit: int = Field(30, ge=1, le=100, description="Number of items to return.")

    @field_validator("path_str", "jail_str", mode="before")
    @classmethod
    def prevent_empty(cls, v: Any) -> str:
        target = str(v) if v is not None else ""
        if not target.strip():
            raise ValueError("Path string cannot be empty or whitespace.")
        return target

    def resolve_paths(self) -> tuple[Path, Path]:
        """Resolve path and jail, checking against path traversal."""
        path_obj = Path(self.path_str).resolve(strict=False)
        jail_obj = Path(self.jail_str).resolve(strict=False)

        if not is_safe_path(path_obj, jail_obj):
            raise ValueError("Access denied: path outside allowed directory or does not exist")

        return path_obj, jail_obj


def _get_directory_keyboard_resolved(
    path_obj: Path, jail_obj: Path, limit: int = 30
) -> "InlineKeyboardMarkup | None":
    """Internal: generate inline keyboard from already-validated Path objects."""
    if InlineKeyboardMarkup is None:
        return None

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

        for item in items[:limit]:
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


def get_directory_keyboard(
    path: Path | str, jail: Path | str, limit: int = 30
) -> "InlineKeyboardMarkup | None":
    """Generate inline keyboard for directory contents (public API, validates inputs)."""
    if InlineKeyboardMarkup is None:
        return None

    try:
        req = FetchRequest(
            path_str=str(path),
            jail_str=str(jail),
            limit=limit,
        )
        path_obj, jail_obj = req.resolve_paths()
    except ValidationError:
        return InlineKeyboardMarkup(
            [[InlineKeyboardButton("⛔ Invalid parameters", callback_data="noop")]]
        )
    except ValueError:
        path_obj = Path(str(jail)).resolve(strict=False)
        jail_obj = path_obj

    return _get_directory_keyboard_resolved(path_obj, jail_obj, limit=req.limit)


async def handle_path(path_str: str | Path, jail: str | Path, limit: int = 30) -> dict:
    """Handle a path selection - return directory listing or file."""
    try:
        req = FetchRequest(
            path_str=str(path_str),
            jail_str=str(jail),
            limit=limit,
        )
        path_obj, jail_obj = req.resolve_paths()
    except ValidationError:
        return {"type": "error", "message": "Validation Error: invalid input parameters."}
    except ValueError as e:
        return {"type": "error", "message": str(e)}

    if path_obj.is_dir():
        result = {"type": "directory"}
        keyboard = _get_directory_keyboard_resolved(path_obj, jail_obj, limit=req.limit)
        if keyboard is not None:
            result["keyboard"] = keyboard
        return result
    elif path_obj.is_file():
        return {"type": "file", "path": path_obj, "filename": path_obj.name}
    else:
        return {"type": "error", "message": "Path does not exist"}


def list_directory(path_str: str | Path, jail_str: str | Path, limit: int = 30) -> str:
    """List directory contents as formatted string for web dashboard."""
    try:
        req = FetchRequest(
            path_str=str(path_str),
            jail_str=str(jail_str),
            limit=limit,
        )
        path_obj, jail_obj = req.resolve_paths()
    except ValidationError:
        return "⛔ Validation Error: invalid input parameters."
    except ValueError as e:
        return f"⛔ {e}"

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
