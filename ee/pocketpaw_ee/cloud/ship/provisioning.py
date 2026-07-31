# ee/pocketpaw_ee/cloud/ship/provisioning.py — the box-lifecycle orchestrator.
#
# ``run_provision`` drives one ShipBox from ``provisioning`` to ``ready`` (or
# ``degraded``) through injected seams, so the whole lifecycle is testable with
# zero network / zero real box:
#
#   * ``provisioner`` — a SHIP-2 ``HcloudProvisioner`` (or fake) that creates the
#     server and returns provider facts.
#   * ``probe`` — an async readiness check
#     ``(handle, ssh_private_key) -> (ready, host_key)``
#     that returns True once the box answers ``dokku version`` over SSH. The real
#     probe SSHes in; the fake returns a scripted sequence.
#
# IDEMPOTENCY: if the box already carries a ``server_id`` the create is SKIPPED
# (a retry after the create landed but before ready never spins a second
# server). READINESS is polled with a bounded attempt count; exhaustion →
# ``degraded`` (never a hang). Every failure path writes a fixed, PII-free
# ``status_reason`` — never a raw provider payload.
#
# Created 2026-07-22 (feat/ship-2-provisioning, SHIP-2): new module.

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from pocketpaw_ee.cloud.ship import store
from pocketpaw_ee.ship_engine.hcloud import ProvisionError
from pocketpaw_ee.ship_engine.port import BoxHandle, BoxSpec

if TYPE_CHECKING:
    from pocketpaw_ee.cloud.models.ship import ShipBox
    from pocketpaw_ee.ship_engine.hcloud import HcloudProvisioner

logger = logging.getLogger(__name__)

# The readiness probe polls this many times before declaring the box degraded.
# The job wires a real inter-attempt sleep; the orchestrator itself takes an
# injected ``sleep`` so tests advance instantly.
DEFAULT_MAX_PROBE_ATTEMPTS = 30

# ``(handle, ssh_private_key) -> (ready, host_key)``. The host key is captured
# trust-on-first-use by the real probe and pinned on the box; a fake may return
# an empty string.
ReadinessProbe = Callable[[BoxHandle, str], Awaitable[tuple[bool, str]]]
Sleep = Callable[[float], Awaitable[None]]


async def run_provision(
    box: ShipBox,
    *,
    provisioner: HcloudProvisioner,
    ssh_public_key: str,
    ssh_private_key: str,
    probe: ReadinessProbe,
    sleep: Sleep,
    max_probe_attempts: int = DEFAULT_MAX_PROBE_ATTEMPTS,
    probe_interval_s: float = 10.0,
) -> ShipBox:
    """Take ``box`` to ``ready`` (or ``degraded``). Returns the updated doc.

    Never raises for an operational failure — a provider or readiness failure is
    recorded on the box as ``degraded`` and the box is returned. It DOES let a
    programming error (e.g. a missing encryption key at store time) propagate,
    matching the run-core gate's "infra failure propagates" posture.
    """
    spec = BoxSpec(
        name=f"paw-ship-{str(box.id)[:12]}",
        region=box.region,
        size=box.server_type,
    )
    key_name = f"paw-ship-{str(box.id)[:12]}"

    # (1) Create the server — SKIP if a prior attempt already did (idempotency).
    if box.server_id:
        handle = BoxHandle(box_id=box.server_id, host=box.ip, ssh_user=box.ssh_user)
        price = box.price_monthly
    else:
        try:
            result = provisioner.create_server(
                spec, ssh_public_key=ssh_public_key, key_name=key_name
            )
        except ProvisionError as exc:
            return await store.mark_degraded(box, reason=str(exc))
        # Persist the server id + ip IMMEDIATELY so a retry reuses this server.
        box = await store.mark_server_created(box, server_id=result.server_id, ip=result.ip)
        handle = result.handle
        price = result.price_monthly

    # (2) Poll for readiness: the box answers ``dokku version`` over SSH.
    for _ in range(max_probe_attempts):
        try:
            ready, host_key = await probe(handle, ssh_private_key)
            if ready:
                # Pin the box's host key BEFORE marking it ready — every later
                # connect verifies against it, so it must be on the doc by the
                # time anything else opens a session.
                if host_key:
                    box = await store.record_host_key(box, host_key=host_key)
                return await store.mark_ready(
                    box, server_id=box.server_id, ip=box.ip, price_monthly=price
                )
        except Exception:  # noqa: BLE001 — a probe blip is expected while booting
            logger.debug("ship probe attempt failed for box=%s (still booting)", box.id)
        await sleep(probe_interval_s)

    return await store.mark_degraded(
        box, reason="box did not become reachable within the provisioning window"
    )
