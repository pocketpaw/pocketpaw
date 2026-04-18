"""Soul Protocol API endpoints.

Updated: 2026-03-29 — v0.2.8: Enriched /soul/status with did, focus, memory_count,
bond, core_memory. Added: GET/PATCH /soul/core-memory, POST /soul/remember,
POST /soul/recall, POST /soul/forget.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, UploadFile

from pocketpaw.api.deps import require_scope

logger = logging.getLogger(__name__)

router = APIRouter(tags=["Soul"], dependencies=[Depends(require_scope("settings:read"))])


@router.get("/soul/dashboard")
async def get_soul_dashboard():
    """Full soul dashboard data — identity, OCEAN, state, memories, bonds, evolution."""
    from pocketpaw.soul.manager import get_soul_manager

    mgr = get_soul_manager()
    if mgr is None or mgr.soul is None:
        return {"enabled": False}

    soul = mgr.soul
    state = soul.state

    # Identity
    # Calculate age safely (handle naive/aware datetime mismatch)
    age_days = 0
    born_iso = ""
    if hasattr(soul, "born") and soul.born:
        born_iso = soul.born.isoformat()
        try:
            from datetime import UTC, datetime

            now = datetime.now(UTC)
            born = soul.born if soul.born.tzinfo else soul.born.replace(tzinfo=UTC)
            age_days = (now - born).days
        except Exception:
            pass

    identity = {
        "name": soul.name,
        "archetype": getattr(soul, "archetype", ""),
        "did": getattr(soul, "did", ""),
        "born": born_iso,
        "age_days": age_days,
        "lifecycle": getattr(soul, "lifecycle", "active"),
        "values": list(getattr(soul, "values", [])),
        "incarnation": getattr(soul, "incarnation", 1),
    }

    # OCEAN personality
    ocean = {}
    if hasattr(soul, "ocean"):
        o = soul.ocean
        ocean = {
            "openness": getattr(o, "openness", 0.5),
            "conscientiousness": getattr(o, "conscientiousness", 0.5),
            "extraversion": getattr(o, "extraversion", 0.5),
            "agreeableness": getattr(o, "agreeableness", 0.5),
            "neuroticism": getattr(o, "neuroticism", 0.5),
        }

    # State
    soul_state = {
        "mood": getattr(state, "mood", "neutral"),
        "energy": getattr(state, "energy", 1.0),
        "social_battery": getattr(state, "social_battery", 1.0),
        "focus": getattr(state, "focus", "general"),
    }

    # Core memory
    core_memory = {"persona": "", "human": ""}
    if hasattr(soul, "get_core_memory"):
        try:
            cm = soul.get_core_memory()
            core_memory = cm.model_dump() if hasattr(cm, "model_dump") else {}
        except Exception:
            pass

    # Communication style
    communication = {
        "warmth": "medium",
        "verbosity": "low",
        "humor_style": "dry",
        "emoji_usage": "minimal",
    }
    if hasattr(soul, "communication"):
        c = soul.communication
        communication = {
            "warmth": getattr(c, "warmth", "medium"),
            "verbosity": getattr(c, "verbosity", "low"),
            "humor_style": getattr(c, "humor_style", "dry"),
            "emoji_usage": getattr(c, "emoji_usage", "minimal"),
        }

    # Skills
    skills = []
    if hasattr(soul, "skills"):
        for s in soul.skills:
            skills.append(
                {
                    "id": getattr(s, "id", ""),
                    "name": getattr(s, "name", ""),
                    "level": getattr(s, "level", 1),
                    "xp": getattr(s, "xp", 0),
                    "xp_to_next": getattr(s, "xp_to_next", 100),
                }
            )

    # Bond
    bond = None
    if hasattr(soul, "bond") and soul.bond:
        try:
            bond = {
                "bonded_to": getattr(soul.bond, "bonded_to", ""),
                "strength": getattr(soul.bond, "bond_strength", 0),
                "interaction_count": getattr(soul.bond, "interaction_count", 0),
                "bonded_at": soul.bond.bonded_at.isoformat()
                if hasattr(soul.bond, "bonded_at") and soul.bond.bonded_at
                else "",
            }
        except Exception:
            pass

    # Memory stats
    memory = {"episodic": 0, "semantic": 0, "procedural": 0, "graph_nodes": 0, "total": 0}
    if hasattr(soul, "memory_count"):
        memory["total"] = soul.memory_count

    # Evolution log
    evolution = []
    if hasattr(soul, "evolution_log"):
        for e in (soul.evolution_log or [])[-20:]:
            evolution.append(
                {
                    "id": getattr(e, "id", ""),
                    "trait": getattr(e, "trait", ""),
                    "old_value": str(getattr(e, "old_value", "")),
                    "new_value": str(getattr(e, "new_value", "")),
                    "reason": getattr(e, "reason", None),
                    "approved": getattr(e, "approved", None),
                    "proposed_at": e.proposed_at.isoformat()
                    if hasattr(e, "proposed_at") and e.proposed_at
                    else "",
                }
            )

    # Self model
    self_model = []
    if hasattr(soul, "self_model") and soul.self_model:
        try:
            images = soul.self_model.get_active_self_images(limit=10)
            self_model = [
                {
                    "domain": img.domain,
                    "confidence": img.confidence,
                    "evidence_count": getattr(img, "evidence_count", 0),
                }
                for img in images
            ]
        except Exception:
            pass

    return {
        "enabled": True,
        "identity": identity,
        "ocean": ocean,
        "state": soul_state,
        "core_memory": core_memory,
        "communication": communication,
        "skills": skills,
        "bond": bond,
        "memory": memory,
        "evolution": evolution,
        "self_model": self_model,
        "observe_count": mgr.observe_count,
    }


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
        "did": soul.did if hasattr(soul, "did") else None,
        "mood": getattr(state, "mood", None),
        "energy": getattr(state, "energy", None),
        "social_battery": getattr(state, "social_battery", None),
        "focus": getattr(state, "focus", None),
        "memory_count": soul.memory_count if hasattr(soul, "memory_count") else 0,
        "observe_count": mgr.observe_count,
    }

    # v0.2.8+: Include bond state
    if hasattr(soul, "bond") and soul.bond:
        try:
            result["bond"] = soul.bond.model_dump() if hasattr(soul.bond, "model_dump") else None
        except Exception:
            pass

    # v0.2.8+: Include core memory summary
    if hasattr(soul, "get_core_memory"):
        try:
            cm = soul.get_core_memory()
            result["core_memory"] = cm.model_dump() if hasattr(cm, "model_dump") else {}
        except Exception:
            pass

    if hasattr(soul, "self_model") and soul.self_model:
        try:
            images = soul.self_model.get_active_self_images(limit=5)
            result["domains"] = [
                {"domain": img.domain, "confidence": img.confidence} for img in images
            ]
        except Exception:
            pass

    return result


@router.get("/soul/core-memory")
async def get_core_memory():
    """Return the soul's core memory (persona and human description)."""
    from pocketpaw.soul.manager import get_soul_manager

    mgr = get_soul_manager()
    if mgr is None or mgr.soul is None:
        return {"error": "Soul not available"}
    if not hasattr(mgr.soul, "get_core_memory"):
        return {"error": "Requires soul-protocol >= 0.2.8."}
    try:
        cm = mgr.soul.get_core_memory()
        return cm.model_dump() if hasattr(cm, "model_dump") else {}
    except Exception as exc:
        return {"error": f"Failed: {exc}"}


