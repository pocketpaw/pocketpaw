# ee/pocketpaw_ee/ship_engine/port.py — the provider-agnostic deploy-engine
# port (SHIP-1, the /ship surface's foundational contract).
#
# ``ShipEngine`` is the Protocol every deploy engine implements — Dokku over
# SSH in v1 (``dokku.DokkuDriver``), Dokploy or an own-Go engine later. The
# CONTRACT is the asset: everything downstream (the SHIP-2 provisioner, HTTP
# routes, the /ship console) depends only on this module, so swapping the
# engine never touches a consumer.
#
# Twelve typed verbs, each speaking frozen framework-free dataclasses (the same
# convention as ``cloud/billing/domain.py`` — a driver adapts its CLI/API
# output into these; consumers never see engine-specific text):
#
#   provision_box   BoxSpec         -> BoxHandle       (create + prepare a VPS)
#   deploy_app      DeployRequest   -> DeployResult    (app exists + image runs)
#   deploy_source   app, SourceSpec -> DeployResult    (app exists + source built)
#   add_domain      app, domain     -> DomainResult    (domain routed, TLS on)
#   db_create       app, svc, type  -> DbResult        (db up + linked to app)
#   set_healthcheck app, enabled    -> HealthcheckResult (zero-downtime checks)
#   scale           app, {proc:n}   -> ScaleResult     (process counts applied)
#   backup          service, path   -> BackupResult    (db dump landed at path)
#   rollback        app, image      -> DeployResult    (previous image re-deployed)
#   logs            app, num        -> LogChunk        (recent app log lines)
#   metrics         app             -> MetricsSnapshot (process + disk health)
#   destroy         app             -> None            (app gone)
#
# A driver that cannot perform a verb raises ``VerbNotSupported`` (e.g. the
# Dokku driver does not provision boxes — that belongs to the SHIP-2
# provisioner). A verb that ran and failed raises ``CommandFailed`` carrying
# the exit code and a REDACTED stderr tail.
#
# SECURITY INVARIANT: no result DTO ever carries secret material — no DSNs
# with passwords, no env values, no tokens. ``DbResult`` deliberately exposes
# the NAME of the env var holding the connection string, never its value.
#
# Created 2026-07-21 (feat/ship-1-engine-contract): new module.
# Updated 2026-07-21 (review fixes): added ``InvalidSpec`` + env-var-name
#   validation in ``AppSpec.__post_init__`` (hostile names now fail at the DTO
#   boundary, before any command string exists), and ``AppSpec.env`` is
#   ``repr=False`` so a logged/debugged spec never prints secret values.
# Updated 2026-07-23 (feat/ship-14-source-deploy, SHIP-14): added the
#   source-agnostic ``deploy_source(app, SourceSpec) -> DeployResult`` verb + the
#   ``SourceSpec`` tagged union (``GitSource`` today; ``ArchiveSource`` reserved
#   for the archive/agent path later). ``deploy_app(image)`` stays for the
#   pre-built path. ``GitSource.token`` is ``repr=False`` and, like ``AppSpec.env``,
#   is REQUEST-side only — it never crosses into a result DTO, an exception, or a
#   log line (the driver builds any tokenized URL only inside its ``_run``
#   chokepoint and redacts it).
# Updated 2026-07-24 (feat/ship-17-databases, SHIP-17): Wave 2 "expose the
#   engine" — three additive verbs/shapes. (A) ``db_create`` gained a ``db_type``
#   (``DbType`` = postgres/redis/mongo, the 90% set; default ``mongo`` keeps SHIP-3
#   behaviour) so one seam drives every Dokku database plugin, not just mongo.
#   (B) ``set_healthcheck(app, enabled, path) -> HealthcheckResult`` exposes Dokku's
#   built-in zero-downtime ``checks``. (C) ``scale(app, {proc: count}) -> ScaleResult``
#   exposes ``ps:scale``. All three keep the invariants — no secret on a result DTO,
#   everything through the driver's redacting chokepoint.

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ShipEngineError(Exception):
    """Base class for every error a ShipEngine implementation raises."""


