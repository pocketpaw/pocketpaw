# ee/pocketpaw_ee/cloud/ship/engine.py — the box → live-engine seam.
#
# One place turns a persisted ``ShipBox`` into something that can talk to it:
# ``box_session`` decrypts the box's SSH key, materializes it as a private temp
# file (asyncssh reads a key PATH), opens SHIP-1's ``AsyncSSHTransport``, and
# yields a ``BoxSession`` carrying both halves —
#
#   * ``engine`` — a SHIP-1 ``DokkuDriver`` for the nine typed app verbs.
#   * ``transport`` — the raw exec channel, for BOX-level facts the port does
#     not own (it is an app-level contract plus ``provision_box``). Box CPU /
#     memory / disk is exactly such a fact, so it is read here rather than by
#     bolting a tenth verb onto a frozen contract. SHIP-2's provisioning job
#     already probes the box the same way (``dokku version`` over a raw
#     transport), so this is the established seam, not a new one.
#
# It is ALSO the injection point: ``service`` and the deploy job take a
# ``BoxSessionFactory`` and fall back to ``box_session``, so tests drive the
# whole HTTP surface against SHIP-1's zero-network ``FakeSSHTransport`` +
# recorded transcripts.
#
# SECURITY: the decrypted key exists only as a 0600 temp file for the life of
# the session and is unlinked in ``finally`` — it never enters a DTO, an event,
# or a log line.
#
# Created 2026-07-22 (feat/ship-3-cloud-entity, SHIP-3): new module.

from __future__ import annotations

import contextlib
import logging
import os
import tempfile
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pocketpaw_ee.cloud.ship import store
from pocketpaw_ee.ship_engine.dokku import AsyncSSHTransport, DokkuDriver, SSHTransport
from pocketpaw_ee.ship_engine.port import ShipEngine

if TYPE_CHECKING:
    from pocketpaw_ee.cloud.models.ship import ShipBox

logger = logging.getLogger(__name__)

# One round trip, four numeric lines: 1-minute load average, CPU count, memory
# used as a percentage, root-filesystem used as a percentage. A fixed literal —
# nothing is interpolated into it, so there is no injection surface.
BOX_METRICS_COMMAND = (
    "awk '{print $1}' /proc/loadavg; nproc; "
    "free -m | awk '/^Mem:/{printf \"%.1f\\n\", $3/$2*100}'; "
    "df -Pk / | awk 'NR==2{print $5}'"
)


@dataclass(frozen=True)
class BoxSession:
    """A live connection to one box: the typed engine plus the raw exec channel."""

    engine: ShipEngine
    transport: SSHTransport


BoxSessionFactory = Callable[[Any], AbstractAsyncContextManager[BoxSession]]


@asynccontextmanager
async def box_session(box: ShipBox) -> AsyncIterator[BoxSession]:
    """Open an engine session against ``box``, closing it (and shredding the
    on-disk key) on the way out."""
    key_path: str | None = None
    transport: AsyncSSHTransport | None = None
    try:
        fd, key_path = tempfile.mkstemp(prefix="paw-ship-", suffix=".key")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(store.decrypt_ssh_key(box))
        transport = AsyncSSHTransport(
            box.ip,
            port=box.ssh_port,
            username=box.ssh_user,
            client_key_path=key_path,
        )
        yield BoxSession(engine=DokkuDriver(transport), transport=transport)
    finally:
        if transport is not None:
            with contextlib.suppress(Exception):  # best-effort teardown
                await transport.aclose()
        if key_path and os.path.exists(key_path):
            os.unlink(key_path)


async def read_box_metrics(session: BoxSession) -> tuple[float, float, float]:
    """Read ``(cpu, mem, disk)`` percentages off the box. Never raises on a
    weird box — an unparseable field reads as 0.0 rather than 500ing a poll."""
    result = await session.transport.run(BOX_METRICS_COMMAND)
    return parse_box_metrics(result.stdout)


def parse_box_metrics(stdout: str) -> tuple[float, float, float]:
    """Parse ``BOX_METRICS_COMMAND`` output into ``(cpu, mem, disk)`` percentages.

    CPU has no cheap point-in-time percentage on a stock box, so it is derived
    from the 1-minute load average over the core count and capped at 100 — the
    same approximation every load-based CPU gauge uses.
    """
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    load = _as_float(lines[0] if len(lines) > 0 else "")
    cores = _as_float(lines[1] if len(lines) > 1 else "")
    mem = _as_float(lines[2] if len(lines) > 2 else "")
    disk = _as_float(lines[3].rstrip("%") if len(lines) > 3 else "")
    cpu = min(100.0, round(load / cores * 100, 1)) if cores > 0 else 0.0
    return cpu, min(100.0, mem), min(100.0, disk)


def _as_float(raw: str) -> float:
    try:
        return float(raw)
    except ValueError:
        logger.debug("ship: unparseable box metric field %r", raw)
        return 0.0