@router.patch("/soul/core-memory")
async def edit_core_memory(body: dict):
    """Edit core memory. Body: {"persona": "...", "human": "..."}"""
    from pocketpaw.soul.manager import get_soul_manager

    mgr = get_soul_manager()
    if mgr is None or mgr.soul is None:
        return {"error": "Soul not available"}
    try:
        kwargs = {k: v for k, v in body.items() if k in ("persona", "human") and v}
        if not kwargs:
            return {"error": "Provide 'persona' or 'human'."}
        await mgr.soul.edit_core_memory(**kwargs)
        mgr._dirty = True
        logger.warning("Soul core memory edited: fields=%s", list(kwargs.keys()))
        return {"ok": True, "updated": list(kwargs.keys())}
    except Exception as exc:
        return {"error": f"Failed: {exc}"}


@router.post("/soul/remember")
async def soul_remember(body: dict):
    """Store a memory. Body: {"content": "...", "importance": 5}"""
    from pocketpaw.soul.manager import get_soul_manager

    mgr = get_soul_manager()
    if mgr is None or mgr.soul is None:
        return {"error": "Soul not available"}
    content = body.get("content", "")
    if not content:
        return {"error": "Missing 'content'"}
    try:
        importance = max(1, min(10, body.get("importance", 5)))
        mid = await mgr.soul.remember(content, importance=importance)
        mgr._dirty = True
        return {"ok": True, "memory_id": mid}
    except Exception as exc:
        return {"error": f"Failed: {exc}"}


@router.post("/soul/recall")
async def soul_recall(body: dict):
    """Search memories. Body: {"query": "...", "limit": 10}"""
    from pocketpaw.soul.manager import get_soul_manager

    mgr = get_soul_manager()
    if mgr is None or mgr.soul is None:
        return {"error": "Soul not available"}
    try:
        memories = await mgr.soul.recall(body.get("query", ""), limit=body.get("limit", 10))
        return [
            m.model_dump() if hasattr(m, "model_dump") else {"content": str(m)} for m in memories
        ]
    except Exception as exc:
        return {"error": f"Failed: {exc}"}


@router.post("/soul/forget")
async def soul_forget(body: dict):
    """Forget memories matching query. Body: {"query": "..."}"""
    from pocketpaw.soul.manager import get_soul_manager

    mgr = get_soul_manager()
    if mgr is None or mgr.soul is None:
        return {"error": "Soul not available"}
    if not body.get("query"):
        return {"error": "Missing 'query'"}
    result = await mgr.forget(body["query"])
    logger.warning("Soul forget executed: query=%r, result=%s", body["query"], result)
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


@router.post("/soul/reload")
async def reload_soul():
    """Reload the soul from its .soul file on disk (v0.2.4+).

    Useful when the file was modified by another client.
    """
    from pocketpaw.soul.manager import get_soul_manager

    mgr = get_soul_manager()
    if mgr is None or mgr.soul is None:
        return {"error": "Soul not enabled"}

    success = await mgr.reload()
    if success:
        return {"status": "reloaded", "name": mgr.soul.name}
    return {"error": "Reload failed. Check if the .soul file exists and is valid."}


@router.post("/soul/evaluate")
async def evaluate_soul(body: dict):
    """Run rubric-based self-evaluation on a response (v0.2.4+).

    Body: {"user_input": "...", "agent_output": "..."}
    Returns heuristic scores for 7 criteria.
    """
    from pocketpaw.soul.manager import get_soul_manager

    mgr = get_soul_manager()
    if mgr is None or mgr.soul is None:
        return {"error": "Soul not enabled"}

    user_input = body.get("user_input", "")
    agent_output = body.get("agent_output", "")
    if not user_input or not agent_output:
        return {"error": "Both 'user_input' and 'agent_output' are required"}

    result = await mgr.evaluate(user_input, agent_output)
    if result is None:
        return {"error": "Self-evaluation not available. Requires soul-protocol >= 0.2.4."}
    return {"status": "evaluated", "scores": result}


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
