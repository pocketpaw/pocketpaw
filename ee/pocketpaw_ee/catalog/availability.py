# ee/pocketpaw_ee/catalog/availability.py — per-workspace "out of credit" marks
# for the model picker.
#
# When a chat run fails because the provider account has no credit (the
# ``agent.provider_out_of_credit`` frame from run_core), run_core calls
# ``record_out_of_credit``; when a run completes it calls ``clear``. The catalog
# router reads the marks back with ``overlay`` so the picker can say "Out of
# credit" before the user starts a build on that model.
#
# What a mark covers. Credit belongs to an account, not a model, so a mark is
# keyed on the provider parsed from the model id ("deepseek/..." -> deepseek)
# and covers every model of that provider. A turn paid by the workspace's own
# key (BYOK) marks "*": that key pays for every model, so all of them are out.
# A model id with no provider prefix (the agent default, "litellm:claude-...")
# is marked by its bare name.
#
# Storage: one Redis hash per workspace, field = subject, value = JSON with an
# absolute ``until``. The hash's own EXPIRE is refreshed on each write, and a
# read drops fields past their ``until``, so a mark clears itself after
# ``ttl_seconds()`` (default 20 min) once credit is added. Workspace isolation
# is the key: no read ever names another workspace's hash.
#
# Invariant: every function here is best-effort. Redis unset or down reads as
# "nothing marked" and a write failure is logged at debug; a chat run or a
# catalog read must never fail because of this module. No key or provider text
# is stored, only the model id and a fixed sentence.

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

from pocketpaw_ee.catalog.models import ModelAvailability, ModelCatalogEntry
from pocketpaw_ee.cloud._core import redis_client

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 20 * 60
_KEY_PREFIX = "pocketpaw:model-availability:"
_ALL = "*"

PROVIDER_MESSAGE = (
    "This provider ran out of credit on a recent run. Add credit, or pick another model."
)
BYOK_MESSAGE = (
    "Your API key's account ran out of credit on a recent run. Add credit with your "
    "provider, or switch keys."
)


class _Entry(Protocol):
    id: str
    provider: str


def ttl_seconds() -> int:
    raw = os.environ.get("POCKETPAW_MODEL_AVAILABILITY_TTL_SECONDS", "").strip()
    try:
        val = int(raw) if raw else _DEFAULT_TTL_SECONDS
    except ValueError:
        return _DEFAULT_TTL_SECONDS
    return val if val > 0 else _DEFAULT_TTL_SECONDS


def _now() -> float:
    return time.time()


def _key(workspace_id: str) -> str:
    return f"{_KEY_PREFIX}{workspace_id}"


def _normalize(model: str) -> str:
    """Drop a pydantic_ai ``provider:`` prefix ("litellm:claude-x" -> "claude-x").

    Only when the part before the colon has no slash, so a tag such as
    "ollama/llama3:8b" is left alone.
    """
    model = (model or "").strip()
    head, sep, rest = model.partition(":")
    if sep and "/" not in head and rest:
        return rest
    return model


def _subject(model: str, *, byok: bool) -> str | None:
    if byok:
        return _ALL
    model = _normalize(model)
    if not model:
        return None
    if "/" in model:
        return f"provider:{model.split('/', 1)[0].lower()}"
    return f"model:{model.lower()}"


async def record_out_of_credit(workspace_id: str, model: str, *, byok: bool) -> None:
    """Mark ``model``'s provider (or every model, for BYOK) out of credit."""
    subject = _subject(model, byok=byok)
    if not workspace_id or subject is None:
        return
    now = _now()
    ttl = ttl_seconds()
    try:
        redis = redis_client.get_redis()
        existing = await redis.hget(_key(workspace_id), subject)
        since = now
        if existing:
            try:
                prior = json.loads(existing)
                if float(prior.get("until", 0)) > now:
                    since = float(prior.get("since_ts", now))
            except (ValueError, TypeError):
                pass
        value = {
            "since_ts": since,
            "until": now + ttl,
            "byok": byok,
        }
        await redis.hset(_key(workspace_id), subject, json.dumps(value))
        await redis.expire(_key(workspace_id), ttl)
    except Exception:
        logger.debug("model availability: record failed for ws=%s", workspace_id, exc_info=True)


async def clear(workspace_id: str, model: str, *, byok: bool) -> None:
    """A run on ``model`` succeeded: drop the marks it disproves."""
    if not workspace_id:
        return
    subjects = {s for s in (_subject(model, byok=False),) if s}
    if byok:
        subjects.add(_ALL)
    if not subjects:
        return
    try:
        await redis_client.get_redis().hdel(_key(workspace_id), *subjects)
    except Exception:
        logger.debug("model availability: clear failed for ws=%s", workspace_id, exc_info=True)


async def read_marks(workspace_id: str) -> dict[str, dict[str, Any]]:
    """The workspace's unexpired marks, subject -> stored value."""
    if not workspace_id:
        return {}
    try:
        raw = await redis_client.get_redis().hgetall(_key(workspace_id))
    except Exception:
        logger.debug("model availability: read failed for ws=%s", workspace_id, exc_info=True)
        return {}
    now = _now()
    marks: dict[str, dict[str, Any]] = {}
    for subject, value in (raw or {}).items():
        try:
            data = json.loads(value)
            if float(data.get("until", 0)) > now:
                marks[subject] = data
        except (ValueError, TypeError):
            continue
    return marks


def availability_for(entry: _Entry, marks: dict[str, dict[str, Any]]) -> ModelAvailability:
    """``entry``'s availability under ``marks`` (from ``read_marks``)."""
    if not marks:
        return ModelAvailability()
    model_id = entry.id.lower()
    candidates = (
        _ALL,
        f"provider:{(entry.provider or '').lower()}",
        f"model:{model_id}",
        f"model:{model_id.rsplit('/', 1)[-1]}",
    )
    for subject in candidates:
        mark = marks.get(subject)
        if mark is None:
            continue
        since_ts = mark.get("since_ts")
        when = datetime.fromtimestamp(float(since_ts), tz=UTC) if since_ts is not None else None
        return ModelAvailability(
            status="out_of_credit",
            since=when,
            message=BYOK_MESSAGE if subject == _ALL else PROVIDER_MESSAGE,
        )
    return ModelAvailability()


async def overlay(
    workspace_id: str | None, entries: Iterable[ModelCatalogEntry]
) -> list[ModelCatalogEntry]:
    """Copies of ``entries`` carrying this workspace's availability.

    The catalog cache is shared by every tenant, so entries are copied, never
    written. Without a workspace the copies leave ``availability`` unset.
    """
    entries = list(entries)
    if not workspace_id:
        return entries
    marks = await read_marks(workspace_id)
    return [e.model_copy(update={"availability": availability_for(e, marks)}) for e in entries]


__all__ = [
    "BYOK_MESSAGE",
    "PROVIDER_MESSAGE",
    "availability_for",
    "clear",
    "overlay",
    "read_marks",
    "record_out_of_credit",
    "ttl_seconds",
]
