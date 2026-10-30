"""Ask Paw: answer a question asked during a LiveKit call, out loud.

Runs in the API process. The call bot (``agent.py``, a subprocess) owns the
room; this module talks to it only through Redis:

- reads ``livekit:transcript:{group_id}`` (live segments the bot mirrors) and
  ``livekit:progressive:{group_id}`` (5-minute summaries) without consuming them;
- PUBLISHes commands on ``livekit:bot:cmd:{group_id}`` (``say``, ``say_end``,
  ``stop``, ``listen_start``, ``listen_end``);
- BLPOPs a spoken question from ``livekit:ask:q:{ask_id}`` after ``listen_end``,
  and ``livekit:ask:done:{ask_id}`` before going idle, so ``idle`` and the lock
  release wait (bounded) until the answer has actually played;
- holds ``livekit:ask:lock:{group_id}`` (SET NX EX, value = ask_id; the asker in
  ``livekit:ask:asker:{ask_id}``) so one call answers one question at a time.
  Only the holder releases it, except ``stop`` and the push-to-talk watchdog.

Every ask ends with exactly one ``idle``: after the answer, after ``error``, on
``stop`` (the route emits it), on a missed or abandoned spoken question, and when
the lock is lost mid-answer.

Grounding is the live call, the group's recent chat, the workspace knowledge base
and workspace-shared memory facts. A question too short to search on is looked up
with the latest call lines instead. The room has several members, so nothing
member-private is read: KB uses only the ``workspace:{id}`` scope (never
``user:``), memory facts attributed to one member are skipped, and the group chat
is left out while a guest is in the call or the room cannot be checked. Every
state change is emitted as ``call.assistant_state`` to the group's members.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import Awaitable
from typing import Any

from pocketpaw_ee.cloud._core.realtime.emit import emit
from pocketpaw_ee.cloud._core.realtime.events import CallAssistantState
from pocketpaw_ee.cloud.livekit.agent import (
    _REDIS_PROGRESSIVE_KEY,
    BOT_IDENTITY,
    PAW_SPEAKER,
    REDIS_ASK_DONE_KEY,
    REDIS_ASK_QUESTION_KEY,
    REDIS_BOT_CMD_CHANNEL,
    REDIS_TRANSCRIPT_KEY,
)
from pocketpaw_ee.cloud.livekit.llm import split_sentences, stream_text
from pocketpaw_ee.cloud.livekit.prompts import ASK_PAW_PROMPT

logger = logging.getLogger(__name__)

ASK_LOCK_KEY = "livekit:ask:lock:{group_id}"
ASK_LOCK_TTL_SECONDS = 120
ASK_ASKER_KEY = "livekit:ask:asker:{ask_id}"
ASK_LISTENING_KEY = "livekit:ask:listening:{ask_id}"
LISTEN_WATCHDOG_SECONDS = 25.0
ASK_STOPPED_KEY = "livekit:ask:stopped:{ask_id}"
ASK_STOPPED_TTL_SECONDS = 120
ASK_RATE_KEY = "livekit:ask:rate:{user_id}"
ASK_RATE_LIMIT = 10
ASK_RATE_WINDOW_SECONDS = 60

_GATHER_TIMEOUT_SECONDS = 1.5
# kb-go is a subprocess per search; spawn plus search often runs past 1.5s.
_KB_TIMEOUT_SECONDS = 3.0
_TRANSCRIPT_TAIL_SEGMENTS = 2000
_KB_MAX_CHARS = 4000
_MEMORY_FETCH_LIMIT = 200
_MEMORY_TOP_K = 8
_MIN_QUERY_TERMS = 2
_CHAT_MESSAGES = 20
_CHAT_MAX_CHARS = 3000
_PAW_EARLIER = "Paw (you, earlier)"
_FALLBACK_QUERY_LINES = 3
_SHARED_MEMORY_USER_IDS = (None, "", "default")

_PLAYBACK_BASE_SECONDS = 4.0
_PLAYBACK_PER_SENTENCE_SECONDS = 3.0
_PLAYBACK_MAX_SECONDS = 45.0

DIDNT_CATCH_THAT = "Sorry, I didn't catch that."
_APOLOGY = "Sorry, I couldn't answer that right now."

_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_REFRESH_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

_WORD = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "the and for are was were what when where which who why how this that with from "
    "have has had you your our we they them did does can could would should about "
    "into there their then than just paw".split()
)

_ask_tasks: dict[str, asyncio.Task] = {}
_watchdogs: set[asyncio.Task] = set()


class _AskStopped(Exception):
    """The ask lock moved on (``stop`` from any web process, or it expired); stop speaking."""


def _redis() -> Any:
    from pocketpaw_ee.cloud._core.redis_client import get_redis

    return get_redis()


def _blocking_redis() -> Any:
    from pocketpaw_ee.cloud._core.redis_client import get_blocking_redis

    return get_blocking_redis()


def new_ask_id() -> str:
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Lock, rate limit, bot commands
# ---------------------------------------------------------------------------


async def acquire_ask_lock(group_id: str, ask_id: str, user_id: str = "") -> bool:
    """Take the call's one question slot; ``user_id`` is recorded as the asker."""
    redis = _redis()
    key = ASK_LOCK_KEY.format(group_id=group_id)
    if not await redis.set(key, ask_id, nx=True, ex=ASK_LOCK_TTL_SECONDS):
        return False
    if user_id:
        await redis.set(ASK_ASKER_KEY.format(ask_id=ask_id), user_id, ex=ASK_LOCK_TTL_SECONDS)
    return True


