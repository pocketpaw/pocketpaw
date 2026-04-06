"""WebSocket handler for PocketPaw dashboard.

Extracted from dashboard.py — contains the main websocket_handler() function
and helper functions: handle_tool(), handle_file_navigation(), handle_file_browse().

Changes:
  - 2026-03-08: Fix switch_session silently dropping requests for non-existent or
    path-traversal session IDs (caused WS hang). Now sends empty session_history response.
"""

import asyncio
import base64
import logging
import uuid
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

from pocketpaw.config import Settings, get_access_token, validate_api_keys
from pocketpaw.dashboard_state import (
    _settings_lock,
    active_connections,
    agent_loop,
    ws_adapter,
)
from pocketpaw.llm.client import resolve_backend_env
from pocketpaw.memory import get_memory_manager
from pocketpaw.scheduler import get_scheduler
from pocketpaw.security.rate_limiter import ws_limiter
from pocketpaw.security.session_tokens import verify_session_token
from pocketpaw.skills import SkillExecutor, get_skill_loader

logger = logging.getLogger(__name__)


def _api_key_response(message: str, warnings: list[str] | None = None) -> dict:
    """Build a standard ``api_key_saved`` WS response, optionally with warnings."""
    resp: dict = {"type": "api_key_saved", "content": message}
    if warnings:
        resp["warnings"] = warnings
    return resp


