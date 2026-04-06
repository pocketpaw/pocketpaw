# Skills router — list, search, install, remove, reload.
# Created: 2026-02-20

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request

from pocketpaw.api.v1.schemas.common import StatusResponse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Skills"])


@router.get("/skills")
async def list_installed_skills():
    """List all installed user-invocable skills."""
    from pocketpaw.skills import get_skill_loader

    loader = get_skill_loader()
    loader.reload()
    return [
        {"name": s.name, "description": s.description, "argument_hint": s.argument_hint}
        for s in loader.get_invocable()
    ]


@router.get("/skills/search")
async def search_skills_library(q: str = "", limit: int = Query(30, ge=1, le=100)):
    """Proxy search to skills.sh API."""
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


@router.post("/skills/install")
async def install_skill(request: Request):
    """Install a skill by cloning its GitHub repo."""
    import shutil
    import tempfile
    from pathlib import Path

    from pocketpaw.skills import get_skill_loader

    data = await request.json()
    source = data.get("source", "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="Missing 'source' field")

    if ".." in source or ";" in source or "|" in source or "&" in source:
        raise HTTPException(status_code=400, detail="Invalid source format")

    parts = source.split("/")
    if len(parts) < 2:
        raise HTTPException(status_code=400, detail="Source must be owner/repo or owner/repo/skill")

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
                raise HTTPException(status_code=500, detail=f"Clone failed: {err}")

            tmp = Path(tmpdir)
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
                raise HTTPException(
                    status_code=404,
                    detail=f"No SKILL.md found for '{skill_name or source}'",
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
        raise HTTPException(status_code=504, detail="Clone timed out (30s)")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Skill install failed")
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/skills/remove")
async def remove_skill(request: Request):
    """Remove an installed skill by deleting its directory."""
    import shutil
    from pathlib import Path

    from pocketpaw.skills import get_skill_loader

    data = await request.json()
    name = data.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Missing 'name' field")

    if ".." in name or "/" in name or ";" in name or "|" in name or "&" in name:
        raise HTTPException(status_code=400, detail="Invalid name format")

    for base in [Path.home() / ".agents" / "skills", Path.home() / ".pocketpaw" / "skills"]:
        skill_dir = base / name
        if skill_dir.is_dir() and (skill_dir / "SKILL.md").exists():
            shutil.rmtree(skill_dir)
            loader = get_skill_loader()
            loader.reload()
            return StatusResponse()

    raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")


@router.post("/skills/reload")
async def reload_skills():
    """Force reload skills from disk."""
    from pocketpaw.skills import get_skill_loader

    loader = get_skill_loader()
    skills = loader.reload()
    return {"status": "ok", "count": len([s for s in skills.values() if s.user_invocable])}