async def ask_asker(ask_id: str) -> str | None:
    return await _redis().get(ASK_ASKER_KEY.format(ask_id=ask_id))


async def refresh_ask_lock(group_id: str, ask_id: str) -> bool:
    """Restart the lock's TTL for the answer phase, only if ``ask_id`` still holds it."""
    key = ASK_LOCK_KEY.format(group_id=group_id)
    return bool(await _redis().eval(_REFRESH_LUA, 1, key, ask_id, ASK_LOCK_TTL_SECONDS))


async def ask_lock_holder(group_id: str) -> str | None:
    return await _redis().get(ASK_LOCK_KEY.format(group_id=group_id))


async def release_ask_lock(group_id: str, ask_id: str) -> bool:
    """Release the lock only if ``ask_id`` still holds it."""
    key = ASK_LOCK_KEY.format(group_id=group_id)
    return bool(await _redis().eval(_RELEASE_LUA, 1, key, ask_id))


async def force_release_ask_lock(group_id: str) -> None:
    await _redis().delete(ASK_LOCK_KEY.format(group_id=group_id))


async def bot_in_call(group_id: str) -> bool:
    """True when a call bot is subscribed to this call's command channel."""
    channel = REDIS_BOT_CMD_CHANNEL.format(group_id=group_id)
    counts = await _redis().pubsub_numsub(channel)
    return any(int(count) > 0 for _, count in counts)


async def allow_ask(user_id: str) -> bool:
    """Fixed-window per-user rate limit: ``ASK_RATE_LIMIT`` asks a minute."""
    key = ASK_RATE_KEY.format(user_id=user_id)
    async with _redis().pipeline(transaction=True) as pipe:
        pipe.set(key, 0, ex=ASK_RATE_WINDOW_SECONDS, nx=True)
        pipe.incr(key)
        _, count = await pipe.execute()
    return int(count) <= ASK_RATE_LIMIT


async def publish_bot_command(group_id: str, command: dict[str, Any]) -> None:
    channel = REDIS_BOT_CMD_CHANNEL.format(group_id=group_id)
    await _redis().publish(channel, json.dumps(command))


async def emit_assistant_state(
    *,
    group_id: str,
    state: str,
    ask_id: str | None,
    asked_by: str | None,
    asked_by_name: str | None,
    question: str | None = None,
    answer: str | None = None,
    error: str | None = None,
) -> None:
    try:
        await emit(
            CallAssistantState(
                data={
                    "group_id": group_id,
                    "state": state,
                    "ask_id": ask_id,
                    "asked_by": asked_by,
                    "asked_by_name": asked_by_name,
                    "question": question,
                    "answer": answer,
                    "error": error,
                }
            )
        )
    except Exception:
        logger.warning("Ask Paw: failed to emit %s state for group %s", state, group_id)


