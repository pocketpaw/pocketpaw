# tests/cloud/auth/test_insecure_first_boot.py
# Created: 2026-06-10 (security W0e — insecure-by-default first boot).
#
# Verifies the two hardening guarantees added to ee/cloud/auth/core.py:
#   1. Production posture refuses to boot when AUTH_SECRET is unset or still
#      the public placeholder; dev posture substitutes an ephemeral random
#      secret (never the public default) and warns instead of crashing.
#   2. seed_admin() never seeds the hardcoded "admin123", never logs the
#      password, prefers an operator-supplied ADMIN_PASSWORD, rejects the
#      legacy default, and discloses a generated password only on stdout.

from __future__ import annotations

import os

os.environ.setdefault("POCKETPAW_HIBP_ENABLED", "false")

import logging

import pytest
from pocketpaw_ee.cloud.auth import core
from pocketpaw_ee.cloud.auth.password_policy import validate_password_async

_DEFAULT = "change-me-in-production-please"


# ---------------------------------------------------------------------------
# Posture detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["production", "PRODUCTION", "prod", "Prod"])
def test_is_production_true_for_env(monkeypatch, value):
    monkeypatch.setenv("POCKETPAW_ENV", value)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
    assert core._is_production() is True


def test_is_production_true_for_secure_cookie(monkeypatch):
    monkeypatch.delenv("POCKETPAW_ENV", raising=False)
    monkeypatch.setenv("POCKETPAW_AUTH_COOKIE_SECURE", "true")
    assert core._is_production() is True


@pytest.mark.parametrize("env", ["", "development", "dev", "test", "local", "staging"])
def test_is_production_false_for_non_prod(monkeypatch, env):
    monkeypatch.setenv("POCKETPAW_ENV", env)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
    assert core._is_production() is False


def test_is_production_false_when_nothing_set(monkeypatch):
    monkeypatch.delenv("POCKETPAW_ENV", raising=False)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
    assert core._is_production() is False


# ---------------------------------------------------------------------------
# Secret gate — the core "ownable tenant" fix
# ---------------------------------------------------------------------------


def test_prod_boot_raises_when_secret_unset(monkeypatch):
    monkeypatch.setenv("POCKETPAW_ENV", "production")
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
    with pytest.raises(RuntimeError) as exc:
        core._resolve_secret()
    assert "AUTH_SECRET" in str(exc.value)


def test_prod_boot_raises_when_secret_is_default(monkeypatch):
    monkeypatch.setenv("POCKETPAW_ENV", "production")
    monkeypatch.setenv("AUTH_SECRET", _DEFAULT)
    with pytest.raises(RuntimeError) as exc:
        core._resolve_secret()
    assert "placeholder" in str(exc.value).lower() or "production" in str(exc.value).lower()


def test_prod_boot_raises_via_secure_cookie_signal(monkeypatch):
    """A TLS-terminated deployment (Secure cookies) is prod even without
    POCKETPAW_ENV — it must still refuse the default secret."""
    monkeypatch.delenv("POCKETPAW_ENV", raising=False)
    monkeypatch.setenv("POCKETPAW_AUTH_COOKIE_SECURE", "true")
    monkeypatch.setenv("AUTH_SECRET", _DEFAULT)
    with pytest.raises(RuntimeError):
        core._resolve_secret()


def test_prod_boot_accepts_real_secret(monkeypatch):
    monkeypatch.setenv("POCKETPAW_ENV", "production")
    monkeypatch.setenv("AUTH_SECRET", "a-genuinely-strong-random-secret-value")
    assert core._resolve_secret() == "a-genuinely-strong-random-secret-value"


def test_dev_boot_generates_ephemeral_secret(monkeypatch, caplog):
    monkeypatch.delenv("POCKETPAW_ENV", raising=False)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    with caplog.at_level(logging.WARNING, logger=core.logger.name):
        secret = core._resolve_secret()
    # Never the public default, and long enough to be a real random value.
    assert secret != _DEFAULT
    assert len(secret) >= 32
    # Two calls yield different ephemeral secrets (per-process random).
    assert core._resolve_secret() != secret
    assert any("ephemeral" in r.message.lower() for r in caplog.records)


