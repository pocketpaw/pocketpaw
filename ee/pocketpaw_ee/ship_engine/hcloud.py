# ee/pocketpaw_ee/ship_engine/hcloud.py — the Hetzner provisioner: turns a
# workspace's Hetzner API token into a running, Dokku-ready box.
#
# ``HcloudProvisioner`` implements the ONE verb the SHIP-1 Dokku driver does
# NOT (``DokkuDriver.provision_box`` raises ``VerbNotSupported``): it creates an
# hcloud server with our provisioning SSH public key + a basic firewall
# (22/80/443 in), captures the server type's monthly price at create time, and
# returns a SHIP-1 ``BoxHandle`` plus the facts the ``provision_box`` job
# persists onto the ``ShipBox`` doc.
#
# Provisioning is POLL-AND-PROBE (v1), not phone-home: ``create_server`` returns
# a handle immediately; the caller (the arq job) waits for the provider to
# report the server running, then SSH-probes ``dokku version`` for readiness.
# This keeps the whole path box-free-testable — the hcloud client and the SSH
# probe are both injectable seams.
#
# SECRETS: the keypair is minted by the CALLER (the job) and only the PUBLIC key
# reaches this module (for the hcloud ssh-key upload + cloud-init authorize).
# The private key is Fernet-encrypted onto the ShipBox doc by the job; it never
# passes through here.
#
# Created 2026-07-22 (feat/ship-2-provisioning, SHIP-2): new module.

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from pocketpaw_ee.ship_engine.cloudinit import render_user_data
from pocketpaw_ee.ship_engine.port import BoxHandle, BoxSpec, ShipEngineError

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Inbound firewall the box boots behind: SSH for the driver, 80/443 for the
# apps' HTTP(S) + Let's Encrypt challenges. Nothing else is exposed.
_FIREWALL_PORTS = ("22", "80", "443")


class ProvisionError(ShipEngineError):
    """A clean, PII-free provisioning failure. The job surfaces ``str(exc)``
    into the ShipBox ``status_reason``, so these messages are fixed safe
    strings — never a raw provider payload."""


@dataclass(frozen=True)
class ProvisionResult:
    """What a successful ``create_server`` yields.

    ``handle`` is the SHIP-1 ``BoxHandle`` a driver targets; ``server_id`` is the
    provider id (idempotency anchor + destroy key); ``price_monthly`` is the
    server type's captured monthly gross price (None if the provider reported
    none); ``server_type`` / ``region`` echo what was provisioned.
    """

    handle: BoxHandle
    server_id: str
    ip: str
    price_monthly: float | None
    server_type: str
    region: str


class HcloudClientLike(Protocol):
    """The slice of the hcloud SDK the provisioner uses.

    A Protocol so tests inject a fake without touching the network. The real
    adapter (``build_hcloud_client``) wraps ``hcloud.Client``; the fake mirrors
    these methods over in-memory state.
    """

    def ensure_ssh_key(self, *, name: str, public_key: str) -> Any: ...
    def ensure_firewall(self, *, name: str, ports: tuple[str, ...]) -> Any: ...
    def price_monthly(self, server_type: str) -> float | None: ...
    def create_server(
        self,
        *,
        name: str,
        server_type: str,
        image: str,
        location: str,
        ssh_key: Any,
        firewall: Any,
        user_data: str,
    ) -> tuple[str, str]: ...