def start_ask_task(group_id: str, coro: Awaitable[Any]) -> asyncio.Task:
    """Run an ask in the background, keeping a reference so ``stop`` can cancel it."""
    task = asyncio.ensure_future(coro)
    _ask_tasks[group_id] = task

    def _done(t: asyncio.Task) -> None:
        if _ask_tasks.get(group_id) is t:
            _ask_tasks.pop(group_id, None)

    task.add_done_callback(_done)
    return task


async def stop_ask(group_id: str) -> str | None:
    """Silence Paw now: cancel the running answer, tell the bot to stop, free the lock.

    Returns the ask_id that was stopped. It is marked stopped so an answer
    running in another web process ends quietly: the stop route emits that
    ask's one ``idle``.
    """
    holder = await ask_lock_holder(group_id)
    if holder:
        await _redis().set(ASK_STOPPED_KEY.format(ask_id=holder), "1", ex=ASK_STOPPED_TTL_SECONDS)
        await _redis().delete(ASK_LISTENING_KEY.format(ask_id=holder))
    task = _ask_tasks.pop(group_id, None)
    if task is not None and not task.done():
        task.cancel()
    try:
        await publish_bot_command(group_id, {"type": "stop", "ask_id": None})
    finally:
        await force_release_ask_lock(group_id)
    return holder


async def begin_listening(ask_id: str) -> None:
    await _redis().set(ASK_LISTENING_KEY.format(ask_id=ask_id), "1", ex=ASK_LOCK_TTL_SECONDS)


async def end_listening(ask_id: str) -> bool:
    """Close the listening phase; False if the watchdog (or stop) already closed it."""
    return bool(await _redis().delete(ASK_LISTENING_KEY.format(ask_id=ask_id)))


async def _listen_watchdog(group_id: str, ask_id: str, who: dict[str, str]) -> None:
    await asyncio.sleep(LISTEN_WATCHDOG_SECONDS)
    try:
        if not await end_listening(ask_id):
            return
        if not await release_ask_lock(group_id, ask_id):
            return
    except Exception:
        logger.warning("Ask Paw: listen watchdog for %s failed", ask_id, exc_info=True)
        return
    logger.info("Ask Paw: push-to-talk %s in group %s was never ended", ask_id, group_id)
    await emit_assistant_state(state="idle", **who)


def start_listen_watchdog(group_id: str, ask_id: str, user_id: str, asked_by_name: str) -> None:
    """End a push-to-talk ask whose ``end`` never arrives: release the lock, go idle."""
    task = asyncio.ensure_future(
        _listen_watchdog(group_id, ask_id, _who(group_id, ask_id, user_id, asked_by_name))
    )
    _watchdogs.add(task)
    task.add_done_callback(_watchdogs.discard)


async def abort_claimed_ask(group_id: str, ask_id: str, user_id: str, asked_by_name: str) -> None:
    """A step after taking the lock failed: free the slot and end the ask with idle."""
    try:
        await end_listening(ask_id)
        await release_ask_lock(group_id, ask_id)
    except Exception:
        logger.warning("Ask Paw: could not free the slot for %s", ask_id, exc_info=True)
    await emit_assistant_state(state="idle", **_who(group_id, ask_id, user_id, asked_by_name))


async def wait_for_playback(ask_id: str, sentences: int) -> bool:
    """Block until the bot reports the answer played out (or was cut), within a bound."""
    timeout = min(
        _PLAYBACK_BASE_SECONDS + _PLAYBACK_PER_SENTENCE_SECONDS * sentences,
        _PLAYBACK_MAX_SECONDS,
    )
    try:
        result = await _blocking_redis().blpop(
            [REDIS_ASK_DONE_KEY.format(ask_id=ask_id)], timeout=timeout
        )
    except Exception:
        logger.warning("Ask Paw: waiting for playback of %s failed", ask_id, exc_info=True)
        return False
    if not result:
        logger.info("Ask Paw: no playback-done signal for %s within %.0fs", ask_id, timeout)
    return bool(result)


async def _say_and_wait(group_id: str, ask_id: str, text: str) -> None:
    """Voice one fixed line and wait for it to play; failures only log."""
    try:
        await publish_bot_command(group_id, {"type": "say", "ask_id": ask_id, "text": text})
        await publish_bot_command(group_id, {"type": "say_end", "ask_id": ask_id})
    except Exception:
        logger.warning("Ask Paw: could not say %r for %s", text, ask_id, exc_info=True)
        return
    await wait_for_playback(ask_id, 1)


