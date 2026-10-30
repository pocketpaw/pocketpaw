# tests/cloud/auth/test_password_offload.py — password hashing stays off the
# event loop, at OWASP argon2id parameters, bounded, and never downgraded.
#
# What each test guards:
#   * every auth flow that hashes (register, login known/unknown/wrong,
#     forgot + reset, MFA disable + regenerate, guest mint + upgrade,
#     seed_admin) runs the hash on a worker thread, never the loop thread;
#   * an unknown email still costs one hash and gets the same 400 as a wrong
#     password (timing-attack mitigation);
#   * rehash-on-login only raises cost: a weaker argon2 (or bcrypt) hash is
#     rewritten, the older stronger 64 MiB hash and a mixed one are kept;
#   * the loop keeps ticking while 8 logins hash (a blocking fake stands in
#     for argon2 so the margin is not at the mercy of Windows' ~16 ms timer);
#   * the pool caps in-flight hashes, also across cancelled requests, and the
#     pending cap fails fast with 429 auth.busy (also through /auth/login);
#   * a missing / garbage stored hash is a wrong password, not a 500;
#   * guest upgrade runs the register password policy (weak + breached);
#   * MFA disable / regenerate are rate-limited per user;
#   * the UserManager overrides still match the upstream fastapi-users code
#     they were copied from.

from __future__ import annotations

import asyncio
import hashlib
import inspect
import os
import threading
import time

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

import pyotp
import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pocketpaw_ee.cloud._core.http import add_error_handler
from pocketpaw_ee.cloud.auth import guest as guest_service
from pocketpaw_ee.cloud.auth import password_hashing
from pocketpaw_ee.cloud.auth.core import UserCreate, UserManager, get_user_db
from pocketpaw_ee.cloud.auth.router import router as auth_router
from pocketpaw_ee.cloud.models.user import User
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

_EMAIL = "offload@example.com"
_PASSWORD = "StrongPass123!"
_NEW_PASSWORD = "NewerPass456!"
_FORM = {"Content-Type": "application/x-www-form-urlencoded"}
_NEW_PARAMS = "$argon2id$v=19$m=19456,t=2,p=1$"


class _Creds:
    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password


@pytest.fixture
def recorder(monkeypatch):
    """Record which thread every hash / verify on the shared helper runs on."""
    helper = password_hashing.password_helper
    calls: list[tuple[str, int]] = []
    real_hash, real_verify = helper.hash, helper.verify_and_update

    def _hash(password):
        calls.append(("hash", threading.get_ident()))
        return real_hash(password)

    def _verify(plain, hashed):
        calls.append(("verify", threading.get_ident()))
        return real_verify(plain, hashed)

    monkeypatch.setattr(helper, "hash", _hash)
    monkeypatch.setattr(helper, "verify_and_update", _verify)
    return calls


def _assert_off_loop(calls: list[tuple[str, int]]) -> None:
    loop_thread = threading.get_ident()
    assert calls, "expected at least one hash/verify call"
    on_loop = [kind for kind, ident in calls if ident == loop_thread]
    assert not on_loop, f"{on_loop} ran on the event loop thread"


async def _manager() -> UserManager:
    async for db in get_user_db():
        return UserManager(db)
    raise AssertionError("no user db")


def _app() -> FastAPI:
    app = FastAPI()
    add_error_handler(app)
    app.include_router(auth_router, prefix="/api/v1")
    return app


@pytest_asyncio.fixture
async def client(mongo_db):  # noqa: ARG001 — fixture initializes Beanie
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        yield c


async def _login(client: AsyncClient, email: str, password: str):
    return await client.post(
        "/api/v1/auth/login", data={"username": email, "password": password}, headers=_FORM
    )


# ---------------------------------------------------------------------------
# Every flow hashes off the loop
# ---------------------------------------------------------------------------


