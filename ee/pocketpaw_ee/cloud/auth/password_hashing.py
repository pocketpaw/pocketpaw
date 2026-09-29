# ee/pocketpaw_ee/cloud/auth/password_hashing.py — the one password hasher the
# cloud auth stack uses, and the only way async code may call it.
#
# argon2 is a deliberate ~20 ms CPU + memory burn per call. Run on the event
# loop it stalls every request, WebSocket frame and SSE chunk in the process,
# so every async caller goes through ``hash_password`` / ``verify_and_update``,
# which run the call on a dedicated pool (argon2-cffi releases the GIL).
#
# Two bounds, both load-bearing:
#   * the pool has MAX_CONCURRENT_HASHES threads, so at most that many hashes
#     (and ``memory_cost`` of RAM each) run at once. A cancelled request does
#     not free its slot early: a running thread cannot be stopped, and the
#     slot is released by the thread finishing, not by the awaiting coroutine;
#   * MAX_PENDING_HASHES counts running + queued jobs. Past it a call fails
#     fast with 429 ``auth.busy`` instead of queueing, so a flood of one
#     endpoint cannot build an unbounded backlog in front of every login.
# The counter is released from the concurrent future's done-callback (worker
# thread, or cancel of a still-queued job), hence the threading lock.
#
# Parameters: argon2id at the OWASP Password Storage Cheat Sheet minimum
# (m=19 MiB, t=2, p=1). Rehash-on-login never lowers cost: a stored argon2 hash
# is rewritten only when every one of m/t/p is <= ours and one is lower, so the
# older pwdlib-default hashes (m=64 MiB, t=3, p=4) are kept as they are. bcrypt
# stays verify-only and is always rehashed to argon2id on login.
# A missing or unparseable stored hash verifies as a wrong password.

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any, TypeVar

import argon2
from fastapi_users.password import PasswordHelper, PasswordHelperProtocol
from pwdlib import PasswordHash
from pwdlib.exceptions import UnknownHashError
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

from pocketpaw_ee.cloud._core.errors import RateLimited

logger = logging.getLogger(__name__)

MAX_CONCURRENT_HASHES = 4
MAX_PENDING_HASHES = 64

_T = TypeVar("_T")


class _NoDowngradeArgon2Hasher(Argon2Hasher):
    """Argon2 hasher whose rehash check only ever raises cost."""

    def __init__(self, *, memory_cost: int, time_cost: int, parallelism: int) -> None:
        super().__init__(memory_cost=memory_cost, time_cost=time_cost, parallelism=parallelism)
        self._current = (memory_cost, time_cost, parallelism)

    def check_needs_rehash(self, hash: str | bytes) -> bool:
        stored = argon2.extract_parameters(hash if isinstance(hash, str) else hash.decode())
        pairs = list(
            zip(
                (stored.memory_cost, stored.time_cost, stored.parallelism),
                self._current,
                strict=True,
            )
        )
        return all(s <= c for s, c in pairs) and any(s < c for s, c in pairs)


password_helper = PasswordHelper(
    PasswordHash(
        (
            _NoDowngradeArgon2Hasher(memory_cost=19456, time_cost=2, parallelism=1),
            BcryptHasher(),
        )
    )
)

# Threads exit with the interpreter (ThreadPoolExecutor joins its workers at
# exit), so no explicit shutdown hook is needed.
_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_HASHES, thread_name_prefix="pwhash")
_pending = 0
_pending_lock = threading.Lock()


def _release(_: Future) -> None:
    global _pending
    with _pending_lock:
        _pending -= 1


async def _offload(fn: Callable[..., _T], *args: Any) -> _T:
    global _pending
    with _pending_lock:
        if _pending >= MAX_PENDING_HASHES:
            raise RateLimited("auth.busy", "Sign-in is busy right now. Try again in a moment.")
        _pending += 1
    cf = _executor.submit(fn, *args)
    cf.add_done_callback(_release)
    return await asyncio.wrap_future(cf)


def _safe_verify(
    helper: PasswordHelperProtocol, plain_password: str, hashed_password: Any
) -> tuple[bool, str | None]:
    try:
        return helper.verify_and_update(plain_password, hashed_password)
    except (TypeError, UnknownHashError):
        logger.warning("stored password hash is missing or unrecognised; treating as invalid")
        return False, None


async def hash_password(password: str, helper: PasswordHelperProtocol | None = None) -> str:
    """``helper.hash(password)`` on the bounded hashing pool."""
    return await _offload((helper or password_helper).hash, password)


async def verify_and_update(
    plain_password: str, hashed_password: Any, helper: PasswordHelperProtocol | None = None
) -> tuple[bool, str | None]:
    """``helper.verify_and_update`` on the bounded hashing pool.

    A None / unrecognised ``hashed_password`` returns ``(False, None)``.
    """
    return await _offload(_safe_verify, helper or password_helper, plain_password, hashed_password)
