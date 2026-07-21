# ee/pocketpaw_ee/ship_engine/dokku.py — the Dokku implementation of the
# ``ShipEngine`` port (SHIP-1): drives a Dokku box over SSH and parses its CLI
# output into the typed results.
#
# Transport split: ``DokkuDriver`` never talks to a socket itself — it issues
# command strings through an injected ``SSHTransport`` (``run(command) ->
# CommandResult``). Production wires ``AsyncSSHTransport`` (asyncssh, lazily
# imported so the contract + transcript tests never load it); tests wire
# ``transcripts.FakeSSHTransport``, which replays recorded CLI output with
# zero network.
#
# ONE chokepoint: every verb funnels through ``_run(verb, cmd)`` — per-verb
# timeout (``DEFAULT_TIMEOUTS``, override via the ``timeouts`` ctor arg),
# output capture, REDACTION of secret-like material (env assignments, DSN
# passwords, KEY/TOKEN/SECRET/PASSWORD-named values) before anything is
# logged or raised, and mapping of non-zero exits to ``CommandFailed``.
#
# Verb → Dokku commands:
#   provision_box  — raises VerbNotSupported (provisioning is SHIP-2's job)
#   deploy_app     — apps:exists (create if missing) → config:set --no-restart
#                    (when env present) → git:from-image
#   add_domain     — domains:add → letsencrypt:enable (when enable_tls)
#   db_create      — mongo:create → mongo:link (the mongo plugin)
#   backup         — mongo:export > dest_path, then stat for the size.
#                    v1 LIMITATION: the dump lands on the BOX's local disk —
#                    offsite/object-storage backup is a later slice.
#   rollback       — git:from-image with the pinned previous tag. v1 CHOICE:
#                    Dokku's releases surface isn't scriptable enough to walk
#                    history reliably, so rollback == re-deploy of a known
#                    image tag the caller supplies (the deploy history lives
#                    with the caller in a later slice).
#   logs           — logs --num N
#   metrics        — ps:report + df -Pk / (box root-disk usage)
#   destroy        — --force apps:destroy
#
# Created 2026-07-21 (feat/ship-1-engine-contract): new module.

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from pocketpaw_ee.ship_engine.port import (
    BackupResult,
    BoxHandle,
    BoxSpec,
    CommandFailed,
    DbResult,
    DeployRequest,
    DeployResult,
    DomainResult,
    LogChunk,
    MetricsSnapshot,
    VerbNotSupported,
)

logger = logging.getLogger(__name__)

# Per-verb wall-clock budgets (seconds) for a single engine command. Deploys
# pull images and rebuild containers; backups stream a full dump — both get
# long budgets. Everything else is an interactive-scale command.
DEFAULT_TIMEOUTS: dict[str, float] = {
    "deploy_app": 600.0,
    "add_domain": 300.0,  # letsencrypt:enable does an ACME round-trip
    "db_create": 300.0,
    "backup": 900.0,
    "rollback": 600.0,
    "logs": 30.0,
    "metrics": 30.0,
    "destroy": 120.0,
}

_STDERR_TAIL_CHARS = 500

# Secret-like material scrubbed from EVERYTHING that leaves the chokepoint
# (log lines, CommandFailed fields). Three families:
#   * URL credentials — ``scheme://user:password@`` (mongo DSNs etc.)
#   * shell env assignments — ``VAR=value`` (config:set carries app secrets)
#   * reported config vars — ``SOME_KEY: value`` / ``SOME_KEY=value`` where
#     the name smells secret (KEY/TOKEN/SECRET/PASSWORD/PASSWD/PWD/DSN/URL)
_URL_CREDS_RE = re.compile(r"(\w+://)[^\s/@:]+:[^\s@]+@")
_ENV_ASSIGN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=(?:'[^']*'|\"[^\"]*\"|\S+)")
_SECRET_VAR_RE = re.compile(
    r"\b([A-Za-z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|DSN|URL)[A-Za-z0-9_]*)"
    r"(\s*:\s+)\S+",
    re.IGNORECASE,
)


def redact(text: str) -> str:
    """Scrub secret-like material from ``text`` before it is logged or raised."""
    text = _URL_CREDS_RE.sub(r"\1[redacted]@", text)
    text = _ENV_ASSIGN_RE.sub(r"\1=[redacted]", text)
    text = _SECRET_VAR_RE.sub(r"\1\2[redacted]", text)
    return text


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class CommandResult:
    """What one remote command produced: exit status + captured output."""

    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class SSHTransport(Protocol):
    """How the driver reaches the box: run one command, capture its result.

    Implementations: ``AsyncSSHTransport`` (production, asyncssh) and
    ``transcripts.FakeSSHTransport`` (tests, replays recorded output). A
    non-zero exit is a RESULT, not an exception — the driver decides.
    """

    async def run(self, command: str) -> CommandResult: ...


