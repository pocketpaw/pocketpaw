# ee/pocketpaw_ee/sites/_wrangler.py — the shared wrangler-invocation resolver for
# the sites workers-deploy + D1-migrate paths.
#
# Created 2026-07-09 (fix/sites-wrangler-bunx-windows): both workers_deploy.py and
# d1_migrate.py resolved PAW_CF_WRANGLER_CMD (default `bunx wrangler@4.101.0`) with a
# bare `shlex.split`. On Windows that default is unlaunchable: create_subprocess_exec
# (CreateProcess) resolves a bare `bun` to bun.exe, but a bare `bunx` to the
# NON-EXISTENT bunx.exe — bunx ships only as `.cmd` / `.ps1` shims, which can't be
# launched without a shell — so it raised FileNotFoundError and every workers-mode
# deploy 500'd with `sites.workers_wrangler_missing` (and the D1 migrate hit the same
# wall). This centralizes the tokenization + a Windows `bunx`->`bun x` rewrite so BOTH
# paths share one Windows-safe resolver instead of duplicating the broken default.
from __future__ import annotations

import os
import shlex
import sys

# The pinned wrangler invocation. Default `bunx wrangler@4.101.0` (pulls + runs the
# pinned wrangler at publish time — needs network). Override with PAW_CF_WRANGLER_CMD
# to pin a different version or point at a baked binary, e.g.
# `/opt/node_modules/.bin/wrangler`. Mirrors PAW_SITES_GEN_CMD's override seam.
_DEFAULT_WRANGLER_CMD = "bunx wrangler@4.101.0"

# argv[0] basenames that mean "run bunx". create_subprocess_exec can't launch any of
# them on Windows (there is no bunx.exe), so they get rewritten to `bun x`.
_BUNX_NAMES = frozenset({"bunx", "bunx.cmd", "bunx.exe", "bunx.ps1"})


def wrangler_argv() -> list[str]:
    """The wrangler invocation, tokenised and made directly launchable by
    ``create_subprocess_exec`` (no shell). Reads PAW_CF_WRANGLER_CMD (default
    ``bunx wrangler@4.101.0``) — the SAME override seam workers_deploy + d1_migrate
    share, so a deploy image pins one wrangler version for both.

    Windows fix: a leading ``bunx`` is rewritten to ``bun x``.
    ``create_subprocess_exec`` (CreateProcess) resolves a bare ``bun`` to ``bun.exe``
    but a bare ``bunx`` to the non-existent ``bunx.exe`` (bunx is only ``.cmd`` /
    ``.ps1`` shims on Windows, unlaunchable without a shell) → FileNotFoundError →
    ``sites.workers_wrangler_missing``. ``bun x`` is the exact equivalent of ``bunx``
    and ``bun`` resolves cleanly. An absolute ``.../bunx`` keeps its directory (the
    sibling ``bun`` is used). No-op on POSIX (a real ``bunx`` exists) and no-op when
    PAW_CF_WRANGLER_CMD was overridden to something that isn't bunx."""
    argv = shlex.split(os.environ.get("PAW_CF_WRANGLER_CMD", _DEFAULT_WRANGLER_CMD))
    if sys.platform != "win32" or not argv:
        return argv
    head = argv[0]
    if os.path.basename(head).lower() in _BUNX_NAMES:
        base_dir = os.path.dirname(head)
        bun = os.path.join(base_dir, "bun") if base_dir else "bun"
        return [bun, "x", *argv[1:]]
    return argv
