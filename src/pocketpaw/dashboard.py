"""PocketPaw Web Dashboard - API Server

Lightweight FastAPI server that serves the frontend and handles WebSocket communication.

Changes:
  - 2026-02-17: Health heartbeat — periodic checks every 5 min via APScheduler, broadcasts health_update on status transitions.
  - 2026-02-17: Health Engine API (GET /api/health, POST /api/health/check, WS get_health/run_health_check).
  - 2026-02-06: WebSocket auth via first message instead of URL query param; accept wss://.
  - 2026-02-06: Channel config REST API (GET /api/channels/status, POST save/toggle).
  - 2026-02-06: Refactored adapter storage to _channel_adapters dict; auto-start all configured.
  - 2026-02-06: Auto-start Discord/WhatsApp adapters alongside dashboard; WhatsApp webhook routes.
  - 2026-02-12: Call ensure_project_directories() on startup for migration.
  - 2026-02-12: handle_file_browse() accepts optional `context` param echoed in response for
    sidebar vs modal file routing.
  - 2026-02-12: Fixed handle_file_browse bug: filter hidden files BEFORE applying 50-item limit.
  - 2026-02-12: Added Deep Work API router at /api/deep-work/*.
  - 2026-02-05: Added Mission Control API router at /api/mission-control/*.
  - 2026-02-04: Added Telegram setup API endpoints (/api/telegram/status, /api/telegram/setup, /api/telegram/pairing-status).
  - 2026-02-03: Cleaned up duplicate imports, fixed duplicate save() calls.
  - 2026-02-02: Added agent status to get_settings response.
  - 2026-02-02: Enhanced logging to show which backend is processing requests.
"""

import asyncio
import base64
import io
import json
import logging
from pathlib import Path

try:
    import qrcode
    import uvicorn
    from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import Response
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates
except ImportError as _exc:
    raise ImportError(
        "Dashboard dependencies (fastapi, uvicorn, qrcode, jinja2) are required "
        "but not installed. Reinstall with: pip install --upgrade pocketpaw"
    ) from _exc

import pocketpaw.dashboard_state as _state
from pocketpaw.api.v1 import mount_v1_routers
from pocketpaw.bootstrap import DefaultBootstrapProvider
from pocketpaw.config import Settings, get_access_token, get_config_path
from pocketpaw.dashboard_auth import (
    AuthMiddleware,
    _is_genuine_localhost,  # noqa: F401 — re-export for backward compat
    auth_router,
    verify_token,  # noqa: F401 — re-export for backward compat
)
from pocketpaw.dashboard_channels import (
    _start_channel_adapter,
    _stop_channel_adapter,
    channels_router,
    get_channels_status,  # noqa: F401 — re-export for backward compat
    save_channel_config,  # noqa: F401 — re-export for backward compat
    toggle_channel,  # noqa: F401 — re-export for backward compat
)
from pocketpaw.dashboard_lifecycle import (
    _broadcast_audit_entry,  # noqa: F401 — re-export for backward compat
    _broadcast_health_update,  # noqa: F401 — re-export for backward compat
    broadcast_intention,  # noqa: F401 — re-export for backward compat
    broadcast_reminder,  # noqa: F401 — re-export for backward compat
)
from pocketpaw.dashboard_lifecycle import (
    shutdown_event as _shutdown_event,
)
from pocketpaw.dashboard_lifecycle import (
    startup_event as _startup_event,
)
from pocketpaw.dashboard_state import (
    _CHANNEL_CONFIG_KEYS,  # noqa: F401 — re-export for backward compat
    _CHANNEL_DEPS,  # noqa: F401 — re-export for backward compat
    _MEMORY_CONFIG_KEYS,
    _OAUTH_SCOPES,
    _channel_adapters,  # noqa: F401 — re-export for backward compat
    _channel_autostart_enabled,  # noqa: F401 — re-export for backward compat
    _channel_is_configured,  # noqa: F401 — re-export for backward compat
    _channel_is_running,  # noqa: F401 — re-export for backward compat
    _is_module_importable,  # noqa: F401 — re-export for backward compat
    _settings_lock,  # noqa: F401 — re-export for backward compat
    _telegram_pairing_state,
    active_connections,  # noqa: F401 — re-export for backward compat
    agent_loop,
)
from pocketpaw.dashboard_ws import (
    handle_file_browse,  # noqa: F401 — re-export for backward compat
    handle_file_navigation,  # noqa: F401 — re-export for backward compat
    handle_tool,  # noqa: F401 — re-export for backward compat
)
from pocketpaw.deep_work.api import router as deep_work_router
from pocketpaw.memory import MemoryType, get_memory_manager
from pocketpaw.mission_control.api import router as mission_control_router
from pocketpaw.security import get_audit_logger
from pocketpaw.skills import get_skill_loader
from pocketpaw.tunnel import get_tunnel_manager

logger = logging.getLogger(__name__)

# Module-level uvicorn server reference (set by run_dashboard, read by restart_server)
_uvicorn_server = None

# Get frontend directory
FRONTEND_DIR = Path(__file__).parent / "frontend"
TEMPLATES_DIR = FRONTEND_DIR / "templates"

# Initialize Templates
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Create FastAPI app
app = FastAPI(
    title="PocketPaw API",
    description="Self-hosted AI agent — REST API for external clients and the web dashboard.",
    version="1.0.0",
    docs_url="/api/v1/docs",
    redoc_url="/api/v1/redoc",
    openapi_url="/api/v1/openapi.json",
)

# CORS — localhost + Cloudflare tunnel + Tauri desktop + custom origins from config
_BUILTIN_ORIGINS = [
    "tauri://localhost",
    "https://tauri.localhost",  # Tauri v2
    "http://localhost:1420",  # Tauri dev server
]
try:
    _custom_origins = Settings.load().api_cors_allowed_origins
except Exception:
    _custom_origins = []
_EXTRA_ORIGINS = list(set(_BUILTIN_ORIGINS + _custom_origins))

