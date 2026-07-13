# Code Mode runner — writes the agent's script + the generated stub library to
# a throwaway dir, starts the RPC bridge, runs the script in a sandboxed child
# process, and returns ONLY its final stdout (intermediate tool results stay
# inside the sandbox).
# Created: 2026-06-16 (feat/code-mode-ptc) — Programmatic Tool Calling v1.
#
# Subprocess safety mirrors the belt executor (ee/.../belt/executor.py):
#   * arg LISTS only — never ``shell=True``, never string-interpolate the script
#     into a command. ``[python, script_path]`` with the script as a FILE.
#   * the work dir is a per-run throwaway under a code-mode root, removed in a
#     ``finally`` block (success or failure) — no half-state left behind.
#   * the child env is SECRET-SCRUBBED (KEY/TOKEN/SECRET/PASSWORD/... stripped)
#     and carries only the runner-resolved tenancy + the bridge socket path.
#   * a wall-clock timeout kills a runaway child; stdout is capped via
#     ``cap_tool_output`` before it returns to agent context.

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

from pocketpaw.tools.code_mode.bridge import BridgeConfig, CodeModeBridge
from pocketpaw.tools.code_mode.stubgen import generate_stub_module
from pocketpaw.tools.output_budget import cap_tool_output
from pocketpaw.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Defaults — overridable per run by the tool call.
DEFAULT_MAX_CALLS = 50
DEFAULT_TIMEOUT_S = 30
DEFAULT_STDOUT_CAP = 12_000

# Env var names matching any of these (case-insensitive substring) are stripped
# from the child env so a script can't read host credentials. Belt-and-braces:
# the child only needs the tenancy vars + the socket path we add back.
_SECRET_PATTERNS = re.compile(
    r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|PRIVATE|SESSION|COOKIE|"
    r"API|DSN|CONN|DATABASE_URL|MONGO|REDIS|SLACK|STRIPE|OPENAI|ANTHROPIC|GITHUB|"
    r"FERNET|JWT|BEARER)",
    re.IGNORECASE,
)

# A minimal env allowlist the child keeps regardless of the secret scrub — the
# bare-minimum to run a Python interpreter on a clean PATH. HOME is deliberately
# NOT inherited: it's replaced with the throwaway work_dir below so the child
# can't read the host user's home (dotfiles, caches, credential stores).
_ENV_KEEP = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "PYTHONHASHSEED")

# Env var the stub reads to find the bridge socket.
_SOCKET_ENV = "PAW_CODE_MODE_SOCKET"


@dataclass
class CodeModeResult:
    """Outcome of one code-mode run."""

    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool
    tool_calls: int
    rejected_calls: list[str]


def _scrub_env(workspace_id: str, user_id: str, work_dir: Path) -> dict[str, str]:
    """Build the SECRET-SCRUBBED child env.

    Start from the keep-list (PATH etc.), then add the runner-resolved tenancy.
    Nothing matching a secret pattern survives — even if it were on the keep
    list. The bridge socket path is added by the caller after the socket is
    bound.
    """
    base: dict[str, str] = {}
    for key in _ENV_KEEP:
        val = os.environ.get(key)
        if val is not None and not _SECRET_PATTERNS.search(key):
            base[key] = val
    # HOME points at the throwaway work_dir, never the host user's home — so a
    # script can't read ~/.aws, ~/.config, credential stores, or shell history.
    base["HOME"] = str(work_dir)
    # Thread tenancy so any in-process store the bridge dispatches to scopes
    # correctly. These are NON-secret identifiers.
    if workspace_id:
        base["POCKETPAW_WORKSPACE_ID"] = workspace_id
    if user_id:
        base["POCKETPAW_USER_ID"] = user_id
    # Force-disable any inherited interpreter startup hooks.
    base["PYTHONSTARTUP"] = ""
    base["PYTHONNOUSERSITE"] = "1"
    return base


