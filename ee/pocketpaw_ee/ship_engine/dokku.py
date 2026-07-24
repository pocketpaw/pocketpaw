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
#   deploy_source  — apps:exists (create if missing) → config:set --no-restart
#                    (when env present) → git:sync --build <repo> <ref>. Dokku
#                    clones/fetches the repo and auto-detects the build source
#                    (buildpack / nixpacks / Dockerfile). A private-repo token is
#                    injected into the clone URL ONLY here, inside the redacting
#                    chokepoint, and never reaches a result or an error.
#   add_domain     — domains:add → letsencrypt:enable (when enable_tls)
#   db_create      — <plugin>:create → <plugin>:link, plugin ∈ {postgres, redis,
#                    mongo} keyed on db_type (default mongo). The link injects a
#                    <SVC>_URL config var whose NAME is returned; the DSN value is
#                    a secret and is redacted from every log line + never on a DTO.
#   set_healthcheck— checks:enable / checks:disable (Dokku's built-in
#                    zero-downtime deploy checks). An optional HTTP health path is
#                    recorded on the app; Dokku applies it from app.json at deploy.
#   scale          — ps:scale <app> web=N worker=M (Procfile process types)
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
# Updated 2026-07-21 (review fixes):
#   * env pairs are quoted WHOLE (``shlex.quote(f"{key}={value}")``) so a
#     hostile key can't smuggle shell syntax — belt to the port's DTO-level
#     key validation (suspenders);
#   * ``AsyncSSHTransport`` no longer passes ``known_hosts=None`` to asyncssh
#     (which would DISABLE host-key verification) — the kwarg is only sent
#     when a value is configured;
#   * ``logs`` runs ``redact()`` over returned lines (the no-secrets
#     invariant now covers app log content too);
#   * a non-numeric ps:report value maps to ``CommandFailed``, not a bare
#     ``ValueError``;
#   * redaction patterns tightened: URL credentials may contain ``@``; env
#     assignments cover shlex-quoted values containing quotes.
# Updated 2026-07-23 (feat/ship-14-source-deploy, SHIP-14): added ``deploy_source``
#   for a ``GitSource`` — ``git:sync --build`` through the same ``_run`` chokepoint,
#   with the same env ``config:set`` as ``deploy_app``. A private-repo token is
#   built into an ``x-access-token`` clone URL only inside the chokepoint, where
#   the existing URL-credential redaction scrubs it from every log line and error.
# Updated 2026-07-24 (feat/ship-17-databases, SHIP-17): Wave 2. ``db_create`` now
#   takes a ``db_type`` and drives the matching plugin (``_DB_PLUGINS``) instead of
#   hardcoding mongo; ``_parse_exposed_env_var`` takes the plugin's default var
#   name. Added ``set_healthcheck`` (``checks:enable`` / ``checks:disable``) and
#   ``scale`` (``ps:scale``) — both funnel through the same ``_run`` chokepoint, and
#   ``scale`` validates process names/counts before interpolating them.
# Updated 2026-07-24 (feat/ship-18-ops, SHIP-18): Wave 3. Added ``set_resources``
#   (``resource:limit --cpu/--memory``), ``create_volume`` (``storage:create`` +
#   ``storage:mount --container-dir``, the modern k3s-ready named-entry form), and
#   ``restart`` / ``rebuild`` (``ps:restart`` / ``ps:rebuild``). All four go through
#   the same ``_run`` chokepoint; ``create_volume`` shape-validates the entry name
#   (``_VOLUME_NAME_RE``) and the absolute container path (``_CONTAINER_PATH_RE``)
#   before either reaches a command, and ``set_resources`` coerces cpu/memory to
#   ints and rejects an all-zero call (which Dokku would treat as a read, not a set).

from __future__ import annotations

import asyncio
import logging
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable
from urllib.parse import quote

from pocketpaw_ee.ship_engine.port import (
    AppSpec,
    BackupResult,
    BoxHandle,
    BoxSpec,
    CommandFailed,
    DbResult,
    DbType,
    DeployRequest,
    DeployResult,
    DomainResult,
    GitSource,
    HealthcheckResult,
    InvalidSpec,
    LifecycleResult,
    LogChunk,
    MetricsSnapshot,
    ResourceResult,
    ScaleResult,
    ShipEngineError,
    SourceSpec,
    VerbNotSupported,
    VolumeResult,
)

logger = logging.getLogger(__name__)

