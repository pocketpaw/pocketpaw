# tests/ee/sites/test_wrangler_argv.py
# Created: 2026-07-09 (fix/sites-wrangler-bunx-windows) — reproduce-first coverage for
# the "workers-mode deploy 500s on Windows with sites.workers_wrangler_missing" bug.
#
# Root cause: the default PAW_CF_WRANGLER_CMD is `bunx wrangler@4.101.0`, and the
# deploy / D1-migrate paths launch it with asyncio.create_subprocess_exec (CreateProcess
# on Windows). CreateProcess resolves a bare `bun` to bun.exe but a bare `bunx` to the
# NON-EXISTENT bunx.exe (bunx ships only as .cmd/.ps1 shims on Windows) -> FileNotFoundError
# -> sites.workers_wrangler_missing. Fix: the shared wrangler_argv() rewrites a leading
# `bunx` to `bun x` on Windows only. These tests patch sys.platform so both branches are
# exercised deterministically on any host.
from __future__ import annotations

import pytest

pytest.importorskip("pocketpaw_ee")

from pocketpaw_ee.sites import _wrangler  # noqa: E402


def test_bunx_rewritten_to_bun_x_on_windows(monkeypatch):
    """The bug repro: on Windows the default `bunx ...` must become `bun x ...` so
    create_subprocess_exec can actually launch it (bun.exe exists, bunx.exe does not)."""
    monkeypatch.setattr(_wrangler.sys, "platform", "win32")
    monkeypatch.delenv("PAW_CF_WRANGLER_CMD", raising=False)  # exercise the default

    assert _wrangler.wrangler_argv() == ["bun", "x", "wrangler@4.101.0"]


def test_bunx_left_alone_on_posix(monkeypatch):
    """POSIX has a real `bunx` on PATH — the rewrite is a no-op there."""
    monkeypatch.setattr(_wrangler.sys, "platform", "linux")
    monkeypatch.delenv("PAW_CF_WRANGLER_CMD", raising=False)

    assert _wrangler.wrangler_argv() == ["bunx", "wrangler@4.101.0"]


def test_absolute_bunx_keeps_its_directory_on_windows(monkeypatch):
    """An absolute `.../bunx` override still resolves to the sibling `bun` (+ `x`), so
    the directory the operator pinned is preserved — not collapsed to a bare `bun`."""
    monkeypatch.setattr(_wrangler.sys, "platform", "win32")
    monkeypatch.setenv("PAW_CF_WRANGLER_CMD", "C:/tools/npm/bunx wrangler@4.101.0")

    argv = _wrangler.wrangler_argv()
    # os.path.join on Windows uses backslash; compare on the normalized parts.
    assert argv[0].replace("\\", "/") == "C:/tools/npm/bun"
    assert argv[1:] == ["x", "wrangler@4.101.0"]


def test_non_bunx_override_is_untouched_on_windows(monkeypatch):
    """A pinned absolute wrangler binary (not bunx) is launched as-is — no rewrite."""
    monkeypatch.setattr(_wrangler.sys, "platform", "win32")
    monkeypatch.setenv("PAW_CF_WRANGLER_CMD", "C:/opt/node_modules/.bin/wrangler.exe")

    assert _wrangler.wrangler_argv() == ["C:/opt/node_modules/.bin/wrangler.exe"]


def test_bun_exe_x_override_is_untouched(monkeypatch):
    """The explicit `bun.exe x wrangler` form (what a .env pin would use) already
    launches on Windows — it must pass through unchanged."""
    monkeypatch.setattr(_wrangler.sys, "platform", "win32")
    monkeypatch.setenv("PAW_CF_WRANGLER_CMD", "C:/npm/bun.exe x wrangler@4.101.0")

    assert _wrangler.wrangler_argv() == ["C:/npm/bun.exe", "x", "wrangler@4.101.0"]