async def websocket_handler(
    websocket: WebSocket,
    token: str | None,
    resume_session: str | None,
    *,
    _is_genuine_localhost_fn=None,
    _get_access_token_fn=None,
):
    """Core WebSocket handler logic.

    Parameters
    ----------
    _is_genuine_localhost_fn:
        Callable to check genuine localhost. Injected from dashboard.py
        to avoid circular import with auth helpers.
    _get_access_token_fn:
        Callable returning the current access token. Injected from
        dashboard.py so mock patches in tests take effect.
    """
    from pocketpaw.daemon import get_daemon

    logger.info(
        "WS handler called: client=%s, has_token=%s, has_cookie=%s, localhost_fn=%s",
        websocket.client,
        token is not None,
        "pocketpaw_session" in (websocket.headers.get("cookie") or ""),
        _is_genuine_localhost_fn is not None,
    )

    # Rate limit WebSocket connections
    client_ip = websocket.client.host if websocket.client else "unknown"
    if not ws_limiter.allow(client_ip):
        logger.warning("WS rate limited: %s", client_ip)
        await websocket.close(code=4029, reason="Too many connections")
        return

    _get_token = _get_access_token_fn or get_access_token
    expected_token = _get_token()

    def _token_valid(t: str | None) -> bool:
        if not t:
            return False
        if t == expected_token:
            return True
        # Accept session tokens (format: "expires:hmac")
        if ":" in t and verify_session_token(t, expected_token):
            return True
        # Accept API keys (pp_* prefix)
        if t.startswith("pp_") and not t.startswith("ppat_"):
            try:
                from pocketpaw.api.api_keys import get_api_key_manager

                if get_api_key_manager().verify(t) is not None:
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("API key verification failed: %s", exc)
        # Accept OAuth2 access tokens (ppat_* prefix)
        if t.startswith("ppat_"):
            try:
                from pocketpaw.api.oauth2.server import get_oauth_server

                if get_oauth_server().verify_access_token(t) is not None:
                    return True
            except Exception as exc:  # noqa: BLE001
                logger.debug("OAuth token verification failed: %s", exc)
        return False

    # Check HTTP-only session cookie
    cookie_token = websocket.cookies.get("pocketpaw_session")
    logger.info(
        "WS auth: cookie=%s, token_valid=%s, cookie_valid=%s",
        cookie_token[:20] + "..." if cookie_token else "none",
        _token_valid(token),
        _token_valid(cookie_token),
    )
    if not _token_valid(token) and _token_valid(cookie_token):
        token = cookie_token  # Use cookie token for subsequent checks

    # Check Authorization header (non-browser clients may send it)
    if not _token_valid(token):
        auth_header = websocket.headers.get("authorization", "")
        if auth_header.startswith("Bearer "):
            bearer = auth_header.removeprefix("Bearer ").strip()
            if _token_valid(bearer):
                token = bearer

    # Check Sec-WebSocket-Protocol for token (common pattern for browser clients)
    if not _token_valid(token):
        protocols = websocket.headers.get("sec-websocket-protocol", "")
        for proto in protocols.split(","):
            candidate = proto.strip()
            if candidate.startswith(("ppat_", "pp_")) and _token_valid(candidate):
                token = candidate
                break

    # Allow genuine localhost bypass for WebSocket (not tunneled proxies)
    is_localhost = _is_genuine_localhost_fn(websocket) if _is_genuine_localhost_fn else False
    logger.info(
        "WS auth final: token_valid=%s, is_localhost=%s",
        _token_valid(token),
        is_localhost,
    )

    if not _token_valid(token) and not is_localhost:
        logger.warning(
            "WebSocket auth failed: token=%s, cookie=%s, localhost=%s",
            "present" if token else "missing",
            "present" if cookie_token else "missing",
            is_localhost,
        )
        await websocket.close(
            code=4003,
            reason="Unauthorized: invalid or missing authentication token",
        )
        return

    await websocket.accept()

    # Track connection
    active_connections.append(websocket)

    # Generate session ID for bus (or resume existing)
    chat_id = str(uuid.uuid4())

    # Resume session if requested
    resumed = False
    if resume_session:
        # Parse safe_key to extract channel and raw UUID
        parts = resume_session.split("_", 1)
        if len(parts) == 2 and parts[0] == "websocket":
            raw_id = parts[1]
            # Verify session file exists and path stays under sessions dir
            sessions_dir = Path.home() / ".pocketpaw" / "memory" / "sessions"
            session_file = sessions_dir / f"{resume_session}.json"
            try:
                session_file.resolve().relative_to(sessions_dir.resolve())
            except ValueError:
                logger.warning("Path traversal attempt in resume_session: %s", resume_session)
                resume_session = None  # fall through to fresh session
            else:
                if session_file.exists():
                    chat_id = raw_id
                    resumed = True

    await ws_adapter.register_connection(websocket, chat_id)

    # Build session safe_key for frontend
    safe_key = f"websocket_{chat_id}"

    # Load settings
    settings = Settings.load()

    # Legacy state
    agent_active = False

    try:
        # Send welcome notification with session info
        await websocket.send_json(
            {
                "type": "connection_info",
                "content": "Connected to PocketPaw",
                "id": safe_key,
            }
        )

        # If resuming, send session history
        if resumed:
            session_key = f"websocket:{chat_id}"
            try:
                manager = get_memory_manager()
                history = await manager.get_session_history(session_key, limit=100)
                await websocket.send_json(
                    {
                        "type": "session_history",
                        "session_id": safe_key,
                        "messages": history,
                    }
                )
            except Exception as e:
                logger.warning("Failed to load session history for resume: %s", e)

        while True:
            data = await websocket.receive_json()
            action = data.get("action")

            # Handle chat via MessageBus
            if action == "chat":
                log_msg = (
                    f"\u26a1 Processing message with Backend: {settings.agent_backend}"
                    f" (Provider: {settings.llm_provider})"
                )
                logger.info(log_msg)

                await ws_adapter.handle_message(chat_id, data)

            # Stop in-flight response
            elif action == "stop":
                session_key = f"websocket:{chat_id}"
                cancelled = await agent_loop.cancel_session(session_key)
                if not cancelled:
                    await websocket.send_json({"type": "stream_end"})

            # Session switching
            elif action == "switch_session":
                session_id = data.get("session_id", "")
                # Parse safe_key: "websocket_<uuid>"
                parts = session_id.split("_", 1)
                if len(parts) == 2 and parts[0] == "websocket":
                    raw_id = parts[1]
                    channel_prefix = parts[0]
                    new_session_key = f"{channel_prefix}:{raw_id}"

                    # Verify session file exists and path stays under sessions dir
                    sessions_dir = Path.home() / ".pocketpaw" / "memory" / "sessions"
                    session_file = sessions_dir / f"{session_id}.json"
                    try:
                        session_file.resolve().relative_to(sessions_dir.resolve())
                    except ValueError:
                        logger.warning("Path traversal attempt in switch_session: %s", session_id)
                        await websocket.send_json(
                            {
                                "type": "session_history",
                                "session_id": session_id,
                                "messages": [],
                            }
                        )
                        continue
                    if not session_file.exists():
                        await websocket.send_json(
                            {
                                "type": "session_history",
                                "session_id": session_id,
                                "messages": [],
                            }
                        )
                        continue

                    # Unregister old connection, register with new chat_id
                    await ws_adapter.unregister_connection(chat_id)
                    chat_id = raw_id
                    await ws_adapter.register_connection(websocket, chat_id)

                    # Load and send history
                    try:
                        manager = get_memory_manager()
                        history = await manager.get_session_history(new_session_key, limit=100)
                        await websocket.send_json(
                            {
                                "type": "session_history",
                                "session_id": session_id,
                                "messages": history,
                            }
                        )
                    except Exception as e:
                        logger.warning("Failed to load session history: %s", e)
                        await websocket.send_json(
                            {
                                "type": "session_history",
                                "session_id": session_id,
                                "messages": [],
                            }
                        )

            # New session
            elif action == "new_session":
                await ws_adapter.unregister_connection(chat_id)
                chat_id = str(uuid.uuid4())
                await ws_adapter.register_connection(websocket, chat_id)
                safe_key = f"websocket_{chat_id}"
                await websocket.send_json({"type": "new_session", "id": safe_key})

            # Legacy/Other actions
            elif action == "tool":
                tool = data.get("tool")
                await handle_tool(websocket, tool, settings, data)

            # Handle agent toggle (Legacy router control)
            elif action == "toggle_agent":
                agent_active = data.get("active", False)
                await websocket.send_json(
                    {
                        "type": "notification",
                        "content": (
                            f"Legacy Mode: {'ON' if agent_active else 'OFF'} (Bus is always active)"
                        ),
                    }
                )

            # Handle settings update
            elif action == "settings":
                async with _settings_lock:
                    settings.agent_backend = data.get("agent_backend", settings.agent_backend)
                    if data.get("claude_sdk_provider"):
                        settings.claude_sdk_provider = data["claude_sdk_provider"]
                    if "claude_sdk_model" in data:
                        settings.claude_sdk_model = data["claude_sdk_model"]
                    if "claude_sdk_max_turns" in data:
                        val = data["claude_sdk_max_turns"]
                        if isinstance(val, int | float) and 1 <= val <= 200:
                            settings.claude_sdk_max_turns = int(val)
                    # OpenAI Agents
                    if data.get("openai_agents_provider"):
                        settings.openai_agents_provider = data["openai_agents_provider"]
                    if "openai_agents_model" in data:
                        settings.openai_agents_model = data["openai_agents_model"]
                    if "openai_agents_max_turns" in data:
                        val = data["openai_agents_max_turns"]
                        if isinstance(val, int | float) and 1 <= val <= 200:
                            settings.openai_agents_max_turns = int(val)
                    # Google ADK
                    if data.get("google_adk_provider"):
                        settings.google_adk_provider = data["google_adk_provider"]
                    if "google_adk_model" in data:
                        settings.google_adk_model = data["google_adk_model"]
                    if "google_adk_max_turns" in data:
                        val = data["google_adk_max_turns"]
                        if isinstance(val, int | float) and 1 <= val <= 200:
                            settings.google_adk_max_turns = int(val)
                    # Codex CLI
                    if "codex_cli_model" in data:
                        settings.codex_cli_model = data["codex_cli_model"]
                    if "codex_cli_max_turns" in data:
                        val = data["codex_cli_max_turns"]
                        if isinstance(val, int | float) and 1 <= val <= 200:
                            settings.codex_cli_max_turns = int(val)
                    # Copilot SDK
                    if data.get("copilot_sdk_provider"):
                        settings.copilot_sdk_provider = data["copilot_sdk_provider"]
                    if "copilot_sdk_model" in data:
                        settings.copilot_sdk_model = data["copilot_sdk_model"]
                    if "copilot_sdk_max_turns" in data:
                        val = data["copilot_sdk_max_turns"]
                        if isinstance(val, int | float) and 1 <= val <= 200:
                            settings.copilot_sdk_max_turns = int(val)
                    # Deep Agents
                    if "deep_agents_model" in data:
                        settings.deep_agents_model = data["deep_agents_model"]
                    if "deep_agents_max_turns" in data:
                        val = data["deep_agents_max_turns"]
                        if isinstance(val, int | float) and 1 <= val <= 200:
                            settings.deep_agents_max_turns = int(val)
                    # OpenCode
                    if "opencode_base_url" in data:
                        settings.opencode_base_url = data["opencode_base_url"]
                    if "opencode_model" in data:
                        settings.opencode_model = data["opencode_model"]
                    if "opencode_max_turns" in data:
                        val = data["opencode_max_turns"]
                        if isinstance(val, int | float) and 1 <= val <= 200:
                            settings.opencode_max_turns = int(val)
                    settings.llm_provider = data.get("llm_provider", settings.llm_provider)
                    if data.get("ollama_host"):
                        settings.ollama_host = data["ollama_host"]
                    if data.get("ollama_model"):
                        settings.ollama_model = data["ollama_model"]
                    if data.get("anthropic_model"):
                        settings.anthropic_model = data.get("anthropic_model")
                    if data.get("openai_compatible_base_url") is not None:
                        settings.openai_compatible_base_url = data["openai_compatible_base_url"]
                    if data.get("openai_compatible_api_key"):
                        settings.openai_compatible_api_key = data["openai_compatible_api_key"]
                    if data.get("openai_compatible_model") is not None:
                        settings.openai_compatible_model = data["openai_compatible_model"]
                    if "openai_compatible_max_tokens" in data:
                        val = data["openai_compatible_max_tokens"]
                        if isinstance(val, int | float) and 0 <= val <= 1000000:
                            settings.openai_compatible_max_tokens = int(val)
                    if data.get("gemini_model"):
                        settings.gemini_model = data["gemini_model"]
                    # LiteLLM
                    if data.get("litellm_api_base") is not None:
                        settings.litellm_api_base = data["litellm_api_base"]
                    if data.get("litellm_api_key") is not None:
                        settings.litellm_api_key = data["litellm_api_key"]
                    if data.get("litellm_model") is not None:
                        settings.litellm_model = data["litellm_model"]
                    if "litellm_max_tokens" in data:
                        val = data["litellm_max_tokens"]
                        if isinstance(val, int | float) and 0 <= val <= 1000000:
                            settings.litellm_max_tokens = int(val)
                    if "bypass_permissions" in data:
                        settings.bypass_permissions = bool(data.get("bypass_permissions"))
                    if data.get("web_search_provider"):
                        settings.web_search_provider = data["web_search_provider"]
                    if data.get("url_extract_provider"):
                        settings.url_extract_provider = data["url_extract_provider"]
                    if "injection_scan_enabled" in data:
                        settings.injection_scan_enabled = bool(data["injection_scan_enabled"])
                    if "injection_scan_llm" in data:
                        settings.injection_scan_llm = bool(data["injection_scan_llm"])
                    if "pii_scan_enabled" in data:
                        settings.pii_scan_enabled = bool(data["pii_scan_enabled"])
                    if data.get("pii_default_action"):
                        settings.pii_default_action = data["pii_default_action"]
                    if "pii_scan_memory" in data:
                        settings.pii_scan_memory = bool(data["pii_scan_memory"])
                    if "pii_scan_audit" in data:
                        settings.pii_scan_audit = bool(data["pii_scan_audit"])
                    if "pii_scan_logs" in data:
                        settings.pii_scan_logs = bool(data["pii_scan_logs"])
                    if data.get("tool_profile"):
                        settings.tool_profile = data["tool_profile"]
                    if "plan_mode" in data:
                        settings.plan_mode = bool(data["plan_mode"])
                    if "plan_mode_tools" in data:
                        raw = data["plan_mode_tools"]
                        if isinstance(raw, str):
                            settings.plan_mode_tools = [
                                t.strip() for t in raw.split(",") if t.strip()
                            ]
                        elif isinstance(raw, list):
                            settings.plan_mode_tools = raw
                    if "smart_routing_enabled" in data:
                        settings.smart_routing_enabled = bool(data["smart_routing_enabled"])
                    if data.get("model_tier_simple"):
                        settings.model_tier_simple = data["model_tier_simple"]
                    if data.get("model_tier_moderate"):
                        settings.model_tier_moderate = data["model_tier_moderate"]
                    if data.get("model_tier_complex"):
                        settings.model_tier_complex = data["model_tier_complex"]
                    if data.get("tts_provider"):
                        settings.tts_provider = data["tts_provider"]
                    if "tts_voice" in data:
                        settings.tts_voice = data["tts_voice"]
                    if data.get("stt_provider"):
                        settings.stt_provider = data["stt_provider"]
                    if data.get("stt_model"):
                        settings.stt_model = data["stt_model"]
                    if data.get("ocr_provider"):
                        settings.ocr_provider = data["ocr_provider"]
                    if data.get("sarvam_tts_language"):
                        settings.sarvam_tts_language = data["sarvam_tts_language"]
                    if "self_audit_enabled" in data:
                        settings.self_audit_enabled = bool(data["self_audit_enabled"])
                    if data.get("self_audit_schedule"):
                        settings.self_audit_schedule = data["self_audit_schedule"]
                    # Memory settings
                    if data.get("memory_backend"):
                        settings.memory_backend = data["memory_backend"]
                    if "file_vector_enabled" in data:
                        settings.file_vector_enabled = bool(data["file_vector_enabled"])
                    if data.get("vector_store"):
                        settings.vector_store = data["vector_store"]
                    if data.get("embedding_provider"):
                        settings.embedding_provider = data["embedding_provider"]
                    if data.get("embedding_model"):
                        settings.embedding_model = data["embedding_model"]
                    if data.get("embedding_base_url"):
                        settings.embedding_base_url = data["embedding_base_url"]
                    if "mem0_auto_learn" in data:
                        settings.mem0_auto_learn = bool(data["mem0_auto_learn"])
                    if data.get("mem0_llm_provider"):
                        settings.mem0_llm_provider = data["mem0_llm_provider"]
                    if data.get("mem0_llm_model"):
                        settings.mem0_llm_model = data["mem0_llm_model"]
                    if data.get("mem0_embedder_provider"):
                        settings.mem0_embedder_provider = data["mem0_embedder_provider"]
                    if data.get("mem0_embedder_model"):
                        settings.mem0_embedder_model = data["mem0_embedder_model"]
                    if data.get("mem0_vector_store"):
                        settings.mem0_vector_store = data["mem0_vector_store"]
                    if data.get("mem0_ollama_base_url"):
                        settings.mem0_ollama_base_url = data["mem0_ollama_base_url"]
                    # Web server host/port
                    if "web_host" in data:
                        settings.web_host = data["web_host"]
                    if "web_port" in data:
                        val = data["web_port"]
                        if isinstance(val, int | float) and 1 <= val <= 65535:
                            settings.web_port = int(val)
                    # A2A Protocol
                    if "a2a_enabled" in data:
                        settings.a2a_enabled = bool(data["a2a_enabled"])
                    if data.get("a2a_agent_name"):
                        settings.a2a_agent_name = data["a2a_agent_name"]
                    if "a2a_agent_description" in data:
                        settings.a2a_agent_description = data.get("a2a_agent_description", "")
                    if "a2a_task_timeout" in data:
                        val = data["a2a_task_timeout"]
                        if isinstance(val, int | float) and 10 <= val <= 600:
                            settings.a2a_task_timeout = int(val)
                    if "a2a_trusted_agents" in data:
                        val = data["a2a_trusted_agents"]
                        if isinstance(val, list):
                            settings.a2a_trusted_agents = [
                                s.strip() for s in val if isinstance(s, str) and s.strip()
                            ]

                    # Soul Protocol
                    if "soul_enabled" in data:
                        settings.soul_enabled = bool(data["soul_enabled"])
                    if data.get("soul_name"):
                        settings.soul_name = data["soul_name"]
                    if data.get("soul_archetype"):
                        settings.soul_archetype = data["soul_archetype"]
                    if "soul_persona" in data:
                        settings.soul_persona = data.get("soul_persona", "")
                    if "soul_auto_save_interval" in data:
                        val = data["soul_auto_save_interval"]
                        if isinstance(val, int | float) and 0 <= val <= 3600:
                            settings.soul_auto_save_interval = int(val)
                    if "soul_biorhythm" in data:
                        val = data["soul_biorhythm"]
                        if isinstance(val, dict):
                            allowed = {
                                "energy_drain_rate",
                                "mood_inertia",
                                "tired_threshold",
                                "auto_regen",
                            }
                            clean = {}
                            for k, v in val.items():
                                if k in allowed and isinstance(v, int | float):
                                    clean[k] = float(max(0.0, min(1.0, v)))
                            if clean:
                                settings.soul_biorhythm = {
                                    **settings.soul_biorhythm,
                                    **clean,
                                }
                    warnings = validate_api_keys(settings)
                    settings.save()

                # Sync env vars so running backends see updated keys
                resolve_backend_env(settings, force=True)

                # Reset the agent loop's router to pick up new settings
                agent_loop.reset_router()

                # Clear settings cache so memory manager picks up new values
                from pocketpaw.config import get_settings as _get_settings

                _get_settings.cache_clear()

                # Reload memory manager with fresh settings
                agent_loop.memory = get_memory_manager(force_reload=True)
                agent_loop.context_builder.memory = agent_loop.memory

                # Re-run health checks so status reflects new settings
                # (e.g. switching provider to litellm clears the "no API key" warning)
                try:
                    from pocketpaw.health import get_health_engine

                    engine = get_health_engine()
                    engine.run_startup_checks()
                    await websocket.send_json({"type": "health_update", "data": engine.summary})
                except Exception:
                    pass  # health refresh is best-effort

                await websocket.send_json(
                    {
                        "type": "settings_saved",
                        "content": "\u2699\ufe0f Settings updated",
                        "warnings": warnings,
                    }
                )

            # Handle API key save
            elif action == "save_api_key":
                from pocketpaw.config import validate_api_key

                provider = data.get("provider")
                key = data.get("key", "")

                # Map provider names to field names for validation.
                # Note: Some providers (google, tavily, brave, parallel, elevenlabs) don't
                # have format validation patterns in _API_KEY_PATTERNS yet and will pass through.
                # Patterns can be added in config.py as needed.
                provider_to_field = {
                    "anthropic": "anthropic_api_key",
                    "openai": "openai_api_key",
                    "google": "google_api_key",
                    "tavily": "tavily_api_key",
                    "brave": "brave_api_key",
                    "parallel": "parallel_api_key",
                    "elevenlabs": "elevenlabs_api_key",
                    "openai_compatible": "openai_compatible_api_key",
                }

                field_name = provider_to_field.get(provider)

                # Validate key format — warn but never block save
                key_warnings: list[str] = []
                if field_name and key:
                    is_valid, warning = validate_api_key(field_name, key)
                    if not is_valid:
                        key_warnings.append(warning)

                async with _settings_lock:
                    if provider == "anthropic" and key:
                        settings.anthropic_api_key = key
                        settings.save()
                        resolve_backend_env(settings, force=True)
                        agent_loop.reset_router()
                        await websocket.send_json(
                            _api_key_response(
                                "\u2705 Anthropic API key saved!",
                                warnings=key_warnings or None,
                            )
                        )
                    elif provider == "openai" and key:
                        settings.openai_api_key = key
                        settings.save()
                        resolve_backend_env(settings, force=True)
                        agent_loop.reset_router()
                        await websocket.send_json(
                            _api_key_response(
                                "\u2705 OpenAI API key saved!",
                                warnings=key_warnings or None,
                            )
                        )
                    elif provider == "google" and key:
                        settings.google_api_key = key
                        settings.save()
                        resolve_backend_env(settings, force=True)
                        agent_loop.reset_router()
                        await websocket.send_json(_api_key_response("\u2705 Google API key saved!"))
                    elif provider == "tavily" and key:
                        settings.tavily_api_key = key
                        settings.save()
                        await websocket.send_json(_api_key_response("\u2705 Tavily API key saved!"))
                    elif provider == "brave" and key:
                        settings.brave_search_api_key = key
                        settings.save()
                        await websocket.send_json(
                            _api_key_response("\u2705 Brave Search API key saved!")
                        )
                    elif provider == "parallel" and key:
                        settings.parallel_api_key = key
                        settings.save()
                        await websocket.send_json(
                            _api_key_response("\u2705 Parallel AI API key saved!")
                        )
                    elif provider == "elevenlabs" and key:
                        settings.elevenlabs_api_key = key
                        settings.save()
                        await websocket.send_json(
                            _api_key_response("\u2705 ElevenLabs API key saved!")
                        )
                    elif provider == "google_oauth_id" and key:
                        settings.google_oauth_client_id = key
                        settings.save()
                        await websocket.send_json(
                            _api_key_response("\u2705 Google OAuth Client ID saved!")
                        )
                    elif provider == "google_oauth_secret" and key:
                        settings.google_oauth_client_secret = key
                        settings.save()
                        await websocket.send_json(
                            _api_key_response("\u2705 Google OAuth Client Secret saved!")
                        )
                    elif provider == "spotify_client_id" and key:
                        settings.spotify_client_id = key
                        settings.save()
                        await websocket.send_json(
                            _api_key_response("\u2705 Spotify Client ID saved!")
                        )
                    elif provider == "spotify_client_secret" and key:
                        settings.spotify_client_secret = key
                        settings.save()
                        await websocket.send_json(
                            _api_key_response("\u2705 Spotify Client Secret saved!")
                        )
                    elif provider == "sarvam" and key:
                        settings.sarvam_api_key = key
                        settings.save()
                        await websocket.send_json(
                            _api_key_response("\u2705 Sarvam AI API key saved!")
                        )
                    else:
                        await websocket.send_json(
                            {"type": "error", "content": "Invalid API key or provider"}
                        )

            # Handle get_settings - return current settings to frontend
            elif action == "get_settings":
                agent_status = {
                    "status": "running" if agent_loop._running else "stopped",
                    "backend": "AgentLoop",
                }

                await websocket.send_json(
                    {
                        "type": "settings",
                        "content": {
                            "agentBackend": settings.agent_backend,
                            "claudeSdkProvider": settings.claude_sdk_provider,
                            "claudeSdkModel": settings.claude_sdk_model,
                            "claudeSdkMaxTurns": settings.claude_sdk_max_turns,
                            "openaiAgentsProvider": settings.openai_agents_provider,
                            "openaiAgentsModel": settings.openai_agents_model,
                            "openaiAgentsMaxTurns": settings.openai_agents_max_turns,
                            "googleAdkProvider": settings.google_adk_provider,
                            "googleAdkModel": settings.google_adk_model,
                            "googleAdkMaxTurns": settings.google_adk_max_turns,
                            "codexCliModel": settings.codex_cli_model,
                            "codexCliMaxTurns": settings.codex_cli_max_turns,
                            "copilotSdkProvider": settings.copilot_sdk_provider,
                            "copilotSdkModel": settings.copilot_sdk_model,
                            "copilotSdkMaxTurns": settings.copilot_sdk_max_turns,
                            "deepAgentsModel": settings.deep_agents_model,
                            "deepAgentsMaxTurns": settings.deep_agents_max_turns,
                            "opencodeBaseUrl": settings.opencode_base_url,
                            "opencodeModel": settings.opencode_model,
                            "opencodeMaxTurns": settings.opencode_max_turns,
                            "llmProvider": settings.llm_provider,
                            "ollamaHost": settings.ollama_host,
                            "ollamaModel": settings.ollama_model,
                            "anthropicModel": settings.anthropic_model,
                            "openaiCompatibleBaseUrl": settings.openai_compatible_base_url,
                            "openaiCompatibleModel": settings.openai_compatible_model,
                            "openaiCompatibleMaxTokens": settings.openai_compatible_max_tokens,
                            "hasOpenaiCompatibleKey": bool(settings.openai_compatible_api_key),
                            "geminiModel": settings.gemini_model,
                            "litellmApiBase": settings.litellm_api_base,
                            "litellmModel": settings.litellm_model,
                            "litellmMaxTokens": settings.litellm_max_tokens,
                            "hasLitellmKey": bool(settings.litellm_api_key),
                            "hasGoogleApiKey": bool(settings.google_api_key),
                            "bypassPermissions": settings.bypass_permissions,
                            "hasAnthropicKey": bool(settings.anthropic_api_key),
                            "hasOpenaiKey": bool(settings.openai_api_key),
                            "webSearchProvider": settings.web_search_provider,
                            "urlExtractProvider": settings.url_extract_provider,
                            "hasTavilyKey": bool(settings.tavily_api_key),
                            "hasBraveKey": bool(settings.brave_search_api_key),
                            "hasParallelKey": bool(settings.parallel_api_key),
                            "injectionScanEnabled": settings.injection_scan_enabled,
                            "injectionScanLlm": settings.injection_scan_llm,
                            "piiScanEnabled": settings.pii_scan_enabled,
                            "piiDefaultAction": settings.pii_default_action,
                            "piiScanMemory": settings.pii_scan_memory,
                            "piiScanAudit": settings.pii_scan_audit,
                            "piiScanLogs": settings.pii_scan_logs,
                            "toolProfile": settings.tool_profile,
                            "planMode": settings.plan_mode,
                            "planModeTools": ",".join(settings.plan_mode_tools),
                            "smartRoutingEnabled": settings.smart_routing_enabled,
                            "modelTierSimple": settings.model_tier_simple,
                            "modelTierModerate": settings.model_tier_moderate,
                            "modelTierComplex": settings.model_tier_complex,
                            "ttsProvider": settings.tts_provider,
                            "ttsVoice": settings.tts_voice,
                            "sttProvider": settings.stt_provider,
                            "sttModel": settings.stt_model,
                            "ocrProvider": settings.ocr_provider,
                            "sarvamTtsLanguage": settings.sarvam_tts_language,
                            "selfAuditEnabled": settings.self_audit_enabled,
                            "selfAuditSchedule": settings.self_audit_schedule,
                            "memoryBackend": settings.memory_backend,
                            "fileVectorEnabled": settings.file_vector_enabled,
                            "vectorStore": settings.vector_store,
                            "embeddingProvider": settings.embedding_provider,
                            "embeddingModel": settings.embedding_model,
                            "embeddingBaseUrl": settings.embedding_base_url,
                            "mem0AutoLearn": settings.mem0_auto_learn,
                            "mem0LlmProvider": settings.mem0_llm_provider,
                            "mem0LlmModel": settings.mem0_llm_model,
                            "mem0EmbedderProvider": settings.mem0_embedder_provider,
                            "mem0EmbedderModel": settings.mem0_embedder_model,
                            "mem0VectorStore": settings.mem0_vector_store,
                            "mem0OllamaBaseUrl": settings.mem0_ollama_base_url,
                            "hasElevenlabsKey": bool(settings.elevenlabs_api_key),
                            "hasGoogleOAuthId": bool(settings.google_oauth_client_id),
                            "hasGoogleOAuthSecret": bool(settings.google_oauth_client_secret),
                            "hasSpotifyClientId": bool(settings.spotify_client_id),
                            "hasSpotifyClientSecret": bool(settings.spotify_client_secret),
                            "hasSarvamKey": bool(settings.sarvam_api_key),
                            "webHost": settings.web_host,
                            "webPort": settings.web_port,
                            # A2A Protocol
                            "a2aEnabled": settings.a2a_enabled,
                            "a2aAgentName": settings.a2a_agent_name,
                            "a2aAgentDescription": settings.a2a_agent_description,
                            "a2aTaskTimeout": settings.a2a_task_timeout,
                            "a2aTrustedAgents": settings.a2a_trusted_agents,
                            # Soul Protocol
                            "soulEnabled": settings.soul_enabled,
                            "soulName": settings.soul_name,
                            "soulArchetype": settings.soul_archetype,
                            "soulPersona": settings.soul_persona,
                            "soulAutoSaveInterval": settings.soul_auto_save_interval,
                            "soulBiorhythm": settings.soul_biorhythm,
                            "agentActive": agent_active,
                            "agentStatus": agent_status,
                        },
                    }
                )

            # Handle file navigation (legacy)
            elif action == "navigate":
                path = data.get("path", "")
                await handle_file_navigation(websocket, path, settings)

            # Health engine actions
            elif action == "get_health":
                try:
                    from pocketpaw.health import get_health_engine

                    engine = get_health_engine()
                    await websocket.send_json({"type": "health_update", "data": engine.summary})
                except Exception as e:
                    await websocket.send_json(
                        {
                            "type": "health_update",
                            "data": {"status": "unknown", "error": str(e)},
                        }
                    )

            elif action == "run_health_check":
                try:
                    from pocketpaw.health import get_health_engine

                    engine = get_health_engine()
                    await engine.run_all_checks()
                    await websocket.send_json({"type": "health_update", "data": engine.summary})
                except Exception as e:
                    await websocket.send_json(
                        {
                            "type": "health_update",
                            "data": {"status": "unknown", "error": str(e)},
                        }
                    )

            elif action == "get_health_errors":
                try:
                    from pocketpaw.health import get_health_engine

                    engine = get_health_engine()
                    limit = data.get("limit", 20)
                    search = data.get("search", "")
                    errors = engine.get_recent_errors(limit=limit, search=search)
                    await websocket.send_json({"type": "health_errors", "errors": errors})
                except Exception as e:
                    await websocket.send_json(
                        {"type": "health_errors", "errors": [], "error": str(e)}
                    )

            # Handle file browser
            elif action == "browse":
                path = data.get("path", "~")
                context = data.get("context")
                await handle_file_browse(websocket, path, settings, context=context)

            # Handle reminder actions
            elif action == "get_reminders":
                scheduler = get_scheduler()
                reminders = scheduler.get_reminders()
                # Add time remaining to each reminder
                for r in reminders:
                    r["time_remaining"] = scheduler.format_time_remaining(r)
                await websocket.send_json({"type": "reminders", "reminders": reminders})

            elif action == "add_reminder":
                try:
                    message = data.get("message", "")
                    scheduler = get_scheduler()
                    reminder = scheduler.add_reminder(message)

                    if reminder:
                        reminder["time_remaining"] = scheduler.format_time_remaining(reminder)
                        await websocket.send_json({"type": "reminder_added", "reminder": reminder})
                    else:
                        await websocket.send_json(
                            {
                                "type": "reminder_error",
                                "content": ("Could not parse time. Try 'in 5 minutes' or 'at 3pm'"),
                            }
                        )
                except Exception:
                    await websocket.send_json(
                        {"type": "reminder_error", "content": "Error adding reminder"}
                    )

            elif action == "delete_reminder":
                reminder_id = data.get("id", "")
                scheduler = get_scheduler()
                if scheduler.delete_reminder(reminder_id):
                    await websocket.send_json({"type": "reminder_deleted", "id": reminder_id})
                else:
                    await websocket.send_json(
                        {"type": "reminder_error", "content": "Reminder not found"}
                    )

            # ==================== Intentions API ====================

            elif action == "get_intentions":
                daemon = get_daemon()
                intentions = daemon.get_intentions()
                await websocket.send_json({"type": "intentions", "intentions": intentions})

            elif action == "create_intention":
                daemon = get_daemon()
                try:
                    intention = daemon.create_intention(
                        name=data.get("name", "Unnamed"),
                        prompt=data.get("prompt", ""),
                        trigger=data.get(
                            "trigger",
                            {"type": "cron", "schedule": "0 9 * * *"},
                        ),
                        context_sources=data.get("context_sources", []),
                        enabled=data.get("enabled", True),
                    )
                    await websocket.send_json({"type": "intention_created", "intention": intention})
                except Exception as e:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "content": f"Failed to create intention: {e}",
                        }
                    )

            elif action == "update_intention":
                daemon = get_daemon()
                intention_id = data.get("id", "")
                updates = data.get("updates", {})
                intention = daemon.update_intention(intention_id, updates)
                if intention:
                    await websocket.send_json({"type": "intention_updated", "intention": intention})
                else:
                    await websocket.send_json({"type": "error", "content": "Intention not found"})

            elif action == "delete_intention":
                daemon = get_daemon()
                intention_id = data.get("id", "")
                if daemon.delete_intention(intention_id):
                    await websocket.send_json({"type": "intention_deleted", "id": intention_id})
                else:
                    await websocket.send_json({"type": "error", "content": "Intention not found"})

            elif action == "toggle_intention":
                daemon = get_daemon()
                intention_id = data.get("id", "")
                intention = daemon.toggle_intention(intention_id)
                if intention:
                    await websocket.send_json({"type": "intention_toggled", "intention": intention})
                else:
                    await websocket.send_json({"type": "error", "content": "Intention not found"})

            elif action == "run_intention":
                daemon = get_daemon()
                intention_id = data.get("id", "")
                intention = daemon.get_intention(intention_id)
                if intention:
                    # Run in background, results streamed via broadcast_intention
                    await websocket.send_json(
                        {
                            "type": "notification",
                            "content": f"\U0001f680 Running intention: {intention['name']}",
                        }
                    )
                    asyncio.create_task(daemon.run_intention_now(intention_id))
                else:
                    await websocket.send_json({"type": "error", "content": "Intention not found"})

            # ==================== Plan Mode API ====================

            elif action == "approve_plan":
                from pocketpaw.agents.plan_mode import get_plan_manager

                pm = get_plan_manager()
                session_key = data.get("session_key", "")
                plan = pm.approve_plan(session_key)
                if plan:
                    await websocket.send_json({"type": "plan_approved", "session_key": session_key})
                else:
                    await websocket.send_json(
                        {"type": "error", "content": "No active plan to approve"}
                    )

            elif action == "reject_plan":
                from pocketpaw.agents.plan_mode import get_plan_manager

                pm = get_plan_manager()
                session_key = data.get("session_key", "")
                plan = pm.reject_plan(session_key)
                if plan:
                    await websocket.send_json({"type": "plan_rejected", "session_key": session_key})
                else:
                    await websocket.send_json(
                        {"type": "error", "content": "No active plan to reject"}
                    )

            # ==================== Skills API ====================

            elif action == "get_skills":
                loader = get_skill_loader()
                loader.reload()  # Refresh to catch new installs
                skills = [
                    {
                        "name": s.name,
                        "description": s.description,
                        "argument_hint": s.argument_hint,
                    }
                    for s in loader.get_invocable()
                ]
                await websocket.send_json({"type": "skills", "skills": skills})

            elif action == "run_skill":
                skill_name = data.get("name", "")
                skill_args = data.get("args", "")

                loader = get_skill_loader()
                skill = loader.get(skill_name)

                if not skill:
                    # Not a skill — forward as a normal chat message so
                    # CommandHandler can pick up /backend, /model, etc.
                    full_text = f"/{skill_name}"
                    if skill_args:
                        full_text += f" {skill_args}"
                    data["content"] = full_text
                    await ws_adapter.handle_message(chat_id, data)
                else:
                    await websocket.send_json(
                        {
                            "type": "notification",
                            "content": f"\U0001f3af Running skill: {skill_name}",
                        }
                    )

                    # Execute skill through agent
                    executor = SkillExecutor(settings)
                    await websocket.send_json({"type": "stream_start"})
                    try:
                        async for chunk in executor.execute_skill(skill, skill_args):
                            await websocket.send_json(chunk)
                    finally:
                        await websocket.send_json({"type": "stream_end"})

    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)
        await ws_adapter.unregister_connection(chat_id)