# NOTE: CORSMiddleware is registered AFTER AuthMiddleware below so that CORS
# is outermost (Starlette processes last-added first) and handles OPTIONS
# preflight before auth can reject them.  See line ~193.


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add security headers to all responses."""
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # CSP: allow self + CDN + inline styles/scripts (required by Alpine.js/UnoCSS)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
        "https://cdn.jsdelivr.net https://unpkg.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com https://cdn.jsdelivr.net; "
        "img-src 'self' data: blob:; "
        "connect-src 'self' ws: wss: https://cdn.jsdelivr.net https://unpkg.com; "
        "frame-ancestors 'none'"
    )
    # HSTS only when accessed via HTTPS (tunnel or reverse proxy)
    if request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


# Mount static files
app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

# Mount Mission Control API router
app.include_router(mission_control_router, prefix="/api/mission-control")

# Mount Deep Work API router

app.include_router(deep_work_router, prefix="/api/deep-work")

# Mount API v1 routers at /api/v1/ (canonical) — see api/v1/__init__.py
mount_v1_routers(app)

# Mount channel management router (webhooks, extras, channel status/toggle)
app.include_router(channels_router)

# Mount auth router (session tokens, cookie login/logout, QR code, token regeneration)
app.include_router(auth_router)

# Middleware order matters: last added = outermost = runs first.
# Auth must be registered BEFORE CORS so CORS is outermost and handles
# OPTIONS preflight requests before auth can reject them.
app.add_middleware(AuthMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_EXTRA_ORIGINS,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event():
    await _startup_event(_start_channel_adapter_fn=_start_channel_adapter)


@app.on_event("shutdown")
async def shutdown_event():
    await _shutdown_event(_stop_channel_adapter_fn=_stop_channel_adapter)


# ==================== MCP Server API ====================


@app.get("/api/mcp/status")
async def get_mcp_status():
    """Get status of all configured MCP servers."""
    from pocketpaw.mcp.manager import get_mcp_manager

    mgr = get_mcp_manager()
    return mgr.get_server_status()


@app.post("/api/mcp/add")
async def add_mcp_server(request: Request):
    """Add a new MCP server configuration and optionally start it."""
    from pocketpaw.mcp.config import MCPServerConfig
    from pocketpaw.mcp.manager import get_mcp_manager

    data = await request.json()
    config = MCPServerConfig(
        name=data.get("name", ""),
        transport=data.get("transport", "stdio"),
        command=data.get("command", ""),
        args=data.get("args", []),
        url=data.get("url", ""),
        env=data.get("env", {}),
        enabled=data.get("enabled", True),
    )
    if not config.name:
        raise HTTPException(status_code=400, detail="Server name is required")

    mgr = get_mcp_manager()
    mgr.add_server_config(config)

    # Auto-start if enabled
    if config.enabled:
        try:
            await mgr.start_server(config)
        except Exception as e:
            logger.warning("Failed to auto-start MCP server '%s': %s", config.name, e)

    return {"status": "ok"}


@app.post("/api/mcp/remove")
async def remove_mcp_server(request: Request):
    """Remove an MCP server config and stop it if running."""
    from pocketpaw.mcp.manager import get_mcp_manager

    data = await request.json()
    name = data.get("name", "")

    mgr = get_mcp_manager()
    await mgr.stop_server(name)
    removed = mgr.remove_server_config(name)
    if not removed:
        return {"error": f"Server '{name}' not found"}
    return {"status": "ok"}


@app.post("/api/mcp/toggle")
async def toggle_mcp_server(request: Request):
    """Toggle an MCP server: start if stopped/disconnected, stop if running."""
    from pocketpaw.mcp.config import load_mcp_config
    from pocketpaw.mcp.manager import get_mcp_manager

    data = await request.json()
    name = data.get("name", "")

    mgr = get_mcp_manager()
    status = mgr.get_server_status()
    server_info = status.get(name)

    if server_info is None:
        return {"error": f"Server '{name}' not found"}

    if server_info["connected"]:
        # Running → stop and disable
        mgr.toggle_server_config(name)  # enabled → False
        await mgr.stop_server(name)
        return {"status": "ok", "enabled": False}
    else:
        # Not connected → ensure enabled and (re)start
        configs = load_mcp_config()
        config = next((c for c in configs if c.name == name), None)
        if not config:
            return {"error": f"No config found for '{name}'"}
        if not config.enabled:
            mgr.toggle_server_config(name)  # disabled → enabled
        connected = await mgr.start_server(config)
        return {"status": "ok", "enabled": True, "connected": connected}


@app.post("/api/mcp/test")
async def test_mcp_server(request: Request):
    """Test an MCP server connection and return discovered tools."""
    from pocketpaw.mcp.config import MCPServerConfig
    from pocketpaw.mcp.manager import get_mcp_manager

    data = await request.json()
    config = MCPServerConfig(
        name=data.get("name", "test"),
        transport=data.get("transport", "stdio"),
        command=data.get("command", ""),
        args=data.get("args", []),
        url=data.get("url", ""),
        env=data.get("env", {}),
    )

    mgr = get_mcp_manager()
    success = await mgr.start_server(config)
    if not success:
        status = mgr.get_server_status().get(config.name, {})
        return {"connected": False, "error": status.get("error", "Unknown error"), "tools": []}

    tools = mgr.discover_tools(config.name)
    # Stop the test server
    await mgr.stop_server(config.name)
    return {
        "connected": True,
        "tools": [{"name": t.name, "description": t.description} for t in tools],
    }


# ==================== MCP Preset Routes ====================


@app.get("/api/mcp/presets")
async def list_mcp_presets():
    """Return all MCP presets with installed flag."""
    from pocketpaw.mcp.config import load_mcp_config
    from pocketpaw.mcp.presets import get_all_presets

    installed_names = {c.name for c in load_mcp_config()}
    presets = get_all_presets()
    return [
        {
            "id": p.id,
            "name": p.name,
            "description": p.description,
            "icon": p.icon,
            "category": p.category,
            "package": p.package,
            "transport": p.transport,
            "url": p.url,
            "docs_url": p.docs_url,
            "needs_args": p.needs_args,
            "oauth": p.oauth,
            "installed": p.id in installed_names,
            "env_keys": [
                {
                    "key": e.key,
                    "label": e.label,
                    "required": e.required,
                    "placeholder": e.placeholder,
                    "secret": e.secret,
                }
                for e in p.env_keys
            ],
        }
        for p in presets
    ]


@app.post("/api/mcp/presets/install")
async def install_mcp_preset(request: Request):
    """Install an MCP preset by ID with user-supplied env vars."""
    from fastapi.responses import JSONResponse

    from pocketpaw.mcp.manager import get_mcp_manager
    from pocketpaw.mcp.presets import get_preset, preset_to_config

    data = await request.json()
    preset_id = data.get("preset_id", "")
    env = data.get("env", {})
    extra_args = data.get("extra_args", None)

    preset = get_preset(preset_id)
    if not preset:
        return JSONResponse({"error": f"Unknown preset: {preset_id}"}, status_code=404)

    # Validate required env keys
    missing = [ek.key for ek in preset.env_keys if ek.required and not env.get(ek.key)]
    if missing:
        return JSONResponse(
            {"error": f"Missing required env vars: {', '.join(missing)}"},
            status_code=400,
        )

    config = preset_to_config(preset, env=env, extra_args=extra_args)
    mgr = get_mcp_manager()
    mgr.add_server_config(config)
    connected = await mgr.start_server(config)
    tools = mgr.discover_tools(config.name) if connected else []

    return {
        "status": "ok",
        "connected": connected,
        "tools": [{"name": t.name, "description": t.description} for t in tools],
    }


@app.get("/api/mcp/oauth/callback")
async def mcp_oauth_callback(code: str = "", state: str = ""):
    """OAuth callback endpoint — receives authorization code from OAuth provider.

    This is the redirect target after user authenticates with GitHub, Notion, etc.
    Auth-exempt because the OAuth provider redirects the user's browser here.
    """
    from fastapi.responses import HTMLResponse

    from pocketpaw.mcp.manager import set_oauth_callback_result

    if not code or not state:
        return HTMLResponse(
            "<html><body><h3>Missing code or state parameter.</h3></body></html>",
            status_code=400,
        )

    resolved = set_oauth_callback_result(state, code)
    if resolved:
        return HTMLResponse(
            "<html><body>"
            "<h3>Authenticated! You can close this tab.</h3>"
            "<script>window.close()</script>"
            "</body></html>"
        )
    return HTMLResponse(
        "<html><body><h3>OAuth flow expired or not found.</h3></body></html>",
        status_code=400,
    )


# ==================== Skills Library API ====================


@app.get("/api/skills")
async def list_installed_skills():
    """List all installed user-invocable skills."""
    loader = get_skill_loader()
    loader.reload()
    return [
        {
            "name": s.name,
            "description": s.description,
            "argument_hint": s.argument_hint,
        }
        for s in loader.get_invocable()
    ]


@app.get("/api/skills/search")
async def search_skills_library(q: str = "", limit: int = 30):
    """Proxy search to skills.sh API (avoids CORS for browsers)."""
    import httpx

    if not q:
        return {"skills": [], "count": 0}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://skills.sh/api/search",
                params={"q": q, "limit": limit},
            )
            resp.raise_for_status()
            return resp.json()
    except httpx.HTTPError as exc:
        logger.warning("skills.sh search failed: %s", exc)
        return {"skills": [], "count": 0, "error": str(exc)}


@app.post("/api/skills/install")
async def install_skill(request: Request):
    """Install a skill by cloning its GitHub repo and copying the skill directory."""
    import shutil
    import tempfile
    from pathlib import Path

    from fastapi.responses import JSONResponse

    data = await request.json()
    source = data.get("source", "").strip()
    if not source:
        return JSONResponse({"error": "Missing 'source' field"}, status_code=400)

    if ".." in source or ";" in source or "|" in source or "&" in source:
        return JSONResponse({"error": "Invalid source format"}, status_code=400)

    parts = source.split("/")
    if len(parts) < 2:
        return JSONResponse(
            {"error": "Source must be owner/repo or owner/repo/skill"}, status_code=400
        )

    owner, repo = parts[0], parts[1]
    skill_name = parts[2] if len(parts) >= 3 else None

    install_dir = Path.home() / ".agents" / "skills"
    install_dir.mkdir(parents=True, exist_ok=True)

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--depth=1",
                f"https://github.com/{owner}/{repo}.git",
                tmpdir,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            if proc.returncode != 0:
                err = stderr.decode(errors="replace").strip()
                return JSONResponse({"error": f"Clone failed: {err}"}, status_code=500)

            tmp = Path(tmpdir)

            # Find skill directories containing SKILL.md.
            # Repos may store skills at root level or inside a skills/ subdirectory.
            skill_dirs: list[tuple[str, Path]] = []

            if skill_name:
                for candidate in [tmp / skill_name, tmp / "skills" / skill_name]:
                    if (candidate / "SKILL.md").exists():
                        skill_dirs.append((skill_name, candidate))
                        break
            else:
                for scan_dir in [tmp, tmp / "skills"]:
                    if not scan_dir.is_dir():
                        continue
                    for item in sorted(scan_dir.iterdir()):
                        if item.is_dir() and (item / "SKILL.md").exists():
                            skill_dirs.append((item.name, item))

            if not skill_dirs:
                return JSONResponse(
                    {"error": f"No SKILL.md found for '{skill_name or source}'"},
                    status_code=404,
                )

            installed = []
            for name, src_dir in skill_dirs:
                dest = install_dir / name
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(src_dir, dest)
                installed.append(name)

            loader = get_skill_loader()
            loader.reload()
            return {"status": "ok", "installed": installed}

    except TimeoutError:
        return JSONResponse({"error": "Clone timed out (30s)"}, status_code=504)
    except Exception as exc:
        logger.exception("Skill install failed")
        return JSONResponse({"error": str(exc)}, status_code=500)


@app.post("/api/skills/remove")
async def remove_skill(request: Request):
    """Remove an installed skill by deleting its directory."""
    import shutil
    from pathlib import Path

    from fastapi.responses import JSONResponse

    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        return JSONResponse({"error": "Missing 'name' field"}, status_code=400)

    if ".." in name or "/" in name or ";" in name or "|" in name or "&" in name:
        return JSONResponse({"error": "Invalid name format"}, status_code=400)

    # Check both skill locations
    for base in [Path.home() / ".agents" / "skills", Path.home() / ".pocketpaw" / "skills"]:
        skill_dir = base / name
        if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
            shutil.rmtree(skill_dir)
            loader = get_skill_loader()
            loader.reload()
            return {"status": "ok"}

    return JSONResponse({"error": f"Skill '{name}' not found"}, status_code=404)


@app.post("/api/skills/reload")
async def reload_skills():
    """Force reload skills from disk."""
    loader = get_skill_loader()
    skills = loader.reload()
    return {
        "status": "ok",
        "count": len([s for s in skills.values() if s.user_invocable]),
    }


# ─── Backend Discovery ───────────────────────────────────────────
@app.get("/api/backends")
async def list_available_backends():
    """List all registered agent backends with availability and capabilities."""
    import importlib
    import shutil

    from pocketpaw.agents.backend import Capability
    from pocketpaw.agents.registry import get_backend_class, get_backend_info, list_backends

    # Map backend names to their CLI binary for availability checks
    _CLI_BINARY: dict[str, str] = {
        "codex_cli": "codex",
        "opencode": "opencode",
        "copilot_sdk": "copilot",
    }

    def _check_available(info) -> bool:
        """Check if a backend's external dependencies are actually installed."""
        hint = info.install_hint
        if not hint:
            return True
        # Check pip dependency via verify_import (+ optional verify_attr)
        verify = hint.get("verify_import")
        if verify:
            try:
                mod = importlib.import_module(verify)
                # Optionally check for a specific attribute to avoid
                # false positives from unrelated packages with same name
                attr = hint.get("verify_attr")
                if attr and not hasattr(mod, attr):
                    return False
            except ImportError:
                return False
        # Check CLI binary if this backend needs one
        binary = _CLI_BINARY.get(info.name)
        if binary and not shutil.which(binary):
            return False
        return True

    results = []
    for name in list_backends():
        info = get_backend_info(name)
        available = get_backend_class(name) is not None
        if info:
            available = available and _check_available(info)
            results.append(
                {
                    "name": info.name,
                    "displayName": info.display_name,
                    "available": available,
                    "capabilities": [c.name.lower() for c in Capability if c in info.capabilities],
                    "builtinTools": info.builtin_tools,
                    "requiredKeys": info.required_keys,
                    "supportedProviders": info.supported_providers,
                    "installHint": info.install_hint,
                    "beta": info.beta,
                }
            )
        else:
            results.append(
                {
                    "name": name,
                    "displayName": name,
                    "available": False,
                    "capabilities": [],
                    "builtinTools": [],
                    "requiredKeys": [],
                    "supportedProviders": [],
                    "installHint": {},
                    "beta": False,
                }
            )
    return results