class InvalidSpec(ShipEngineError):
    """A request DTO failed validation at construction.

    Raised at the DTO boundary (e.g. ``AppSpec`` rejecting a hostile env var
    name) so malformed input never reaches a driver's command construction.
    """


class VerbNotSupported(ShipEngineError):
    """The engine does not implement this verb (by design, not by failure).

    Carries ``verb`` (the contract verb name) and ``engine`` (the driver's
    human name) so callers can route to the component that DOES own the verb
    — e.g. ``provision_box`` on the Dokku driver routes to the SHIP-2
    provisioner.
    """

    def __init__(self, verb: str, engine: str) -> None:
        self.verb = verb
        self.engine = engine
        super().__init__(f"{engine} does not support the '{verb}' verb")


class CommandFailed(ShipEngineError):
    """An engine command ran and failed (or timed out).

    ``command`` and ``stderr_tail`` are REDACTED before construction — a
    driver must never place secret material (env values, DSN passwords,
    tokens) on this exception, because it flows into logs and API errors.
    ``exit_code`` is the process exit status; drivers use ``-1`` for a
    timeout (no exit status exists).
    """

    def __init__(self, command: str, exit_code: int, stderr_tail: str) -> None:
        self.command = command
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail
        super().__init__(f"command failed (exit {exit_code}): {command} — {stderr_tail}")


# --------------------------------------------------------------------------- #
# Request DTOs (frozen, framework-free — billing/domain.py convention)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BoxSpec:
    """What to provision: a box the engine will run apps on.

    Consumed by ``provision_box`` (the SHIP-2 provisioner's verb — the Dokku
    driver raises ``VerbNotSupported``). ``name`` is the box's label at the
    provider, ``region``/``size`` are provider-native identifiers (e.g.
    Hetzner ``fsn1``/``cx22``), ``image`` is the base OS image.
    """

    name: str
    region: str
    size: str
    image: str = "ubuntu-24.04"


# POSIX shell/environment variable name — the only env key shape AppSpec
# accepts, so a hostile "name" can never smuggle shell syntax into a driver.
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class AppSpec:
    """An app as the engine should know it.

    ``env`` carries the app's config vars. Values MAY be secrets — that is
    what app config is — so ``env`` is REQUEST-side only: no result DTO ever
    echoes it, it is excluded from ``repr`` (``repr=False``) so a logged spec
    never prints values, and drivers redact env values from every log line
    and error. Env var NAMES must match ``[A-Za-z_][A-Za-z0-9_]*`` —
    construction raises ``InvalidSpec`` otherwise, so shell metacharacters
    in a key die here, before any driver builds a command from them.
    """

    name: str
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        for key in self.env:
            if not _ENV_KEY_RE.match(key):
                raise InvalidSpec(f"invalid env var name: {key!r}")


@dataclass(frozen=True)
class DeployRequest:
    """Deploy ``image`` (a pre-built container image reference, tag included)
    as ``app``. The pre-built-image path; source builds go through
    ``deploy_source`` + a ``SourceSpec`` instead."""

    app: AppSpec
    image: str


# --------------------------------------------------------------------------- #
# Source specs (frozen tagged union — the deploy_source input)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SourceSpec:
    """Base of the deploy-source tagged union consumed by ``deploy_source``.

    A source describes WHERE the app's code comes from, leaving the build to the
    engine (buildpack / nixpacks / Dockerfile auto-detection). ``GitSource`` is
    the v1 member; an ``ArchiveSource`` sibling (a tarball of an agent's work
    dir) is reserved for later — the verb is source-agnostic so it bolts on
    without a second seam. Never instantiated directly; a driver matches on the
    concrete member.
    """


