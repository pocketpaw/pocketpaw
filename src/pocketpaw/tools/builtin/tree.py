"""Recursive directory tree tool."""

from pathlib import Path
from typing import Any

from pocketpaw.config import get_settings
from pocketpaw.tools.protocol import BaseTool

MAX_ENTRIES = 500


class DirectoryTreeTool(BaseTool):
    """Generate a recursive directory tree listing."""

    @property
    def name(self) -> str:
        return "directory_tree"

    @property
    def description(self) -> str:
        return (
            "Generate a recursive tree view of a directory structure, "
            "similar to the `tree` command. Useful for understanding project layouts."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the directory to scan",
                },
                "max_depth": {
                    "type": "integer",
                    "description": "Maximum recursion depth (default: 3)",
                    "default": 3,
                },
                "show_hidden": {
                    "type": "boolean",
                    "description": "Include hidden files/directories (default: false)",
                    "default": False,
                },
                "show_size": {
                    "type": "boolean",
                    "description": "Show file sizes (default: false)",
                    "default": False,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        max_depth: int = 3,
        show_hidden: bool = False,
        show_size: bool = False,
    ) -> str:
        try:
            dir_path = Path(path).expanduser().resolve()

            # Security: check file jail
            jail = get_settings().file_jail_path.resolve()
            if not dir_path.is_relative_to(jail):
                return self._error(f"Access denied: {path} is outside allowed directory")

            if not dir_path.exists():
                return self._error(f"Directory not found: {path}")

            if not dir_path.is_dir():
                return self._error(f"Not a directory: {path}")

            lines: list[str] = [str(dir_path)]
            counts = {"dirs": 0, "files": 0}

            truncated = self._walk(
                dir_path,
                prefix="",
                depth=0,
                max_depth=max_depth,
                show_hidden=show_hidden,
                show_size=show_size,
                lines=lines,
                counts=counts,
                jail=jail,
            )

            summary = f"\n{counts['dirs']} directories, {counts['files']} files"
            if truncated:
                summary += f" (output truncated at {MAX_ENTRIES} entries)"

            lines.append(summary)
            return "\n".join(lines)

        except PermissionError:
            return self._error(f"Permission denied: {path}")
        except Exception as e:
            return self._error(str(e))

    def _walk(
        self,
        directory: Path,
        prefix: str,
        depth: int,
        max_depth: int,
        show_hidden: bool,
        show_size: bool,
        lines: list[str],
        counts: dict[str, int],
        jail: Path,
    ) -> bool:
        if depth >= max_depth:
            return False

        if len(lines) > MAX_ENTRIES:
            return True

        try:
            entries = sorted(directory.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except PermissionError:
            lines.append(f"{prefix}[permission denied]")
            return False

        # Filter hidden files
        if not show_hidden:
            entries = [e for e in entries if not e.name.startswith(".")]

        for i, entry in enumerate(entries):
            if len(lines) > MAX_ENTRIES:
                return True

            is_last = i == len(entries) - 1
            connector = "└── " if is_last else "├── "
            extension = "    " if is_last else "│   "

            display = entry.name

            if entry.is_symlink():
                lines.append(f"{prefix}{connector}{display} -> [symlink skipped]")
                continue

            if entry.is_dir():
                resolved_entry = entry.resolve(strict=False)
                if not resolved_entry.is_relative_to(jail):
                    lines.append(f"{prefix}{connector}{display}/ -> [outside jail skipped]")
                    continue

                display += "/"
                counts["dirs"] += 1
            else:
                counts["files"] += 1
                if show_size:
                    try:
                        size = entry.stat().st_size
                        display += f" ({self._format_size(size)})"
                    except OSError:
                        pass

            lines.append(f"{prefix}{connector}{display}")

            if entry.is_dir():
                truncated = self._walk(
                    entry,
                    prefix=prefix + extension,
                    depth=depth + 1,
                    max_depth=max_depth,
                    show_hidden=show_hidden,
                    show_size=show_size,
                    lines=lines,
                    counts=counts,
                    jail=jail,
                )
                if truncated:
                    return True

        return False

    @staticmethod
    def _format_size(size: int) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024:
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"