def _socket_path(work_dir: Path) -> str:
    """A short UDS path inside the work dir.

    Unix socket paths are length-limited (~104 chars on macOS). The work dir
    already lives under the system temp root, so a short basename keeps us well
    under the cap; fall back to a temp path if the dir path is pathologically
    long.
    """
    candidate = str(work_dir / "b.sock")
    if len(candidate) < 100:
        return candidate
    # Pathological work-dir path — use a short flat temp socket instead.
    return str(Path(tempfile.gettempdir()) / f"pcm-{uuid.uuid4().hex[:8]}.sock")


async def run_code_mode(
    *,
    registry: ToolRegistry,
    script: str,
    workspace_id: str = "",
    user_id: str = "",
    max_calls: int = DEFAULT_MAX_CALLS,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    stdout_cap: int = DEFAULT_STDOUT_CAP,
) -> CodeModeResult:
    """Run one code-mode script and return its captured outcome.

    Writes ``paw_tools.py`` (generated stubs) + ``script.py`` (the agent's code)
    into a throwaway dir, starts the bridge, runs the script as a child process
    with a scrubbed env + the bridge socket, captures stdout/stderr, and cleans
    up in ``finally``. ``workspace_id`` / ``user_id`` are the RESOLVED tenancy
    the bridge forces onto every tool call.
    """
    cm_root = Path(tempfile.gettempdir()) / "pocketpaw-code-mode"
    cm_root.mkdir(parents=True, exist_ok=True)
    work_dir = cm_root / f"run-{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=False)

    sock_path = _socket_path(work_dir)
    bridge_config = BridgeConfig(
        workspace_id=workspace_id,
        user_id=user_id,
        max_calls=max(1, int(max_calls)),
    )

    try:
        # Generate the read-safe stub library + write both files into the dir.
        stub_src = generate_stub_module(registry)
        (work_dir / "paw_tools.py").write_text(stub_src, encoding="utf-8")
        script_path = work_dir / "script.py"
        script_path.write_text(script, encoding="utf-8")

        child_env = _scrub_env(workspace_id, user_id, work_dir)
        child_env[_SOCKET_ENV] = sock_path
        # The child imports ``paw_tools`` — work_dir must be on its path. cwd is
        # work_dir so a bare ``import paw_tools`` resolves without PYTHONPATH
        # leaking host dirs.
        child_env["PYTHONPATH"] = str(work_dir)

        async with CodeModeBridge(registry, bridge_config, sock_path) as bridge:
            timed_out = False
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(script_path),
                    cwd=str(work_dir),
                    env=child_env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except Exception as exc:  # noqa: BLE001
                return CodeModeResult(
                    stdout="",
                    stderr=f"failed to start code-mode child: {exc}",
                    exit_code=-1,
                    timed_out=False,
                    tool_calls=bridge.call_count,
                    rejected_calls=bridge.rejected_calls,
                )

            try:
                out_b, err_b = await asyncio.wait_for(
                    proc.communicate(), timeout=max(1, int(timeout_s))
                )
            except TimeoutError:
                timed_out = True
                proc.kill()
                with _suppress():
                    await proc.wait()
                out_b, err_b = b"", b""

            stdout = out_b.decode("utf-8", "replace") if out_b else ""
            stderr = err_b.decode("utf-8", "replace") if err_b else ""
            exit_code = proc.returncode if proc.returncode is not None else -1

            # Cap ONLY the final stdout before it reaches agent context.
            capped = cap_tool_output(stdout, cap=stdout_cap, tool_name="code_mode")

            return CodeModeResult(
                stdout=capped,
                stderr=stderr,
                exit_code=exit_code,
                timed_out=timed_out,
                tool_calls=bridge.call_count,
                rejected_calls=bridge.rejected_calls,
            )
    finally:
        # ALWAYS clean up. The socket usually lives inside work_dir (removed by
        # the rmtree below), but the pathological-path fallback in _socket_path
        # puts it under the temp root, OUTSIDE work_dir — unlink it explicitly so
        # that branch never leaks a stale socket.
        with _suppress():
            Path(sock_path).unlink(missing_ok=True)
        with _suppress():
            shutil.rmtree(work_dir, ignore_errors=True)


class _suppress:
    """Swallow any exception in best-effort cleanup paths."""

    def __enter__(self) -> None:
        return None

    def __exit__(self, *_exc: object) -> bool:
        return True