@dataclass(frozen=True)
class GitSource(SourceSpec):
    """Deploy from a git repository the engine clones/fetches and builds.

    ``repo_url`` is the plain, secret-free clone URL (``https://host/owner/repo``
    or ``.git``); ``ref`` is the branch/tag/SHA to build (default ``main``).
    ``token`` is an OPTIONAL access token for a private repo — it is
    REQUEST-side only, ``repr=False`` so a logged spec never prints it, and a
    driver injects it into a clone URL ONLY inside its redacting ``_run``
    chokepoint. ``None`` means a public repo (a plain URL). Like ``AppSpec.env``,
    the token never appears in a result DTO, an exception, or a log line.
    """

    repo_url: str
    ref: str = "main"
    token: str | None = field(default=None, repr=False)


# --------------------------------------------------------------------------- #
# Result DTOs (frozen; NEVER carry secret material)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BoxHandle:
    """A provisioned box, ready for an engine to target.

    ``box_id`` is the provider's id (for destroy/resize later); the SSH
    coordinates are how a driver reaches it. Credentials are NOT here — key
    material stays in the secret store, referenced out-of-band.
    """

    box_id: str
    host: str
    ssh_port: int = 22
    ssh_user: str = "root"


@dataclass(frozen=True)
class DeployResult:
    """A completed deploy (or rollback): ``image`` is now live as ``app``.

    ``app_url`` is the engine-reported URL the app answers on ("" when the
    engine reports none). Failure never returns one of these — it raises.
    """

    app: str
    image: str
    app_url: str = ""


@dataclass(frozen=True)
class DomainResult:
    """A domain routed to an app. ``tls_enabled`` reports whether the engine
    finished issuing a certificate for it in this call."""

    app: str
    domain: str
    tls_enabled: bool


# The database engines a driver can stand up. The 90% set Railway headlines;
# Dokku ships six official plugins (mysql / clickhouse / elasticsearch follow
# the identical ``<svc>:create`` + ``<svc>:link`` shape and slot in behind the
# same verb later). ``mongo`` is the default so SHIP-3's behaviour is unchanged.
DbType = Literal["postgres", "redis", "mongo"]


@dataclass(frozen=True)
class DbResult:
    """A database service created and linked to an app.

    ``exposed_env_var`` is the NAME of the env var the link injected (e.g.
    ``DATABASE_URL`` for postgres, ``REDIS_URL``, ``MONGO_URL``). The connection
    string itself is a secret and never appears on this DTO — the app reads it
    from its own environment.
    """

    service: str
    linked_app: str
    exposed_env_var: str


@dataclass(frozen=True)
class HealthcheckResult:
    """The zero-downtime health-check state now in force for an app.

    ``zero_downtime`` reports whether Dokku's ``checks`` are enabled (the engine
    default is on — a settling probe + connection-draining on release, the
    Heroku dyno-shutdown parity). ``path`` is the optional HTTP health path the
    app carries; "" means the engine's default TCP-port check. Carries no secret.
    """

    app: str
    zero_downtime: bool
    path: str = ""


@dataclass(frozen=True)
class ScaleResult:
    """The process counts now applied to an app.

    ``scale`` maps a Procfile process type (``web``, ``worker``, …) to its
    running container count, as the engine set it. Carries no secret.
    """

    app: str
    scale: Mapping[str, int]


@dataclass(frozen=True)
class BackupResult:
    """A database dump written to ``dest_path`` (engine-local in v1 — see the
    driver for the offsite limitation). ``size_bytes`` is the dump's size."""

    service: str
    dest_path: str
    size_bytes: int


@dataclass(frozen=True)
class LogChunk:
    """A bounded chunk of recent app log lines, newest last."""

    app: str
    lines: tuple[str, ...]


