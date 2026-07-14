# tests/ee/sites/test_d1_migrate.py — unit tests for the DP0-3 wrangler-migrate
# helper (ee/pocketpaw_ee/sites/d1_migrate.py). Created 2026-07-09 (DP0-6). Mocks
# asyncio.create_subprocess_exec (mirroring test_workers_deploy) so the tests run
# with NO real wrangler: they assert the exact command shape
# (`wrangler d1 migrations apply paw-site-<id> --remote`), the cwd, the sanitized
# database name, and the three fail-closed Internal contracts (non-zero exit,
# missing toolchain, timeout). The LIVE wrangler-apply against a real/miniflare D1
# is out of scope here — it lives in the real-CF smoke runbook (gated on wrangler
# being installed), not in the hermetic unit suite.
from __future__ import annotations

import asyncio
import sys

import pytest
from pocketpaw_ee.cloud._core.errors import Internal
from pocketpaw_ee.sites import d1_migrate
from pocketpaw_ee.sites.generator_client import _BuildTimeout


class _FakeProc:
    """Minimal asyncio subprocess stand-in: returns (returncode, stdout, stderr)
    from communicate(). Mirrors the fake in test_workers_deploy."""

    def __init__(self, returncode: int, stdout: bytes, stderr: bytes) -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


def _patch_subprocess(monkeypatch, proc: _FakeProc) -> dict:
    """Patch d1_migrate's create_subprocess_exec to return ``proc`` and capture the
    argv + kwargs of the call."""
    captured: dict = {}

    async def _fake_exec(*argv, **kwargs):
        captured["argv"] = list(argv)
        captured["kwargs"] = kwargs
        return proc

    monkeypatch.setattr(d1_migrate.asyncio, "create_subprocess_exec", _fake_exec)
    return captured


def test_database_name_is_paw_site_prefixed_and_sanitized() -> None:
    # A 24-hex ObjectId passes through unchanged.
    oid = "0123456789abcdef01234567"
    assert d1_migrate.database_name(oid) == f"paw-site-{oid}"


@pytest.mark.asyncio
async def test_apply_migrations_runs_the_expected_wrangler_command(tmp_path, monkeypatch):
    monkeypatch.setenv("PAW_CF_WRANGLER_CMD", "bunx wrangler@4.101.0")
    captured = _patch_subprocess(monkeypatch, _FakeProc(0, b"Migrations applied\n", b""))

    site_id = "0123456789abcdef01234567"
    await d1_migrate.apply_migrations(site_id, str(tmp_path))

    argv = captured["argv"]
    # ...wrangler... d1 migrations apply paw-site-<id> --remote
    assert argv[-5:] == ["d1", "migrations", "apply", f"paw-site-{site_id}", "--remote"]
    # The shared _wrangler tokenizer rewrites a leading `bunx` to `bun x` on Windows
    # (there is no launchable bunx.exe — see ee/pocketpaw_ee/sites/_wrangler.py); on
    # POSIX a real `bunx` exists, so it passes through unchanged.
    expected_head = (
        ["bun", "x", "wrangler@4.101.0"]
        if sys.platform == "win32"
        else ["bunx", "wrangler@4.101.0"]
    )
    assert argv[: len(expected_head)] == expected_head
    assert captured["kwargs"]["cwd"] == str(tmp_path)
    # runs in its own session so a wedged wrangler's whole group can be killed
    assert captured["kwargs"]["start_new_session"] is True


@pytest.mark.asyncio
async def test_apply_migrations_raises_on_nonzero_exit(tmp_path, monkeypatch):
    _patch_subprocess(monkeypatch, _FakeProc(1, b"", b"D1_ERROR: no such database\n"))

    with pytest.raises(Internal) as exc:
        await d1_migrate.apply_migrations("0123456789abcdef01234567", str(tmp_path))
    assert exc.value.code == "sites.migrate_failed"
    # the stderr tail is surfaced for debugging
    assert "no such database" in str(exc.value)


@pytest.mark.asyncio
async def test_apply_migrations_raises_when_wrangler_missing(tmp_path, monkeypatch):
    async def _boom(*argv, **kwargs):
        raise FileNotFoundError("wrangler")

    monkeypatch.setattr(d1_migrate.asyncio, "create_subprocess_exec", _boom)

    with pytest.raises(Internal) as exc:
        await d1_migrate.apply_migrations("0123456789abcdef01234567", str(tmp_path))
    assert exc.value.code == "sites.migrate_wrangler_missing"


@pytest.mark.asyncio
async def test_apply_migrations_raises_on_timeout(tmp_path, monkeypatch):
    # A proc whose communicate never returns; a tiny timeout forces the bounded
    # wait to fire, which _communicate_bounded converts to _BuildTimeout → Internal.
    class _HangProc:
        returncode = None

        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

    async def _fake_exec(*argv, **kwargs):
        return _HangProc()

    monkeypatch.setattr(d1_migrate.asyncio, "create_subprocess_exec", _fake_exec)
    # _communicate_bounded kills the process group on timeout; make that a no-op so
    # the test doesn't try to signal a fake pid.
    monkeypatch.setattr(d1_migrate, "_communicate_bounded", _raising_timeout)

    with pytest.raises(Internal) as exc:
        await d1_migrate.apply_migrations("0123456789abcdef01234567", str(tmp_path))
    assert exc.value.code == "sites.migrate_timeout"


async def _raising_timeout(proc, timeout_s, label):
    raise _BuildTimeout(label, timeout_s)