class AsyncSSHTransport:
    """Production ``SSHTransport``: one asyncssh connection, opened lazily.

    ``asyncssh`` is imported inside ``_connect`` so that importing this
    module (e.g. for the contract or the fake-transport tests) never loads
    the SSH stack. Auth is key-based only — key material is referenced by
    path (``client_key_path``), never held on this object as bytes.
    """

    def __init__(
        self,
        host: str,
        *,
        port: int = 22,
        username: str = "root",
        client_key_path: str | None = None,
        known_hosts: str | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._client_key_path = client_key_path
        self._known_hosts = known_hosts
        self._conn: Any = None

    async def _connect(self) -> Any:
        if self._conn is None:
            import asyncssh  # lazy: only the production path needs it

            self._conn = await asyncssh.connect(
                self._host,
                port=self._port,
                username=self._username,
                client_keys=[self._client_key_path] if self._client_key_path else None,
                known_hosts=self._known_hosts,
            )
        return self._conn

    async def run(self, command: str) -> CommandResult:
        conn = await self._connect()
        completed = await conn.run(command, check=False)
        return CommandResult(
            exit_code=completed.exit_status if completed.exit_status is not None else -1,
            stdout=str(completed.stdout or ""),
            stderr=str(completed.stderr or ""),
        )

    async def aclose(self) -> None:
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()
            self._conn = None


# --------------------------------------------------------------------------- #
# The driver
# --------------------------------------------------------------------------- #


class DokkuDriver:
    """``ShipEngine`` over a Dokku box reached through an ``SSHTransport``.

    Implements the 8 box-level verbs; ``provision_box`` raises
    ``VerbNotSupported`` — creating the box belongs to the SHIP-2
    provisioner, which hands a ready box to this driver.
    """

    name = "dokku"

    def __init__(
        self,
        transport: SSHTransport,
        *,
        timeouts: Mapping[str, float] | None = None,
    ) -> None:
        self._transport = transport
        self._timeouts = {**DEFAULT_TIMEOUTS, **(timeouts or {})}

    # ------------------------------------------------------------------ #
    # The chokepoint — ALL SSH exec goes through here.
    # ------------------------------------------------------------------ #

    async def _run(self, verb: str, command: str, *, check: bool = True) -> CommandResult:
        """Run one command: per-verb timeout, redacted logging, typed failure.

        ``check=False`` returns the result even on a non-zero exit (used for
        probes like ``apps:exists``); the default maps failure to
        ``CommandFailed`` with a REDACTED command + stderr tail.
        """
        safe_command = redact(command)
        timeout = self._timeouts.get(verb, 60.0)
        logger.debug("ship.dokku %s: %s", verb, safe_command)
        try:
            result = await asyncio.wait_for(self._transport.run(command), timeout=timeout)
        except TimeoutError:
            raise CommandFailed(
                safe_command,
                exit_code=-1,
                stderr_tail=f"timed out after {timeout:.0f}s",
            ) from None
        # Redact BEFORE slicing the tail — slicing first could cut a DSN's
        # ``scheme://`` prefix off and leave a bare password the URL-credential
        # pattern no longer matches.
        logger.debug(
            "ship.dokku %s: exit=%d stdout=%s stderr=%s",
            verb,
            result.exit_code,
            redact(result.stdout)[-_STDERR_TAIL_CHARS:],
            redact(result.stderr)[-_STDERR_TAIL_CHARS:],
        )
        if check and result.exit_code != 0:
            raise CommandFailed(
                safe_command,
                exit_code=result.exit_code,
                stderr_tail=redact(result.stderr)[-_STDERR_TAIL_CHARS:].strip(),
            )
        return result

    # ------------------------------------------------------------------ #
    # Verbs
    # ------------------------------------------------------------------ #

    async def provision_box(self, spec: BoxSpec) -> BoxHandle:
        """Not this driver's job — the SHIP-2 provisioner creates boxes."""
        raise VerbNotSupported("provision_box", self.name)

    async def deploy_app(self, request: DeployRequest) -> DeployResult:
        app = request.app.name
        exists = await self._run("deploy_app", f"dokku apps:exists {shlex.quote(app)}", check=False)
        if exists.exit_code != 0:
            await self._run("deploy_app", f"dokku apps:create {shlex.quote(app)}")
        if request.app.env:
            pairs = " ".join(
                f"{key}={shlex.quote(value)}" for key, value in sorted(request.app.env.items())
            )
            await self._run(
                "deploy_app",
                f"dokku config:set --no-restart {shlex.quote(app)} {pairs}",
            )
        deployed = await self._run(
            "deploy_app",
            f"dokku git:from-image {shlex.quote(app)} {shlex.quote(request.image)}",
        )
        return DeployResult(app=app, image=request.image, app_url=_parse_app_url(deployed.stdout))

    async def add_domain(self, app: str, domain: str, *, enable_tls: bool = True) -> DomainResult:
        await self._run("add_domain", f"dokku domains:add {shlex.quote(app)} {shlex.quote(domain)}")
        if enable_tls:
            await self._run("add_domain", f"dokku letsencrypt:enable {shlex.quote(app)}")
        return DomainResult(app=app, domain=domain, tls_enabled=enable_tls)

    async def db_create(self, app: str, service: str) -> DbResult:
        await self._run("db_create", f"dokku mongo:create {shlex.quote(service)}")
        linked = await self._run(
            "db_create", f"dokku mongo:link {shlex.quote(service)} {shlex.quote(app)}"
        )
        return DbResult(
            service=service,
            linked_app=app,
            exposed_env_var=_parse_exposed_env_var(linked.stdout),
        )

    async def backup(self, service: str, dest_path: str) -> BackupResult:
        # v1 LIMITATION (documented in the module comment): the dump lands on
        # the box's local disk at ``dest_path`` — shipping it offsite is a
        # later slice.
        await self._run(
            "backup", f"dokku mongo:export {shlex.quote(service)} > {shlex.quote(dest_path)}"
        )
        sized = await self._run("backup", f"stat -c%s {shlex.quote(dest_path)}")
        try:
            size_bytes = int(sized.stdout.strip())
        except ValueError:
            size_bytes = -1  # dump exists; size unparseable — don't fail the backup
        return BackupResult(service=service, dest_path=dest_path, size_bytes=size_bytes)

    async def rollback(self, app: str, image: str) -> DeployResult:
        # v1 CHOICE (documented in the module comment): rollback is a pinned
        # re-deploy of a previously deployed image tag.
        redeployed = await self._run(
            "rollback", f"dokku git:from-image {shlex.quote(app)} {shlex.quote(image)}"
        )
        return DeployResult(app=app, image=image, app_url=_parse_app_url(redeployed.stdout))

    async def logs(self, app: str, *, num: int = 100) -> LogChunk:
        result = await self._run("logs", f"dokku logs {shlex.quote(app)} --num {int(num)}")
        lines = tuple(line for line in result.stdout.splitlines() if line.strip())
        return LogChunk(app=app, lines=lines)

    async def metrics(self, app: str) -> MetricsSnapshot:
        report = await self._run("metrics", f"dokku ps:report {shlex.quote(app)}")
        disk = await self._run("metrics", "df -Pk /")
        fields = _parse_ps_report(report.stdout)
        return MetricsSnapshot(
            app=app,
            deployed=fields.get("deployed", "").lower() == "true",
            running=fields.get("running", "").lower() == "true",
            processes=int(fields.get("processes", "0") or 0),
            disk_used_pct=_parse_df_used_pct(disk.stdout),
        )

    async def destroy(self, app: str) -> None:
        await self._run("destroy", f"dokku --force apps:destroy {shlex.quote(app)}")


# --------------------------------------------------------------------------- #
# Dokku CLI output parsers (pure functions — transcript-tested)
# --------------------------------------------------------------------------- #

_APP_URL_RE = re.compile(r"^\s*(https?://\S+)\s*$", re.MULTILINE)
_PS_REPORT_FIELD_RE = re.compile(r"^\s{2,}([A-Za-z][A-Za-z0-9 ]*?):\s+(.*)$")


def _parse_app_url(stdout: str) -> str:
    """Pull the deployed-app URL out of ``git:from-image`` output ("" if none).

    Dokku ends a successful deploy with ``=====> Application deployed:``
    followed by indented URL lines; the first one is the canonical URL.
    """
    match = _APP_URL_RE.search(stdout)
    return match.group(1) if match else ""


def _parse_exposed_env_var(stdout: str) -> str:
    """Pull the injected env-var NAME from ``mongo:link`` output.

    The link step prints ``-----> Setting config vars`` then an indented
    ``MONGO_URL: <dsn>`` line — the NAME is the contract-safe part; the DSN
    value is a secret and is never returned.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        match = re.match(r"^([A-Z][A-Z0-9_]*):\s", stripped)
        if match:
            return match.group(1)
    return "MONGO_URL"  # dokku-mongo's documented default injection


def _parse_ps_report(stdout: str) -> dict[str, str]:
    """Parse ``ps:report`` "Key:   value" lines into a lowercase-key dict."""
    fields: dict[str, str] = {}
    for line in stdout.splitlines():
        match = _PS_REPORT_FIELD_RE.match(line)
        if match:
            fields[match.group(1).strip().lower()] = match.group(2).strip()
    return fields


def _parse_df_used_pct(stdout: str) -> float:
    """Parse the Capacity column ("38%") from POSIX ``df -Pk /`` output."""
    for line in stdout.splitlines()[1:]:
        columns = line.split()
        if len(columns) >= 5 and columns[4].endswith("%"):
            try:
                return float(columns[4].rstrip("%"))
            except ValueError:
                continue
    return -1.0
