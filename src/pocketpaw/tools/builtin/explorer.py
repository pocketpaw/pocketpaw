# Explorer tool - open files/folders in the client's file explorer.
#
# Calls the dashboard REST API (POST /api/v1/files/open) so it works both
# in-process (tool bridge) and from the CLI subprocess (Claude SDK via Bash).

from pathlib import Path
from typing import Any

from pocketpaw.tools.protocol import BaseTool


class OpenExplorerTool(BaseTool):
    """Open a file or folder in the user's desktop file explorer."""

    @property
    def name(self) -> str:
        return "open_in_explorer"

    @property
    def description(self) -> str:
        return (
            "Open a file or folder in the user's desktop file explorer. "
            "Use this after finding or creating files the user asked about."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file or folder to open",
                },
                "action": {
                    "type": "string",
                    "enum": ["navigate", "view"],
                    "description": (
                        "Action to perform: 'navigate' opens a folder in the explorer, "
                        "'view' opens a file in the viewer. Auto-detected if omitted."
                    ),
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, action: str | None = None) -> str:
        """Open a path in the client's file explorer via the dashboard API."""
        try:
            resolved = Path(path).expanduser().resolve()

            if not resolved.exists():
                return self._error(f"Path not found: {path}")

            # Auto-detect action from path type
            if action is None:
                action = "view" if resolved.is_file() else "navigate"

            # Call the dashboard REST endpoint — works both in-process and
            # from the CLI subprocess (Claude SDK launches tools via Bash).
            import httpx

            from pocketpaw.config import get_settings

            settings = get_settings()
            port = settings.web_port
            url = f"http://127.0.0.1:{port}/api/v1/files/open"
            payload = {"path": str(resolved), "action": action}

            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.post(url, json=payload)
                data = resp.json()

            if not data.get("ok"):
                return self._error(data.get("error", "Unknown error from dashboard"))

            kind = "file" if action == "view" else "folder"
            return f"Opened {kind} in explorer: {resolved}"

        except Exception as e:
            return self._error(f"Failed to open path: {e}")
