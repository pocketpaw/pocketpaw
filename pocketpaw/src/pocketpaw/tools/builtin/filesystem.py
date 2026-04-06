# Filesystem tools - ReadFileTool, WriteFileTool, ListDirTool, EditFileTool.
# Created: 2026-02-02
# Modified: 2026-03-12 - Added EditFileTool for find-and-replace file editing


from pathlib import Path
from typing import Any

from pocketpaw.config import get_settings
from pocketpaw.tools.fetch import is_safe_path
from pocketpaw.tools.protocol import BaseTool


class ReadFileTool(BaseTool):
    """Read file contents."""

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read the contents of a file."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read",
                },
                "encoding": {
                    "type": "string",
                    "description": "File encoding (default: utf-8)",
                    "default": "utf-8",
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, encoding: str = "utf-8") -> str:
        """Read a file."""
        try:
            file_path = Path(path).expanduser().resolve()

            # Security: check file jail
            jail = get_settings().file_jail_path.resolve()
            if not is_safe_path(file_path, jail):
                return self._error(f"Access denied: {path} is outside allowed directory")

            if not file_path.exists():
                return self._error(f"File not found: {path}")

            if not file_path.is_file():
                return self._error(f"Not a file: {path}")

            content = file_path.read_text(encoding=encoding)

            # Truncate very large files
            if len(content) > 100_000:
                content = content[:100_000] + "\n\n...(truncated, file too large)"

            return content

        except UnicodeDecodeError:
            return self._error(f"Cannot read {path}: not a text file or wrong encoding")
        except Exception as e:
            return self._error(str(e))


class WriteFileTool(BaseTool):
    """Write content to a file."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file. Creates the file if it doesn't exist."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write",
                },
                "content": {
                    "type": "string",
                    "description": "Content to write to the file",
                },
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str) -> str:
        """Write to a file."""
        try:
            file_path = Path(path).expanduser().resolve()

            # Security: check file jail
            jail = get_settings().file_jail_path.resolve()
            if not is_safe_path(file_path, jail):
                return self._error(f"Access denied: {path} is outside allowed directory")

            # Create parent directories
            file_path.parent.mkdir(parents=True, exist_ok=True)

            file_path.write_text(content, encoding="utf-8")

            return f"Successfully wrote {len(content)} characters to {path}"

        except Exception as e:
            return self._error(str(e))


class ListDirTool(BaseTool):
    """List directory contents."""

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return "List the contents of a directory."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the directory to list",
                },
                "show_hidden": {
                    "type": "boolean",
                    "description": "Show hidden files (default: false)",
                    "default": False,
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, show_hidden: bool = False) -> str:
        """List directory contents."""
        try:
            dir_path = Path(path).expanduser().resolve()

            # Security: check file jail
            jail = get_settings().file_jail_path.resolve()
            if not is_safe_path(dir_path, jail):
                return self._error(f"Access denied: {path} is outside allowed directory")

            if not dir_path.exists():
                return self._error(f"Directory not found: {path}")

            if not dir_path.is_dir():
                return self._error(f"Not a directory: {path}")

            items = []
            for item in sorted(dir_path.iterdir()):
                if not show_hidden and item.name.startswith("."):
                    continue

                prefix = "[DIR] " if item.is_dir() else "[FILE]"
                size = ""
                if item.is_file():
                    size = f" ({item.stat().st_size} bytes)"
                items.append(f"{prefix} {item.name}{size}")

            if not items:
                return "(empty directory)"

            return "\n".join(items)

        except Exception as e:
            return self._error(str(e))


class EditFileTool(BaseTool):
    """Edit a file by replacing an exact string match with new content."""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edit a file by replacing an exact string match with new content. "
            "The old_string must appear exactly once in the file for the edit to succeed, "
            "unless replace_all is set to true."
        )

    @property
    def trust_level(self) -> str:
        return "standard"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit",
                },
                "old_string": {
                    "type": "string",
                    "description": "The exact text to find and replace",
                },
                "new_string": {
                    "type": "string",
                    "description": "The replacement text",
                },
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences instead of requiring uniqueness",
                    "default": False,
                },
            },
            "required": ["path", "old_string", "new_string"],
        }

    async def execute(
        self,
        path: str,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> str:
        """Edit a file by replacing old_string with new_string."""
        try:
            file_path = Path(path).expanduser().resolve()

            # Security: check file jail
            jail = get_settings().file_jail_path.resolve()
            if not is_safe_path(file_path, jail):
                return self._error(f"Access denied: {path} is outside allowed directory")

            if not file_path.exists():
                return self._error(f"File not found: {path}")

            if not file_path.is_file():
                return self._error(f"Not a file: {path}")

            content = file_path.read_text(encoding="utf-8")

            count = content.count(old_string)

            if count == 0:
                return self._error("old_string not found in file")

            if not replace_all and count > 1:
                return self._error(
                    f"old_string appears {count} times. Provide more context to make it "
                    f"unique, or set replace_all=true"
                )

            new_content = content.replace(old_string, new_string)
            file_path.write_text(new_content, encoding="utf-8")

            replacements = count if replace_all else 1
            return f"Successfully made {replacements} replacement(s) in {path}"

        except UnicodeDecodeError:
            return self._error(f"Cannot read {path}: not a text file or wrong encoding")
        except Exception as e:
            return self._error(str(e))