@app.post("/api/backends/install")
async def install_backend(request: Request):
    """Auto-install a pip-installable backend SDK."""
    import asyncio
    import importlib
    import shutil
    import subprocess
    import sys

    from pocketpaw.agents.registry import get_backend_info

    data = await request.json()
    backend_name = data.get("backend", "")
    info = get_backend_info(backend_name)
    if not info:
        return {"error": f"Unknown backend: {backend_name}"}

    hint = info.install_hint
    pip_spec = hint.get("pip_spec")
    verify_import = hint.get("verify_import")
    if not pip_spec or not verify_import:
        return {"error": f"Backend '{backend_name}' is not pip-installable"}

    def _install() -> None:
        in_venv = hasattr(sys, "real_prefix") or sys.prefix != sys.base_prefix
        uv = shutil.which("uv")
        if uv:
            cmd = [uv, "pip", "install", "--python", sys.executable]
            if not in_venv:
                cmd.append("--system")
            cmd.append(pip_spec)
        else:
            cmd = [sys.executable, "-m", "pip", "install"]
            if not in_venv:
                cmd.append("--user")
            cmd.append(pip_spec)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to install {pip_spec}:\n{result.stderr.strip()}")

        importlib.invalidate_caches()
        # Clear stale module entries so Python retries imports
        for key in list(sys.modules):
            if key == verify_import or key.startswith(verify_import + "."):
                del sys.modules[key]
        importlib.import_module(verify_import)

    try:
        await asyncio.to_thread(_install)
    except RuntimeError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Install failed: {exc}"}

    # Clear cached backend modules so the registry re-discovers them
    for key in list(sys.modules):
        if key.startswith("pocketpaw.agents."):
            del sys.modules[key]
    importlib.invalidate_caches()

    return {"status": "ok"}


