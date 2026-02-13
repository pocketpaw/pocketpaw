"""
Default bootstrap provider reading from local files.
Created: 2026-02-02
"""

from pathlib import Path

from pocketclaw.bootstrap.protocol import BootstrapContext, BootstrapProviderProtocol
from pocketclaw.config import get_config_dir


class DefaultBootstrapProvider(BootstrapProviderProtocol):
    """
    Loads identity from:
    - ~/.pocketclaw/identity/IDENTITY.md
    - ~/.pocketclaw/identity/SOUL.md
    - ~/.pocketclaw/identity/STYLE.md
    """

    def __init__(self, base_path: Path | None = None):
        self.base_path = base_path or (get_config_dir() / "identity")
        self.base_path.mkdir(parents=True, exist_ok=True)

        # Initialize default files if they don't exist
        self._ensure_defaults()

    def _ensure_defaults(self) -> None:
        """Create default identity files if missing."""
        identity_file = self.base_path / "IDENTITY.md"
        if not identity_file.exists():
            identity_file.write_text(
                "You are PocketPaw, an AI agent running locally on the user's machine.\n"
                "You are helpful, private, and secure."
            )

        soul_file = self.base_path / "SOUL.md"
        if not soul_file.exists():
            soul_file.write_text(
                "You believe in user sovereignty and local-first computing.\n"
                "You never exfiltrate data without explicit user consent."
            )

        style_file = self.base_path / "STYLE.md"
        if not style_file.exists():
            style_file.write_text(
                "- Be concise and direct.\n"
                "- Use emoji sparingly but effectively.\n"
                "- Prefer code over prose for technical explanations."
            )

        user_file = self.base_path / "USER.md"
        if not user_file.exists():
            user_file.write_text(
                "# User Profile\n"
                "Name: (your name)\n"
                "Timezone: UTC\n"
                "Preferences: (describe your communication preferences)\n"
            )

    async def get_context(self) -> BootstrapContext:
        """Load context from files."""
        identity = (self.base_path / "IDENTITY.md").read_text()
        soul = (self.base_path / "SOUL.md").read_text()
        style = (self.base_path / "STYLE.md").read_text()

        user_profile = ""
        user_file = self.base_path / "USER.md"
        if user_file.exists():
            user_profile = user_file.read_text().strip()

        return BootstrapContext(
            name="PocketPaw",
            identity=identity,
            soul=soul,
            style=style,
            user_profile=user_profile,
        )
