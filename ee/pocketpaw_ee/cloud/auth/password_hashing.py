# ee/pocketpaw_ee/cloud/auth/password_hashing.py — the one password hasher the
# cloud auth stack uses, and the only way async code may call it.
#
# argon2 is a deliberate ~20 ms CPU + memory burn per call. Run on the event
# loop it stalls every request, WebSocket frame and SSE chunk in the process
# for that long, so every async caller goes through ``hash_password`` /
# ``verify_and_update`` here, which run the call on a worker thread
# (argon2-cffi releases the GIL, so threads give real parallelism).
#
# A semaphore caps concurrent hashes at MAX_CONCURRENT_HASHES: each call
# allocates ``memory_cost`` of RAM, so an unbounded burst of logins would
# multiply that. It is rebuilt per event loop because an asyncio.Semaphore
# binds to the loop it is first contended on.
#
# Parameters: argon2id at the OWASP Password Storage Cheat Sheet minimum
# (m=19 MiB, t=2, p=1). Hashes made with pwdlib's older default (m=64 MiB,
# t=3, p=4) still verify, and ``verify_and_update`` returns a new-params hash
# for them, which UserManager.authenticate saves on login. bcrypt stays as a
# verify-only fallback, same as fastapi-users' default helper.

from __future__ import annotations

import asyncio

from fastapi_users.password import PasswordHelper, PasswordHelperProtocol
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from pwdlib.hashers.bcrypt import BcryptHasher

MAX_CONCURRENT_HASHES = 4

password_helper = PasswordHelper(
    PasswordHash(
        (
            Argon2Hasher(memory_cost=19456, time_cost=2, parallelism=1),
            BcryptHasher(),
        )
    )
)

# One process serves one loop, so a single cached (loop, semaphore) pair is
# enough; it is rebuilt when a new loop shows up (tests, restarts). No weakref
# dict: uvloop's Loop is not guaranteed to be weak-referenceable.
_sem: asyncio.Semaphore | None = None
_sem_loop: asyncio.AbstractEventLoop | None = None


def _semaphore() -> asyncio.Semaphore:
    global _sem, _sem_loop
    loop = asyncio.get_running_loop()
    if _sem is None or _sem_loop is not loop:
        _sem, _sem_loop = asyncio.Semaphore(MAX_CONCURRENT_HASHES), loop
    return _sem


async def hash_password(password: str, helper: PasswordHelperProtocol | None = None) -> str:
    """``helper.hash(password)`` on a worker thread, concurrency-capped."""
    helper = helper or password_helper
    async with _semaphore():
        return await asyncio.to_thread(helper.hash, password)


async def verify_and_update(
    plain_password: str, hashed_password: str, helper: PasswordHelperProtocol | None = None
) -> tuple[bool, str | None]:
    """``helper.verify_and_update`` on a worker thread, concurrency-capped."""
    helper = helper or password_helper
    async with _semaphore():
        return await asyncio.to_thread(helper.verify_and_update, plain_password, hashed_password)