@app.get("/api/oauth/authorize")
async def oauth_authorize(service: str = Query("google_gmail")):
    """Start OAuth flow — redirects user to provider consent screen."""
    from fastapi.responses import RedirectResponse

    settings = Settings.load()

    scopes = _OAUTH_SCOPES.get(service)
    if not scopes:
        raise HTTPException(status_code=400, detail=f"Unknown service: {service}")

    # Determine provider and credentials from service name
    if service == "spotify":
        provider = "spotify"
        client_id = settings.spotify_client_id
        if not client_id:
            raise HTTPException(
                status_code=400,
                detail="Spotify Client ID not configured. Set it in Settings first.",
            )
    else:
        provider = "google"
        client_id = settings.google_oauth_client_id
        if not client_id:
            raise HTTPException(
                status_code=400,
                detail="Google OAuth Client ID not configured. Set it in Settings first.",
            )

    from pocketpaw.integrations.oauth import OAuthManager

    manager = OAuthManager()
    redirect_uri = f"http://localhost:{settings.web_port}/oauth/callback"
    state = f"{provider}:{service}"

    auth_url = manager.get_auth_url(
        provider=provider,
        client_id=client_id,
        redirect_uri=redirect_uri,
        scopes=scopes,
        state=state,
    )
    return RedirectResponse(auth_url)


