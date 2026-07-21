# ee/pocketpaw_ee/ship_engine/port.py — the provider-agnostic deploy-engine
# port (SHIP-1, the /ship surface's foundational contract).
#
# ``ShipEngine`` is the Protocol every deploy engine implements — Dokku over
# SSH in v1 (``dokku.DokkuDriver``), Dokploy or an own-Go engine later. The
# CONTRACT is the asset: everything downstream (the SHIP-2 provisioner, HTTP
# routes, the /ship console) depends only on this module, so swapping the
# engine never touches a consumer.
#
# Nine typed verbs, each speaking frozen framework-free dataclasses (the same
# convention as ``cloud/billing/domain.py`` — a driver adapts its CLI/API
# output into these; consumers never see engine-specific text):
#
#   provision_box  BoxSpec        -> BoxHandle      (create + prepare a VPS)
#   deploy_app     DeployRequest  -> DeployResult   (app exists + image runs)
#   add_domain     app, domain    -> DomainResult   (domain routed, TLS on)
#   db_create      app, service   -> DbResult       (db up + linked to app)
#   backup         service, path  -> BackupResult   (db dump landed at path)
#   rollback       app, image     -> DeployResult   (previous image re-deployed)
#   logs           app, num       -> LogChunk       (recent app log lines)
#   metrics        app            -> MetricsSnapshot (process + disk health)
#   destroy        app            -> None           (app gone)
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

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class ShipEngineError(Exception):
    """Base class for every error a ShipEngine implementation raises."""


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


@dataclass(frozen=True)
class AppSpec:
    """An app as the engine should know it.

    ``env`` carries the app's config vars. Values MAY be secrets — that is
    what app config is — so ``env`` is REQUEST-side only: no result DTO ever
    echoes it, and drivers redact env values from every log line and error.
    """

    name: str
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DeployRequest:
    """Deploy ``image`` (a pre-built container image reference, tag included)
    as ``app``. v1 is image-based deploy only — git-push builds are a later
    slice."""

    app: AppSpec
    image: str


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


@dataclass(frozen=True)
class DbResult:
    """A database service created and linked to an app.

    ``exposed_env_var`` is the NAME of the env var the link injected (e.g.
    ``MONGO_URL``). The connection string itself is a secret and never
    appears on this DTO — the app reads it from its own environment.
    """

    service: str
    linked_app: str
    exposed_env_var: str


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
    """

    app: str
    deployed: bool
    running: bool
    processes: int
    disk_used_pct: float


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

    async def add_domain(self, app: str, domain: str, *, enable_tls: bool = True) -> DomainResult:
        """Route ``domain`` to ``app``; issue a TLS cert when ``enable_tls``."""
        ...

    async def db_create(self, app: str, service: str) -> DbResult:
        """Create database service ``service`` and link it to ``app``."""
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
