# engine.py — The one place this backend shells out to node (CS-1).
#
# Created 2026-07-21 (feat/codescaffold). Runs the vendored recipe engine and
# returns the composed project as a source map.
#
# ── Why a subprocess at all ─────────────────────────────────────────────────
# The recipe engine is the template's own code, in TypeScript, and it is the
# thing that knows how to splice at anchors, stack migration numbers, merge
# dependency lists and fail closed on a collision. Reimplementing that in Python
# would mean maintaining two engines that must agree exactly, forever, about
# somebody's source code. Shelling out keeps one implementation.
#
# ── Why it needs nothing installed ──────────────────────────────────────────
# `node --experimental-strip-types` plus a fifteen-line resolve hook we own runs
# the vendored TypeScript directly: no `tsx`, no `node_modules`, no bundler, no
# per-platform native binary in a Python wheel. Verified byte-identical against a
# tsx-driven compose before this was written. The cost is a node floor of 22.6.
#
# ── The lesson this file is built around ────────────────────────────────────
# `paw-sites` shipped this exact seam and it broke in production twice, both
# times invisibly: once because the backend's PATH had no node (a WinError 2
# surfacing as a bare 500), and once because a vendored directory was swallowed
# by .dockerignore and the symptom was a 5xx with nothing in it. So:
#   * the command is overridable by env, exactly as PAW_SITES_GEN_CMD is;
#   * a missing node and a missing template are DISTINCT, named errors that say
#     which one happened;
#   * the template path is asserted at call time, not assumed.
from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
from pathlib import Path

from pocketpaw_ee.cloud._core.errors import CloudError, with_cause

logger = logging.getLogger(__name__)

# The vendored template, relative to this file. Ships inside the package, so it
# is present wherever pocketpaw_ee is importable — including the compiled wheel.
TEMPLATE_DIR = Path(__file__).parent / "_template"
RUNNER = TEMPLATE_DIR / "_runner" / "compose.mjs"
REGISTER = TEMPLATE_DIR / "_runner" / "register.mjs"

# Wall-clock ceiling. Composition is pure file work over ~50 small files and
# takes well under a second; anything near this bound is a hang, not slow work.
COMPOSE_TIMEOUT_SECONDS = 60

# Ceiling on the composed output. The base is ~220 KB of source; a runaway here
# would mean the engine walked somewhere it should not have.
MAX_OUTPUT_BYTES = 32 * 1024 * 1024


def _node_argv() -> list[str]:
    """The node invocation, tokenised.

    Resolved with ``shutil.which`` rather than passed as a bare ``"node"``, and
    that is not cosmetic: on Windows a bare name fails to spawn from asyncio's
    subprocess path even when node is plainly on PATH, and the failure is a
    WinError 2 that reads exactly like "node is not installed". This was the
    paw-sites bug (PAW_SITES_GEN_CMD exists because of it) and it reproduced here
    on the first test run.

    Override with PAW_CODESCAFFOLD_NODE when the backend's PATH has no node or
    the wrong one.
    """
    raw = os.environ.get("PAW_CODESCAFFOLD_NODE", "").strip()
    if not raw:
        # Falls back to the bare name so a PATH-less box still produces the
        # clean, named 503 below rather than a TypeError here.
        return [shutil.which("node") or "node"]
    # An override naming an existing file is used VERBATIM. `shlex` cannot be
    # trusted with a Windows path: posix mode eats the backslashes
    # ("C:Program Filesnodejsnode.exe") and non-posix mode splits on the space in
    # "Program Files". The overwhelmingly common override is a bare absolute
    # path, so check for that before tokenising anything.
    if Path(raw).is_file():
        return [raw]
    # Multi-token override ("node --some-flag", or a launcher plus a script).
    # Non-posix mode on Windows so backslashes survive, but it RETAINS the quotes
    # a Windows user must put around a path containing spaces — so strip them,
    # or CreateProcess is handed a literal '"C:\...\node.exe"' and fails with the
    # same WinError 2 this function exists to prevent.
    tokens = shlex.split(raw, posix=os.name != "nt")
    return [t.strip('"') for t in tokens]