@app.get("/oauth/callback")
async def oauth_callback(
    code: str = Query(""),
    state: str = Query(""),
    error: str = Query(""),
):
    """OAuth callback route — exchanges auth code for tokens."""
    from fastapi.responses import HTMLResponse

    if error:
        return HTMLResponse(f"<h2>OAuth Error</h2><p>{error}</p><p>You can close this window.</p>")

    if not code:
        return HTMLResponse("<h2>Missing authorization code</h2>")

    try:
        from pocketpaw.integrations.oauth import OAuthManager
        from pocketpaw.integrations.token_store import TokenStore

        settings = Settings.load()
        manager = OAuthManager(TokenStore())

        # State encodes: "{provider}:{service}" e.g. "google:google_gmail"
        parts = state.split(":", 1)
        provider = parts[0] if parts else "google"
        service = parts[1] if len(parts) > 1 else "google_gmail"

        redirect_uri = f"http://localhost:{settings.web_port}/oauth/callback"

        scopes = _OAUTH_SCOPES.get(service, [])

        # Resolve credentials per provider
        if provider == "spotify":
            client_id = settings.spotify_client_id or ""
            client_secret = settings.spotify_client_secret or ""
        else:
            client_id = settings.google_oauth_client_id or ""
            client_secret = settings.google_oauth_client_secret or ""

        await manager.exchange_code(
            provider=provider,
            service=service,
            code=code,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            scopes=scopes,
        )

        return HTMLResponse(
            "<h2>Authorization Successful</h2>"
            "<p>Tokens saved. You can close this window and return to PocketPaw.</p>"
        )

    except Exception as e:
        logger.error("OAuth callback error: %s", e)
        return HTMLResponse(f"<h2>OAuth Error</h2><p>{e}</p>")


def _static_version() -> str:
    """Generate a cache-busting version string from JS file mtimes."""
    import hashlib

    js_dir = FRONTEND_DIR / "js"
    if not js_dir.exists():
        return "0"
    mtimes = []
    for f in sorted(js_dir.rglob("*.js")):
        mtimes.append(str(int(f.stat().st_mtime)))
    return hashlib.md5("|".join(mtimes).encode()).hexdigest()[:8]


@app.get("/api/version")
async def get_version_info():
    """Return current version and update availability."""
    from importlib.metadata import version as get_version

    from pocketpaw.config import get_config_dir
    from pocketpaw.update_check import check_for_updates

    current = get_version("pocketpaw")
    info = check_for_updates(current, get_config_dir())
    return info or {"current": current, "latest": current, "update_available": False}


@app.get("/")
async def index(request: Request):
    """Serve the main dashboard page."""
    from importlib.metadata import version as get_version

    return templates.TemplateResponse(
        "base.html",
        {"request": request, "v": _static_version(), "app_version": get_version("pocketpaw")},
    )


# NOTE: Session token exchange, cookie login/logout, QR code, and token
# regeneration routes are all provided by auth_router (from dashboard_auth.py),
# which is mounted above via app.include_router(auth_router).


# ==================== Tunnel API ====================


@app.get("/api/remote/status")
async def get_tunnel_status():
    """Get active tunnel status."""
    manager = get_tunnel_manager()
    return manager.get_status()


@app.post("/api/remote/start")
async def start_tunnel():
    """Start Cloudflare tunnel."""
    manager = get_tunnel_manager()
    try:
        url = await manager.start()
        return {"url": url, "active": True}
    except Exception as e:
        # Error handling via JSON to frontend
        return {"error": str(e), "active": False}


@app.post("/api/remote/stop")
async def stop_tunnel():
    """Stop Cloudflare tunnel."""
    manager = get_tunnel_manager()
    await manager.stop()
    return {"active": False}


# ============================================================================
# Telegram Setup API
# ============================================================================


@app.get("/api/telegram/status")
async def get_telegram_status():
    """Get current Telegram configuration status."""
    settings = Settings.load()
    return {
        "configured": bool(settings.telegram_bot_token and settings.allowed_user_id),
        "user_id": settings.allowed_user_id,
    }


