# ee/pocketpaw_ee/sites/domain.py — frozen value objects for the Sites control
# plane. SiteDeploy describes a deployed Worker; CustomHostname + HostnameStatus
# describe the Cloudflare-for-SaaS hostname lifecycle the Domains panel polls.
# Created: 2026-05-30 (feat/paw-sites-backend, Task 2.2).

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class HostnameStatus(StrEnum):
    """Lifecycle of a Cloudflare-for-SaaS custom hostname, mapped to the UI
    states the Domains panel shows (Pending DNS → Verifying → Live)."""

    PENDING = "pending"  # awaiting the customer's CNAME at their registrar
    VERIFYING = "verifying"  # CNAME seen, TLS cert issuing
    LIVE = "live"  # hostname active + cert active
    ERROR = "error"


@dataclass(frozen=True)
class CustomHostname:
    id: str
    hostname: str
    status: HostnameStatus
    cname_target: str  # the single CNAME the client pastes


@dataclass(frozen=True)
class SiteDeploy:
    script_name: str
    deployed: bool
