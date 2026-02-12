"""Configuration management for PocketPaw.

Changes:
  - 2026-02-02: Added claude_agent_sdk to agent_backend options.
  - 2026-02-02: Simplified backends - removed 2-layer mode.
  - 2026-02-02: claude_agent_sdk is now RECOMMENDED (uses official SDK).
"""

import json
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def get_config_dir() -> Path:
    """Get the config directory, creating if needed."""
    config_dir = Path.home() / ".pocketclaw"
    config_dir.mkdir(exist_ok=True)
    return config_dir


def get_config_path() -> Path:
    """Get the config file path."""
    return get_config_dir() / "config.json"


def get_token_path() -> Path:
    """Get the access token file path."""
    return get_config_dir() / "access_token"


class Settings(BaseSettings):
    """PocketPaw settings with env and file support."""

    model_config = SettingsConfigDict(env_prefix="POCKETCLAW_", env_file=".env", extra="ignore")

    # Telegram
    telegram_bot_token: str | None = Field(
        default=None, description="Telegram Bot Token from @BotFather"
    )
    allowed_user_id: int | None = Field(
        default=None, description="Telegram User ID allowed to control the bot"
    )

    # Agent Backend
    agent_backend: str = Field(
        default="pocketpaw_native",

        description="Agent backend: 'claude_agent_sdk' (recommended), 'pocketpaw_native', or 'open_interpreter'",
    )

    # LLM Configuration
    llm_provider: str = Field(
        default="auto", description="LLM provider: 'auto', 'ollama', 'openai', 'anthropic'"
    )
    ollama_host: str = Field(default="http://localhost:11434", description="Ollama API host")
    ollama_model: str = Field(default="llama3.2", description="Ollama model to use")
    openai_api_key: str | None = Field(default=None, description="OpenAI API key")
    openai_model: str = Field(default="gpt-4o", description="OpenAI model to use")
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key")
    anthropic_model: str = Field(
        default="claude-sonnet-4-5-20250929", description="Anthropic model to use"
    )

    # Memory Backend
    memory_backend: str = Field(
        default="file",
        description="Memory backend: 'file' (simple markdown), 'mem0' (semantic with LLM)",
    )
    memory_use_inference: bool = Field(
        default=True, description="Use LLM to extract facts from memories (only for mem0 backend)"
    )

    # Tool Policy
    tool_profile: str = Field(
        default="full", description="Tool profile: 'minimal', 'coding', or 'full'"
    )
    tools_allow: list[str] = Field(
        default_factory=list, description="Explicit tool allow list (merged with profile)"
    )
    tools_deny: list[str] = Field(
        default_factory=list, description="Explicit tool deny list (highest priority)"
    )

    # Discord
    discord_bot_token: str | None = Field(default=None, description="Discord bot token")
    discord_allowed_guild_ids: list[int] = Field(
        default_factory=list, description="Discord guild IDs allowed to use the bot"
    )
    discord_allowed_user_ids: list[int] = Field(
        default_factory=list, description="Discord user IDs allowed to use the bot"
    )

    # Slack
    slack_bot_token: str | None = Field(
        default=None, description="Slack Bot OAuth token (xoxb-...)"
    )
    slack_app_token: str | None = Field(
        default=None, description="Slack App-Level token for Socket Mode (xapp-...)"
    )
    slack_allowed_channel_ids: list[str] = Field(
        default_factory=list, description="Slack channel IDs allowed to use the bot"
    )

    # WhatsApp
    whatsapp_mode: str = Field(
        default="personal",
        description="WhatsApp mode: 'personal' (QR scan via neonize) or 'business' (Cloud API)",
    )
    whatsapp_neonize_db: str = Field(
        default="",
        description="Path to neonize SQLite credential store",
    )
    whatsapp_access_token: str | None = Field(
        default=None, description="WhatsApp Business Cloud API access token"
    )
    whatsapp_phone_number_id: str | None = Field(
        default=None, description="WhatsApp Business phone number ID"
    )
    whatsapp_verify_token: str | None = Field(
        default=None, description="WhatsApp webhook verification token"
    )
    whatsapp_allowed_phone_numbers: list[str] = Field(
        default_factory=list, description="WhatsApp phone numbers allowed to use the bot"
    )

    # Security
    bypass_permissions: bool = Field(
        default=False, description="Skip permission prompts for agent actions (use with caution)"
    )
    file_jail_path: Path = Field(
        default_factory=Path.home, description="Root path for file operations"
    )

    # Web Server
    web_host: str = Field(default="127.0.0.1", description="Web server host")
    web_port: int = Field(default=8888, description="Web server port")

    def save(self) -> None:
        """Save settings to config file.

        Merges with existing config to preserve API keys if not set in current instance.
        """
        config_path = get_config_path()

        # Load existing config to preserve API keys if not set
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except (json.JSONDecodeError, Exception):
                pass

        data = {
            "telegram_bot_token": self.telegram_bot_token or existing.get("telegram_bot_token"),
            "allowed_user_id": self.allowed_user_id or existing.get("allowed_user_id"),
            "agent_backend": self.agent_backend,
            "memory_backend": self.memory_backend,
            "memory_use_inference": self.memory_use_inference,
            "llm_provider": self.llm_provider,
            "ollama_host": self.ollama_host,
            "ollama_model": self.ollama_model,
            "openai_api_key": self.openai_api_key or existing.get("openai_api_key"),
            "openai_model": self.openai_model,
            "anthropic_api_key": self.anthropic_api_key or existing.get("anthropic_api_key"),
            "anthropic_model": self.anthropic_model,
            # Discord
            "discord_bot_token": (self.discord_bot_token or existing.get("discord_bot_token")),
            "discord_allowed_guild_ids": self.discord_allowed_guild_ids,
            "discord_allowed_user_ids": self.discord_allowed_user_ids,
            # Slack
            "slack_bot_token": self.slack_bot_token or existing.get("slack_bot_token"),
            "slack_app_token": self.slack_app_token or existing.get("slack_app_token"),
            "slack_allowed_channel_ids": self.slack_allowed_channel_ids,
            # WhatsApp
            "whatsapp_mode": self.whatsapp_mode,
            "whatsapp_neonize_db": self.whatsapp_neonize_db,
            "whatsapp_access_token": (
                self.whatsapp_access_token or existing.get("whatsapp_access_token")
            ),
            "whatsapp_phone_number_id": (
                self.whatsapp_phone_number_id or existing.get("whatsapp_phone_number_id")
            ),
            "whatsapp_verify_token": (
                self.whatsapp_verify_token or existing.get("whatsapp_verify_token")
            ),
            "whatsapp_allowed_phone_numbers": self.whatsapp_allowed_phone_numbers,
        }
        config_path.write_text(json.dumps(data, indent=2))

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from config file, falling back to env/defaults."""
        config_path = get_config_path()
        if config_path.exists():
            try:
                data = json.loads(config_path.read_text())
                return cls(**data)
            except (json.JSONDecodeError, Exception):
                pass
        return cls()


@lru_cache
def get_settings(force_reload: bool = False) -> Settings:
    """Get cached settings instance."""
    if force_reload:
        get_settings.cache_clear()
    return Settings.load()


def get_access_token() -> str:
    """
    Get the current access token.
    If it doesn't exist, generate a new one.
    """
    token_path = get_token_path()
    if token_path.exists():
        token = token_path.read_text().strip()
        if token:
            return token

    return regenerate_token()


def regenerate_token() -> str:
    """
    Generate a new secure access token and save it.
    Invalidates previous tokens.
    """
    import uuid

    token = str(uuid.uuid4())
    token_path = get_token_path()
    token_path.write_text(token)
    return token
