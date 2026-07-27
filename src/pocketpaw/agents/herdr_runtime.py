"""HerdrRuntime — a thin, flagged, fail-open adapter for the ``herdr`` terminal multiplexer.

Created: 2026-07-18 (feat/herdr-runtime-adapter, HR-1).
Updated: 2026-07-24 (HR-1b) — added ``close()`` pane teardown (the ``spawn``
counterpart every consumer that opens a pane needs to end it and its process).
Updated: 2026-07-27 (HR-1c) — hard deployment boundary: herdr is refused in
shared multi-tenant cloud mode. See "Deployment boundary" below.

Deployment boundary — dedicated box ONLY, never shared multi-tenant cloud
------------------------------------------------------------------------
herdr panes are OS processes with PTYs, and **herdr has no tenant model**: it
mints its own flat workspace namespace (``w1``, ``w2``, …) with no notion of
which paw workspace owns a pane. On a shared box that means one tenant's admin
could observe another tenant's panes — a boundary we cannot configure our way
out of, because it does not exist upstream. The per-process cost compounds it
(one pane ≈ one agent process, so N concurrent users ≈ N processes).

So this adapter is for **single-operator deployments only**: a per-tenant
dedicated box (Track A), a developer machine, or a private/self-hosted stack,
where the workspace admin *is* the box operator.

Two gates enforce that, and both must pass:

1. ``herdr_runtime_enabled`` (default **False**) — the operator opt-in.
2. ``POCKETPAW_REQUIRE_WORKSPACE_SCOPE`` must **not** be set. That env marks a
   shared multi-tenant cloud deployment; when it is set this adapter reports
   itself permanently unavailable regardless of the flag, and logs an error.

Gate 2 degrades through the ordinary "herdr is absent" path (every method
raises :class:`HerdrUnavailable`, callers fall back), so a shared-cloud
deployment that sets the flag by mistake loses the feature — it does not break.

What this is
------------
A single, thin adapter that lets PocketPaw spawn and drive coding-agent
terminals ("panes") through **herdr** — a terminal multiplexer for coding
agents. Four future consumers (Mission Control, the belt/deep-work runtimes,
etc.) share this one adapter instead of each shelling out to herdr by hand.

herdr is driven ONLY as a separate process through its ``herdr`` binary. We
never import, link, vendor, or copy herdr code — it is AGPL-3.0 and this is a
process-boundary integration. Every call shells out via async subprocess (the
same discipline as ``delegation.py`` / the CLI backends) and parses the JSON
envelope herdr's socket-API subcommands emit:

  * success → ``{"id": "cli:<group>:<cmd>", "result": {...}}``
  * error   → ``{"id": "cli:<group>:<cmd>", "error": {"code": ..., "message": ...}}``

herdr exits 0 even on the error envelope, so we detect failure from the JSON
``error`` key, never from the exit code.

Flagged + fail-open
-------------------
The adapter is gated by the ``herdr_runtime_enabled`` settings flag (default
False) and by the presence of the ``herdr`` binary. When herdr is unavailable
— flag off, binary missing, server down, socket error, timeout, or an error
envelope — every public method raises :class:`HerdrUnavailable`. That is the
adapter's single, consistent fail-open contract: callers MUST catch it and
degrade to today's non-herdr behaviour; nothing here ever crashes PocketPaw
when herdr is absent (same discipline as the Fable advisor). Guard cheaply
with the :pyattr:`available` property (no subprocess) or :pymeth:`probe`
(a live socket check) to avoid the exception path entirely.

Ids are opaque
--------------
Pane / agent / workspace / terminal ids are OPAQUE strings minted by herdr.
We only ever extract them from JSON (via :class:`PaneRef` / :class:`WorktreeRef`)
and hand them back — we never construct or parse their internal shape.

Status mapping
--------------
herdr's agent states map onto PocketPaw's Mission Control ``AgentStatus`` enum
(``pocketpaw.mission_control.models``): working→ACTIVE, idle→IDLE,
blocked→BLOCKED, unknown→OFFLINE. herdr also emits ``done`` (agent finished a
turn) which maps to IDLE (finished ≈ available). Any unrecognised value fails
safe to OFFLINE. ``AgentStatus`` is imported lazily inside :func:`map_agent_status`
so importing this module never pulls in the mission_control package (whose
``__init__`` imports ``agents.router`` — a cycle we sidestep).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pocketpaw.agents.errors import HerdrUnavailable

if TYPE_CHECKING:
    from pocketpaw.config import Settings
    from pocketpaw.mission_control.models import AgentStatus

logger = logging.getLogger(__name__)

# herdr's own valid agent-status strings (the authority is `herdr` v0.7.4).
_HERDR_STATUSES: frozenset[str] = frozenset({"idle", "working", "blocked", "done", "unknown"})

# herdr agent-status string -> Mission Control AgentStatus *member name*. We map
# by name (not the enum member) so this table needs no mission_control import at
# module-import time; :func:`map_agent_status` resolves the name to the real
# enum lazily. ``done`` is herdr-only (a finished turn) -> IDLE.
_HERDR_TO_MC_NAME: dict[str, str] = {
    "working": "ACTIVE",
    "idle": "IDLE",
    "blocked": "BLOCKED",
    "done": "IDLE",
    "unknown": "OFFLINE",
}
_DEFAULT_MC_NAME = "OFFLINE"  # any unrecognised herdr value fails safe

# Mission Control AgentStatus *value* -> herdr status, for the reverse direction
# (a caller passing an ``AgentStatus`` to :pymeth:`HerdrRuntime.wait`). ``idle``
# and ``blocked`` are identical on both sides and resolve via _HERDR_STATUSES
# first, so only the two that differ need listing.
_MC_VALUE_TO_HERDR: dict[str, str] = {
    "active": "working",
    "offline": "unknown",
}

_DEFAULT_TIMEOUT_MS = 15000

# Shared multi-tenant cloud marker. The cloud deployment sets this to mandate
# fail-closed workspace scoping (see ``pocketpaw.stores``); we reuse it as the
# authoritative "this box serves more than one tenant" signal. Same env name and
# same truthy set as stores.py so the two can never disagree about the mode.
_REQUIRE_WORKSPACE_SCOPE_ENV = "POCKETPAW_REQUIRE_WORKSPACE_SCOPE"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _shared_cloud_mode() -> bool:
    """True when this process serves a shared multi-tenant cloud deployment.

    Read at construction time (not import time) so tests and a re-created
    runtime observe the current environment.
    """
    return os.environ.get(_REQUIRE_WORKSPACE_SCOPE_ENV, "").strip().lower() in _TRUTHY


# Sentinel so _run_json can distinguish "use the configured default timeout"
# from an explicit ``None`` (block indefinitely — used by blocking waits).
_USE_DEFAULT_TIMEOUT = object()


def map_agent_status(herdr_status: str | None) -> AgentStatus:
    """Map a herdr agent-status string onto the Mission Control ``AgentStatus``.

    ``working`` → ACTIVE, ``idle`` → IDLE, ``blocked`` → BLOCKED, ``done`` →
    IDLE, ``unknown`` (and anything unrecognised) → OFFLINE. Imports the enum
    lazily to keep this module import-cheap and cycle-free.
    """
    from pocketpaw.mission_control.models import AgentStatus

    name = _HERDR_TO_MC_NAME.get((herdr_status or "").lower(), _DEFAULT_MC_NAME)
    return AgentStatus[name]


def _to_herdr_status(status: Any) -> str:
    """Coerce a wait-target status into a herdr status string.

    Accepts a raw herdr status (``idle|working|blocked|done|unknown``) or a
    Mission Control ``AgentStatus`` (a ``StrEnum``, so ``str()`` yields its
    value). ``idle``/``blocked`` are valid on both sides and pass through.
    """
    s = str(status).lower()
    if s in _HERDR_STATUSES:
        return s
    if s in _MC_VALUE_TO_HERDR:
        return _MC_VALUE_TO_HERDR[s]
    raise ValueError(
        f"unknown wait status {status!r}: expected one of "
        f"{sorted(_HERDR_STATUSES)} or a Mission Control AgentStatus"
    )


@dataclass(frozen=True)
class PaneRef:
    """Opaque handle to a herdr pane / agent, built only from herdr JSON.

    ``pane_id`` is the universal target string every herdr command accepts.
    The other ids are carried for convenience (and for :pymeth:`HerdrRuntime.attach_info`);
    ``raw`` keeps the full record herdr returned. None of these are ever
    constructed by us — they are opaque strings minted by herdr.
    """

    pane_id: str
    terminal_id: str | None = None
    workspace_id: str | None = None
    tab_id: str | None = None
    agent: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @classmethod
    def from_record(cls, rec: Mapping[str, Any]) -> PaneRef:
        return cls(
            pane_id=rec["pane_id"],
            terminal_id=rec.get("terminal_id"),
            workspace_id=rec.get("workspace_id"),
            tab_id=rec.get("tab_id"),
            agent=rec.get("agent"),
            raw=dict(rec),
        )


@dataclass(frozen=True)
class WorktreeRef:
    """Opaque handle to a herdr-created git worktree / workspace.

    Returned by :pymeth:`HerdrRuntime.worktree_create` and accepted by
    :pymeth:`HerdrRuntime.spawn` (spawn into this worktree's workspace) and
    :pymeth:`HerdrRuntime.worktree_remove`. ``workspace_id`` is the opaque id
    herdr uses to address the worktree.
    """

    workspace_id: str | None
    path: str | None = None
    branch: str | None = None
    root_pane_id: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)


class HerdrRuntime:
    """Thin, flagged, fail-open adapter over the ``herdr`` CLI.

    Construct with PocketPaw ``Settings``; the adapter reads
    ``herdr_runtime_enabled``, ``herdr_cli_path`` and ``herdr_cli_timeout_ms``.
    All methods are async and shell out to the ``herdr`` binary. When herdr is
    unavailable they raise :class:`HerdrUnavailable` — the single fail-open
    contract (guard with :pyattr:`available` to skip the exception path).
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._enabled = bool(getattr(settings, "herdr_runtime_enabled", False))
        # Deployment boundary (see module docstring): herdr has no tenant model,
        # so it is refused outright on a shared multi-tenant box even when the
        # operator opted in. Disabling here routes every caller through the
        # already-tested "herdr absent" path rather than inventing a new failure.
        self._shared_cloud = _shared_cloud_mode()
        if self._enabled and self._shared_cloud:
            logger.error(
                "herdr_runtime_enabled is set but %s marks this a shared "
                "multi-tenant deployment — REFUSING to enable herdr. herdr panes "
                "are not workspace-scoped, so one tenant could observe another's "
                "panes. herdr is supported only on a dedicated/single-operator "
                "box. Treating herdr as unavailable.",
                _REQUIRE_WORKSPACE_SCOPE_ENV,
            )
            self._enabled = False
        self._binary = _resolve_binary(settings)
        timeout_ms = int(
            getattr(settings, "herdr_cli_timeout_ms", _DEFAULT_TIMEOUT_MS) or _DEFAULT_TIMEOUT_MS
        )
        self._timeout_s = max(timeout_ms, 1) / 1000.0
        if self._enabled and self._binary is None:
            logger.warning(
                "herdr_runtime_enabled is set but no herdr binary was found "
                "(install herdr or set herdr_cli_path); the adapter will report "
                "unavailable and callers will degrade to non-herdr behaviour."
            )
        elif self._enabled:
            logger.info("HerdrRuntime enabled; binary=%s", self._binary)

    # -- availability -------------------------------------------------------

    @property
    def available(self) -> bool:
        """True when the flag is on AND a herdr binary is resolved.

        Cheap (no subprocess). A True here does NOT prove the herdr server is
        running — use :pymeth:`probe` for a live check.
        """
        return self._enabled and self._binary is not None

    @property
    def binary(self) -> str | None:
        """The resolved herdr executable path, or None when unresolved."""
        return self._binary

    def _require_available(self) -> None:
        if not self._enabled:
            if self._shared_cloud:
                # Distinguish "operator left it off" from "we refused it" — the
                # latter is a deployment boundary, not a missing toggle.
                raise HerdrUnavailable(
                    "herdr is not supported on a shared multi-tenant deployment "
                    f"({_REQUIRE_WORKSPACE_SCOPE_ENV} is set); it requires a "
                    "dedicated/single-operator box"
                )
            raise HerdrUnavailable("herdr_runtime_enabled flag is off")
        if self._binary is None:
            raise HerdrUnavailable("herdr binary not found (install herdr or set herdr_cli_path)")

    async def probe(self) -> bool:
        """Best-effort live check — True if the herdr server answers.

        Runs a cheap read-only socket call (``pane list``). Never raises;
        returns False when herdr is disabled, absent, or unreachable, so it is
        safe to call as a guard.
        """
        if not self.available:
            return False
        try:
            await self._run_json(["pane", "list"])
        except HerdrUnavailable:
            return False
        return True

    # -- core subprocess/JSON plumbing --------------------------------------

    async def _run_json(
        self, args: Sequence[str], *, timeout: Any = _USE_DEFAULT_TIMEOUT
    ) -> dict[str, Any]:
        """Run ``herdr <args>`` and return the parsed ``result`` payload.

        ``timeout`` is seconds: the ``_USE_DEFAULT_TIMEOUT`` sentinel uses the
        configured default; ``None`` blocks indefinitely (blocking waits only).
        Raises :class:`HerdrUnavailable` on any failure — launch error, timeout,
        non-JSON output, or a herdr error-envelope.
        """
        self._require_available()
        assert self._binary is not None  # guaranteed by _require_available
        argv = [self._binary, *[str(a) for a in args]]
        env = {**os.environ, "HERDR_ENV": "1"}
        timeout_s = self._timeout_s if timeout is _USE_DEFAULT_TIMEOUT else timeout

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except (FileNotFoundError, OSError) as exc:
            raise HerdrUnavailable(f"failed to launch herdr: {exc}") from exc

        try:
            if timeout_s is None:
                stdout_b, stderr_b = await proc.communicate()
            else:
                stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except TimeoutError as exc:
            _kill_quietly(proc)
            cmd = " ".join(str(a) for a in args)
            raise HerdrUnavailable(
                f"herdr command timed out after {timeout_s:.1f}s: herdr {cmd}"
            ) from exc

        stdout = stdout_b.decode("utf-8", errors="replace").strip()
        stderr = stderr_b.decode("utf-8", errors="replace").strip()

        if not stdout:
            raise HerdrUnavailable(
                f"herdr produced no output (exit={proc.returncode}): {stderr or '<no stderr>'}"
            )

        try:
            envelope = json.loads(stdout)
        except json.JSONDecodeError as exc:
            raise HerdrUnavailable(f"herdr emitted non-JSON output: {stdout[:200]!r}") from exc

        if not isinstance(envelope, dict):
            raise HerdrUnavailable(f"herdr envelope was not a JSON object: {stdout[:200]!r}")

        # herdr exits 0 even on the error envelope — detect failure by the key.
        if "error" in envelope:
            err = envelope.get("error")
            if isinstance(err, Mapping):
                code = err.get("code", "unknown")
                message = err.get("message", "")
            else:
                code, message = "unknown", str(err)
            raise HerdrUnavailable(f"herdr error [{code}]: {message}")

        result = envelope.get("result")
        if not isinstance(result, dict):
            raise HerdrUnavailable(f"herdr envelope missing 'result': {stdout[:200]!r}")
        return result

    @staticmethod
    def _target(ref: PaneRef | str) -> str:
        """The herdr command target for a ref — its opaque ``pane_id``.

        Accepts a :class:`PaneRef` or a raw pane-id string.
        """
        return ref.pane_id if isinstance(ref, PaneRef) else str(ref)

    # -- spawn / enumerate --------------------------------------------------

    async def spawn(
        self,
        agent: str,
        *,
        argv: Sequence[str] | None = None,
        cwd: str | os.PathLike[str] | None = None,
        worktree: WorktreeRef | str | None = None,
        workspace: str | None = None,
        env: Mapping[str, str] | None = None,
        split: str | None = None,
        focus: bool = False,
    ) -> PaneRef:
        """Start an agent CLI in a new herdr pane (``herdr agent start``).

        ``agent`` is the herdr agent/integration label (e.g. ``"claude"``).
        ``argv`` is the command line to launch after ``--``; omit to let herdr
        use its configured launch for that agent. ``worktree`` spawns into a
        worktree's workspace (its ``workspace_id``/``path`` fill in
        ``workspace``/``cwd`` when those are not given). ``env`` sets the spawned
        agent's env (``--env K=V``), not herdr's. Returns an opaque
        :class:`PaneRef`.
        """
        args: list[str] = ["agent", "start", agent]

        ws = workspace
        wt_cwd: str | None = None
        if worktree is not None:
            if isinstance(worktree, WorktreeRef):
                ws = ws or worktree.workspace_id
                wt_cwd = worktree.path
            else:
                ws = ws or str(worktree)
        eff_cwd = cwd if cwd is not None else wt_cwd
        if eff_cwd is not None:
            args += ["--cwd", str(eff_cwd)]
        if ws:
            args += ["--workspace", str(ws)]
        if split:
            args += ["--split", split]
        if env:
            for key, value in env.items():
                args += ["--env", f"{key}={value}"]
        args.append("--focus" if focus else "--no-focus")
        if argv:
            args.append("--")
            args += [str(a) for a in argv]

        result = await self._run_json(args)
        rec = result.get("agent") or {}
        if not rec.get("pane_id"):
            raise HerdrUnavailable(f"herdr agent start returned no pane: {str(result)[:200]}")
        return PaneRef.from_record(rec)

    async def list_agents(self) -> list[PaneRef]:
        """Enumerate agent panes (``herdr agent list``)."""
        result = await self._run_json(["agent", "list"])
        return [PaneRef.from_record(r) for r in result.get("agents", []) if r.get("pane_id")]

    async def list_panes(self) -> list[PaneRef]:
        """Enumerate all panes (``herdr pane list``)."""
        result = await self._run_json(["pane", "list"])
        return [PaneRef.from_record(r) for r in result.get("panes", []) if r.get("pane_id")]

    # -- drive a pane -------------------------------------------------------

    async def status(self, ref: PaneRef | str) -> AgentStatus:
        """Current agent status as a Mission Control ``AgentStatus`` (``herdr agent get``)."""
        result = await self._run_json(["agent", "get", self._target(ref)])
        rec = result.get("agent") or {}
        return map_agent_status(rec.get("agent_status"))

    async def read(
        self,
        ref: PaneRef | str,
        *,
        source: str = "visible",
        lines: int | None = None,
    ) -> str:
        """Read a pane/agent's scrollback text (``herdr agent read``).

        ``source`` is ``visible`` | ``recent`` | ``recent-unwrapped``.
        """
        args = ["agent", "read", self._target(ref), "--source", source]
        if lines is not None:
            args += ["--lines", str(int(lines))]
        result = await self._run_json(args)
        read = result.get("read") or {}
        return str(read.get("text", ""))

    async def send(self, ref: PaneRef | str, text: str) -> None:
        """Send literal text to an agent (``herdr agent send``).

        Writes the text verbatim — no trailing Enter. (Use a herdr pane ``run``
        surface for command-plus-Enter; this adapter keeps to literal sends.)
        """
        await self._run_json(["agent", "send", self._target(ref), text])

    async def wait(
        self,
        ref: PaneRef | str,
        *,
        status: Any = None,
        output_match: str | None = None,
        timeout_ms: int | None = None,
        regex: bool = False,
        source: str | None = None,
        lines: int | None = None,
    ) -> dict[str, Any]:
        """Block until an agent-status or output match (``herdr wait ...``).

        Pass exactly one of ``status`` (``herdr wait agent-status`` — accepts a
        herdr status string, incl. ``done``, or a Mission Control ``AgentStatus``)
        or ``output_match`` (``herdr wait output`` — set ``regex=True`` to treat
        it as a regex). ``timeout_ms`` bounds the wait; without it the call
        blocks until a match (no subprocess timeout is imposed). Returns the raw
        herdr ``result`` payload (a ``wait_matched`` / ``output_matched``
        envelope); a timeout surfaces as :class:`HerdrUnavailable` carrying
        herdr's timeout error.
        """
        if (status is None) == (output_match is None):
            raise ValueError("wait() requires exactly one of status= or output_match=")

        target = self._target(ref)
        if status is not None:
            args = ["wait", "agent-status", target, "--status", _to_herdr_status(status)]
        else:
            args = ["wait", "output", target, "--match", str(output_match)]
            if regex:
                args.append("--regex")
            if source:
                args += ["--source", source]
            if lines is not None:
                args += ["--lines", str(int(lines))]
        if timeout_ms is not None:
            args += ["--timeout", str(int(timeout_ms))]

        # The subprocess timeout must outlast herdr's own --timeout so herdr,
        # not us, reports the timeout (as an error envelope). Without a
        # timeout_ms the caller asked to block, so impose no subprocess bound.
        sub_timeout = (int(timeout_ms) / 1000.0 + 5.0) if timeout_ms is not None else None
        return await self._run_json(args, timeout=sub_timeout)

    async def attach_info(self, ref: PaneRef | str) -> dict[str, Any]:
        """What a caller needs to attach to the pane (``herdr agent get``).

        Returns a fresh dict of the opaque ids a UI/CLI needs to attach:
        ``pane_id``, ``workspace_id``, ``tab_id``, ``terminal_id``, plus the
        ``agent`` label and current ``agent_status`` string.
        """
        result = await self._run_json(["agent", "get", self._target(ref)])
        rec = result.get("agent") or {}
        return {
            "pane_id": rec.get("pane_id"),
            "workspace_id": rec.get("workspace_id"),
            "tab_id": rec.get("tab_id"),
            "terminal_id": rec.get("terminal_id"),
            "agent": rec.get("agent"),
            "agent_status": rec.get("agent_status"),
        }

    async def close(self, ref: PaneRef | str) -> None:
        """Close a pane and end its process (``herdr pane close``).

        The teardown counterpart to :pymeth:`spawn`. herdr has no separate
        "kill" verb — ``pane close`` ends the pane and whatever agent/command
        runs in it. Raises :class:`HerdrUnavailable` if herdr can't service the
        call; callers doing best-effort cleanup typically suppress it.
        """
        await self._run_json(["pane", "close", self._target(ref)])

    # -- worktrees ----------------------------------------------------------

    async def worktree_create(
        self,
        *,
        branch: str | None = None,
        base: str | None = None,
        path: str | os.PathLike[str] | None = None,
        workspace: str | None = None,
        cwd: str | os.PathLike[str] | None = None,
        focus: bool = False,
    ) -> WorktreeRef:
        """Create a git worktree via herdr (``herdr worktree create --json``).

        Source repo is selected by ``workspace`` (a workspace id) or ``cwd`` (a
        path inside the repo). ``branch``/``base``/``path`` map to herdr's
        ``--branch``/``--base``/``--path``. Returns an opaque :class:`WorktreeRef`.
        """
        args = ["worktree", "create", "--json"]
        if workspace:
            args += ["--workspace", str(workspace)]
        elif cwd is not None:
            args += ["--cwd", str(cwd)]
        if branch:
            args += ["--branch", branch]
        if base:
            args += ["--base", base]
        if path is not None:
            args += ["--path", str(path)]
        args.append("--focus" if focus else "--no-focus")

        result = await self._run_json(args)
        return _worktree_ref_from_result(result)

    async def worktree_remove(
        self, workspace: WorktreeRef | str, *, force: bool = False
    ) -> dict[str, Any]:
        """Remove a herdr worktree (``herdr worktree remove --json``).

        ``workspace`` is a :class:`WorktreeRef` or a raw workspace id. Returns
        herdr's ``worktree_removed`` result payload.
        """
        ws_id = workspace.workspace_id if isinstance(workspace, WorktreeRef) else str(workspace)
        if not ws_id:
            raise ValueError("worktree_remove requires a workspace id")
        args = ["worktree", "remove", "--workspace", ws_id, "--json"]
        if force:
            args.append("--force")
        return await self._run_json(args)


def _resolve_binary(settings: Settings) -> str | None:
    """Locate the herdr executable: explicit ``herdr_cli_path`` else PATH.

    An explicit path that is not an executable file yields None (fail-safe:
    surface the misconfig rather than silently using a different PATH herdr).
    """
    explicit = getattr(settings, "herdr_cli_path", None)
    if explicit:
        p = Path(explicit)
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
        logger.warning(
            "herdr_cli_path=%s is not an executable file; treating herdr as unavailable", explicit
        )
        return None
    return shutil.which("herdr")


def _worktree_ref_from_result(result: Mapping[str, Any]) -> WorktreeRef:
    """Build a :class:`WorktreeRef` from a ``worktree_created`` result payload."""
    wt = result.get("worktree") or {}
    ws = result.get("workspace") or {}
    root = result.get("root_pane") or {}
    return WorktreeRef(
        workspace_id=ws.get("workspace_id") or wt.get("open_workspace_id"),
        path=wt.get("path"),
        branch=wt.get("branch"),
        root_pane_id=root.get("pane_id"),
        raw=dict(result),
    )


def _kill_quietly(proc: asyncio.subprocess.Process) -> None:
    """Best-effort kill of a timed-out subprocess; never raises."""
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    except Exception as exc:  # noqa: BLE001 — cleanup must not mask the timeout
        logger.debug("failed to kill timed-out herdr subprocess: %s", exc)


__all__ = [
    "HerdrRuntime",
    "HerdrUnavailable",
    "PaneRef",
    "WorktreeRef",
    "map_agent_status",
]