# Per-verb wall-clock budgets (seconds) for a single engine command. Deploys
# pull images and rebuild containers; backups stream a full dump — both get
# long budgets. Everything else is an interactive-scale command.
DEFAULT_TIMEOUTS: dict[str, float] = {
    "deploy_app": 600.0,
    # git:sync clones the repo AND runs the full build (buildpack / nixpacks /
    # Dockerfile) in one command — the longest single call the driver makes.
    "deploy_source": 900.0,
    "add_domain": 300.0,  # letsencrypt:enable does an ACME round-trip
    "db_create": 300.0,
    "set_healthcheck": 60.0,
    # ps:scale spins containers up/down — more than interactive, less than a deploy.
    "scale": 300.0,
    # resource:limit just writes config; storage:create + mount touch the disk.
    "set_resources": 60.0,
    "create_volume": 120.0,
    # ps:restart bounces containers; ps:rebuild re-runs the build, so it's deploy-scale.
    "restart": 300.0,
    "rebuild": 600.0,
    "backup": 900.0,
    "rollback": 600.0,
    "logs": 30.0,
    "metrics": 30.0,
    "destroy": 120.0,
}

_STDERR_TAIL_CHARS = 500

# Secret-like material scrubbed from EVERYTHING that leaves the chokepoint
# (log lines, CommandFailed fields). Three families:
#   * URL credentials — ``scheme://userinfo@`` (mongo DSNs etc.). The
#     userinfo segment is matched greedily up to the LAST ``@`` before the
#     host, so a password containing ``@`` is still fully scrubbed (the
#     username goes with it — over-redaction is fine, under-redaction is not).
#   * shell env assignments — ``VAR=value`` (config:set carries app secrets).
#     After ``=`` the value is a run of QUOTED SPANS or single non-space
#     characters, so whitespace only terminates the value OUTSIDE quotes —
#     shlex-quoted values containing quotes and spaces
#     (``'API_KEY=pa'"'"'ss word'``) are consumed whole, even though the
#     match starts mid-token (after the opening quote).
#   * reported config vars — ``SOME_KEY: value`` where the name smells
#     secret (KEY/TOKEN/SECRET/PASSWORD/PASSWD/PWD/DSN/URL)
_URL_CREDS_RE = re.compile(r"(\w+://)[^\s/]+@")
_ENV_ASSIGN_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=((?:'[^']*'|\"[^\"]*\"|\S)+)")
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


def _env_pairs(env: Mapping[str, str]) -> list[str]:
    """Quote env pairs for a remote shell — the WHOLE ``KEY=value`` token.

    Quoting the whole pair (not just the value) means a key that somehow
    carries shell metacharacters still travels as one inert argv token —
    dokku receives the identical ``KEY=value`` argv either way. This is
    defense-in-depth behind ``AppSpec``'s DTO-level key validation.
    """
    return [shlex.quote(f"{key}={value}") for key, value in sorted(env.items())]


def _tokenized_git_url(repo_url: str, token: str | None) -> str:
    """Build the clone URL ``git:sync`` receives, injecting ``token`` for a
    private repo.

    ``token=None`` returns ``repo_url`` unchanged (a public repo, plain URL). A
    token is placed as the ``x-access-token`` userinfo of an ``https://`` URL —
    the GitHub/GitLab PAT-over-HTTPS convention — and URL-encoded so a token with
    reserved characters can't break the URL (and can't smuggle a ``/`` past the
    ``scheme://userinfo@`` redaction). The credential therefore rides exactly
    where ``_URL_CREDS_RE`` scrubs it from every logged command and error. A
    token on a non-``https`` URL (``git://``, ``ssh``) is meaningless, so the URL
    is returned untouched rather than mangled.
    """
    if not token:
        return repo_url
    prefix = "https://"
    if not repo_url.startswith(prefix):
        return repo_url
    return f"{prefix}x-access-token:{quote(token, safe='')}@{repo_url[len(prefix) :]}"


# db_type -> (Dokku plugin name, the env var the plugin's :link injects). The
# plugin name equals the type today, but the mapping stays explicit so a type
# whose plugin differs (or whose injected var differs) is a one-line change. The
# injected NAME is also the fallback ``_parse_exposed_env_var`` returns if the
# link output can't be parsed — these are the dokku plugins' documented defaults.
_DB_PLUGINS: dict[DbType, tuple[str, str]] = {
    "postgres": ("postgres", "DATABASE_URL"),
    "redis": ("redis", "REDIS_URL"),
    "mongo": ("mongo", "MONGO_URL"),
}

# A Procfile process type (``web``, ``worker``, ``release``): lowercase
# alphanumeric with hyphens/underscores inside. ``scale`` validates every key
# against this before it interpolates ``proc=count`` into a command — the counts
# are ints, so a validated key + an int value has no shell surface at all.
_PROC_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# A storage-entry name (``storage:create <name>``): lowercase alphanumeric with
# hyphens/underscores inside. ``create_volume`` validates the name before it goes
# near a command — the entry also names a real box directory
# (``/var/lib/dokku/data/storage/<name>``), so a hostile name could otherwise
# smuggle path syntax. The container mount path must be ABSOLUTE and free of the
# ``:`` Dokku uses as its host:container separator (and of whitespace), so a mount
# path can never split into extra Dokku arguments.
_VOLUME_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_CONTAINER_PATH_RE = re.compile(r"^/[^\s:]+$")

# Where Dokku's ``storage:create`` places a named entry's backing directory.
_STORAGE_ROOT = "/var/lib/dokku/data/storage"


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

    HOST-KEY POSTURE: when ``known_hosts`` is unset, the kwarg is NOT passed
    to ``asyncssh.connect`` — asyncssh's default applies (``~/.ssh/known_hosts``
    etc., connection REFUSED on an unknown or changed key). Passing
    ``known_hosts=None`` explicitly would DISABLE verification entirely, so
    this transport never forwards an unset value. Set ``known_hosts`` to a
    known-hosts file path to pin the box's key (the SHIP-2 provisioner will
    record it at provision time).
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

            kwargs: dict[str, Any] = {
                "port": self._port,
                "username": self._username,
            }
            if self._client_key_path:
                kwargs["client_keys"] = [self._client_key_path]
            if self._known_hosts is not None:
                # Only when configured — an explicit known_hosts=None would
                # DISABLE asyncssh's host-key verification (see class docstring).
                kwargs["known_hosts"] = self._known_hosts
            self._conn = await asyncssh.connect(self._host, **kwargs)
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
            pairs = " ".join(_env_pairs(request.app.env))
            await self._run(
                "deploy_app",
                f"dokku config:set --no-restart {shlex.quote(app)} {pairs}",
            )
        deployed = await self._run(
            "deploy_app",
            f"dokku git:from-image {shlex.quote(app)} {shlex.quote(request.image)}",
        )
        return DeployResult(app=app, image=request.image, app_url=_parse_app_url(deployed.stdout))

    async def deploy_source(self, app: AppSpec, source: SourceSpec) -> DeployResult:
        if not isinstance(source, GitSource):
            # GitSource is the only v1 member; an ArchiveSource sibling lands on
            # this same verb later. An unknown member is a programming error,
            # surfaced as a typed contract error, not a bare TypeError mid-build.
            raise InvalidSpec(f"unsupported source kind: {type(source).__name__}")
        name = app.name
        exists = await self._run(
            "deploy_source", f"dokku apps:exists {shlex.quote(name)}", check=False
        )
        if exists.exit_code != 0:
            await self._run("deploy_source", f"dokku apps:create {shlex.quote(name)}")
        if app.env:
            pairs = " ".join(_env_pairs(app.env))
            await self._run(
                "deploy_source",
                f"dokku config:set --no-restart {shlex.quote(name)} {pairs}",
            )
        # Inject the token (if any) into the clone URL HERE, inside the redacting
        # chokepoint: the tokenized URL is scrubbed from every log line + error by
        # _URL_CREDS_RE, and source.token (repr=False) never reaches the result.
        build_url = _tokenized_git_url(source.repo_url, source.token)
        synced = await self._run(
            "deploy_source",
            f"dokku git:sync --build {shlex.quote(name)} "
            f"{shlex.quote(build_url)} {shlex.quote(source.ref)}",
        )
        # The result carries the PLAIN, token-free repo_url as provenance (the
        # engine built from this source) — never the tokenized URL.
        return DeployResult(app=name, image=source.repo_url, app_url=_parse_app_url(synced.stdout))

    async def add_domain(self, app: str, domain: str, *, enable_tls: bool = True) -> DomainResult:
        await self._run("add_domain", f"dokku domains:add {shlex.quote(app)} {shlex.quote(domain)}")
        if enable_tls:
            await self._run("add_domain", f"dokku letsencrypt:enable {shlex.quote(app)}")
        return DomainResult(app=app, domain=domain, tls_enabled=enable_tls)

    async def db_create(self, app: str, service: str, db_type: DbType = "mongo") -> DbResult:
        plugin, default_env_var = _DB_PLUGINS[db_type]
        await self._run("db_create", f"dokku {plugin}:create {shlex.quote(service)}")
        linked = await self._run(
            "db_create", f"dokku {plugin}:link {shlex.quote(service)} {shlex.quote(app)}"
        )
        return DbResult(
            service=service,
            linked_app=app,
            exposed_env_var=_parse_exposed_env_var(linked.stdout, default=default_env_var),
        )

    async def set_healthcheck(
        self, app: str, *, enabled: bool, path: str = ""
    ) -> HealthcheckResult:
        # Dokku's zero-downtime checks are a per-app toggle: enable runs the
        # settle-and-drain deploy checks, disable turns them off. The HTTP health
        # ``path`` is not a checks:* argument — Dokku reads it from the app's
        # app.json healthcheck at deploy — so v1 records it on the app (the caller
        # persists it) and only the enable/disable toggle hits the engine here.
        verb = "enable" if enabled else "disable"
        await self._run("set_healthcheck", f"dokku checks:{verb} {shlex.quote(app)}")
        return HealthcheckResult(app=app, zero_downtime=enabled, path=path)

    async def scale(self, app: str, scale: Mapping[str, int]) -> ScaleResult:
        if not scale:
            raise InvalidSpec("scale requires at least one process=count pair")
        for proc, count in scale.items():
            if not _PROC_NAME_RE.match(proc):
                raise InvalidSpec(f"invalid process type: {proc!r}")
            if int(count) < 0:
                raise InvalidSpec(f"invalid scale count for {proc!r}: {count!r}")
        # Keys are validated against _PROC_NAME_RE and values coerced to int, so
        # the ``proc=count`` tokens carry no shell syntax; the app name is quoted.
        # Sorted for a deterministic command surface (transcript-testable).
        pairs = " ".join(f"{proc}={int(count)}" for proc, count in sorted(scale.items()))
        await self._run("scale", f"dokku ps:scale {shlex.quote(app)} {pairs}")
        return ScaleResult(app=app, scale=dict(scale))

    async def set_resources(self, app: str, *, cpu: int = 0, memory_mb: int = 0) -> ResourceResult:
        # cpu/memory are ints coerced below, so the flag values carry no shell
        # syntax; the app name is quoted. At least one dimension must be set —
        # ``resource:limit`` with no flags PRINTS limits rather than setting any,
        # so an all-zero call would silently no-op.
        cpu, memory_mb = int(cpu), int(memory_mb)
        if cpu < 0 or memory_mb < 0:
            raise InvalidSpec(
                f"resource limits must be non-negative: cpu={cpu} memory_mb={memory_mb}"
            )
        if cpu == 0 and memory_mb == 0:
            raise InvalidSpec("set_resources requires a non-zero cpu or memory_mb")
        flags = []
        if cpu:
            flags.append(f"--cpu {cpu}")
        if memory_mb:
            flags.append(f"--memory {memory_mb}")
        await self._run(
            "set_resources", f"dokku resource:limit {' '.join(flags)} {shlex.quote(app)}"
        )
        return ResourceResult(app=app, cpu=cpu, memory_mb=memory_mb)

    async def create_volume(self, app: str, *, name: str, mount_path: str) -> VolumeResult:
        # The name becomes a real box directory and the mount path a Dokku argument,
        # so both are shape-validated before they reach a command (defense in depth
        # behind the shlex-quoting). The modern named-entry form is used
        # (``storage:create`` + ``storage:mount ... --container-dir``) so the same
        # call works on a future k3s scheduler, not just docker-local.
        if not _VOLUME_NAME_RE.match(name):
            raise InvalidSpec(f"invalid volume name: {name!r}")
        if not _CONTAINER_PATH_RE.match(mount_path):
            raise InvalidSpec(
                f"volume mount_path must be an absolute path without ':': {mount_path!r}"
            )
        await self._run("create_volume", f"dokku storage:create {shlex.quote(name)}")
        await self._run(
            "create_volume",
            f"dokku storage:mount {shlex.quote(app)} {shlex.quote(name)} "
            f"--container-dir {shlex.quote(mount_path)}",
        )
        return VolumeResult(
            app=app, name=name, mount_path=mount_path, host_path=f"{_STORAGE_ROOT}/{name}"
        )

    async def restart(self, app: str) -> LifecycleResult:
        await self._run("restart", f"dokku ps:restart {shlex.quote(app)}")
        return LifecycleResult(app=app, action="restart")

    async def rebuild(self, app: str) -> LifecycleResult:
        await self._run("rebuild", f"dokku ps:rebuild {shlex.quote(app)}")
        return LifecycleResult(app=app, action="rebuild")

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
        # Redact returned lines too: app logs are exactly where a framework
        # dumps its config on crash, so the no-secrets invariant covers them.
        lines = tuple(redact(line) for line in result.stdout.splitlines() if line.strip())
        return LogChunk(app=app, lines=lines)

    async def metrics(self, app: str) -> MetricsSnapshot:
        command = f"dokku ps:report {shlex.quote(app)}"
        report = await self._run("metrics", command)
        disk = await self._run("metrics", "df -Pk /")
        fields = _parse_ps_report(report.stdout)
        raw_processes = fields.get("processes", "0") or "0"
        try:
            processes = int(raw_processes)
        except ValueError:
            # A malformed engine report is a failed command, not a bare
            # ValueError bubbling out of the driver.
            raise CommandFailed(
                command,
                exit_code=0,
                stderr_tail=f"unparseable ps:report value: Processes: {raw_processes!r}",
            ) from None
        cpu_pct, mem_pct = await self._container_usage(app)
        return MetricsSnapshot(
            app=app,
            deployed=fields.get("deployed", "").lower() == "true",
            running=fields.get("running", "").lower() == "true",
            processes=processes,
            disk_used_pct=_parse_df_used_pct(disk.stdout),
            cpu_pct=cpu_pct,
            mem_pct=mem_pct,
        )

    async def _container_usage(self, app: str) -> tuple[float | None, float | None]:
        """Real per-app CPU% + memory% from ``docker stats`` — Dokku's own
        ``ps:report`` reports only process STATE, never resource usage.

        BEST-EFFORT: any failure (old Docker, a down container, a stats format
        change) returns ``(None, None)`` so a metrics read degrades to
        process-state-only rather than failing. Dokku names an app's containers
        ``<app>.<proc>.<n>``; ``--filter name=<app>.`` scopes stats to this app,
        and ``--no-stream`` takes one sample instead of streaming. The value is
        interpolated through ``shlex.quote`` like every other app name.
        """
        quoted = shlex.quote(app)
        # A filter-substring match on the app's container name prefix. The
        # trailing dot pins it to ``<app>.`` so app ``web`` never matches
        # ``webapp``. ``{{.CPUPerc}} {{.MemPerc}}`` prints e.g. ``12.34% 5.60%``.
        command = (
            f"docker stats --no-stream --no-trunc "
            f"--format '{{{{.CPUPerc}}}} {{{{.MemPerc}}}}' "
            f"--filter name={quoted}."
        )
        try:
            result = await self._run("metrics", command, check=False)
        except ShipEngineError:
            return None, None
        if result.exit_code != 0:
            return None, None
        return _parse_docker_stats(result.stdout)

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