def _assert_vendored() -> None:
    """Fail with a NAMED error when the template did not ship.

    This is the .dockerignore trap, made loud. A vendored directory can be
    silently dropped by either .gitignore or .dockerignore, and the symptom
    downstream is an unexplained 5xx. Checking here means the error says
    "the template is missing, at this path" instead.
    """
    if not RUNNER.is_file():
        logger.error("codescaffold: vendored template missing at %s", TEMPLATE_DIR)
        raise CloudError(
            500,
            "codescaffold.template_missing",
            "The project template did not ship with this build",
        )


async def compose(recipe_ids: list[str]) -> dict:
    """Compose the base template plus `recipe_ids` into a source map.

    Returns the runner's envelope: ``{order, secrets, files, plan}``. `files` is
    ``{path: contents}`` with POSIX-relative paths — the shape both runtimes
    consume (tar for Daytona, `fs.mount` for a WebContainer), which is why this
    returns a map rather than writing a directory.

    Every failure is a clean CloudError. A half-composed project must never be
    reported as a project.
    """
    _assert_vendored()

    argv = [
        *_node_argv(),
        # The engine's own output is JSON on stdout; node's experimental warning
        # goes to stderr and would be noise in the logs.
        "--no-warnings",
        "--experimental-strip-types",
        # A file:// URL, not a path. Node parses `--import` as a module specifier,
        # so an absolute Windows path is read as the scheme "d:" and rejected with
        # ERR_UNSUPPORTED_ESM_URL_SCHEME. POSIX absolute paths happen to work,
        # which is exactly how this would have shipped broken on Windows only.
        "--import",
        REGISTER.as_uri(),
        str(RUNNER),
        *recipe_ids,
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(TEMPLATE_DIR),
        )
    except FileNotFoundError as exc:
        # THE paw-sites failure, caught by name. Without this branch the caller
        # sees a bare OSError and the operator sees a 500 with no cause.
        logger.error("codescaffold: node not found (argv=%r)", argv[:1], exc_info=True)
        raise with_cause(
            CloudError(
                503,
                "codescaffold.node_missing",
                "The scaffold engine needs node, which is not on this server's PATH",
            ),
            exc,
        ) from exc
    except OSError as exc:
        raise with_cause(
            CloudError(500, "codescaffold.spawn_failed", "The scaffold engine could not start"),
            exc,
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=COMPOSE_TIMEOUT_SECONDS)
    except TimeoutError as exc:
        proc.kill()
        # Reap, so a killed engine cannot leave a zombie behind.
        await proc.wait()
        raise with_cause(
            CloudError(504, "codescaffold.timeout", "Composing the project timed out"),
            exc,
        ) from exc

    if len(stdout) > MAX_OUTPUT_BYTES:
        raise CloudError(
            500, "codescaffold.output_too_large", "The composed project was implausibly large"
        )

    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        # Reached when node itself failed before the runner could print its
        # envelope — a syntax error, a bad flag, an unsupported node. stderr is
        # the only thing that explains it, so it goes in the log verbatim.
        logger.error(
            "codescaffold: unparseable engine output (rc=%s): %s",
            proc.returncode,
            stderr.decode("utf-8", "replace")[:2000],
        )
        raise with_cause(
            CloudError(500, "codescaffold.engine_failed", "The scaffold engine failed"),
            exc,
        ) from exc

    if not payload.get("ok"):
        message = str(payload.get("error", "unknown error"))
        # A `recipe` kind is the engine refusing cleanly — a missing anchor, a
        # file collision, an unknown dependency. That is a 422: the request was
        # understood and cannot be satisfied. Anything else is ours.
        if payload.get("kind") == "recipe":
            logger.warning("codescaffold: engine refused: %s", message)
            raise CloudError(422, "codescaffold.compose_refused", message)
        logger.error("codescaffold: engine error: %s", message)
        raise CloudError(500, "codescaffold.engine_failed", "The scaffold engine failed")

    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise CloudError(500, "codescaffold.empty_output", "The scaffold engine produced no files")

    return payload


__all__ = ["COMPOSE_TIMEOUT_SECONDS", "TEMPLATE_DIR", "compose"]