def test_dev_boot_with_default_secret_substitutes(monkeypatch):
    """Even if AUTH_SECRET is literally the placeholder, dev must not USE it."""
    monkeypatch.delenv("POCKETPAW_ENV", raising=False)
    monkeypatch.delenv("POCKETPAW_AUTH_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("AUTH_SECRET", _DEFAULT)
    assert core._resolve_secret() != _DEFAULT


# ---------------------------------------------------------------------------
# Generated admin password satisfies the policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generated_admin_password_passes_policy():
    pwd = core._generate_admin_password()
    assert pwd != "admin123"
    assert len(pwd) >= 16
    # Must satisfy the live password policy (upper/lower/digit/symbol/length).
    await validate_password_async(pwd, email="admin@pocketpaw.ai")


def test_generated_admin_passwords_are_unique():
    assert core._generate_admin_password() != core._generate_admin_password()


# ---------------------------------------------------------------------------
# seed_admin — no admin123, never logged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_seed_admin_password_is_not_admin123_and_not_logged(
    mongo_db, monkeypatch, caplog, capsys
):
    """Default first boot: generated password, not admin123, never in logs."""
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setenv("ADMIN_EMAIL", "owner@example.com")

    with caplog.at_level(logging.DEBUG):
        user = await core.seed_admin()

    assert user is not None
    # The password must never appear in any log record.
    joined_logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "admin123" not in joined_logs
    assert "password:" not in joined_logs.lower()

    # The login must NOT work with the old hardcoded password.
    from pocketpaw_ee.cloud.auth.core import UserManager, get_user_db

    async for db in get_user_db():
        manager = UserManager(db)
        authed = await manager.authenticate(
            _Creds(username="owner@example.com", password="admin123")
        )
        assert authed is None  # the old hardcoded password must never work
        break

    # The generated password is disclosed on stdout exactly once, redacted
    # from logs. We can't read it back from the DB (hashed), but we assert the
    # stdout channel carried a credential block and the logger did not.
    out = capsys.readouterr().out
    assert "initial admin account created" in out.lower()
    assert "owner@example.com" in out


@pytest.mark.asyncio
async def test_seed_admin_honors_operator_password(mongo_db, monkeypatch, caplog, capsys):
    """An operator-supplied ADMIN_PASSWORD is used verbatim and lets login in."""
    monkeypatch.setenv("ADMIN_EMAIL", "op@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "Operator-Chosen-9!pass")

    with caplog.at_level(logging.DEBUG):
        user = await core.seed_admin()
    assert user is not None

    # Never log the chosen password.
    joined_logs = "\n".join(r.getMessage() for r in caplog.records)
    assert "Operator-Chosen-9!pass" not in joined_logs

    # Operator-supplied passwords are NOT re-printed to stdout (the operator
    # already knows them).
    out = capsys.readouterr().out
    assert "Operator-Chosen-9!pass" not in out

    # Login works with the operator password.
    from pocketpaw_ee.cloud.auth.core import UserManager, get_user_db

    async for db in get_user_db():
        manager = UserManager(db)
        authed = await manager.authenticate(
            _Creds(username="op@example.com", password="Operator-Chosen-9!pass")
        )
        assert authed is not None
        break


@pytest.mark.asyncio
async def test_seed_admin_rejects_legacy_default_password(mongo_db, monkeypatch, caplog):
    """A stale ADMIN_PASSWORD=admin123 must be ignored, not honored."""
    monkeypatch.setenv("ADMIN_EMAIL", "stale@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "admin123")

    with caplog.at_level(logging.WARNING):
        user = await core.seed_admin()
    assert user is not None
    assert any("legacy default" in r.getMessage().lower() for r in caplog.records)

    # admin123 must NOT authenticate.
    from pocketpaw_ee.cloud.auth.core import UserManager, get_user_db

    async for db in get_user_db():
        manager = UserManager(db)
        authed = await manager.authenticate(
            _Creds(username="stale@example.com", password="admin123")
        )
        assert authed is None  # the legacy default must never authenticate
        break


class _Creds:
    """Minimal OAuth2 form stand-in for UserManager.authenticate()."""

    def __init__(self, username: str, password: str) -> None:
        self.username = username
        self.password = password
