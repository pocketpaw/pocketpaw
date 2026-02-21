# Backends router — list, install.
# Created: 2026-02-20

from __future__ import annotations

import importlib
import logging
import shutil

from fastapi import APIRouter, Depends, Request

from pocketpaw.api.deps import require_scope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Backends"])

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
    verify = hint.get("verify_import")
    if verify:
        try:
            mod = importlib.import_module(verify)
            attr = hint.get("verify_attr")
            if attr and not hasattr(mod, attr):
                return False
        except ImportError:
            return False
    binary = _CLI_BINARY.get(info.name)
    if binary and not shutil.which(binary):
        return False
    return True


@router.get("/backends")
async def list_available_backends():
    """List all registered agent backends with availability and capabilities."""
    from pocketpaw.agents.backend import Capability
    from pocketpaw.agents.registry import get_backend_class, get_backend_info, list_backends

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


@router.post("/backends/install", dependencies=[Depends(require_scope("admin"))])
async def install_backend(request: Request):
    """Auto-install a pip-installable backend SDK."""
    import asyncio
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
            cmd = [uv, "pip", "install"]
            if not in_venv:
                cmd.append("--system")
            cmd.append(pip_spec)
        else:
            cmd = ["pip", "install"]
            if not in_venv:
                cmd.append("--user")
            cmd.append(pip_spec)

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to install {pip_spec}:\n{result.stderr.strip()}")

        importlib.invalidate_caches()
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

    import sys as _sys

    for key in list(_sys.modules):
        if key.startswith("pocketpaw.agents."):
            del _sys.modules[key]
    importlib.invalidate_caches()

    return {"status": "ok"}