class HcloudProvisioner:
    """Provision Hetzner boxes for a workspace over an injected client seam."""

    def __init__(self, client: HcloudClientLike, *, ssh_user: str = "root") -> None:
        self._client = client
        self._ssh_user = ssh_user

    def create_server(
        self, spec: BoxSpec, *, ssh_public_key: str, key_name: str
    ) -> ProvisionResult:
        """Create a server for ``spec`` and return a targetable box handle.

        Uploads the provisioning public key + ensures the firewall, captures the
        server type's monthly price, and creates the server with cloud-init that
        installs Dokku. Raises ``ProvisionError`` (a safe string) on any provider
        failure. This does NOT wait for readiness — the caller polls + probes.
        """
        try:
            ssh_key = self._client.ensure_ssh_key(name=key_name, public_key=ssh_public_key)
            firewall = self._client.ensure_firewall(name=f"{key_name}-fw", ports=_FIREWALL_PORTS)
            price = self._client.price_monthly(spec.size)
            user_data = render_user_data(ssh_public_key=ssh_public_key)
            server_id, ip = self._client.create_server(
                name=spec.name,
                server_type=spec.size,
                image=spec.image,
                location=spec.region,
                ssh_key=ssh_key,
                firewall=firewall,
                user_data=user_data,
            )
        except ProvisionError:
            raise
        except Exception as exc:  # noqa: BLE001 — map ANY provider failure to a safe string
            # Never leak the raw provider payload (may carry token fragments /
            # account detail) into a persisted status_reason.
            logger.warning("hcloud provision failed for box=%s: %s", spec.name, type(exc).__name__)
            raise ProvisionError(f"provider create failed ({type(exc).__name__})") from exc

        if not server_id:
            raise ProvisionError("provider returned no server id")

        handle = BoxHandle(box_id=server_id, host=ip, ssh_port=22, ssh_user=self._ssh_user)
        return ProvisionResult(
            handle=handle,
            server_id=server_id,
            ip=ip,
            price_monthly=price,
            server_type=spec.size,
            region=spec.region,
        )


def build_hcloud_client(token: str) -> HcloudClientLike:  # pragma: no cover - thin SDK wiring
    """Wrap ``hcloud.Client`` into the ``HcloudClientLike`` seam.

    Lazy-imports the SDK so the module stays import-clean on the test path (the
    fake client is used there). Real network calls live only inside the returned
    adapter's methods.
    """
    from hcloud import Client
    from hcloud.firewalls.domain import FirewallRule
    from hcloud.images.domain import Image
    from hcloud.locations.domain import Location
    from hcloud.server_types.domain import ServerType

    if not token:
        raise ProvisionError("no Hetzner API token configured")

    client = Client(token=token)

    class _Adapter:
        def ensure_ssh_key(self, *, name: str, public_key: str) -> Any:
            existing = client.ssh_keys.get_by_name(name)
            if existing is not None:
                return existing
            return client.ssh_keys.create(name=name, public_key=public_key)

        def ensure_firewall(self, *, name: str, ports: tuple[str, ...]) -> Any:
            existing = client.firewalls.get_by_name(name)
            if existing is not None:
                return existing
            rules = [
                FirewallRule(
                    direction="in",
                    protocol="tcp",
                    port=p,
                    source_ips=["0.0.0.0/0", "::/0"],
                )
                for p in ports
            ]
            return client.firewalls.create(name=name, rules=rules)

        def price_monthly(self, server_type: str) -> float | None:
            st = client.server_types.get_by_name(server_type)
            if st is None or not getattr(st, "prices", None):
                return None
            for entry in st.prices:
                monthly = (entry or {}).get("price_monthly") or {}
                gross = monthly.get("gross")
                if gross is not None:
                    return float(gross)
            return None

        def create_server(
            self,
            *,
            name: str,
            server_type: str,
            image: str,
            location: str,
            ssh_key: Any,
            firewall: Any,
            user_data: str,
        ) -> tuple[str, str]:
            resp = client.servers.create(
                name=name,
                server_type=ServerType(name=server_type),
                image=Image(name=image),
                ssh_keys=[ssh_key],
                firewalls=[firewall],
                location=Location(name=location) if location else None,
                user_data=user_data,
            )
            server = resp.server
            ip = ""
            if server.public_net and server.public_net.ipv4:
                ip = server.public_net.ipv4.ip
            return str(server.id), ip

    return _Adapter()
