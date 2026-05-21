"""Configuration management for PocketPaw.

Changes:
  - 2026-05-21: Added ``auto_install_bundled_skills`` and
    ``auto_install_bundled_kb_scopes`` — toggle the boot-time mirror of
    bundled SKILL.md files and pre-compiled kb-go scopes.
  - 2026-04-30: Added pluggable embedding adapter settings — ``kb_vectors_enabled``,
    ``embedding_adapter``, ``embedding_dim``, ``embedding_monthly_cap_usd``,
    ``vertex_project_id``, ``vertex_location``. Stage 2.D of "Files as Knowledge".
  - 2026-04-30: Added ``kb_scopes`` (list[str]) for multi-scope KB queries.
    ``kb_scope`` (single string) is now a deprecation shim — when set and
    ``kb_scopes`` is empty, it copies forward and emits DeprecationWarning.
    Stage 1.B of "Files as Knowledge".
  - 2026-04-16: SSRF guard on URL config fields — opencode_base_url,
    litellm_api_base, openai_compatible_base_url, mem0_ollama_base_url,
    embedding_base_url, signal_api_url, mcp_client_metadata_url are now
    validated by security.url_validators.validate_external_url. Closes #703.
  - 2026-04-10: Removed old pocketclaw migration warning — fully shifted to pocketpaw.
  - 2026-04-04: Added soul_cognitive_model setting for cheaper cognitive processing.
  - 2026-03-16: Use Literal types for whatsapp_mode, tts_provider, stt_provider (#638).
  - 2026-02-17: Added health_check_on_startup field for Health Engine.
  - 2026-02-06: Secrets stored encrypted via CredentialStore; auto-migrate plaintext keys.
  - 2026-02-06: Harden file/directory permissions (700 dir, 600 files).
  - 2026-02-02: Added claude_agent_sdk to agent_backend options.
  - 2026-02-02: Simplified backends - removed 2-layer mode.
  - 2026-02-02: claude_agent_sdk is now RECOMMENDED (uses official SDK).
"""

from __future__ import annotations

import json
import logging
import os
import re
import warnings
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from pocketpaw.security.url_validators import validate_external_url

# Shorthand for Settings URL fields that must be safe from SSRF (#703).
# Applies scheme + loopback/RFC1918 guards from security.url_validators.
ExternalUrl = Annotated[str, AfterValidator(validate_external_url)]

logger = logging.getLogger(__name__)


# API key validation patterns
_API_KEY_PATTERNS = {
    "anthropic_api_key": {
        "pattern": re.compile(r"^sk-ant-"),
        "example": "sk-ant-...",
        "name": "Anthropic API key",
    },
    "openai_api_key": {
        "pattern": re.compile(r"^sk-"),
        "example": "sk-...",
        "name": "OpenAI API key",
    },
    "openrouter_api_key": {
        "pattern": re.compile(r"^sk-or-v1-"),
        "example": "sk-or-v1-...",
        "name": "OpenRouter API key",
    },
    "telegram_bot_token": {
        "pattern": re.compile(r"^\d+:AA[A-Za-z0-9_-]{30,}$"),
        "example": "123456789:AAH...",
        "name": "Telegram bot token",
    },
}


def validate_api_key(field_name: str, value: str) -> tuple[bool, str]:
    """Validate a **single** API key against strict regex patterns.

    Used by the REST ``PUT /settings`` endpoint and the WS ``save_api_key``
    handler to check format *before* saving.  Returns a per-key verdict so
    the caller can surface a targeted warning.

    See also :func:`validate_api_keys` which validates *all* keys on a
    :class:`Settings` instance using looser prefix checks.

    Args:
        field_name: Settings field name (e.g., ``"anthropic_api_key"``).
        value: The raw API key string to validate.

    Returns:
        ``(True, "")`` when the format is acceptable, or
        ``(False, "<human-readable warning>")`` when it is not.
    """
    if not value or not value.strip():
        return True, ""  # Empty values are allowed (user may want to unset)

    value = value.strip()

    validator = _API_KEY_PATTERNS.get(field_name)
    if not validator:
        return True, ""  # No validation rule for this field

    if not validator["pattern"].match(value):
        return False, (
            f"{validator['name']} doesn't match expected format "
            f"(expected format: {validator['example']}). "
            f"Double-check for typos or truncation."
        )

    return True, ""


def _chmod_safe(path: Path, mode: int) -> None:
    """Set file permissions, ignoring errors on Windows."""
    try:
        path.chmod(mode)
    except OSError:
        pass


def get_config_dir() -> Path:
    """Get the config directory, creating if needed."""
    config_dir = Path.home() / ".pocketpaw"
    config_dir.mkdir(exist_ok=True)
    _chmod_safe(config_dir, 0o700)
    return config_dir


def get_config_path() -> Path:
    """Get the config file path."""
    return get_config_dir() / "config.json"


def get_token_path() -> Path:
    """Get the access token file path."""
    return get_config_dir() / "access_token"


# Telegram bot token format: numeric id + colon + alphanumeric secret
_TELEGRAM_BOT_TOKEN_RE = re.compile(r"^\d+:[A-Za-z0-9_-]+$")


def validate_api_keys(settings: Settings) -> list[str]:
    """Validate **all** API keys on a :class:`Settings` instance (batch, loose).

    Uses simple prefix checks (not the strict regexes in :func:`validate_api_key`)
    and returns a list of human-readable warnings.  Designed for advisory use
    (e.g. ``Settings.save()`` logs warnings) — callers must **never** block a
    save based on these results.
    """
    warnings: list[str] = []
    if settings.anthropic_api_key and not settings.anthropic_api_key.startswith("sk-ant-"):
        warnings.append("Anthropic API key may be invalid: expected to start with sk-ant-")
    if settings.openai_api_key and not settings.openai_api_key.startswith("sk-"):
        warnings.append("OpenAI API key may be invalid: expected to start with sk-")
    if settings.telegram_bot_token and not _TELEGRAM_BOT_TOKEN_RE.fullmatch(
        settings.telegram_bot_token.strip()
    ):
        warnings.append(
            "Telegram bot token may be invalid: expected format is numeric_id:alphanumeric_secret"
        )
    return warnings