# ─── Tool / File Helpers ─────────────────────────────────────────


async def handle_tool(websocket: WebSocket, tool: str, settings: Settings, data: dict):
    """Handle tool execution."""
    if tool == "status":
        # Run blocking status check in thread pool to avoid freezing websocket
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        from pocketpaw.tools.status import get_system_status

        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            status = await loop.run_in_executor(pool, get_system_status)
        await websocket.send_json({"type": "status", "content": status})

    elif tool == "screenshot":
        from pocketpaw.tools.screenshot import take_screenshot

        result = take_screenshot()  # sync function

        if isinstance(result, bytes):
            await websocket.send_json(
                {"type": "screenshot", "image": base64.b64encode(result).decode()}
            )
        else:
            error_msg = result if isinstance(result, str) else "Screenshot capture failed"
            await websocket.send_json({"type": "error", "content": error_msg})

    elif tool == "fetch":
        from pocketpaw.tools.fetch import list_directory

        path = data.get("path") or str(Path.home())
        result = list_directory(path, settings.file_jail_path)  # sync function
        await websocket.send_json({"type": "message", "content": result})

    elif tool == "panic":
        await websocket.send_json(
            {
                "type": "message",
                "content": "\U0001f6d1 PANIC: All agent processes stopped!",
            }
        )
        try:
            # Snapshot to avoid "dictionary changed size during iteration"
            tasks = list(agent_loop._active_tasks.values())
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            # Only stop router if one was already created (no lazy init for panic)
            router = agent_loop._router
            if router is not None:
                await router.stop()
        except Exception as e:
            logger.exception("Panic stop failed: %s", e)

    else:
        await websocket.send_json({"type": "error", "content": f"Unknown tool: {tool}"})