@app.post("/api/telegram/setup")
async def setup_telegram(request: Request):
    """Start Telegram pairing flow."""
    import secrets

    from telegram import Update
    from telegram.ext import Application, CommandHandler, ContextTypes

    data = await request.json()
    bot_token = data.get("bot_token", "").strip()

    if not bot_token:
        return {"error": "Bot token is required"}

    # Generate session secret
    session_secret = secrets.token_urlsafe(32)
    _telegram_pairing_state["session_secret"] = session_secret
    _telegram_pairing_state["paired"] = False
    _telegram_pairing_state["user_id"] = None

    # Save token to settings
    settings = Settings.load()
    settings.telegram_bot_token = bot_token
    settings.save()

    try:
        # Initialize temporary bot to verify token and get username
        builder = Application.builder().token(bot_token)
        temp_app = builder.build()

        bot_user = await temp_app.bot.get_me()
        username = bot_user.username

        # Generate Deep Link: https://t.me/<username>?start=<secret>
        deep_link = f"https://t.me/{username}?start={session_secret}"

        # Generate QR code
        qr = qrcode.QRCode(version=1, box_size=10, border=2)
        qr.add_data(deep_link)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = io.BytesIO()
        img.save(buffer, format="PNG")
        qr_base64 = base64.b64encode(buffer.getvalue()).decode()
        qr_url = f"data:image/png;base64,{qr_base64}"

        # Define pairing handler
        async def handle_pairing_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not update.message or not update.effective_user:
                return

            text = update.message.text or ""
            parts = text.split()

            if len(parts) < 2:
                await update.message.reply_text(
                    "⏳ Waiting for pairing... Please scan the QR code to start."
                )
                return

            secret = parts[1]
            if secret != _telegram_pairing_state["session_secret"]:
                await update.message.reply_text(
                    "❌ Invalid session token. Please refresh the setup page."
                )
                return

            # Success!
            user_id = update.effective_user.id
            _telegram_pairing_state["paired"] = True
            _telegram_pairing_state["user_id"] = user_id

            # Save to config
            settings = Settings.load()
            settings.allowed_user_id = user_id
            settings.save()

            await update.message.reply_text(
                "🎉 **Connected!**\n\nPocketPaw is now paired with this device.\nYou can close the browser window now.",
                parse_mode="Markdown",
            )

            logger.info(
                f"✅ Telegram paired with user: {update.effective_user.username} ({user_id})"
            )

        # Start listening for /start <secret>
        temp_app.add_handler(CommandHandler("start", handle_pairing_start))
        await temp_app.initialize()
        await temp_app.start()
        await temp_app.updater.start_polling(drop_pending_updates=True)

        # Store for cleanup later
        _telegram_pairing_state["temp_bot_app"] = temp_app

        return {"qr_url": qr_url, "deep_link": deep_link}

    except Exception as e:
        logger.error(f"Telegram setup failed: {e}")
        return {"error": f"Failed to connect to Telegram: {str(e)}"}


@app.get("/api/telegram/pairing-status")
async def get_telegram_pairing_status():
    """Check if Telegram pairing is complete."""
    paired = _telegram_pairing_state.get("paired", False)
    user_id = _telegram_pairing_state.get("user_id")

    # If paired, cleanup the temporary bot
    if paired and _telegram_pairing_state.get("temp_bot_app"):
        try:
            temp_app = _telegram_pairing_state["temp_bot_app"]
            if temp_app.updater.running:
                await temp_app.updater.stop()
            if temp_app.running:
                await temp_app.stop()
            await temp_app.shutdown()
            _telegram_pairing_state["temp_bot_app"] = None
        except Exception as e:
            logger.warning(f"Error cleaning up temp bot: {e}")

    return {"paired": paired, "user_id": user_id}


@app.websocket("/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str | None = Query(None),
    resume_session: str | None = Query(None),
):
    """WebSocket endpoint — delegates to dashboard_ws.websocket_handler()."""
    from pocketpaw.dashboard_ws import websocket_handler

    await websocket_handler(
        websocket,
        token,
        resume_session,
        _is_genuine_localhost_fn=_is_genuine_localhost,
        _get_access_token_fn=get_access_token,
    )


# ==================== Transparency APIs ====================


@app.get("/api/identity")
async def get_identity():
    """Get agent identity context (all 5 identity files)."""
    provider = DefaultBootstrapProvider(get_config_path().parent)
    context = await provider.get_context()
    return {
        "identity_file": context.identity,
        "soul_file": context.soul,
        "style_file": context.style,
        "instructions_file": context.instructions,
        "user_file": context.user_profile,
    }


@app.put("/api/identity")
async def save_identity(request: Request):
    """Save edits to agent identity files. Changes take effect on the next message."""
    data = await request.json()
    identity_dir = get_config_path().parent / "identity"
    identity_dir.mkdir(parents=True, exist_ok=True)

    file_map = {
        "identity_file": "IDENTITY.md",
        "soul_file": "SOUL.md",
        "style_file": "STYLE.md",
        "instructions_file": "INSTRUCTIONS.md",
        "user_file": "USER.md",
    }
    updated = []
    for key, filename in file_map.items():
        if key in data and isinstance(data[key], str):
            (identity_dir / filename).write_text(data[key])
            updated.append(filename)

    return {"ok": True, "updated": updated}


@app.get("/api/sessions")
async def list_sessions_v2(limit: int = 50):
    """List sessions using the fast session index."""
    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "_load_session_index"):
        index = store._load_session_index()
        # Sort by last_activity descending
        entries = sorted(
            index.items(),
            key=lambda kv: kv[1].get("last_activity", ""),
            reverse=True,
        )[:limit]
        sessions = []
        for safe_key, meta in entries:
            sessions.append({"id": safe_key, **meta})
        return {"sessions": sessions, "total": len(index)}

    # Fallback for non-file stores
    return {"sessions": [], "total": 0}


@app.get("/api/memory/sessions")
async def list_sessions(limit: int = 20):
    """List all available sessions with metadata (legacy endpoint)."""
    result = await list_sessions_v2(limit=limit)
    return result.get("sessions", [])


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    """Delete a session by ID."""
    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "delete_session"):
        deleted = await store.delete_session(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"status": "ok"}

    raise HTTPException(status_code=501, detail="Store does not support session deletion")


@app.post("/api/sessions/{session_id}/title")
async def update_session_title(session_id: str, request: Request):
    """Update the title of a session."""
    data = await request.json()
    title = data.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required")

    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "update_session_title"):
        updated = await store.update_session_title(session_id, title)
        if not updated:
            raise HTTPException(status_code=404, detail="Session not found")
        return {"status": "ok"}

    raise HTTPException(status_code=501, detail="Store does not support title updates")


@app.get("/api/sessions/search")
async def search_sessions(q: str = Query(""), limit: int = 20):
    """Search sessions by content (non-blocking)."""
    manager = get_memory_manager()
    results = await manager.search_sessions(q, limit=limit)
    return {"sessions": results}


@app.get("/api/memory/session")
async def get_session_memory(id: str = "", limit: int = 50):
    """Get session memory."""
    if not id:
        return []
    manager = get_memory_manager()
    return await manager.get_session_history(id, limit=limit)