@dataclass(frozen=True)
class MetricsSnapshot:
    """A point-in-time health snapshot for an app and its box.

    ``deployed``/``running`` are the engine's process-level flags,
    ``processes`` the running process count, ``disk_used_pct`` the box's
    root-filesystem usage (0.0–100.0).

    ``cpu_pct``/``mem_pct`` are the app's REAL per-container resource usage
    from ``docker stats`` (Dokku's ``ps:report`` gives only process STATE, not
    resource usage). They are ``None`` when the box could not report them (an
    old Docker, a container that is down) — a metrics view shows "—" rather than
    a false 0. The whole snapshot degrades gracefully: process state without
    resource numbers is still useful.
    """

    app: str
    deployed: bool
    running: bool
    processes: int
    disk_used_pct: float
    cpu_pct: float | None = None
    mem_pct: float | None = None


# --------------------------------------------------------------------------- #
# The port
# --------------------------------------------------------------------------- #


@runtime_checkable
class ShipEngine(Protocol):
    """Port for a managed-deploy engine (Dokku in v1; Dokploy/own-Go later).

    All verbs are async. A verb either returns its typed result or raises:
    ``VerbNotSupported`` when the engine doesn't own the verb,
    ``CommandFailed`` when the engine ran it and it failed. Implementations
    adapt their CLI/API output into the frozen DTOs above and NEVER leak
    secret material into results, exceptions, or logs.
    """

    async def provision_box(self, spec: BoxSpec) -> BoxHandle:
        """Create and prepare a box per ``spec``, returning its handle."""
        ...

    async def deploy_app(self, request: DeployRequest) -> DeployResult:
        """Ensure the app exists, apply its env, and run ``request.image``."""
        ...

    async def deploy_source(self, app: AppSpec, source: SourceSpec) -> DeployResult:
        """Ensure the app exists, apply its env, and build+run ``source``.

        The source-agnostic sibling of ``deploy_app``: instead of a pre-built
        image, the engine builds the app from ``source`` (a git repo in v1) with
        its own build-source auto-detection. A build failure raises
        ``CommandFailed`` with a redacted log tail — never a silent hang. Any
        secret carried by the source (a private-repo token) never reaches the
        returned ``DeployResult`` or a raised error.
        """
        ...

    async def add_domain(self, app: str, domain: str, *, enable_tls: bool = True) -> DomainResult:
        """Route ``domain`` to ``app``; issue a TLS cert when ``enable_tls``."""
        ...

    async def db_create(
        self, app: str, service: str, db_type: DbType = "mongo"
    ) -> DbResult:
        """Create a ``db_type`` database ``service`` and link it to ``app``.

        Every ``db_type`` drives the SAME plugin shape (``<svc>:create`` then
        ``<svc>:link <app>``, which injects a connection-string env var); only
        the plugin and the injected var NAME differ. ``mongo`` is the default so
        an existing caller is unchanged.
        """
        ...

    async def set_healthcheck(
        self, app: str, *, enabled: bool, path: str = ""
    ) -> HealthcheckResult:
        """Enable or disable ``app``'s zero-downtime health checks.

        Exposes the engine's BUILT-IN zero-downtime deploy checks (a settling
        probe + connection-draining on release). ``path`` is an optional HTTP
        health path recorded for the app; the engine applies it at deploy.
        """
        ...

    async def scale(self, app: str, scale: Mapping[str, int]) -> ScaleResult:
        """Set ``app``'s per-process container counts (``{"web": 2, ...}``)."""
        ...

    async def backup(self, service: str, dest_path: str) -> BackupResult:
        """Dump database service ``service`` to ``dest_path``."""
        ...

    async def rollback(self, app: str, image: str) -> DeployResult:
        """Put ``app`` back on ``image`` (a previously deployed tag)."""
        ...

    async def logs(self, app: str, *, num: int = 100) -> LogChunk:
        """Fetch the last ``num`` log lines for ``app``."""
        ...

    async def metrics(self, app: str) -> MetricsSnapshot:
        """Snapshot process + disk health for ``app`` and its box."""
        ...

    async def destroy(self, app: str) -> None:
        """Remove ``app`` and its containers. Data services are NOT destroyed."""
        ...