async def test_register_login_reset_run_hashes_off_the_loop(client, recorder, monkeypatch):
    resp = await client.post("/api/v1/auth/register", json={"email": _EMAIL, "password": _PASSWORD})
    assert resp.status_code == 201, resp.text
    stored = await User.find_one(User.email == _EMAIL)
    assert stored.hashed_password.startswith(_NEW_PARAMS)

    assert (await _login(client, _EMAIL, _PASSWORD)).status_code in (200, 204)
    wrong = await _login(client, _EMAIL, "WrongPass999!")
    assert wrong.status_code == 400

    # Unknown email: exactly one hash (timing protection), same error as wrong password.
    recorder.clear()
    unknown = await _login(client, "nobody@example.com", _PASSWORD)
    assert unknown.status_code == 400
    assert unknown.json() == wrong.json()
    assert [kind for kind, _ in recorder] == ["hash"]

    # Forgot + reset password.
    tokens: list[str] = []

    async def _capture(self, user, token, request=None):
        tokens.append(token)

    monkeypatch.setattr(UserManager, "on_after_forgot_password", _capture)
    resp = await client.post("/api/v1/auth/forgot-password", json={"email": _EMAIL})
    assert resp.status_code == 202, resp.text
    assert tokens
    resp = await client.post(
        "/api/v1/auth/reset-password", json={"token": tokens[0], "password": _NEW_PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    assert (await _login(client, _EMAIL, _PASSWORD)).status_code == 400
    assert (await _login(client, _EMAIL, _NEW_PASSWORD)).status_code in (200, 204)

    _assert_off_loop(recorder)


async def test_mfa_disable_and_regenerate_verify_off_the_loop(client, recorder):
    manager = await _manager()
    await manager.create(UserCreate(email=_EMAIL, password=_PASSWORD, is_verified=True))
    assert (await _login(client, _EMAIL, _PASSWORD)).status_code in (200, 204)

    secret = pyotp.random_base32()
    user = await User.find_one(User.email == _EMAIL)
    user.mfa_enabled = True
    user.mfa_totp_secret = secret
    await user.save()

    recorder.clear()
    bad = await client.post(
        "/api/v1/auth/mfa/backup-codes/regenerate",
        json={"password": "WrongPass999!", "code": pyotp.TOTP(secret).now()},
    )
    assert bad.status_code == 400 and "mfa_invalid_password" in bad.text
    regen = await client.post(
        "/api/v1/auth/mfa/backup-codes/regenerate",
        json={"password": _PASSWORD, "code": pyotp.TOTP(secret).now()},
    )
    assert regen.status_code == 200, regen.text
    disable = await client.post(
        "/api/v1/auth/mfa/disable",
        json={"password": _PASSWORD, "code": pyotp.TOTP(secret).now()},
    )
    assert disable.status_code == 200, disable.text
    assert [kind for kind, _ in recorder] == ["verify", "verify", "verify"]
    _assert_off_loop(recorder)


async def test_guest_mint_and_upgrade_hash_off_the_loop(mongo_db, recorder, monkeypatch):
    from unittest.mock import MagicMock

    from cryptography.fernet import Fernet

    monkeypatch.setenv("CLOUD_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr("pocketpaw_ee.cloud.workspace.service.get_resolver", lambda: MagicMock())

    async def _ok(api_key, **_kw):
        return None

    monkeypatch.setattr("pocketpaw_ee.cloud.byok.service.validate_key", _ok)

    guest = await guest_service.mint_guest("sk-ant-api03-" + "k" * 48)
    assert guest.hashed_password.startswith(_NEW_PARAMS)
    upgraded = await guest_service.upgrade_guest(guest, email="real@x.co", password=_PASSWORD)
    assert upgraded.id == guest.id

    manager = await _manager()
    assert await manager.authenticate(_Creds("real@x.co", _PASSWORD)) is not None
    assert [kind for kind, _ in recorder] == ["hash", "hash", "verify"]
    _assert_off_loop(recorder)


async def test_seed_admin_hashes_off_the_loop(mongo_db, recorder, monkeypatch):
    from pocketpaw_ee.cloud.auth import core

    monkeypatch.setenv("ADMIN_EMAIL", "seeded@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "Operator-Chosen-9!pass")
    assert await core.seed_admin() is not None
    _assert_off_loop(recorder)


# ---------------------------------------------------------------------------
# Parameters and rehash-on-login
# ---------------------------------------------------------------------------


async def _login_with_stored(manager: UserManager, stored: str) -> str:
    user = await User.find_one(User.email == _EMAIL)
    await manager.user_db.update(user, {"hashed_password": stored})
    assert await manager.authenticate(_Creds(_EMAIL, _PASSWORD)) is not None
    return (await User.find_one(User.email == _EMAIL)).hashed_password


async def test_older_stronger_hash_logs_in_and_is_kept(mongo_db):
    manager = await _manager()
    await manager.create(UserCreate(email=_EMAIL, password=_PASSWORD))
    old_hash = PasswordHash((Argon2Hasher(),)).hash(_PASSWORD)  # pwdlib default
    assert "$m=65536,t=3,p=4$" in old_hash
    assert await _login_with_stored(manager, old_hash) == old_hash
    # Mixed (lower memory, higher time) is not strictly weaker: kept too.
    mixed = PasswordHash((Argon2Hasher(memory_cost=8192, time_cost=3, parallelism=1),))
    mixed_hash = mixed.hash(_PASSWORD)
    assert await _login_with_stored(manager, mixed_hash) == mixed_hash
    assert await manager.authenticate(_Creds(_EMAIL, "WrongPass999!")) is None


async def test_weaker_hash_logs_in_and_is_rehashed(mongo_db):
    manager = await _manager()
    await manager.create(UserCreate(email=_EMAIL, password=_PASSWORD))
    weak = PasswordHash((Argon2Hasher(memory_cost=8192, time_cost=1, parallelism=1),))
    weak_hash = weak.hash(_PASSWORD)
    rehashed = await _login_with_stored(manager, weak_hash)
    assert rehashed.startswith(_NEW_PARAMS)
    # The rewritten hash still logs in, and is not rewritten again.
    assert await _login_with_stored(manager, rehashed) == rehashed


async def test_missing_or_garbage_stored_hash_is_a_wrong_password():
    for stored in (None, "", "not-a-hash", "$argon2id$garbage"):
        assert await password_hashing.verify_and_update(_PASSWORD, stored) == (False, None)


async def test_mfa_disable_with_unusable_stored_hash_is_400_not_500(client):
    manager = await _manager()
    await manager.create(UserCreate(email=_EMAIL, password=_PASSWORD, is_verified=True))
    assert (await _login(client, _EMAIL, _PASSWORD)).status_code in (200, 204)
    user = await User.find_one(User.email == _EMAIL)
    user.mfa_enabled = True
    user.mfa_totp_secret = pyotp.random_base32()
    user.hashed_password = "not-a-hash"  # e.g. a social-only account
    await user.save()
    resp = await client.post(
        "/api/v1/auth/mfa/disable",
        json={"password": _PASSWORD, "code": pyotp.TOTP(user.mfa_totp_secret).now()},
    )
    assert resp.status_code == 400 and "mfa_invalid_password" in resp.text


async def test_bcrypt_hash_still_verifies():
    from pwdlib.hashers.bcrypt import BcryptHasher

    legacy = BcryptHasher().hash(_PASSWORD)
    ok, updated = await password_hashing.verify_and_update(_PASSWORD, legacy)
    assert ok is True
    assert updated is not None and updated.startswith(_NEW_PARAMS)


# ---------------------------------------------------------------------------
# Loop responsiveness and the concurrency cap
# ---------------------------------------------------------------------------

_FAKE_HASH_SECONDS = 0.15


@pytest.fixture
def slow_helper(monkeypatch):
    """Stand in for argon2 with a GIL-releasing 150 ms sleep, tracking in-flight."""
    helper = password_hashing.password_helper
    state = {"in_flight": 0, "peak": 0}
    lock = threading.Lock()

    def _burn():
        with lock:
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
        time.sleep(_FAKE_HASH_SECONDS)
        with lock:
            state["in_flight"] -= 1

    def _hash(password):
        _burn()
        return "$fake$" + password

    def _verify(plain, hashed):
        _burn()
        return hashed == "$fake$" + plain, None

    monkeypatch.setattr(helper, "hash", _hash)
    monkeypatch.setattr(helper, "verify_and_update", _verify)
    return state


async def _max_gap_while(coro) -> tuple[float, object]:
    gaps: list[float] = []
    done = asyncio.Event()

    async def _ticker():
        last = time.perf_counter()
        while not done.is_set():
            await asyncio.sleep(0.005)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    ticker = asyncio.create_task(_ticker())
    try:
        result = await coro
    finally:
        done.set()
        await ticker
    return max(gaps), result


async def test_event_loop_keeps_ticking_during_eight_logins(mongo_db, slow_helper):
    manager = await _manager()
    await manager.create(UserCreate(email=_EMAIL, password=_PASSWORD))

    creds = [_Creds(_EMAIL, _PASSWORD)] * 4 + [_Creds("nobody@example.com", _PASSWORD)] * 4
    max_gap, results = await _max_gap_while(
        asyncio.gather(*(manager.authenticate(c) for c in creds))
    )
    assert sum(r is not None for r in results) == 4
    # Blocking the loop would show one gap of >= 150 ms per call (1.2 s total);
    # offloaded, the ticker only sees timer granularity (~16 ms on Windows).
    assert max_gap < _FAKE_HASH_SECONDS / 2, f"loop stalled for {max_gap * 1000:.0f} ms"


async def _drain() -> None:
    for _ in range(200):
        if password_hashing._pending == 0:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{password_hashing._pending} hash jobs never finished")


async def test_pool_caps_concurrent_hashes(slow_helper):
    await asyncio.gather(*(password_hashing.hash_password(f"p{i}") for i in range(12)))
    assert slow_helper["peak"] == password_hashing.MAX_CONCURRENT_HASHES


async def test_cancelled_requests_do_not_free_running_slots(slow_helper):
    first = [asyncio.create_task(password_hashing.hash_password(f"a{i}")) for i in range(4)]
    await asyncio.sleep(0.03)  # all four threads are now burning
    for task in first:
        task.cancel()
    await asyncio.gather(*first, return_exceptions=True)
    # The cancelled jobs' threads are still burning, so they still hold slots.
    assert password_hashing._pending == 4
    await asyncio.gather(*(password_hashing.hash_password(f"b{i}") for i in range(4)))
    # A slot freed on cancel would have let 8 hashes run at once.
    assert slow_helper["peak"] == password_hashing.MAX_CONCURRENT_HASHES
    await _drain()


async def test_pending_cap_fails_fast_with_auth_busy(slow_helper, monkeypatch):
    from pocketpaw_ee.cloud._core.errors import RateLimited

    monkeypatch.setattr(password_hashing, "MAX_PENDING_HASHES", 6)
    results = await asyncio.gather(
        *(password_hashing.hash_password(f"p{i}") for i in range(10)), return_exceptions=True
    )
    busy = [r for r in results if isinstance(r, RateLimited)]
    assert len(busy) == 4 and sum(isinstance(r, str) for r in results) == 6
    assert busy[0].status_code == 429 and busy[0].code == "auth.busy"
    await _drain()
    # Slots come back once the jobs finish.
    assert (await password_hashing.hash_password("again")).startswith("$fake$")


async def test_busy_login_is_a_429_not_a_500(client, monkeypatch):
    manager = await _manager()
    await manager.create(UserCreate(email=_EMAIL, password=_PASSWORD))
    monkeypatch.setattr(password_hashing, "MAX_PENDING_HASHES", 0)
    resp = await _login(client, _EMAIL, _PASSWORD)
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["code"] == "auth.busy"


async def test_shared_helper_uses_owasp_params():
    hashed = await password_hashing.hash_password(_PASSWORD)
    assert hashed.startswith(_NEW_PARAMS)
    manager = UserManager(user_db=None)
    assert manager.password_helper is password_hashing.password_helper


# ---------------------------------------------------------------------------
# Guest upgrade password policy and the MFA password limiter
# ---------------------------------------------------------------------------


async def _guest() -> User:
    doc = User(
        email=f"guest-{time.monotonic_ns()}@guest.invalid",
        hashed_password="x",
        is_active=True,
        is_guest=True,
    )
    await doc.insert()
    return doc


async def test_guest_upgrade_rejects_weak_and_breached_passwords(mongo_db, monkeypatch):
    from fastapi_users.exceptions import InvalidPasswordException
    from pocketpaw_ee.cloud.auth import password_policy

    guest = await _guest()
    with pytest.raises(InvalidPasswordException) as weak:
        await guest_service.upgrade_guest(guest, email="real@x.co", password="alllowercase1!")
    assert weak.value.reason == "missing_uppercase"

    async def _breached(_password):
        return True

    monkeypatch.setenv("POCKETPAW_HIBP_ENABLED", "true")
    monkeypatch.setattr(password_policy, "_is_breached", _breached)
    with pytest.raises(InvalidPasswordException) as breached:
        await guest_service.upgrade_guest(guest, email="real@x.co", password=_PASSWORD)
    assert breached.value.reason == "breached"
    fresh = await User.get(guest.id)
    assert fresh.is_guest is True and fresh.hashed_password == "x"


async def test_guest_upgrade_route_answers_with_the_register_error_shape(mongo_db):
    from pocketpaw_ee.cloud.auth.core import current_active_user

    guest = await _guest()
    app = _app()
    app.dependency_overrides[current_active_user] = lambda: guest
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(
            "/api/v1/auth/guest/upgrade", json={"email": "real@x.co", "password": "weakpass1!"}
        )
    assert resp.status_code == 400, resp.text
    assert resp.json() == {
        "detail": {"code": "REGISTER_INVALID_PASSWORD", "reason": "missing_uppercase"}
    }


@pytest.mark.parametrize(
    "path", ["/api/v1/auth/mfa/disable", "/api/v1/auth/mfa/backup-codes/regenerate"]
)
async def test_mfa_password_endpoints_are_rate_limited_per_user(client, path):
    manager = await _manager()
    await manager.create(UserCreate(email=_EMAIL, password=_PASSWORD, is_verified=True))
    assert (await _login(client, _EMAIL, _PASSWORD)).status_code in (200, 204)
    secret = pyotp.random_base32()
    user = await User.find_one(User.email == _EMAIL)
    user.mfa_enabled = True
    user.mfa_totp_secret = secret
    await user.save()

    for _ in range(5):
        bad = await client.post(path, json={"password": "WrongPass999!", "code": "000000"})
        assert bad.status_code == 400
    # Sixth attempt is refused even with the right password.
    resp = await client.post(path, json={"password": _PASSWORD, "code": pyotp.TOTP(secret).now()})
    assert resp.status_code == 429 and "mfa_too_many_attempts" in resp.text


# ---------------------------------------------------------------------------
# Upstream drift guard
# ---------------------------------------------------------------------------

# sha256[:16] of each upstream method's source in fastapi-users 15.0.5, the
# version UserManager's overrides in auth/core.py were copied from.
_UPSTREAM_SOURCE = {
    "create": "3d1328456778ffce",
    "authenticate": "e453ea340730cf50",
    "forgot_password": "785379634e5059ec",
    "reset_password": "f0c5df8b88801729",
    "_update": "1c49e7a79f770662",
}


def test_overrides_still_match_upstream_fastapi_users():
    import fastapi_users
    from fastapi_users.manager import BaseUserManager

    assert fastapi_users.__version__ == "15.0.5", (
        "fastapi-users changed version: re-diff the UserManager hashing overrides in "
        "ee/pocketpaw_ee/cloud/auth/core.py against the new BaseUserManager, then "
        "update this pin."
    )
    for name, expected in _UPSTREAM_SOURCE.items():
        source = inspect.getsource(getattr(BaseUserManager, name)).replace("\r\n", "\n")
        got = hashlib.sha256(source.encode()).hexdigest()[:16]
        assert got == expected, (
            f"BaseUserManager.{name} changed upstream: re-diff UserManager.{name} in "
            "ee/pocketpaw_ee/cloud/auth/core.py and update this pin."
        )
        assert name in vars(UserManager), f"UserManager no longer overrides {name}"