def _export_session_json(entries: list, session_id: str) -> str:
    """Format session entries as JSON export."""
    from datetime import UTC, datetime

    messages = []
    for e in entries:
        ts = e.created_at.isoformat() if hasattr(e.created_at, "isoformat") else str(e.created_at)
        messages.append(
            {
                "id": e.id,
                "role": e.role or "user",
                "content": e.content,
                "timestamp": ts,
                "metadata": e.metadata,
            }
        )

    return json.dumps(
        {
            "export_version": "1.0",
            "exported_at": datetime.now(UTC).isoformat(),
            "session_id": session_id,
            "message_count": len(messages),
            "messages": messages,
        },
        indent=2,
        default=str,
    )


def _export_session_markdown(entries: list, session_id: str) -> str:
    """Format session entries as readable Markdown."""
    from datetime import UTC, datetime

    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    lines = [
        "# Conversation Export",
        f"**Session**: `{session_id}` | **Messages**: {len(entries)} | **Exported**: {now}",
        "",
        "---",
    ]

    for e in entries:
        role = (e.role or "user").capitalize()
        ts = ""
        if hasattr(e.created_at, "strftime"):
            ts = e.created_at.strftime("%H:%M")

        lines.append("")
        lines.append(f"**{role}** ({ts}):" if ts else f"**{role}**:")
        lines.append(e.content)
        lines.append("")
        lines.append("---")

    return "\n".join(lines)


@app.get("/api/memory/session/export")
async def export_session(id: str = "", format: str = "json"):
    """Export a session as downloadable JSON or Markdown."""
    if not id:
        raise HTTPException(status_code=400, detail="Missing required parameter: id")

    if format not in ("json", "md"):
        raise HTTPException(status_code=400, detail="Format must be 'json' or 'md'")

    manager = get_memory_manager()
    entries = await manager._store.get_session(id)

    if not entries:
        raise HTTPException(status_code=404, detail=f"Session not found: {id}")

    if format == "json":
        content = _export_session_json(entries, id)
        media_type = "application/json"
        ext = "json"
    else:
        content = _export_session_markdown(entries, id)
        media_type = "text/markdown"
        ext = "md"

    filename = f"pocketpaw-session-{id[:20]}.{ext}"
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/memory/long_term")
async def get_long_term_memory(limit: int = 50):
    """Get long-term memories."""
    manager = get_memory_manager()
    # Access store directly for filtered query, or use get_by_type if exposed
    # Manager doesn't expose get_by_type publically in facade (it used _store.get_by_type in get_context_for_agent)
    # So we use filtered search or we should expose it.
    # For now, let's use _store hack or add method to manager?
    # I'll rely on a new Manager method or _store for now to keep it simple.
    items = await manager._store.get_by_type(MemoryType.LONG_TERM, limit=limit)
    return [
        {
            "id": item.id,
            "content": item.content,
            "timestamp": item.created_at.isoformat(),
            "tags": item.tags,
        }
        for item in items
    ]


