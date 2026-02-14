"""Configuration management for PocketPaw.

Changes:
  - 2026-02-06: Secrets stored encrypted via CredentialStore; auto-migrate plaintext keys.
  - 2026-02-06: Harden file/directory permissions (700 dir, 600 files).
  - 2026-02-02: Added claude_agent_sdk to agent_backend options.
  - 2026-02-02: Simplified backends - removed 2-layer mode.
  - 2026-02-02: claude_agent_sdk is now RECOMMENDED (uses official SDK).
"""

import json
import logging
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


def _chmod_safe(path: Path, mode: int) -> None:
    """Set file permissions, ignoring errors on Windows."""
    try:
        path.chmod(mode)
    except OSError:
        pass


def get_config_dir() -> Path:
    """Get the config directory, creating if needed."""
    config_dir = Path.home() / ".pocketclaw"
    config_dir.mkdir(exist_ok=True)
    _chmod_safe(config_dir, 0o700)
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
        default="claude_agent_sdk",
        description="Agent backend: 'claude_agent_sdk' (recommended), 'pocketpaw_native', or 'open_interpreter' (experimental)",
    )

    # LLM Configuration
    llm_provider: str = Field(
        default="auto", description="LLM provider: 'auto', 'ollama', 'openai', 'anthropic', 'gemini'"
    )
    ollama_host: str = Field(default="http://localhost:11434", description="Ollama API host")
    ollama_model: str = Field(default="llama3.2", description="Ollama model to use")
    openai_api_key: str | None = Field(default=None, description="OpenAI API key")
    openai_model: str = Field(default="gpt-4o", description="OpenAI model to use")
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key")
    anthropic_model: str = Field(
        default="claude-sonnet-4-5-20250929", description="Anthropic model to use"
    )
    gemini_api_key: str | None = Field(default=None, description="Gemini API key")
    gemini_model: str = Field(
        default="gemini-2.0-pro-exp-02-05", description="Gemini model to use"
    )

    # Memory Backend
    memory_backend: str = Field(
        default="file",
        description="Memory backend: 'file' (simple markdown), 'mem0' (semantic with LLM)",
    )
    memory_use_inference: bool = Field(
        default=True, description="Use LLM to extract facts from memories (only for mem0 backend)"
    )

    # Mem0 Configuration
    mem0_llm_provider: str = Field(
        default="anthropic",
        description="LLM provider for mem0 fact extraction: 'anthropic', 'openai', or 'ollama'",
    )
    mem0_llm_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="LLM model for mem0 fact extraction",
    )
    mem0_embedder_provider: str = Field(
        default="openai",
        description="Embedder provider for mem0 vectors: 'openai', 'ollama', or 'huggingface'",
    )
    mem0_embedder_model: str = Field(
        default="text-embedding-3-small",
        description="Embedding model for mem0 vector search",
    )
    mem0_vector_store: str = Field(
        default="qdrant",
        description="Vector store for mem0: 'qdrant' or 'chroma'",
    )
    mem0_ollama_base_url: str = Field(
        default="http://localhost:11434",
        description="Ollama base URL for mem0 (when using ollama provider)",
    )
    mem0_auto_learn: bool = Field(
        default=True,
        description="Automatically extract facts from conversations into long-term memory",
    )
    file_auto_learn: bool = Field(
        default=False,
        description="Auto-extract facts from conversations for file memory backend (uses Haiku)",
    )

    # Session History Compaction
    compaction_recent_window: int = Field(
        default=10, description="Number of recent messages to keep verbatim"
    )
    compaction_char_budget: int = Field(
        default=8000, description="Max total chars for compacted history"
    )
    compaction_summary_chars: int = Field(
        default=150, description="Max chars per older message one-liner extract"
    )
    compaction_llm_summarize: bool = Field(
        default=False, description="Use Haiku to summarize older messages (opt-in)"
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

    # Web Search
    web_search_provider: str = Field(
        default="tavily", description="Web search provider: 'tavily' or 'brave'"
    )
    tavily_api_key: str | None = Field(default=None, description="Tavily search API key")
    brave_search_api_key: str | None = Field(default=None, description="Brave Search API key")
    parallel_api_key: str | None = Field(default=None, description="Parallel AI API key")
    url_extract_provider: str = Field(
        default="auto", description="URL extract provider: 'auto', 'parallel', or 'local'"
    )

    # Image Generation
    google_api_key: str | None = Field(default=None, description="Google API key (for Gemini)")
    image_model: str = Field(
        default="gemini-2.0-flash-exp", description="Google image generation model"
    )

    # Security
    bypass_permissions: bool = Field(
        default=False, description="Skip permission prompts for agent actions (use with caution)"
    )
    localhost_auth_bypass: bool = Field(
        default=True,
        description="Allow unauthenticated localhost access (disable for non-CF proxies)",
    )
    session_token_ttl_hours: int = Field(
        default=24, description="TTL in hours for HMAC session tokens issued via /api/auth/session"
    )
    file_jail_path: Path = Field(
        default_factory=Path.home, description="Root path for file operations"
    )
    injection_scan_enabled: bool = Field(
        default=True, description="Enable prompt injection scanning on inbound messages"
    )
    injection_scan_llm: bool = Field(
        default=False, description="Use LLM deep scan for suspicious content (requires API key)"
    )
    injection_scan_llm_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Model for LLM-based injection deep scan",
    )

    # Smart Model Routing
    smart_routing_enabled: bool = Field(
        default=False, description="Enable automatic model selection based on task complexity"
    )
    model_tier_simple: str = Field(
        default="claude-haiku-4-5-20251001", description="Model for simple tasks (greetings, facts)"
    )
    model_tier_moderate: str = Field(
        default="claude-sonnet-4-5-20250929",
        description="Model for moderate tasks (coding, analysis)",
    )
    model_tier_complex: str = Field(
        default="claude-opus-4-6", description="Model for complex tasks (planning, debugging)"
    )

    # Plan Mode
    plan_mode: bool = Field(default=False, description="Require approval before executing tools")
    plan_mode_tools: list[str] = Field(
        default_factory=lambda: ["shell", "write_file", "edit_file"],
        description="Tools that require approval in plan mode",
    )

    # Self-Audit Daemon
    self_audit_enabled: bool = Field(default=True, description="Enable daily self-audit daemon")
    self_audit_schedule: str = Field(
        default="0 3 * * *", description="Cron schedule for self-audit (default: 3 AM daily)"
    )

    # OAuth
    google_oauth_client_id: str | None = Field(
        default=None, description="Google OAuth 2.0 client ID"
    )
    google_oauth_client_secret: str | None = Field(
        default=None, description="Google OAuth 2.0 client secret"
    )

    # Voice/TTS
    tts_provider: str = Field(
        default="openai", description="TTS provider: 'openai' or 'elevenlabs'"
    )
    elevenlabs_api_key: str | None = Field(default=None, description="ElevenLabs API key for TTS")
    tts_voice: str = Field(
        default="alloy", description="TTS voice name (OpenAI: alloy/echo/fable/onyx/nova/shimmer)"
    )
    stt_model: str = Field(default="whisper-1", description="OpenAI Whisper model for STT")

    # Spotify
    spotify_client_id: str | None = Field(default=None, description="Spotify OAuth client ID")
    spotify_client_secret: str | None = Field(
        default=None, description="Spotify OAuth client secret"
    )

    # Signal
    signal_api_url: str = Field(
        default="http://localhost:8080", description="Signal-cli REST API URL"
    )
    signal_phone_number: str | None = Field(
        default=None, description="Signal phone number (e.g. +1234567890)"
    )
    signal_allowed_phone_numbers: list[str] = Field(
        default_factory=list, description="Signal phone numbers allowed to use the bot"
    )

    # Matrix
    matrix_homeserver: str | None = Field(
        default=None, description="Matrix homeserver URL (e.g. https://matrix.org)"
    )
    matrix_user_id: str | None = Field(
        default=None, description="Matrix user ID (e.g. @bot:matrix.org)"
    )
    matrix_access_token: str | None = Field(default=None, description="Matrix access token")
    matrix_password: str | None = Field(
        default=None, description="Matrix password (alternative to access token)"
    )
    matrix_allowed_room_ids: list[str] = Field(
        default_factory=list, description="Matrix room IDs allowed to use the bot"
    )
    matrix_device_id: str = Field(default="POCKETPAW", description="Matrix device ID")

    # Microsoft Teams
    teams_app_id: str | None = Field(default=None, description="Microsoft Teams App ID")
    teams_app_password: str | None = Field(default=None, description="Microsoft Teams App Password")
    teams_allowed_tenant_ids: list[str] = Field(
        default_factory=list, description="Allowed Azure AD tenant IDs"
    )
    teams_webhook_port: int = Field(default=3978, description="Teams webhook listener port")

    # Google Chat
    gchat_mode: str = Field(
        default="webhook", description="Google Chat mode: 'webhook' or 'pubsub'"
    )
    gchat_service_account_key: str | None = Field(
        default=None, description="Path to Google service account JSON key file"
    )
    gchat_project_id: str | None = Field(
        default=None, description="Google Cloud project ID for Pub/Sub mode"
    )
    gchat_subscription_id: str | None = Field(default=None, description="Pub/Sub subscription ID")
    gchat_allowed_space_ids: list[str] = Field(
        default_factory=list, description="Google Chat space IDs allowed to use the bot"
    )

    # Generic Inbound Webhooks
    webhook_configs: list[dict] = Field(
        default_factory=list,
        description="Configured webhook slots [{name, secret, description, sync_timeout}]",
    )
    webhook_sync_timeout: int = Field(
        default=30, description="Default timeout (seconds) for sync webhook responses"
    )

    # Web Server
    web_host: str = Field(default="127.0.0.1", description="Web server host")
    web_port: int = Field(default=8888, description="Web server port")

    # Identity / Multi-user
    owner_id: str = Field(
        default="",
        description="Global owner identifier (e.g. Telegram user ID). Empty = single-user mode.",
    )
    notification_channels: list[str] = Field(
        default_factory=list,
        description="Targets for autonomous messages, e.g. ['telegram:12345', 'discord:98765']",
    )

    # Media Downloads
    media_download_dir: str = Field(
        default="", description="Custom media download dir (default: ~/.pocketclaw/media/)"
    )
    media_max_file_size_mb: int = Field(
        default=50, description="Max media file size in MB (0 = unlimited)"
    )

    # UX
    welcome_hint_enabled: bool = Field(
        default=True,
        description="Send a one-time welcome hint on first interaction in non-web channels",
    )

    # Concurrency
    max_concurrent_conversations: int = Field(
        default=5, description="Max parallel conversations processed simultaneously"
    )

    def save(self) -> None:
        """Save settings to config file.

        Non-secret fields go to config.json. Secret fields (API keys, tokens)
        go to the encrypted credential store.
        """
        from pocketclaw.credentials import SECRET_FIELDS, get_credential_store

        config_path = get_config_path()

        # Load existing config to preserve non-secret values if not set
        existing = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except (json.JSONDecodeError, Exception):
                pass

        # Build full settings dict
        all_fields = {
            "telegram_bot_token": self.telegram_bot_token or existing.get("telegram_bot_token"),
            "allowed_user_id": self.allowed_user_id or existing.get("allowed_user_id"),
            "agent_backend": self.agent_backend,
            "memory_backend": self.memory_backend,
            "memory_use_inference": self.memory_use_inference,
            "mem0_llm_provider": self.mem0_llm_provider,
            "mem0_llm_model": self.mem0_llm_model,
            "mem0_embedder_provider": self.mem0_embedder_provider,
            "mem0_embedder_model": self.mem0_embedder_model,
            "mem0_vector_store": self.mem0_vector_store,
            "mem0_ollama_base_url": self.mem0_ollama_base_url,
            "mem0_auto_learn": self.mem0_auto_learn,
            "file_auto_learn": self.file_auto_learn,
            "compaction_recent_window": self.compaction_recent_window,
            "compaction_char_budget": self.compaction_char_budget,
            "compaction_summary_chars": self.compaction_summary_chars,
            "compaction_llm_summarize": self.compaction_llm_summarize,
            "llm_provider": self.llm_provider,
            "ollama_host": self.ollama_host,
            "ollama_model": self.ollama_model,
            "openai_api_key": self.openai_api_key or existing.get("openai_api_key"),
            "openai_model": self.openai_model,
            "anthropic_api_key": self.anthropic_api_key or existing.get("anthropic_api_key"),
            "anthropic_model": self.anthropic_model,
            "gemini_api_key": self.gemini_api_key or existing.get("gemini_api_key"),
            "gemini_model": self.gemini_model,
            # Discord
            "discord_bot_token": (self.discord_bot_token or existing.get("discord_bot_token")),
            "discord_allowed_guild_ids": self.discord_allowed_guild_ids,
            "discord_allowed_user_ids": self.discord_allowed_user_ids,
            # Slack
            "slack_bot_token": self.slack_bot_token or existing.get("slack_bot_token"),
            "slack_app_token": self.slack_app_token or existing.get("slack_app_token"),
            "slack_allowed_channel_ids": self.slack_allowed_channel_ids,
            # Web Search
            "web_search_provider": self.web_search_provider,
            "tavily_api_key": self.tavily_api_key or existing.get("tavily_api_key"),
            "brave_search_api_key": (
                self.brave_search_api_key or existing.get("brave_search_api_key")
            ),
            "parallel_api_key": self.parallel_api_key or existing.get("parallel_api_key"),
            "url_extract_provider": self.url_extract_provider,
            # Image Generation
            "google_api_key": self.google_api_key or existing.get("google_api_key"),
            "image_model": self.image_model,
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
            # Tool policy
            "tool_profile": self.tool_profile,
            "tools_allow": self.tools_allow,
            "tools_deny": self.tools_deny,
            # Security
            "injection_scan_enabled": self.injection_scan_enabled,
            "injection_scan_llm": self.injection_scan_llm,
            "injection_scan_llm_model": self.injection_scan_llm_model,
            "localhost_auth_bypass": self.localhost_auth_bypass,
            "session_token_ttl_hours": self.session_token_ttl_hours,
            # Smart routing
            "smart_routing_enabled": self.smart_routing_enabled,
            "model_tier_simple": self.model_tier_simple,
            "model_tier_moderate": self.model_tier_moderate,
            "model_tier_complex": self.model_tier_complex,
            # Plan mode
            "plan_mode": self.plan_mode,
            "plan_mode_tools": self.plan_mode_tools,
            # Self-audit
            "self_audit_enabled": self.self_audit_enabled,
            "self_audit_schedule": self.self_audit_schedule,
            # OAuth
            "google_oauth_client_id": (
                self.google_oauth_client_id or existing.get("google_oauth_client_id")
            ),
            "google_oauth_client_secret": (
                self.google_oauth_client_secret or existing.get("google_oauth_client_secret")
            ),
            # Voice/TTS
            "tts_provider": self.tts_provider,
            "elevenlabs_api_key": (self.elevenlabs_api_key or existing.get("elevenlabs_api_key")),
            "tts_voice": self.tts_voice,
            "stt_model": self.stt_model,
            # Spotify
            "spotify_client_id": (self.spotify_client_id or existing.get("spotify_client_id")),
            "spotify_client_secret": (
                self.spotify_client_secret or existing.get("spotify_client_secret")
            ),
            # Signal
            "signal_api_url": self.signal_api_url,
            "signal_phone_number": self.signal_phone_number,
            "signal_allowed_phone_numbers": self.signal_allowed_phone_numbers,
            # Matrix
            "matrix_homeserver": self.matrix_homeserver,
            "matrix_user_id": self.matrix_user_id,
            "matrix_access_token": (
                self.matrix_access_token or existing.get("matrix_access_token")
            ),
            "matrix_password": self.matrix_password or existing.get("matrix_password"),
            "matrix_allowed_room_ids": self.matrix_allowed_room_ids,
            "matrix_device_id": self.matrix_device_id,
            # Teams
            "teams_app_id": self.teams_app_id or existing.get("teams_app_id"),
            "teams_app_password": (self.teams_app_password or existing.get("teams_app_password")),
            "teams_allowed_tenant_ids": self.teams_allowed_tenant_ids,
            "teams_webhook_port": self.teams_webhook_port,
            # Google Chat
            "gchat_mode": self.gchat_mode,
            "gchat_service_account_key": (
                self.gchat_service_account_key or existing.get("gchat_service_account_key")
            ),
            "gchat_project_id": self.gchat_project_id,
            "gchat_subscription_id": self.gchat_subscription_id,
            "gchat_allowed_space_ids": self.gchat_allowed_space_ids,
            # Generic Webhooks
            "webhook_configs": self.webhook_configs,
            "webhook_sync_timeout": self.webhook_sync_timeout,
            # Identity / Multi-user
            "owner_id": self.owner_id,
            "notification_channels": self.notification_channels,
            # Media Downloads
            "media_download_dir": self.media_download_dir,
            "media_max_file_size_mb": self.media_max_file_size_mb,
            # UX
            "welcome_hint_enabled": self.welcome_hint_enabled,
            # Concurrency
            "max_concurrent_conversations": self.max_concurrent_conversations,
        }

        # Store secrets in the encrypted credential store, then strip
        # them from the dict before writing config.json to prevent
        # plaintext secret leakage.
        store = get_credential_store()
        for key, value in all_fields.items():
            if key in SECRET_FIELDS and value:
                store.set(key, value)

        safe_fields = {k: v for k, v in all_fields.items() if k not in SECRET_FIELDS}
        config_path.write_text(json.dumps(safe_fields, indent=2))
        _chmod_safe(config_path, 0o600)

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from config file + encrypted credential store."""
        from pocketclaw.credentials import SECRET_FIELDS, get_credential_store

        # Run one-time migration from plaintext config
        _migrate_plaintext_keys()

        config_path = get_config_path()
        data: dict = {}
        if config_path.exists():
            try:
                data = json.loads(config_path.read_text())
            except (json.JSONDecodeError, Exception):
                pass

        # Overlay secrets from encrypted store (falls back to config.json values)
        store = get_credential_store()
        secrets = store.get_all()
        for field in SECRET_FIELDS:
            if field in secrets and secrets[field]:
                data[field] = secrets[field]
            # data[field] may already be set from config.json — keep it as fallback

        if data:
            try:
                return cls(**data)
            except Exception:
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
    _chmod_safe(token_path, 0o600)
    return token


# Flag file to avoid re-running migration on every load
_MIGRATION_DONE_PATH: Path | None = None


def _migrate_plaintext_keys() -> None:
    """One-time migration: move plaintext API keys from config.json to encrypted store."""
    from pocketclaw.credentials import SECRET_FIELDS, get_credential_store

    global _MIGRATION_DONE_PATH  # noqa: PLW0603
    if _MIGRATION_DONE_PATH is None:
        _MIGRATION_DONE_PATH = get_config_dir() / ".secrets_migrated"

    if _MIGRATION_DONE_PATH.exists():
        return

    config_path = get_config_path()
    if not config_path.exists():
        # No config yet — nothing to migrate
        _MIGRATION_DONE_PATH.write_text("1")
        return

    try:
        data = json.loads(config_path.read_text())
    except (json.JSONDecodeError, Exception):
        return

    store = get_credential_store()
    migrated_count = 0

    for field in SECRET_FIELDS:
        value = data.get(field)
        if value and isinstance(value, str):
            store.set(field, value)
            migrated_count += 1

    if migrated_count:
        logger.info("Copied %d secret(s) from config to encrypted store.", migrated_count)

    _MIGRATION_DONE_PATH.write_text("1")
    _chmod_safe(_MIGRATION_DONE_PATH, 0o600)
