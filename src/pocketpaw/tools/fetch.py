"""File browser tool."""

from pathlib import Path

from pydantic import BaseModel, field_validator

try:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
except ImportError:
    InlineKeyboardButton = None
    InlineKeyboardMarkup = None


class FetchRequest(BaseModel):
    """Validated fetch request with security constraints."""

    path: str

    @field_validator("path", mode="before")
    @classmethod
    def validate_path(cls, v: str) -> str:
        """Validate that path is not empty or whitespace-only."""
        if not isinstance(v, str) or not v.strip():
            raise ValueError("Path string cannot be empty or whitespace.")
        return v


def is_safe_path(path: Path, jail: Path) -> bool:
    """Check if path is within the jail directory."""
    try:
        path = path.resolve(strict=False)
        jail = jail.resolve(strict=False)
        return str(path).startswith(str(jail))
    except Exception:
        return False


def get_directory_keyboard(path: Path, jail: Path | None = None) -> InlineKeyboardMarkup:
    """Generate inline keyboard for directory contents."""
    if jail is None:
        jail = Path.home()

    path = Path(path).resolve(strict=False)

    if not is_safe_path(path, jail):
        path = jail

    buttons = []

    # Parent directory button (if not at jail root)
    if path != jail:
        parent = path.parent
        buttons.append([InlineKeyboardButton("📁 ..", callback_data=f"fetch:{parent}")])

    try:
        items = sorted(
            (i for i in path.iterdir() if not i.name.startswith(".")),
            key=lambda x: (not x.is_dir(), x.name.lower()),
        )

        for item in items[:20]:  # Limit to 20 visible items
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


async def handle_path(path_str: str, jail: Path) -> dict:
    """Handle a path selection - return directory listing or file."""
    try:
        # Validate path string
        request = FetchRequest(path=path_str)
    except ValueError as e:
        return {"type": "error", "message": f"Validation Error: {e}"}

    path = Path(request.path).resolve(strict=False)

    if not is_safe_path(path, jail):
        return {"type": "error", "message": "Access denied: path outside allowed directory"}

    if path.is_dir():
        return {"type": "directory", "keyboard": get_directory_keyboard(path, jail)}
    elif path.is_file():
        return {"type": "file", "path": path, "filename": path.name}
    else:
        return {"type": "error", "message": "Path does not exist"}


def list_directory(path_str: str, jail_str: str | None = None) -> str:
    """List directory contents as formatted string for web dashboard."""
    try:
        # Validate path string
        request = FetchRequest(path=path_str)
    except ValueError as e:
        return f"⛔ Validation Error: {e}"

    path = Path(request.path).resolve(strict=False)
    jail = Path(jail_str).resolve(strict=False) if jail_str else Path.home()

    if not is_safe_path(path, jail):
        return "⛔ Access denied: path outside allowed directory"

    if not path.is_dir():
        return f"📄 {path.name} - File selected"

    lines = [f"📂 **{path}**\n"]

    try:
        visible = [i for i in path.iterdir() if not i.name.startswith(".")]
        items = sorted(visible, key=lambda x: (not x.is_dir(), x.name.lower()))

        for item in items[:30]:  # Limit to 30 visible items
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

        if len(items) > 30:
            lines.append(f"\n... and {len(items) - 30} more items")

    except PermissionError:
        lines.append("⛔ Permission denied")

    return "\n".join(lines)