@app.delete("/api/memory/long_term/{entry_id}")
async def delete_long_term_memory(entry_id: str):
    """Delete a long-term memory entry by ID."""
    manager = get_memory_manager()
    deleted = await manager._store.delete(entry_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Memory entry not found")
    return {"ok": True}


@app.get("/api/audit")
async def get_audit_log(limit: int = 100):
    """Get audit logs."""
    logger = get_audit_logger()
    if not logger.log_path.exists():
        return []

    logs: list[dict] = []
    try:
        with open(logger.log_path) as f:
            lines = f.readlines()

        for line in reversed(lines):
            if len(logs) >= limit:
                break
            try:
                logs.append(json.loads(line))
            except Exception:
                pass
    except Exception:
        return []

    return logs


@app.delete("/api/audit")
async def clear_audit_log():
    """Clear the audit log file."""
    logger = get_audit_logger()
    try:
        if logger.log_path.exists():
            logger.log_path.write_text("")
        return {"ok": True}
    except Exception as e:
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/security-audit")
async def run_security_audit_endpoint():
    """Run security audit checks and return results."""
    from pocketpaw.security.audit_cli import (
        _check_audit_log,
        _check_bypass_permissions,
        _check_config_permissions,
        _check_file_jail,
        _check_guardian_reachable,
        _check_plaintext_api_keys,
        _check_tool_profile,
    )

    checks = [
        ("Config file permissions", _check_config_permissions),
        ("Plaintext API keys", _check_plaintext_api_keys),
        ("Audit log", _check_audit_log),
        ("Guardian agent", _check_guardian_reachable),
        ("File jail", _check_file_jail),
        ("Tool profile", _check_tool_profile),
        ("Bypass permissions", _check_bypass_permissions),
    ]

    results = []
    issues = 0
    for label, fn in checks:
        try:
            ok, message, fixable = fn()
            results.append(
                {
                    "check": label,
                    "passed": ok,
                    "message": message,
                    "fixable": fixable,
                }
            )
            if not ok:
                issues += 1
        except Exception as e:
            results.append(
                {
                    "check": label,
                    "passed": False,
                    "message": str(e),
                    "fixable": False,
                }
            )
            issues += 1

    total = len(results)
    return {"total": total, "passed": total - issues, "issues": issues, "results": results}


@app.get("/api/self-audit/reports")
async def get_self_audit_reports():
    """List recent self-audit reports."""
    from pocketpaw.config import get_config_dir

    reports_dir = get_config_dir() / "audit_reports"
    if not reports_dir.exists():
        return []

    import json

    reports = []
    for f in sorted(reports_dir.glob("*.json"), reverse=True)[:20]:
        try:
            data = json.loads(f.read_text())
            reports.append(
                {
                    "date": f.stem,
                    "total": data.get("total_checks", 0),
                    "passed": data.get("passed", 0),
                    "issues": data.get("issues", 0),
                }
            )
        except Exception:
            pass
    return reports


@app.get("/api/self-audit/reports/{date}")
async def get_self_audit_report(date: str):
    """Get a specific self-audit report by date."""
    import json

    from pocketpaw.config import get_config_dir

    report_path = get_config_dir() / "audit_reports" / f"{date}.json"
    if not report_path.exists():
        raise HTTPException(status_code=404, detail="Report not found")
    return json.loads(report_path.read_text())


@app.post("/api/self-audit/run")
async def run_self_audit_endpoint():
    """Trigger a self-audit run and return the report."""
    from pocketpaw.daemon.self_audit import run_self_audit

    report = await run_self_audit()
    return report


# ==================== Health Engine API ====================


@app.get("/api/health")
async def get_health_status():
    """Get current health engine summary."""
    try:
        from pocketpaw.health import get_health_engine

        engine = get_health_engine()
        return engine.summary
    except Exception as e:
        return {"status": "unknown", "check_count": 0, "issues": [], "error": str(e)}


@app.get("/api/health/errors")
async def get_health_errors(limit: int = 20, search: str = ""):
    """Get recent errors from the persistent error log."""
    try:
        from pocketpaw.health import get_health_engine

        engine = get_health_engine()
        return engine.get_recent_errors(limit=limit, search=search)
    except Exception:
        return []


@app.delete("/api/health/errors")
async def clear_health_errors():
    """Clear the persistent error log."""
    try:
        from pocketpaw.health import get_health_engine

        engine = get_health_engine()
        engine.error_store.clear()
        return {"cleared": True}
    except Exception as e:
        return {"cleared": False, "error": str(e)}


@app.post("/api/system/restart")
async def restart_server(request: Request):
    """Restart the server process so host/port changes take effect.

    Requires ``{"confirm": true}`` in the JSON body to prevent accidental restarts.
    Triggers uvicorn's graceful shutdown so FastAPI's ``shutdown`` event runs all cleanup.
    """
    from fastapi.responses import JSONResponse

    body = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except Exception:
            pass

    if not body.get("confirm"):
        return JSONResponse(
            {"error": 'Missing confirm flag. Send {"confirm": true} to restart.'},
            status_code=400,
        )

    settings = Settings.load()
    settings.save()

    if _uvicorn_server:
        _uvicorn_server.should_exit = True
    return {"restarting": True}


@app.post("/api/health/check")
async def trigger_health_check():
    """Run all health checks (startup + connectivity) and return results."""
    try:
        from pocketpaw.health import get_health_engine

        engine = get_health_engine()
        await engine.run_all_checks()
        summary = engine.summary
        # Broadcast to all connected clients
        await _broadcast_health_update(summary)
        return summary
    except Exception as e:
        return {"status": "unknown", "error": str(e)}


# =========================================================================
# Memory Settings API
# =========================================================================


@app.get("/api/memory/settings")
async def get_memory_settings():
    """Get current memory backend configuration."""
    settings = Settings.load()
    return {
        "memory_backend": settings.memory_backend,
        "memory_use_inference": settings.memory_use_inference,
        "mem0_llm_provider": settings.mem0_llm_provider,
        "mem0_llm_model": settings.mem0_llm_model,
        "mem0_embedder_provider": settings.mem0_embedder_provider,
        "mem0_embedder_model": settings.mem0_embedder_model,
        "mem0_vector_store": settings.mem0_vector_store,
        "mem0_ollama_base_url": settings.mem0_ollama_base_url,
        "mem0_auto_learn": settings.mem0_auto_learn,
    }


@app.post("/api/memory/settings")
async def save_memory_settings(request: Request):
    """Save memory backend configuration."""
    data = await request.json()
    settings = Settings.load()

    for key, value in data.items():
        settings_field = _MEMORY_CONFIG_KEYS.get(key)
        if settings_field:
            setattr(settings, settings_field, value)

    settings.save()

    # Clear settings cache so memory manager picks up new values
    from pocketpaw.config import get_settings as _get_settings

    _get_settings.cache_clear()

    # Force reload the memory manager with fresh settings
    from pocketpaw.memory import get_memory_manager

    manager = get_memory_manager(force_reload=True)
    agent_loop.memory = manager
    agent_loop.context_builder.memory = manager

    return {"status": "ok"}


@app.get("/api/memory/stats")
async def get_memory_stats():
    """Get memory backend statistics."""
    manager = get_memory_manager()
    store = manager._store

    if hasattr(store, "get_memory_stats"):
        return await store.get_memory_stats()

    # File backend basic stats
    return {
        "backend": "file",
        "total_memories": "N/A (use mem0 for stats)",
    }


def run_dashboard(
    host: str = "127.0.0.1",
    port: int = 8888,
    open_browser: bool = True,
    dev: bool = False,
):
    """Run the dashboard server."""

    print("\n" + "=" * 50)
    print("🐾 POCKETPAW WEB DASHBOARD")
    print("=" * 50)
    if dev:
        print("🔄 Development mode — auto-reload enabled")
    if host == "0.0.0.0":
        import socket

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
        except Exception:
            local_ip = "<your-server-ip>"
        print(f"\n🌐 Open http://{local_ip}:{port} in your browser")
        print(f"   (listening on all interfaces — {host}:{port})\n")
    else:
        print(f"\n🌐 Open http://localhost:{port} in your browser\n")

    if open_browser:
        _state._open_browser_url = f"http://localhost:{port}"

    if dev:
        import pathlib

        src_dir = str(pathlib.Path(__file__).resolve().parent)
        uvicorn.run(
            "pocketpaw.dashboard:app",
            host=host,
            port=port,
            reload=True,
            reload_dirs=[src_dir],
            reload_includes=["*.py", "*.html", "*.js", "*.css"],
            log_level="debug",
        )
    else:
        global _uvicorn_server
        config = uvicorn.Config(app, host=host, port=port)
        _uvicorn_server = uvicorn.Server(config)
        _uvicorn_server.run()


if __name__ == "__main__":
    run_dashboard()
