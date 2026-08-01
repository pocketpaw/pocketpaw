# ee/pocketpaw_ee/cloud/ship/domain.py — frozen value objects for the /ship
# managed-deploy entity (SHIP-3).
#
# These are the plain, framework-free shapes ``ship.service`` hands back across
# the entity boundary — never the Beanie documents themselves (only
# ``ship.store`` touches those). The router maps a view to its wire DTO through
# the mappers in ``service.py``.
#
# Tenancy (ee/cloud Rule 3): ``workspace_id`` is REQUIRED on every view with no
# default — a view can only be built from a persisted, tenant-checked row, so
# constructing one without an owner is a TypeError, not a silent leak.
#
# SECURITY: no view carries secret material. A box's SSH private key never
# leaves ``store.decrypt_ssh_key``; an app's env VALUES are never read out of the
# engine — ``env_refs`` holds NAMES only, and ``DbView.env_var`` is the NAME of
# the variable holding the connection string, never the string (SHIP-1's
# ``DbResult`` invariant, carried up the stack).
#
# Created 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): new module.

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import NewType

BoxId = NewType("BoxId", str)
AppId = NewType("AppId", str)
DeployId = NewType("DeployId", str)


@dataclass(frozen=True)
class BoxView:
    """Read model for one provisioned box."""

    id: BoxId
    workspace_id: str
    provider: str
    ip: str
    status: str
    price_monthly: float | None = None
    # Set when a DELETE parked a teardown for human approval (SHIP-4 wires the
    # real Instinct proposal); the box's ``status`` is unchanged.
    pending_destroy_proposal_id: str | None = None


@dataclass(frozen=True)
class AppView:
    """Read model for one app deployed onto a box."""

    id: AppId
    workspace_id: str
    box_id: str
    name: str
    status: str
    build_path: str
    git_ref: str
    image: str
    prod: bool
    urls: tuple[str, ...] = ()
    # Env var NAMES the app expects — never values.
    env_refs: tuple[str, ...] = ()
    pending_destroy_proposal_id: str | None = None


@dataclass(frozen=True)
class DeployView:
    """Read model for one deploy attempt."""

    id: DeployId
    workspace_id: str
    app_id: str
    status: str
    started_at: datetime | None = None
    finished_at: datetime | None = None
    image: str = ""
    log_summary: str = ""


@dataclass(frozen=True)
class DomainView:
    """A domain routed to an app (mirrors SHIP-1's ``DomainResult``)."""

    workspace_id: str
    app_id: str
    domain: str
    tls_enabled: bool


@dataclass(frozen=True)
class DbView:
    """A database service linked to an app (mirrors SHIP-1's ``DbResult``).

    ``env_var`` is the NAME of the variable the link injected. The connection
    string is a secret and never appears here.
    """

    workspace_id: str
    app_id: str
    # The engine-side app name the service was linked to.
    linked_app: str
    service: str
    env_var: str


@dataclass(frozen=True)
class LogsView:
    """A bounded chunk of an app's recent log lines, newest last."""

    workspace_id: str
    app_id: str
    lines: tuple[str, ...] = field(default=())


@dataclass(frozen=True)
class BoxMetricsView:
    """A point-in-time box health snapshot: three percentages, 0.0–100.0."""

    workspace_id: str
    box_id: str
    cpu: float
    mem: float
    disk: float


@dataclass(frozen=True)
class DestroyProposalView:
    """A PARKED teardown. Nothing was destroyed — a human still has to approve.

    ``proposal_id`` is a placeholder id minted by ``ship.service`` in SHIP-3;
    SHIP-4 replaces it with a real Instinct proposal id.
    """

    workspace_id: str
    target_kind: str
    target_id: str
    proposal_id: str


__all__ = [
    "AppId",
    "AppView",
    "BoxId",
    "BoxMetricsView",
    "BoxView",
    "DbView",
    "DeployId",
    "DeployView",
    "DestroyProposalView",
    "DomainView",
    "LogsView",
]