class Settings(BaseSettings):
    """PocketPaw settings with env and file support."""

    model_config = SettingsConfigDict(
        env_prefix="POCKETPAW_",
        env_file=".env",
        extra="ignore",
        populate_by_name=True,  # allow field-name assignment alongside aliases
    )

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
        description=(
            "Agent backend: 'claude_agent_sdk', 'openai_agents', 'google_adk', "
            "'codex_cli', 'opencode', 'copilot_sdk', 'deep_agents', or "
            "'langchain_react'. All backends support 'litellm' as a provider "
            "for open-source model access."
        ),
    )
    # backend fallback chain
    fallback_backends: list[str] = Field(
        default_factory=list,
        description=("Ordered list of fallback backends to try if the primary backend fails"),
    )

    # Claude Agent SDK Settings
    claude_sdk_provider: str = Field(
        default="anthropic",
        description=(
            "Provider for Claude SDK: 'anthropic', 'ollama', 'openai_compatible', or 'litellm'"
        ),
    )
    claude_sdk_model: str = Field(
        default="",
        description="Model for Claude SDK backend (empty = let Claude Code auto-select)",
    )
    claude_sdk_max_turns: int = Field(
        default=100,
        description="Max tool-use turns per query in Claude SDK (0 = unlimited)",
    )

    # OpenAI Agents SDK Settings
    openai_agents_provider: str = Field(
        default="openai",
        description=(
            "Provider for OpenAI Agents: 'openai', 'ollama', 'openai_compatible', or 'litellm'"
        ),
    )
    openai_agents_model: str = Field(
        default="", description="Model for OpenAI Agents backend (empty = gpt-5.2)"
    )
    openai_agents_max_turns: int = Field(
        default=100, description="Max turns per query in OpenAI Agents backend (0 = unlimited)"
    )

    # Gemini CLI Settings (legacy, kept for config compat)
    gemini_cli_model: str = Field(
        default="gemini-3-pro-preview", description="Model for Gemini CLI backend (legacy)"
    )
    gemini_cli_max_turns: int = Field(
        default=100, description="Max turns per query in Gemini CLI backend (legacy, 0 = unlimited)"
    )

    # Google ADK Settings
    google_adk_provider: str = Field(
        default="google",
        description="Provider for Google ADK: 'google' or 'litellm'",
    )
    google_adk_model: str = Field(
        default="gemini-3-pro-preview", description="Model for Google ADK backend"
    )
    google_adk_max_turns: int = Field(
        default=100, description="Max turns per query in Google ADK backend (0 = unlimited)"
    )

    # Codex CLI Settings
    codex_cli_model: str = Field(default="gpt-5.3-codex", description="Model for Codex CLI backend")
    codex_cli_max_turns: int = Field(
        default=100, description="Max turns per query in Codex CLI backend (0 = unlimited)"
    )
    codex_cli_api_key: str | None = Field(
        default=None,
        description=(
            "Optional API key for the Codex CLI backend. Falls back to "
            "openai_api_key when unset; useful when the user wants Codex "
            "talking to a different account than the rest of OpenAI tooling."
        ),
    )
    codex_cli_base_url: str | None = Field(
        default=None,
        description=(
            "Optional base URL for the Codex CLI backend (sets OPENAI_BASE_URL "
            "for the codex subprocess). Lets you point Codex at an "
            "OpenAI-compatible proxy (LiteLLM, Azure, etc.) without changing "
            "the global OpenAI base URL."
        ),
    )
    codex_cli_sandbox_mode: str = Field(
        default="danger-full-access",
        description=(
            "Codex CLI sandbox_mode. Values: read-only, workspace-write, "
            "danger-full-access. Default danger-full-access because Codex's "
            "tighter sandboxes (workspace-write, read-only) rely on Linux "
            "seccomp/landlock — on Windows the sandbox can't be created, so "
            "every exec call is auto-declined with status='declined'. "
            "PocketPaw runs Codex in an ephemeral temp dir as a trusted "
            "agent that the operator already authorized; the tighter modes "
            "are only useful on Linux operator deployments that want to "
            "constrain a less-trusted agent."
        ),
    )
    codex_cli_approval_policy: str = Field(
        default="never",
        description=(
            "Codex CLI approval_policy. Values: never, on-request, "
            "on-failure, untrusted. 'never' is required for headless cloud "
            "use (no human to approve). Pair with codex_cli_sandbox_mode="
            "'danger-full-access' on Windows or anywhere the agent can't "
            "be interactively supervised."
        ),
    )

    # Copilot SDK Settings
    copilot_sdk_provider: str = Field(
        default="copilot",
        description=(
            "Provider for Copilot SDK: 'copilot', 'openai', 'azure', 'anthropic', or 'litellm'"
        ),
    )
    copilot_sdk_model: str = Field(
        default="", description="Model for Copilot SDK backend (empty = gpt-5.2)"
    )
    copilot_sdk_max_turns: int = Field(
        default=100, description="Max turns per query in Copilot SDK backend (0 = unlimited)"
    )

    # Deep Agents (LangChain/LangGraph) Settings
    deep_agents_model: str = Field(
        default="anthropic:claude-sonnet-4-6",
        description="Model for Deep Agents backend in ``provider:model`` format.",
    )
    deep_agents_max_turns: int = Field(
        default=100,
        description="Max turns per query in Deep Agents backend (0 = unlimited)",
    )
    deep_agents_disable_thinking: bool = Field(
        default=False,
        description=(
            "Ask the Deep Agents backend's chat model to skip extended "
            "thinking. Sent as a provider-shaped kwarg; providers that "
            "don't recognize the shape ignore it."
        ),
    )
    # Pocket Specialist Settings — see docs/superpowers/specs/2026-05-09-pocket-specialist-design.md
    pocket_specialist_backend: str = Field(
        default="deep_agents",
        description=(
            "Which agent backend runs the pocket specialist's LLM work. Must be a "
            "registered backend name (deep_agents, langchain_react, claude_agent_sdk, "
            "openai_agents, google_adk, codex_cli, opencode, copilot_sdk). Default "
            "deep_agents avoids subprocess cold-start."
        ),
    )
    pocket_specialist_model: str = Field(
        default="anthropic:claude-haiku-4-5-20251001",
        description=(
            "Model the specialist uses for spec generation. Defaults to Haiku — "
            "the specialist's job is emitting structured rippleSpec JSON from a "
            "stable ~12k-token design-rules prompt, which Haiku handles at ~2-4x "
            "Sonnet speed with no measurable quality loss. Override with "
            "provider:model when you need creative liberty (Sonnet) or cheap "
            "self-hosted inference ('openai_compatible:deepseek-v4-pro'). Set to "
            "an empty string to fall back to the chosen backend's default "
            "*_model setting."
        ),
    )
    pocket_specialist_max_validation_retries: int = Field(
        default=3,
        description=(
            "Max draft -> validate -> revise iterations before persisting with "
            "remaining warnings. Specialist always persists; this only bounds revision."
        ),
    )
    pocket_specialist_mode: Literal["subagent", "agent"] = Field(
        default="subagent",
        description=(
            "Which adapter handles ``pocket_specialist__create`` calls. "
            "``subagent`` (default) spawns an isolated backend running the "
            "specialist's own model — the historical flow. ``agent`` uses a "
            "two-call protocol: the first call returns a draft kit (design "
            "rules digest + structural plan + widget list); the chat agent "
            "drafts the rippleSpec inline using its own model and calls back "
            "with ``spec=<draft>`` for validate-and-persist. ``agent`` mode "
            "ignores ``pocket_specialist_backend`` and ``pocket_specialist_model`` "
            "entirely — the chat agent's runtime is the LLM."
        ),
    )
    auto_install_bundled_skills: bool = Field(
        default=True,
        description=(
            "On dashboard startup, mirror bundled AgentSkills-format "
            "SKILL.md files from ``pocketpaw/bundled_skills/_bundled/`` "
            "into ``~/.claude/skills/<name>/SKILL.md``. That destination "
            "is covered by both Claude Code's native skill discovery AND "
            "PocketPaw's ``SkillLoader.SKILL_PATHS`` — so the skill works "
            "for all chat backends (claude_agent_sdk via natural-language "
            "invocation, codex_cli / openai_agents / deep_agents via the "
            "``/<skill-name>`` slash command). Idempotent — SHA-256 hash "
            "compare per file. Set ``false`` to freeze a manually-customized "
            "copy or disable bundled skills entirely. Skill installation "
            "is best-effort: pocket creation still works via the MCP tool "
            "surface even when no skill is installed."
        ),
    )
    auto_install_bundled_kb_scopes: bool = Field(
        default=True,
        description=(
            "On dashboard startup, mirror PocketPaw's pre-compiled kb-go "
            "scopes from ``pocketpaw/bundled_kb/_bundled/<scope>/`` into "
            "``~/.knowledge-base/<scope>/``. The bundle ships "
            "``ripple-recipes`` — pattern recipes (sales-pipeline, "
            "customer-support-app, recipe/how-to viewer) that the chat "
            "agent retrieves at pocket-creation time via the existing "
            "``_get_kb_context`` injection in bootstrap.context_builder. "
            "Idempotent — SHA-256 hash compare per file, no-op when the "
            "destination already matches. Set ``false`` to freeze a "
            "hand-customised scope or disable bundled KB entirely. KB "
            "retrieval is a non-critical enhancement: pocket creation "
            "still works via the MCP tool surface + the bundled skill."
        ),
    )
    deep_agents_skills: list[str] = Field(
        default_factory=list,
        description=(
            "Paths passed to deepagents `skills=` — directories or files loaded "
            "progressively by SkillsMiddleware (AGENTS.md-style). Empty disables."
        ),
    )
    deep_agents_memory: list[str] = Field(
        default_factory=list,
        description=(
            "Paths passed to deepagents `memory=` — files loaded by "
            "MemoryMiddleware for cross-thread recall. Empty disables."
        ),
    )

    # OpenCode Settings
    opencode_base_url: ExternalUrl = Field(
        default="http://localhost:4096",
        description="OpenCode server URL",
    )
    opencode_model: str = Field(
        default="",
        description="Model for OpenCode (provider/model format, e.g. anthropic/claude-sonnet-4-6)",
    )
    opencode_max_turns: int = Field(
        default=100, description="Max turns per query in OpenCode backend (0 = unlimited)"
    )

    # LiteLLM Proxy / SDK Configuration
    litellm_api_base: ExternalUrl = Field(
        default="http://localhost:4000",
        description="LiteLLM proxy server URL (used when any backend provider is set to 'litellm')",
    )
    litellm_api_key: str | None = Field(
        default=None,
        description="API key for LiteLLM proxy (the master key configured on the proxy)",
    )
    litellm_model: str = Field(
        default="",
        description=(
            "Default model for LiteLLM. Use provider/model format for direct mode "
            "(e.g. 'anthropic/claude-sonnet-4-6', 'huggingface/meta-llama/Llama-3-70b') "
            "or a model alias defined in LiteLLM proxy config.yaml"
        ),
    )
    litellm_max_tokens: int = Field(
        default=0,
        description="Max output tokens for LiteLLM models (0 = provider default)",
    )

    # LLM Configuration
    llm_provider: str = Field(
        default="auto",
        description=(
            "LLM provider: 'auto', 'ollama', 'openai', 'anthropic', "
            "'openai_compatible', 'gemini', 'litellm'"
        ),
    )
    ollama_host: str = Field(default="http://localhost:11434", description="Ollama API host")
    ollama_model: str = Field(default="llama3.2", description="Ollama model to use")
    openai_compatible_base_url: ExternalUrl = Field(
        default="",
        description="Base URL for OpenAI-compatible endpoint (LiteLLM, OpenRouter, vLLM, etc.)",
    )
    openai_compatible_api_key: str | None = Field(
        default=None, description="API key for OpenAI-compatible endpoint"
    )
    openai_compatible_model: str = Field(
        default="", description="Model name for OpenAI-compatible endpoint"
    )
    openai_compatible_max_tokens: int = Field(
        default=0,
        description="Max output tokens for OpenAI-compatible endpoint (0 = no limit)",
    )
    openrouter_api_key: str | None = Field(
        default=None, description="API key for OpenRouter (sk-or-v1-...)"
    )
    openrouter_model: str = Field(
        default="", description="Model slug for OpenRouter (e.g. anthropic/claude-sonnet-4-6)"
    )
    gemini_model: str = Field(default="gemini-3-pro-preview", description="Gemini model to use")
    openai_api_key: str | None = Field(default=None, description="OpenAI API key")
    openai_model: str = Field(default="gpt-5.2", description="OpenAI model to use")
    anthropic_api_key: str | None = Field(default=None, description="Anthropic API key")
    claude_code_oauth_token: str | None = Field(
        default=None,
        description=(
            "Claude Code OAuth token JSON (from `claude setup-token`). "
            "Allows Docker/headless use of Max/Pro subscription without an API key."
        ),
    )
    anthropic_model: str = Field(default="claude-sonnet-4-6", description="Anthropic model to use")

    # Memory Backend
    memory_backend: str = Field(
        default="file",
        description=(
            "Memory backend: 'file' (markdown + optional vector retrieval) or "
            "'mem0' (semantic with LLM)"
        ),
    )
    vectordb_path: str = Field(
        default="~/.pocketpaw/chroma_db", description="Storage path for the vector database"
    )
    vectordb_embedding_provider: str = Field(
        default="default",
        description=(
            "Embedding provider: 'default' (sentence-transformers), 'openai', 'huggingface'"
        ),
    )
    vectordb_embedding_model: str = Field(
        default="all-MiniLM-L6-v2",
        description=(
            "Embedding model name. For HuggingFace: any model ID"
            " (e.g. 'BAAI/bge-small-en-v1.5')."
            " For OpenAI: 'text-embedding-3-small'"
        ),
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
    mem0_ollama_base_url: ExternalUrl = Field(
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
    file_vector_enabled: bool = Field(
        default=False,
        description=(
            "Enable vector indexing and semantic retrieval for file memory backend "
            "(opt-in). Also enables knowledge graph extraction with conservative "
            "regex patterns and heuristic filtering."
        ),
    )
    vector_store: str = Field(
        default="sqlite-vec",
        description="Vector store for file memory backend: 'sqlite-vec', 'chromadb', or 'qdrant'",
    )
    embedding_provider: str = Field(
        default="ollama",
        description="Embedding provider for file memory backend (default: ollama)",
    )
    embedding_model: str = Field(
        default="nomic-embed-text",
        description="Embedding model for file memory semantic retrieval",
    )
    embedding_base_url: ExternalUrl = Field(
        default="http://localhost:11434",
        description="Embedding provider base URL (for ollama)",
    )

    # Session History Compaction
    compaction_recent_window: int = Field(
        default=10, gt=0, description="Number of recent messages to keep verbatim"
    )
    compaction_char_budget: int = Field(
        default=16000, gt=0, description="Max total chars for compacted history"
    )
    compaction_summary_chars: int = Field(
        default=300, gt=0, description="Max chars per older message one-liner extract"
    )
    compaction_llm_summarize: bool = Field(
        default=True,
        description="Use Haiku to summarize older messages for better context",
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
    discord_allowed_channel_ids: list[int] = Field(
        default_factory=list, description="Discord channel IDs the bot is restricted to"
    )
    discord_conversation_channel_ids: list[int] = Field(
        default_factory=list,
        description="Discord channels where the bot participates in group conversation",
    )
    discord_conversation_all_channels: bool = Field(
        default=False,
        description="Enable conversation mode in all server channels (overrides channel list)",
    )
    discord_conversation_exclude_channel_ids: list[int] = Field(
        default_factory=list,
        description="Channel IDs excluded from conversation mode (e.g. announcements)",
    )
    discord_bot_name: str = Field(
        default="Paw", description="Display name used by the bot in conversation"
    )
    discord_status_type: str = Field(
        default="online", description="Discord bot status: online, idle, dnd, invisible"
    )
    discord_activity_type: str = Field(
        default="", description="Discord bot activity: playing, watching, listening, competing"
    )
    discord_activity_text: str = Field(default="", description="Discord bot activity text")

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
    whatsapp_mode: Literal["", "personal", "business"] = Field(
        default="",
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
        default=24,
        gt=0,
        description="TTL in hours for HMAC session tokens issued via /api/auth/session",
    )
    api_cors_allowed_origins: list[str] = Field(
        default_factory=list,
        description="Additional CORS origins for external clients (e.g. tauri://localhost)",
    )
    a2a_trusted_agents: list[str] = Field(
        default_factory=list,
        description="Explicitly allowed A2A agent base URLs for task delegation (prevents SSRF)",
    )
    api_rate_limit_per_key: int = Field(
        default=60,
        gt=0,
        description="Max requests per minute per API key (token-bucket capacity)",
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

    # PII Protection
    pii_scan_enabled: bool = Field(
        default=False, description="Enable PII detection and masking (opt-in)"
    )
    pii_default_action: str = Field(
        default="mask", description="Default PII action: 'log', 'mask', or 'hash'"
    )
    pii_type_actions: dict[str, str] = Field(
        default_factory=dict,
        description="Per-type PII actions, e.g. {'email': 'mask', 'ssn': 'hash'}",
    )
    pii_scan_memory: bool = Field(
        default=True,
        description="Apply PII masking before writing to memory (when pii_scan_enabled)",
    )
    pii_scan_audit: bool = Field(
        default=True, description="Apply PII masking to audit log entries (when pii_scan_enabled)"
    )
    pii_scan_logs: bool = Field(
        default=True, description="Extend log scrubber with PII patterns (when pii_scan_enabled)"
    )

    # Chat Title Generation (Haiku-backed, first-message naming)
    chat_title_generation_enabled: bool = Field(
        default=True,
        description=(
            "Auto-generate a short title for a chat from its first user message."
            " Uses a Haiku model when an Anthropic API key is configured, and"
            " falls back to a trimmed excerpt of the first message otherwise."
            " Fires a session_titled SystemEvent on completion."
        ),
    )
    chat_title_model: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Model used by the chat title generator (Anthropic).",
    )

    # Smart Model Routing
    smart_routing_enabled: bool = Field(
        default=False,
        description=(
            "Enable automatic model selection based on task complexity"
            " (may conflict with Claude Code's own routing)"
        ),
    )
    model_tier_simple: str = Field(
        default="claude-haiku-4-5-20251001", description="Model for simple tasks (greetings, facts)"
    )
    model_tier_moderate: str = Field(
        default="claude-sonnet-4-6",
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

    # Budget Controls
    budget_monthly_usd: float = Field(
        default=0.0,
        ge=0.0,
        description="Monthly budget cap in USD. 0 = unlimited",
    )
    budget_warning_threshold: float = Field(
        default=0.8,
        gt=0.0,
        le=1.0,
        description="Warn when spend crosses this fraction of budget (0.8 = 80%)",
    )
    budget_auto_pause: bool = Field(
        default=True,
        description="Auto-pause agent processing when budget is exhausted",
    )
    budget_reset_day: int = Field(
        default=1,
        ge=1,
        le=28,
        description="Day of month when the budget window resets (1-28)",
    )
    per_agent_caps: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "Per-agent monthly budget caps in USD. Keys are agent backend names "
            "(e.g. 'claude_agent_sdk', 'openai_agents'). "
            "0 or missing = inherit global cap. Example: {'claude_agent_sdk': 5.0}"
        ),
    )
    budget_paused: bool = Field(
        default=False,
        exclude=True,  # excluded from JSON serialization
        # validation_alias points to an unreachable key so pydantic-settings
        # never populates this field from the environment
        # (POCKETPAW_BUDGET_PAUSED is ignored at load time).
        validation_alias=AliasChoices("__budget_paused_internal__"),
        description="Internal runtime flag — set programmatically, never from env",
    )
    budget_override_usd: float | None = Field(
        default=None,
        ge=0.0,
        description="Temporary budget override cap in USD (None = no override)",
    )
    budget_override_reason: str = Field(
        default="",
        description="Reason for the active budget override",
    )
    budget_override_expires_at: str | None = Field(
        default=None,
        description="ISO timestamp when the temporary budget override expires",
    )

    # Trace retention
    trace_retention_days: int = Field(
        default=30,
        ge=1,
        le=365,
        description="How many days of trace files to keep",
    )

    # Self-Audit Daemon
    self_audit_enabled: bool = Field(default=True, description="Enable daily self-audit daemon")
    self_audit_schedule: str = Field(
        default="0 3 * * *", description="Cron schedule for self-audit (default: 3 AM daily)"
    )

    # Health Engine
    health_check_on_startup: bool = Field(
        default=True, description="Run health checks when PocketPaw starts"
    )

    # User Preferences (set during onboarding)
    user_display_name: str = Field(default="", description="User's display name")
    user_avatar_emoji: str = Field(default="🐾", description="User's chosen avatar emoji")
    theme_preference: str = Field(
        default="system", description="Theme: 'light', 'dark', or 'system'"
    )
    notifications_enabled: bool = Field(default=True, description="Enable desktop notifications")
    sound_enabled: bool = Field(default=True, description="Enable notification sounds")
    tool_notifications_enabled: bool = Field(
        default=True, description="Show notifications for tool executions"
    )
    default_workspace_dir: str = Field(
        default="", description="Default working directory for the agent"
    )

    # OAuth
    google_oauth_client_id: str | None = Field(
        default=None, description="Google OAuth 2.0 client ID"
    )
    google_oauth_client_secret: str | None = Field(
        default=None, description="Google OAuth 2.0 client secret"
    )

    # Voice/TTS
    tts_provider: Literal["openai", "elevenlabs", "sarvam"] = Field(
        default="openai", description="TTS provider: 'openai', 'elevenlabs', or 'sarvam'"
    )
    elevenlabs_api_key: str | None = Field(default=None, description="ElevenLabs API key for TTS")
    tts_voice: str = Field(
        default="alloy", description="TTS voice name (OpenAI: alloy/echo/fable/onyx/nova/shimmer)"
    )
    tts_default_voice_elevenlabs: str = Field(
        default="pNInz6obpgDQGcFmaJgB", description="ElevenLabs default voice"
    )
    voice_reply_enabled: bool = Field(
        default=True,
        description="Auto-synthesize TTS voice reply when the inbound message was a voice note",
    )
    stt_provider: Literal["openai", "sarvam", "elevenlabs"] = Field(
        default="openai", description="STT provider: 'openai', 'elevenlabs', or 'sarvam'"
    )
    stt_model: str = Field(
        default="whisper-1",
        description=(
            "STT model (whisper-1 for OpenAI, scribe_v1 for ElevenLabs, saaras:v3 for Sarvam)"
        ),
    )

    # OCR
    ocr_provider: str = Field(
        default="openai", description="OCR provider: 'openai', 'sarvam', or 'tesseract'"
    )

    # Sarvam AI
    sarvam_api_key: str | None = Field(default=None, description="Sarvam AI API subscription key")
    sarvam_tts_model: str = Field(default="bulbul:v3", description="Sarvam TTS model")
    sarvam_tts_speaker: str = Field(default="shubh", description="Sarvam TTS speaker voice")
    sarvam_tts_language: str = Field(
        default="hi-IN", description="Sarvam TTS target language (BCP-47 code)"
    )
    sarvam_stt_model: str = Field(default="saaras:v3", description="Sarvam STT model")

    # Spotify
    spotify_client_id: str | None = Field(default=None, description="Spotify OAuth client ID")
    spotify_client_secret: str | None = Field(
        default=None, description="Spotify OAuth client secret"
    )

    # Signal
    signal_api_url: ExternalUrl = Field(
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

    # A2A Protocol
    a2a_enabled: bool = Field(
        default=False,
        description="Enable the A2A Protocol remote endpoints (allow external delegates)",
    )
    a2a_agent_name: str = Field(
        default="PocketPaw",
        description="Agent name advertised in the A2A Agent Card",
    )
    a2a_agent_description: str = Field(
        default="",
        description="Agent description for A2A Agent Card (empty = default)",
    )
    a2a_agent_version: str = Field(
        default="",
        description="Agent version for A2A Agent Card (empty = auto-detect from package)",
    )
    a2a_task_timeout: int = Field(
        default=120,
        description="Timeout in seconds for A2A task processing",
    )

    # MCP OAuth
    mcp_client_metadata_url: ExternalUrl = Field(
        default="",
        description="CIMD URL for MCP OAuth (optional, for servers without dynamic registration)",
    )

    # Identity / Multi-user
    owner_id: str = Field(
        default="",
        description="Global owner identifier (e.g. Telegram user ID). Empty = single-user mode.",
    )

    # Soul Protocol
    soul_enabled: bool = Field(
        default=True,
        description="Enable soul-protocol for persistent AI identity, memory, and emotion",
    )
    soul_name: str = Field(
        default="Paw",
        description="Name for the soul identity",
    )
    soul_archetype: str = Field(
        default="The Helpful Assistant",
        description="Soul archetype (e.g. 'The Coding Expert', 'The Compassionate Creator')",
    )
    soul_persona: str = Field(
        default="",
        description="Custom persona description for the soul (empty = auto-generated)",
    )
    # TODO: soul_values and soul_ocean are not yet exposed in the dashboard UI.
    #  Add controls in a Soul settings tab when the UI is built out.
    soul_values: list[str] = Field(
        default_factory=lambda: ["helpfulness", "precision", "privacy"],
        description="Core values for the soul identity",
    )
    soul_ocean: dict[str, float] = Field(
        default_factory=lambda: {
            "openness": 0.7,
            "conscientiousness": 0.85,
            "extraversion": 0.5,
            "agreeableness": 0.8,
            "neuroticism": 0.2,
        },
        description="OCEAN Big Five personality traits (0.0-1.0)",
    )
    soul_communication: dict[str, str] = Field(
        default_factory=lambda: {"warmth": "medium", "verbosity": "low"},
        description="Communication style settings for the soul",
    )
    soul_path: str = Field(
        default="",
        description="Path to .soul file (empty = ~/.pocketpaw/soul/)",
    )
    soul_auto_save_interval: int = Field(
        default=300,
        description="Auto-save soul state interval in seconds (0 = disabled)",
    )
    soul_biorhythm: dict[str, float] = Field(
        default_factory=lambda: {
            "energy_drain_rate": 0.02,
            "mood_inertia": 0.8,
            "tired_threshold": 0.3,
            "auto_regen": 0.01,
        },
        description=(
            "Biorhythm configuration for soul energy/mood dynamics (v0.2.4+). "
            "energy_drain_rate: how fast energy depletes per interaction. "
            "mood_inertia: resistance to mood change (0-1). "
            "tired_threshold: energy level that triggers fatigue. "
            "auto_regen: passive energy recovery rate."
        ),
    )
    kb_scope: str = Field(
        default="",
        description=(
            "DEPRECATED: single-scope back-compat shim. Prefer ``kb_scopes`` "
            "(list). When ``kb_scopes`` is empty and ``kb_scope`` is set, "
            "the value is copied into ``kb_scopes`` and a DeprecationWarning "
            "is emitted. Set via POCKETPAW_KB_SCOPE."
        ),
    )
    kb_scopes: list[str] = Field(
        default_factory=list,
        description=(
            "Ordered list of kb-go scopes to query when building the agent "
            "system prompt. Each scope receives a slice of the total limit; "
            "results are concatenated under per-scope headers. Set via "
            "POCKETPAW_KB_SCOPES as a JSON array (e.g. "
            '["workspace:w1","agent:a1"]).'
        ),
    )
    kb_binary: str = Field(
        default="kb",
        description="Path to the kb binary (default: `kb` on PATH)",
    )
    kb_limit: int = Field(
        default=3,
        description="Number of top articles to inject from kb search (default: 3)",
    )
    ripple_manifest_url: str = Field(
        default="http://localhost:5174/manifest.json",
        description=(
            "URL to the Ripple UI manifest (widget specs). Defaults to the "
            "local ripple dev server while @ripple-ui/svelte is unreleased; "
            "swap to "
            "https://cdn.jsdelivr.net/npm/@ripple-ui/svelte@latest/dist/manifest.json "
            "(or any pinned version) once published."
        ),
    )
    ripple_manifest_ttl_seconds: int = Field(
        default=86400,
        description="TTL in seconds for cached Ripple manifest (default: 24h)",
    )

    # File extraction chain (Phase 1, "Files as Knowledge")
    extraction_chain: list[str] = Field(
        default_factory=lambda: ["local"],
        description=(
            "Ordered list of extraction adapter names "
            "(e.g. ['gemini-flash', 'local']). The chain runs first-match-wins "
            "per MIME, with offline fallback to 'local'. Set via "
            "POCKETPAW_EXTRACTION_CHAIN as a JSON array."
        ),
    )
    extraction_per_mime: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Per-MIME adapter override map (e.g. {'image/png': 'gemini-flash'}). "
            "Wins over the chain order. Set via POCKETPAW_EXTRACTION_PER_MIME "
            "as a JSON object."
        ),
    )
    extraction_offline_fallback: str = Field(
        default="local",
        description=(
            "Adapter name used when the chosen adapter requires network and "
            "the host is offline. Today the chain hardcodes LocalExtractor as "
            "fallback; this setting reserves the env key for future overrides."
        ),
    )
    gemini_api_key: str | None = Field(
        default=None,
        description=(
            "Google Gemini API key for the gemini-flash extraction adapter. "
            "Read from POCKETPAW_GEMINI_API_KEY. When unset, the gemini-flash "
            "adapter is silently skipped during chain construction."
        ),
    )

    # Embedding adapter (Phase 2, "Files as Knowledge" Stage 2.D)
    kb_vectors_enabled: bool = Field(
        default=False,
        description=(
            "Master switch for the vector-embedding pipeline. When False the "
            "FileReady listener stops after text-ingest and the chat path "
            "skips interleaved-image queries. Set via POCKETPAW_KB_VECTORS_ENABLED."
        ),
    )
    embedding_adapter: str = Field(
        default="",
        description=(
            "Embedding adapter name. Empty disables embeddings even when "
            "kb_vectors_enabled is True. Supported: "
            "'vertex-gemini-embedding-2' (preview, 3072-dim, multimodal), "
            "'vertex-mm-001' (GA, 1408-dim, text+image). Set via "
            "POCKETPAW_EMBEDDING_ADAPTER."
        ),
    )
    embedding_dim: int = Field(
        default=1024,
        gt=0,
        description=(
            "Target output dim. vertex-gemini-embedding-2 uses Matryoshka "
            "truncation; vertex-mm-001 snaps to the closest valid native "
            "dim (128/256/512/1408). All vectors in a single kb-go scope "
            "must agree on this value. Set via POCKETPAW_EMBEDDING_DIM."
        ),
    )
    embedding_monthly_cap_usd: float = Field(
        default=10.0,
        ge=0,
        description=(
            "Soft monthly USD cap for embedding spend. When the running "
            "total would exceed this, the listener falls back to "
            "extraction-only (text still ingests, vector skipped). 0 "
            "disables the cap. Persisted at ~/.pocketpaw/embedding_cost.json. "
            "NOT a billing source — real billing comes from the provider's "
            "dashboard. Set via POCKETPAW_EMBEDDING_MONTHLY_CAP_USD."
        ),
    )
    vertex_project_id: str | None = Field(
        default=None,
        description=(
            "GCP project id for vertex-mm-001 (the multimodalembedding@001 "
            "adapter). When unset, the adapter is silently skipped during "
            "factory construction. Set via POCKETPAW_VERTEX_PROJECT_ID."
        ),
    )
    vertex_location: str | None = Field(
        default=None,
        description=(
            "GCP region for vertex-mm-001 (default: us-central1 when "
            "unset). Set via POCKETPAW_VERTEX_LOCATION."
        ),
    )

    soul_cognitive_model: str = Field(
        default="",
        description=(
            "Model to use for soul cognitive processing (sentiment, significance, "
            "fact/entity extraction). Empty = use main agent backend. Set to a cheaper "
            "model like 'claude-haiku-4-5-20251001' to reduce cost. Requires anthropic SDK."
        ),
    )

    notification_channels: list[str] = Field(
        default_factory=list,
        description="Targets for autonomous messages, e.g. ['telegram:12345', 'discord:98765']",
    )

    # Status API
    status_api_key: str = Field(
        default="",
        description="Optional API key for the agent status endpoint. Leave empty to skip auth.",
    )

    # Media Downloads
    media_download_dir: str = Field(
        default="", description="Custom media download dir (default: ~/.pocketpaw/media/)"
    )
    media_max_file_size_mb: int = Field(
        default=50, ge=0, description="Max media file size in MB (0 = unlimited)"
    )

    # UX
    welcome_hint_enabled: bool = Field(
        default=True,
        description="Send a one-time welcome hint on first interaction in non-web channels",
    )

    # Channel Autostart
    channel_autostart: dict[str, bool] = Field(
        default_factory=dict,
        description="Per-channel autostart on dashboard launch (missing keys default to True)",
    )

    # Concurrency
    max_concurrent_conversations: int = Field(
        default=5, gt=0, description="Max parallel conversations processed simultaneously"
    )

    # Composio — MCP-direct tool provider for the parent cloud chat agent.
    # Wired into src/pocketpaw/agents/claude_sdk.py::_get_mcp_servers; the
    # pocket specialist does NOT receive Composio MCP. When api_key is set,
    # composio_enterprise_id is required to namespace the per-user Composio
    # user_id (avoids collisions across PocketPaw enterprise deployments
    # that share one Composio org).
    composio_api_key: str | None = Field(
        default=None, description="Composio API key (enables Composio MCP for the parent agent)"
    )
    composio_base_url: str | None = Field(
        default=None,
        description="Composio base URL. None = Composio cloud; set for self-hosted runtime.",
    )
    # ``NoDecode`` keeps pydantic-settings from trying to JSON-parse the raw
    # env string; the ``field_validator`` below handles CSV → list[str].
    composio_toolkits: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description=(
            "Allow-list of Composio toolkit slugs (e.g. 'gmail,slack,github'). "
            "Comma-separated when set via env. Empty = fail closed (no toolkits exposed)."
        ),
    )
    composio_enterprise_id: str | None = Field(
        default=None,
        description=(
            "Namespace prefix for Composio user_id (f'{enterprise_id}:{user_id}'). "
            "Required when composio_api_key is set."
        ),
    )
    composio_mcp_url_ttl_seconds: int = Field(
        default=3600,
        gt=0,
        description=(
            "How long per-user Composio tools are cached in-process before "
            "re-fetching via the provider's ``composio.create(user_id=...)`` + "
            "``session.tools()``. The Composio call is a network round-trip "
            "and the per-user toolset rarely changes mid-session, so caching "
            "covers the common case. Default: 1h."
        ),
    )
    composio_connect_link_inline: bool = Field(
        default=True,
        description=(
            "When True, Composio 'needs auth' responses render as an inline Ripple button "
            "instead of a raw URL in the chat. Set False to disable if detection is brittle."
        ),
    )

    @field_validator("composio_toolkits", mode="before")
    @classmethod
    def _parse_composio_toolkits_csv(cls, v: object) -> object:
        """Accept comma-separated env values (e.g. 'gmail, slack ,github').

        pydantic-settings normally requires JSON for list fields; this
        before-validator lets ops set the allow-list as plain CSV in
        ``POCKETPAW_COMPOSIO_TOOLKITS`` without quoting brackets.
        """
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @model_validator(mode="after")
    def _validate_composio_invariants(self) -> Settings:
        """Enforce composio_api_key → composio_enterprise_id required.

        Without the enterprise_id namespace, two PocketPaw deployments
        sharing one Composio org would collide on user_id space. Fail at
        startup rather than at first tool call.
        """
        if self.composio_api_key and not self.composio_enterprise_id:
            raise ValueError(
                "composio_enterprise_id is required when composio_api_key is set "
                "(POCKETPAW_COMPOSIO_ENTERPRISE_ID). Prevents user_id collisions "
                "across enterprise deployments sharing one Composio org."
            )
        return self

    @model_validator(mode="after")
    def _migrate_kb_scope(self) -> Settings:
        """Copy deprecated single ``kb_scope`` into ``kb_scopes`` once.

        When a host has only the legacy ``POCKETPAW_KB_SCOPE`` set we keep
        their KB injection working: the string is appended to ``kb_scopes``
        and a :class:`DeprecationWarning` nudges them to switch. If both
        keys are populated the new list wins and the legacy string is
        ignored (no surprise merging).
        """
        if not self.kb_scopes and self.kb_scope:
            warnings.warn(
                "POCKETPAW_KB_SCOPE is deprecated; use POCKETPAW_KB_SCOPES "
                "(list, e.g. POCKETPAW_KB_SCOPES='[\"workspace:w1\"]')",
                DeprecationWarning,
                stacklevel=2,
            )
            self.kb_scopes = [self.kb_scope]
        return self

    def save(self) -> None:
        """Save settings to config file.

        Non-secret fields go to config.json. Secret fields (API keys, tokens)
        go to the encrypted credential store.

        Uses model_dump() to automatically include all fields — no need to
        manually list every field when new settings are added.

        Runs format validation on API keys before saving; logs warnings but
        never blocks or raises.
        """
        # TODO: When adding new sensitive fields, ensure they are included in SECRET_FIELDS in
        # pocketpaw/credentials.py to prevent plaintext storage.
        from pocketpaw.credentials import SECRET_FIELDS, get_credential_store

        config_path = get_config_path()

        # Load existing config to preserve secret values if current is empty
        existing: dict = {}
        if config_path.exists():
            try:
                existing = json.loads(config_path.read_text())
            except (json.JSONDecodeError, Exception):
                pass

        # Dump all fields with JSON-mode serialization (converts Path→str, etc.)
        all_fields = self.model_dump(mode="json")

        # For secret fields, preserve existing value if current is empty/None
        for key in SECRET_FIELDS:
            if key in all_fields and not all_fields[key] and existing.get(key):
                all_fields[key] = existing[key]

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
    def load(cls) -> Settings:
        """Load settings from config file + encrypted credential store.

        Set ``POCKETPAW_IGNORE_CONFIG_JSON=true`` to skip config.json
        entirely. Useful when ``.env`` is the source of truth and you
        don't want unset fields to silently inherit stale dashboard
        values. Secrets from the encrypted credential store still load.
        """
        from pocketpaw.credentials import SECRET_FIELDS, get_credential_store

        _migrate_plaintext_keys()

        ignore_json = os.environ.get("POCKETPAW_IGNORE_CONFIG_JSON", "").strip().lower() in (
            "1",
            "true",
            "yes",
            "on",
        )

        config_path = get_config_path()
        data: dict = {}
        if config_path.exists() and not ignore_json:
            try:
                data = json.loads(config_path.read_text())
            except (json.JSONDecodeError, Exception):
                pass

        store = get_credential_store()
        secrets = store.get_all()
        for field in SECRET_FIELDS:
            if field in secrets and secrets[field]:
                data[field] = secrets[field]

        env_prefix = cls.model_config.get("env_prefix", "")
        for field in list(data.keys()):
            if os.environ.get(f"{env_prefix}{field.upper()}") is not None:
                data.pop(field, None)

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
    from pocketpaw.credentials import SECRET_FIELDS, get_credential_store

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
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, Exception):
        return

    store = get_credential_store()
    migrated_count = 0

    for field in SECRET_FIELDS:
        value = data.get(field)
        if value and isinstance(value, str):
            store.set(field, value)
            migrated_count += 1
            # Remove plaintext secret from config to prevent leakage
            del data[field]

    if migrated_count:
        logger.info("Copied %d secret(s) from config to encrypted store.", migrated_count)
        # Save the cleaned config back to remove plaintext secrets
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        _chmod_safe(config_path, 0o600)

    _MIGRATION_DONE_PATH.write_text("1")
    _chmod_safe(_MIGRATION_DONE_PATH, 0o600)