async def handle_file_navigation(websocket: WebSocket, path: str, settings: Settings):
    """Handle file browser navigation."""
    from pocketpaw.tools.fetch import list_directory

    result = list_directory(path, settings.file_jail_path)  # sync function
    await websocket.send_json({"type": "message", "content": result})


async def handle_file_browse(
    websocket: WebSocket,
    path: str,
    settings: Settings,
    *,
    context: str | None = None,
):
    """Handle file browser - returns structured JSON for the modal.

    If an optional ``context`` string is provided it is echoed back in the
    response so the frontend can route sidebar vs modal file responses.
    """
    from pocketpaw.tools.fetch import is_safe_path

    def _resp(payload: dict) -> dict:
        """Attach context to every response so frontend can route sidebar vs modal."""
        if context:
            payload["context"] = context
        return payload

    # Resolve ~ to home directory
    if path == "~" or path == "":
        resolved_path = Path.home()
    else:
        # Handle relative paths from home
        if not path.startswith("/"):
            resolved_path = Path.home() / path
        else:
            resolved_path = Path(path)

    resolved_path = resolved_path.resolve()
    jail = settings.file_jail_path.resolve()

    # Security check
    if not is_safe_path(resolved_path, jail):
        await websocket.send_json(
            _resp(
                {
                    "type": "files",
                    "error": "Access denied: path outside allowed directory",
                }
            )
        )
        return

    if not resolved_path.exists():
        await websocket.send_json(_resp({"type": "files", "error": "Path does not exist"}))
        return

    if not resolved_path.is_dir():
        await websocket.send_json(_resp({"type": "files", "error": "Not a directory"}))
        return

    # Build file list
    files = []
    try:
        items = sorted(
            resolved_path.iterdir(),
            key=lambda x: (not x.is_dir(), x.name.lower()),
        )
        # Filter hidden files BEFORE applying the limit
        visible_items = [item for item in items if not item.name.startswith(".")]

        for item in visible_items[:50]:  # Limit to 50 visible items
            file_info = {"name": item.name, "isDir": item.is_dir()}

            if not item.is_dir():
                try:
                    size = item.stat().st_size
                    if size < 1024:
                        file_info["size"] = f"{size} B"
                    elif size < 1024 * 1024:
                        file_info["size"] = f"{size / 1024:.1f} KB"
                    else:
                        file_info["size"] = f"{size / (1024 * 1024):.1f} MB"
                except Exception:
                    file_info["size"] = "?"

            files.append(file_info)

    except PermissionError:
        await websocket.send_json(_resp({"type": "files", "error": "Permission denied"}))
        return

    # Calculate relative path from home for display
    try:
        rel_path = resolved_path.relative_to(Path.home())
        display_path = str(rel_path) if str(rel_path) != "." else "~"
    except ValueError:
        display_path = str(resolved_path)

    await websocket.send_json(_resp({"type": "files", "path": display_path, "files": files}))