def _one_line(text: str) -> str:
    return " ".join(text.split())


async def _idle_unless_stopped(ask_id: str, question: str | None, who: dict[str, str]) -> None:
    """The lock moved on: stopped (that route already went idle) or expired (we go idle)."""
    try:
        stopped = bool(await _redis().exists(ASK_STOPPED_KEY.format(ask_id=ask_id)))
    except Exception:
        stopped = False
    if not stopped:
        await emit_assistant_state(state="idle", question=question, **who)


# ---------------------------------------------------------------------------
# Context gathering
# ---------------------------------------------------------------------------


async def read_live_transcript(group_id: str, max_chars: int = 12000) -> str:
    """Progressive summaries plus the most recent transcript lines within ``max_chars``.

    Read-only: the progressive key is consumed by the bot at call end, never here.
    """
    redis = _redis()
    raw_segments = await redis.lrange(
        REDIS_TRANSCRIPT_KEY.format(group_id=group_id), -_TRANSCRIPT_TAIL_SEGMENTS, -1
    )
    raw_summaries = await redis.lrange(_REDIS_PROGRESSIVE_KEY.format(group_id=group_id), 0, -1)

    summaries: list[str] = []
    for raw in raw_summaries or []:
        try:
            text = str(json.loads(raw).get("segment_summary") or "").strip()
        except (ValueError, AttributeError):
            continue
        if text:
            summaries.append(f"- {text}")

    lines: list[str] = []
    budget = max_chars
    for raw in reversed(raw_segments or []):
        try:
            seg = json.loads(raw)
        except ValueError:
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        speaker = seg.get("speaker") or "Someone"
        if seg.get("identity") == BOT_IDENTITY or speaker == PAW_SPEAKER:
            speaker = _PAW_EARLIER
        line = f"{speaker}: {text}"
        if len(line) + 1 > budget:
            break
        budget -= len(line) + 1
        lines.append(line)
    lines.reverse()

    parts: list[str] = []
    if summaries:
        parts.append("Earlier in the call (summaries):\n" + "\n".join(summaries))
    if lines:
        parts.append("Recent conversation:\n" + "\n".join(lines))
    return "\n\n".join(parts)


async def _kb_context(workspace_id: str, question: str) -> str:
    from pocketpaw_ee.cloud.agents.knowledge import KnowledgeService

    text = await KnowledgeService.search_context_for_scope(
        f"workspace:{workspace_id}", question, limit=3
    )
    return (text or "").strip()[:_KB_MAX_CHARS]


def _terms(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) > 2 and w not in _STOPWORDS}


def _lookup_query(question: str, call: str) -> str:
    """The question, plus the latest call lines when it has too few words to search on."""
    if len(_terms(question)) >= _MIN_QUERY_TERMS:
        return question
    recent = [
        line.removeprefix("- ").split(": ", 1)[-1]
        for line in call.splitlines()
        if line.strip() and not line.endswith(":")
    ][-_FALLBACK_QUERY_LINES:]
    return " ".join([question.strip(), *recent]).strip()


async def _memory_facts(workspace_id: str, question: str) -> list[str]:
    from pocketpaw_ee.cloud.memory.mongo_store import MongoMemoryStore

    entries = await MongoMemoryStore().list_facts_in_workspace(
        workspace_id, limit=_MEMORY_FETCH_LIMIT
    )
    wanted = _terms(question)
    if not wanted:
        return []
    scored: list[tuple[int, str]] = []
    for entry in entries:
        if (entry.metadata or {}).get("user_id") not in _SHARED_MEMORY_USER_IDS:
            continue
        score = len(wanted & _terms(entry.content))
        if score:
            scored.append((score, entry.content.strip()))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [content for _, content in scored[:_MEMORY_TOP_K]]


