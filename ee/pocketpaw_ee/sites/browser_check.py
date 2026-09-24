# ee/pocketpaw_ee/sites/browser_check.py — the BROWSER verification layer's sandbox half:
# upload the paw-sites harness into a Daytona sandbox, run it over a built static tree,
# and read back its verdict.
#
# Created 2026-09-24 (PP-2, feat/sites-verify-pipeline). The harness itself
# (``paw-sites/harness/browser-check.mjs``, contract §4) is vendored beside the generator
# at ``/opt/paw-sites/harness`` by ``scripts/vendor-paw-sites.sh``; this module is only
# the I/O that carries it into a sandbox and the parse of what comes back.
#
# TWO CALLERS, ONE SANDBOX EACH:
#   * svelte / react — ``build_job.run_site_preview_build`` hands :func:`run_in_sandbox`
#     to ``daytona_runner.run_build`` as its ``after_build`` hook, so the harness runs in
#     the SAME sandbox the build just ran in, over the output dir it just wrote. No
#     second sandbox, no second upload of the site.
#   * html — there is no build, so :func:`run_standalone` opens a sandbox, uploads the
#     generated raw tree, runs the harness and tears it down.
#
# THE BROWSER IS NEVER ASSUMED. The harness does not install one; this script first runs
# it as-is (a ``PAW_SITES_VERIFY_IMAGE`` with Playwright's chromium baked in passes here),
# and only on exit 3 (``browser_unavailable``) tries ``playwright-core install chromium``
# in the sandbox, bounded by a timeout, then runs it once more. If the browser still
# cannot launch the layer is ``unverified`` with reason ``browser_unavailable`` — never
# ``passed``. A verification that could not look is not a verification that found
# nothing.
#
# THE SANDBOX IS NEVER SNAPSHOTTED (daytona_runner's invariant). The standalone path
# deletes in a ``finally`` with Daytona's auto-delete as the backstop, exactly like
# ``run_build``.
from __future__ import annotations

import json
import logging
import math
import os
import shlex
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pocketpaw_ee.cloud.daytona.client import DaytonaClient

logger = logging.getLogger(__name__)

#: Where the harness lives inside the sandbox. A sibling of the build project, never
#: inside it, so the build's include-list tar cannot pick it up.
SANDBOX_HARNESS_DIR = "/home/daytona/paw-harness"
#: Where the standalone (html) path materialises the site tree.
SANDBOX_SITE_DIR = "/home/daytona/paw-build"
SANDBOX_SCRIPT_PATH = "/tmp/paw-browser-check.sh"
SANDBOX_RESULT_PATH = "/tmp/paw-browser.json"
SANDBOX_EXIT_PATH = "/tmp/paw-browser.exit"

#: The harness files uploaded into the sandbox. ``node_modules`` is never shipped: the
#: sandbox installs from the frozen lockfile under the harness's own bunfig (7-day floor,
#: no install scripts).
HARNESS_FILES: tuple[str, ...] = ("browser-check.mjs", "package.json", "bun.lock", "bunfig.toml")

#: Per-page load budget handed to the harness (contract §4 default).
PAGE_TIMEOUT_MS = 15000

#: The whole in-sandbox browser step: harness install, a possible chromium install, two
#: harness runs. Added to the preview job's arq timeout so arq never reaps a job that
#: is still inside this budget.
BROWSER_CHECK_BUDGET_SECONDS = 300

#: Script exit markers for failures before the harness ran.
_EXIT_NO_HARNESS_DIR = 11
_EXIT_HARNESS_INSTALL = 10
_EXIT_TIMEOUT = 124

#: Env knobs.
HARNESS_DIR_ENV = "PAW_SITES_HARNESS_DIR"
VERIFY_IMAGE_ENV = "PAW_SITES_VERIFY_IMAGE"
_DEFAULT_HARNESS_DIR = "/opt/paw-sites/harness"


@dataclass(frozen=True)
class BrowserCheckResult:
    """The browser layer's verdict. ``errors`` are UNSCRUBBED harness entries — the
    caller runs them through ``verify_diagnostics.finalize`` before they go anywhere."""

    status: str  # passed | failed | unverified
    reason: str = ""
    errors: list[dict[str, Any]] = field(default_factory=list)
    browser: str = ""

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"status": self.status}
        if self.reason:
            out["reason"] = self.reason
        if self.browser:
            out["browser"] = self.browser
        return out


def verify_image() -> str | None:
    """The sandbox image for preview / verify jobs, or ``None`` for the default image.

    ``PAW_SITES_VERIFY_IMAGE`` should name an image carrying bun, node and Playwright
    1.62.1's chromium (``PLAYWRIGHT_BROWSERS_PATH`` set). Unset keeps the default Paw dev
    image, where the script tries an in-sandbox chromium install instead.
    """
    value = (os.environ.get(VERIFY_IMAGE_ENV) or "").strip()
    return value or None


