"""Soul Protocol API endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, UploadFile

from pocketpaw.api.deps import require_scope

router = APIRouter(tags=["Soul"], dependencies=[Depends(require_scope("settings:read"))])


@router.get("/soul/status")
async def get_soul_status():
    """Return current soul state (mood, energy, personality, domains)."""
    from pocketpaw.soul.manager import get_soul_manager

    mgr = get_soul_manager()
    if mgr is None or mgr.soul is None:
        return {"enabled": False}

    soul = mgr.soul
    state = soul.state
    result: dict = {
        "enabled": True,
        "name": soul.name,
        "mood": getattr(state, "mood", None),
        "energy": getattr(state, "energy", None),
        "social_battery": getattr(state, "social_battery", None),
        "observe_count": mgr.observe_count,
    }

    if hasattr(soul, "self_model") and soul.self_model:
        try:
            images = soul.self_model.get_active_self_images(limit=5)
            result["domains"] = [
                {"domain": img.domain, "confidence": img.confidence} for img in images
            ]
        except Exception:
            pass

    return result


@router.post("/soul/export")
async def export_soul():
    """Save the current soul to its .soul file."""
    from pocketpaw.soul.manager import get_soul_manager

    mgr = get_soul_manager()
    if mgr is None or mgr.soul is None:
        return {"error": "Soul not enabled"}

    await mgr.save()
    return {"path": str(mgr.soul_file), "status": "exported"}


_ALLOWED_IMPORT_SUFFIXES = frozenset({".soul", ".yaml", ".yml", ".json"})


@router.post("/soul/import")
async def import_soul(file: UploadFile):
    """Import a soul from an uploaded .soul, .yaml, .yml, or .json file.

    Replaces the currently active soul with the imported one.
    Requires soul to be enabled in settings.
    """
    from pocketpaw.soul.manager import get_soul_manager

    mgr = get_soul_manager()
    if mgr is None:
        return {"error": "Soul not enabled. Enable it in Settings > Soul first."}

    # Validate file extension
    filename = file.filename or "upload"
    suffix = Path(filename).suffix.lower()
    if suffix not in _ALLOWED_IMPORT_SUFFIXES:
        return {
            "error": f"Unsupported file type: {suffix}. "
            f"Accepted: {', '.join(sorted(_ALLOWED_IMPORT_SUFFIXES))}"
        }

    # Save upload to a temp file in the soul directory
    from pocketpaw.config import get_config_dir

    import_dir = get_config_dir() / "soul" / "imports"
    import_dir.mkdir(parents=True, exist_ok=True)
    temp_path = import_dir / f"import{suffix}"

    try:
        content = await file.read()
        temp_path.write_bytes(content)

        name = await mgr.import_from_file(temp_path)
        return {"status": "imported", "name": name, "path": str(mgr.soul_file)}
    except (ValueError, FileNotFoundError) as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Import failed: {exc}"}
    finally:
        temp_path.unlink(missing_ok=True)


@router.post("/soul/import-path")
async def import_soul_from_path(body: dict):
    """Import a soul from a file path on the server's filesystem.

    Body: {"path": "/path/to/file.soul"} or {"path": "/path/to/config.yaml"}
    """
    from pocketpaw.soul.manager import get_soul_manager

    mgr = get_soul_manager()
    if mgr is None:
        return {"error": "Soul not enabled. Enable it in Settings > Soul first."}

    file_path = body.get("path", "")
    if not file_path:
        return {"error": "Missing 'path' field"}

    path = Path(file_path)

    # Sandbox: only allow paths within ~/.pocketpaw/soul/
    from pocketpaw.config import get_config_dir

    allowed_base = get_config_dir() / "soul"
    try:
        path.resolve().relative_to(allowed_base.resolve())
    except ValueError:
        return {"error": f"Path must be within {allowed_base}"}

    if not path.exists():
        return {"error": f"File not found: {file_path}"}

    suffix = path.suffix.lower()
    if suffix not in _ALLOWED_IMPORT_SUFFIXES:
        return {
            "error": f"Unsupported file type: {suffix}. "
            f"Accepted: {', '.join(sorted(_ALLOWED_IMPORT_SUFFIXES))}"
        }

    try:
        name = await mgr.import_from_file(path)
        return {"status": "imported", "name": name, "path": str(mgr.soul_file)}
    except (ValueError, FileNotFoundError) as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Import failed: {exc}"}