async def _group_chat(group_id: str) -> str:
    """The group's recent chat, newest last, or nothing while a guest is in the call."""
    from pocketpaw_ee.cloud.auth.service import resolve_display_names
    from pocketpaw_ee.cloud.chat import message_service
    from pocketpaw_ee.cloud.livekit.service import CALL_BOT_USER_ID, get_room_info

    room = await get_room_info(group_id)
    if room is None or any(
        str(p.get("identity") or "").startswith("guest-") for p in room.get("participants") or []
    ):
        return ""

    messages = await message_service.list_recent_for_group(group_id, limit=_CHAT_MESSAGES)
    human_ids = {m.sender for m in messages if m.sender_type != "agent" and m.sender}
    names = await resolve_display_names(human_ids) if human_ids else {}

    lines: list[str] = []
    budget = _CHAT_MAX_CHARS
    for m in reversed(messages):
        text = _one_line(m.content or "")
        if not text:
            continue
        if m.sender == CALL_BOT_USER_ID and m.sender_name == PAW_SPEAKER:
            name = _PAW_EARLIER
        elif m.sender_type == "agent":
            name = m.sender_name or m.agent or "An agent"
        else:
            name = m.sender_name or names.get(m.sender or "") or "A member"
        line = f"{_one_line(name)}: {text}"
        if len(line) + 1 > budget:
            break
        budget -= len(line) + 1
        lines.append(line)
    lines.reverse()
    return "\n".join(lines)


async def _bounded(
    label: str, coro: Awaitable[Any], default: Any, timeout: float = _GATHER_TIMEOUT_SECONDS
) -> Any:
    started = time.monotonic()
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except TimeoutError:
        logger.warning("Ask Paw: %s timed out after %.1fs", label, timeout)
    except Exception:
        logger.warning("Ask Paw: %s failed", label, exc_info=True)
    finally:
        logger.info("Ask Paw: %s took %.0fms", label, (time.monotonic() - started) * 1000)
    return default


def _who(group_id: str, ask_id: str, user_id: str, asked_by_name: str) -> dict[str, str]:
    return {
        "group_id": group_id,
        "ask_id": ask_id,
        "asked_by": user_id,
        "asked_by_name": asked_by_name,
    }


def _build_user_prompt(
    *, question: str, asked_by_name: str, call: str, chat: str, kb: str, facts: list[str]
) -> str:
    sections = [
        f"<call-context>\n{call or '(nothing transcribed yet)'}\n</call-context>",
        f"<group-chat>\n{chat or '(none)'}\n</group-chat>",
        f"<workspace-knowledge>\n{kb or '(none found)'}\n</workspace-knowledge>",
        "<workspace-memory>\n"
        + ("\n".join(f"- {f}" for f in facts) if facts else "(none found)")
        + "\n</workspace-memory>",
        f"{asked_by_name} says to you: {question}",
    ]
    return "\n\n".join(sections)


# ---------------------------------------------------------------------------
# Answering
# ---------------------------------------------------------------------------