def harness_dir() -> Path | None:
    """The vendored harness directory, or ``None`` when it is not on this box.

    ``PAW_SITES_HARNESS_DIR`` wins; otherwise ``/opt/paw-sites/harness`` (the image
    layout). A dev box sets the env var to a paw-sites checkout's ``harness/``.
    """
    raw = (os.environ.get(HARNESS_DIR_ENV) or "").strip()
    candidate = Path(raw) if raw else Path(_DEFAULT_HARNESS_DIR)
    if (candidate / "browser-check.mjs").is_file() and (candidate / "package.json").is_file():
        return candidate
    return None


def harness_files() -> dict[str, bytes] | None:
    """The harness files to upload, keyed by name, or ``None`` when unavailable."""
    root = harness_dir()
    if root is None:
        return None
    files: dict[str, bytes] = {}
    for name in HARNESS_FILES:
        path = root / name
        if path.is_file():
            files[name] = path.read_bytes()
    if "browser-check.mjs" not in files or "package.json" not in files:
        return None
    return files


def browser_check_script(static_dir: str, *, budget_seconds: int = BROWSER_CHECK_BUDGET_SECONDS) -> str:
    """Render the bash the sandbox runs. Always exits 0 and records the harness's own
    exit code in ``SANDBOX_EXIT_PATH`` — the evidence pattern ``daytona_build`` uses for
    the build sentinel, so an exec that raises does not erase what happened."""
    run_budget = max(30, budget_seconds // 3)
    install_budget = max(30, budget_seconds // 3)
    harness = shlex.quote(SANDBOX_HARNESS_DIR)
    static = shlex.quote(static_dir)
    out = shlex.quote(SANDBOX_RESULT_PATH)
    exitf = shlex.quote(SANDBOX_EXIT_PATH)
    return f"""#!/usr/bin/env bash
# Generated by pocketpaw_ee.sites.browser_check — do not edit in place.
set -u
LOG=/tmp/paw-browser.log
: > {out}
: > "$LOG"
echo -1 > {exitf}
cd {harness} || {{ echo {_EXIT_NO_HARNESS_DIR} > {exitf}; exit 0; }}
timeout {install_budget}s bun install --frozen-lockfile >>"$LOG" 2>&1 \\
  || {{ echo {_EXIT_HARNESS_INSTALL} > {exitf}; exit 0; }}
RUN=node
command -v node >/dev/null 2>&1 || RUN=bun
run_harness() {{
  timeout {run_budget}s "$RUN" browser-check.mjs --dir {static} \\
    --timeout-ms {PAGE_TIMEOUT_MS} > {out} 2>>"$LOG"
  CODE=$?
}}
run_harness
if [ "$CODE" -eq 3 ] && [ -z "${{PAW_BROWSER_CHECK_CHROMIUM:-}}" ]; then
  timeout {install_budget}s "$RUN" node_modules/playwright-core/cli.js install --with-deps chromium >>"$LOG" 2>&1 \\
    || timeout {install_budget}s "$RUN" node_modules/playwright-core/cli.js install chromium >>"$LOG" 2>&1
  run_harness
fi
echo "$CODE" > {exitf}
exit 0
"""


def parse_harness_output(exit_code: int | None, stdout: str) -> BrowserCheckResult:
    """Map the harness's exit code + stdout line to a layer verdict (contract §4)."""
    line = (stdout or "").strip().splitlines()[-1] if (stdout or "").strip() else ""
    payload: dict[str, Any] | None = None
    if line:
        try:
            parsed = json.loads(line)
            payload = parsed if isinstance(parsed, dict) else None
        except ValueError:
            payload = None

    if exit_code is None or exit_code == -1:
        return BrowserCheckResult("unverified", "harness_lost")
    if exit_code == _EXIT_NO_HARNESS_DIR:
        return BrowserCheckResult("unverified", "harness_unavailable")
    if exit_code == _EXIT_HARNESS_INSTALL:
        return BrowserCheckResult("unverified", "harness_install_failed")
    if exit_code == _EXIT_TIMEOUT:
        return BrowserCheckResult("unverified", "timeout")
    if exit_code == 3:
        return BrowserCheckResult("unverified", "browser_unavailable")
    if exit_code != 0:
        detail = str((payload or {}).get("error") or "")
        if "no .html pages" in detail:
            return BrowserCheckResult("unverified", "no_static_pages")
        return BrowserCheckResult("unverified", "harness_crashed")
    if payload is None or not isinstance(payload.get("pages"), list):
        return BrowserCheckResult("unverified", "harness_output_unreadable")

    from pocketpaw_ee.sites.verify_diagnostics import harness_entries

    errors = harness_entries(payload)
    browser = str((payload.get("harness") or {}).get("browser") or "")
    if payload.get("ok") is True and not errors:
        return BrowserCheckResult("passed", browser=browser)
    first_kind = errors[0]["code"] if errors else "unknown"
    return BrowserCheckResult("failed", first_kind, errors, browser=browser)


async def run_in_sandbox(
    client: DaytonaClient,
    sandbox_id: str,
    *,
    static_dir: str,
    budget_seconds: int = BROWSER_CHECK_BUDGET_SECONDS,
    files: dict[str, bytes] | None = None,
) -> BrowserCheckResult:
    """Upload the harness into a LIVE sandbox, run it over ``static_dir``, read the verdict.

    Never raises: every failure is an ``unverified`` verdict with a reason, because the
    build that just ran in this sandbox must not be lost to a harness problem.
    ``files`` overrides the vendored harness (tests).
    """
    harness = files if files is not None else harness_files()
    if not harness:
        return BrowserCheckResult("unverified", "harness_unavailable")
    uploads: list[tuple[str | bytes, str]] = [
        (contents, f"{SANDBOX_HARNESS_DIR}/{name}") for name, contents in harness.items()
    ]
    uploads.append((browser_check_script(static_dir, budget_seconds=budget_seconds).encode(),
                    SANDBOX_SCRIPT_PATH))
    try:
        await client.bulk_upload(sandbox_id, uploads)
    except Exception as exc:  # noqa: BLE001 — a lost upload is an unverified layer
        logger.warning("browser_check: harness upload failed (%s)", exc)
        return BrowserCheckResult("unverified", "sandbox_unavailable")
    try:
        await client.execute_command(
            sandbox_id, f"bash {SANDBOX_SCRIPT_PATH}", timeout=budget_seconds + 60
        )
    except Exception as exc:  # noqa: BLE001 — read the evidence instead of guessing
        logger.info("browser_check: exec did not return cleanly (%s)", exc)
    try:
        raw_exit = (await client.download_file(sandbox_id, SANDBOX_EXIT_PATH)).decode().strip()
        exit_code: int | None = int(raw_exit)
    except Exception:  # noqa: BLE001
        exit_code = None
    try:
        stdout = (await client.download_file(sandbox_id, SANDBOX_RESULT_PATH)).decode(
            "utf-8", errors="replace"
        )
    except Exception:  # noqa: BLE001
        stdout = ""
    result = parse_harness_output(exit_code, stdout)
    logger.info("browser_check: sandbox %s → %s (%s)", sandbox_id, result.status, result.reason)
    return result


async def run_standalone(
    files: dict[str, str | bytes],
    *,
    static_rel: str = ".",
    client: DaytonaClient | None = None,
    budget_seconds: int = BROWSER_CHECK_BUDGET_SECONDS,
    harness: dict[str, bytes] | None = None,
) -> BrowserCheckResult:
    """Open a sandbox, upload a site tree, run the harness over it, tear it down (html).

    RAISES when the sandbox cannot be created at all — the same contract
    ``daytona_runner.run_build`` keeps, so the job can report ``sandbox_unavailable``.
    """
    if client is None:
        from pocketpaw_ee.cloud.daytona.client import get_daytona_client

        client = get_daytona_client()
        if client is None:
            raise RuntimeError("Daytona is not configured (DAYTONA_API_URL / DAYTONA_API_KEY unset)")

    create_kwargs: dict[str, Any] = {
        "name": f"paw-verify-{int(time.time() * 1000)}",
        "cpu": 1,
        "memory": 2,
        "disk": 5,
        "auto_stop_interval": math.ceil(budget_seconds / 60) + 10,
        # Backstop for this process dying; the explicit delete below is the teardown.
        "auto_delete_interval": 0,
    }
    image = verify_image()
    if image:
        create_kwargs["image"] = image
    sandbox_id: str | None = None
    try:
        info = await client.create_sandbox(**create_kwargs)
        sandbox_id = info.id
        await client.wait_for_sandbox(sandbox_id, target_state="started")
        await client.bulk_upload(
            sandbox_id,
            [
                (contents.encode() if isinstance(contents, str) else contents,
                 f"{SANDBOX_SITE_DIR}/{rel}")
                for rel, contents in files.items()
            ],
        )
        static_dir = SANDBOX_SITE_DIR if static_rel in ("", ".") else f"{SANDBOX_SITE_DIR}/{static_rel}"
        return await run_in_sandbox(
            client, sandbox_id, static_dir=static_dir, budget_seconds=budget_seconds, files=harness
        )
    finally:
        if sandbox_id is not None:
            try:
                await client.delete_sandbox(sandbox_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning("browser_check: delete of %s failed (%s)", sandbox_id, exc)


__all__ = [
    "BROWSER_CHECK_BUDGET_SECONDS",
    "HARNESS_DIR_ENV",
    "HARNESS_FILES",
    "PAGE_TIMEOUT_MS",
    "SANDBOX_HARNESS_DIR",
    "VERIFY_IMAGE_ENV",
    "BrowserCheckResult",
    "browser_check_script",
    "harness_dir",
    "harness_files",
    "parse_harness_output",
    "run_in_sandbox",
    "run_standalone",
    "verify_image",
]