def _parse_exposed_env_var(stdout: str, *, default: str = "MONGO_URL") -> str:
    """Pull the injected env-var NAME from a ``<plugin>:link`` output.

    The link step prints ``-----> Setting config vars`` then an indented
    ``DATABASE_URL: <dsn>`` (or ``REDIS_URL`` / ``MONGO_URL``) line — the NAME is
    the contract-safe part; the DSN value is a secret and is never returned.
    ``default`` is the plugin's documented injection name, returned when the
    output can't be parsed.
    """
    for line in stdout.splitlines():
        stripped = line.strip()
        match = re.match(r"^([A-Z][A-Z0-9_]*):\s", stripped)
        if match:
            return match.group(1)
    return default


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


def _parse_docker_stats(stdout: str) -> tuple[float | None, float | None]:
    """Parse ``docker stats --format '{{.CPUPerc}} {{.MemPerc}}'`` output into
    ``(cpu_pct, mem_pct)``, averaged across the app's containers.

    Each line is ``"12.34% 5.60%"`` for one container; an app may run several
    (web + worker), so the returned figure is the mean of each column — a single
    "this app is using X" number for the metrics tile. A line that doesn't parse
    is skipped; no parseable line at all yields ``(None, None)`` (render "—",
    never a false 0). Empty output (no running container) is the common
    ``(None, None)`` case.
    """
    cpus: list[float] = []
    mems: list[float] = []
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        cpu = _as_pct(parts[0])
        mem = _as_pct(parts[1])
        if cpu is not None:
            cpus.append(cpu)
        if mem is not None:
            mems.append(mem)
    cpu_avg = round(sum(cpus) / len(cpus), 1) if cpus else None
    mem_avg = round(sum(mems) / len(mems), 1) if mems else None
    return cpu_avg, mem_avg


def _as_pct(token: str) -> float | None:
    """A ``docker stats`` percentage token (``"12.34%"``) to a float, or None."""
    try:
        return float(token.rstrip("%"))
    except ValueError:
        return None