async def answer_call_question(
    *,
    group_id: str,
    workspace_id: str,
    user_id: str,
    asked_by_name: str,
    question: str,
    ask_id: str,
) -> str | None:
    """Think, speak the answer sentence by sentence, record it in chat, then go idle.

    The caller must already hold the ask lock for ``ask_id``; it is released here.
    """
    who = _who(group_id, ask_id, user_id, asked_by_name)
    started = time.monotonic()
    spoken: list[str] = []
    try:
        if not await refresh_ask_lock(group_id, ask_id):
            raise _AskStopped
        await emit_assistant_state(state="thinking", question=question, **who)

        call = await _bounded("live transcript", read_live_transcript(group_id), "")
        query = _lookup_query(question, call)
        kb, facts, chat = await asyncio.gather(
            _bounded("knowledge base", _kb_context(workspace_id, query), "", _KB_TIMEOUT_SECONDS),
            _bounded("memory facts", _memory_facts(workspace_id, query), []),
            _bounded("group chat", _group_chat(group_id), ""),
        )
        gathered = time.monotonic()

        prompt = _build_user_prompt(
            question=question,
            asked_by_name=asked_by_name,
            call=call,
            chat=chat,
            kb=kb,
            facts=facts,
        )
        first_sentence_at: float | None = None
        async for sentence in split_sentences(stream_text(ASK_PAW_PROMPT, prompt)):
            if await ask_lock_holder(group_id) != ask_id:
                raise _AskStopped
            if not spoken:
                first_sentence_at = time.monotonic()
                await emit_assistant_state(state="speaking", question=question, **who)
            spoken.append(sentence)
            await publish_bot_command(group_id, {"type": "say", "ask_id": ask_id, "text": sentence})
        await publish_bot_command(group_id, {"type": "say_end", "ask_id": ask_id})

        answer = " ".join(spoken).strip()
        if not answer:
            raise RuntimeError("empty answer")
        logger.info(
            "Ask Paw: answered %s in group %s (gather %.0fms, first sentence %.0fms, total %.0fms)",
            ask_id,
            group_id,
            (gathered - started) * 1000,
            ((first_sentence_at or gathered) - started) * 1000,
            (time.monotonic() - started) * 1000,
        )

        try:
            from pocketpaw_ee.cloud.livekit.service import post_call_bot_message

            await post_call_bot_message(
                group_id,
                f"**Q ({_one_line(asked_by_name)}):** {_one_line(question)}\n"
                f"**Paw:** {_one_line(answer)}",
                "Paw",
            )
        except Exception:
            logger.warning("Ask Paw: could not post Q&A to group %s", group_id, exc_info=True)

        await wait_for_playback(ask_id, len(spoken))
        if await ask_lock_holder(group_id) != ask_id:
            raise _AskStopped
        await emit_assistant_state(state="idle", question=question, answer=answer, **who)
        return answer
    except asyncio.CancelledError:
        logger.info("Ask Paw: %s in group %s was stopped", ask_id, group_id)
        raise
    except _AskStopped:
        logger.info("Ask Paw: %s in group %s was stopped or timed out", ask_id, group_id)
        await _idle_unless_stopped(ask_id, question, who)
        return None
    except Exception:
        logger.warning("Ask Paw: answering %s in group %s failed", ask_id, group_id, exc_info=True)
        await emit_assistant_state(
            state="error",
            question=question,
            error="Paw couldn't answer that right now.",
            **who,
        )
        if spoken:
            try:
                await publish_bot_command(group_id, {"type": "say_end", "ask_id": ask_id})
                await wait_for_playback(ask_id, len(spoken))
            except Exception:
                logger.debug("Ask Paw: say_end after failure not sent", exc_info=True)
        else:
            await _say_and_wait(group_id, ask_id, _APOLOGY)
        await emit_assistant_state(state="idle", question=question, **who)
        return None
    finally:
        try:
            await release_ask_lock(group_id, ask_id)
        except Exception:
            logger.warning("Ask Paw: could not release lock for group %s", group_id)


async def capture_spoken_question(group_id: str, ask_id: str, timeout: float = 4.0) -> str | None:
    """Wait for the bot to hand back the words spoken during a listen window."""
    key = REDIS_ASK_QUESTION_KEY.format(ask_id=ask_id)
    result = await _blocking_redis().blpop([key], timeout=timeout)
    if not result:
        logger.info("Ask Paw: no spoken question for %s in group %s", ask_id, group_id)
        return None
    return str(result[1])


async def answer_spoken_question(
    *,
    group_id: str,
    workspace_id: str,
    user_id: str,
    asked_by_name: str,
    ask_id: str,
) -> str | None:
    """Collect the question the bot heard, then answer it (or say it missed it)."""
    who = _who(group_id, ask_id, user_id, asked_by_name)
    try:
        question = (await capture_spoken_question(group_id, ask_id) or "").strip()
    except Exception:
        logger.warning("Ask Paw: reading spoken question %s failed", ask_id, exc_info=True)
        question = ""
    try:
        still_held = await ask_lock_holder(group_id) == ask_id
    except Exception:
        still_held = True
    if not still_held:
        await _idle_unless_stopped(ask_id, question or None, who)
        return None
    if not question:
        await _say_and_wait(group_id, ask_id, DIDNT_CATCH_THAT)
        await emit_assistant_state(state="idle", **who)
        try:
            await release_ask_lock(group_id, ask_id)
        except Exception:
            logger.warning("Ask Paw: could not release lock for group %s", group_id)
        return None
    return await answer_call_question(
        group_id=group_id,
        workspace_id=workspace_id,
        user_id=user_id,
        asked_by_name=asked_by_name,
        question=question[:500],
        ask_id=ask_id,
    )
