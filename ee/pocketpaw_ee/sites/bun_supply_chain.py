# bun_supply_chain.py — the bunfig each site-build install host writes before
# ``bun install``: an OPEN one for the Daytona sandbox, a FLOORED one for the API host.
#
# Two hosts install, and they install different things:
#
#   * The Daytona sandbox (``daytona_runner``) installs the generated project INCLUDING
#     the author's declared packages. Policy since 2026-10-07 ("open everything"): the
#     author may use any npm package, fresh publishes and install scripts included.
#     The sandbox is the isolation boundary, so its bunfig (:data:`BUILD_BUNFIG`, a.k.a.
#     :data:`SANDBOX_BUNFIG`) sets no release-age floor and does not ignore scripts.
#   * The API host (``generator_client._SubprocessRunner``) only ever installs our own
#     toolchain: ``generator_client`` refuses a host build whose source declares author
#     packages (``HostInstallRefused``). That host holds the app's secrets, so its
#     bunfig (:data:`HOST_BUNFIG`) keeps the 7-day release-age floor and
#     ``ignoreScripts``.
"""Install-time bunfig for generated Paw Site builds.

The developer machines' protections live in ``~/.npmrc`` / ``~/.bunfig.toml``, in no
repo, so neither a fresh container nor the deployed runtime image inherits them. The
host bunfig is written at the build boundary (not templated into the project) so a
template change cannot drop it, and it DISPLACES any project-supplied bunfig.toml so
an author file cannot lower the host floor.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where bun looks, relative to the project root it installs in.
BUILD_BUNFIG_REL = "bunfig.toml"

#: The host release-age floor in seconds (7 days, bun's unit).
MINIMUM_RELEASE_AGE_SECONDS = 604800

#: The SANDBOX bunfig: no floor, scripts allowed. The sandbox is the isolation.
BUILD_BUNFIG = """# Written by pocketpaw's build lane for the Daytona build sandbox.
# Author packages install here with no release-age floor and with lifecycle
# scripts enabled: the sandbox is the isolation boundary. See bun_supply_chain.
[install]
"""
SANDBOX_BUNFIG = BUILD_BUNFIG

#: The HOST bunfig. Read the module header before changing either value.
HOST_BUNFIG = """# Written by pocketpaw's build lane — NOT part of your site's source.
# Install-time supply-chain floor for host builds. See bun_supply_chain.HOST_BUNFIG.
[install]
# 7 days, in seconds. Matches the floor the dev machines enforce via ~/.bunfig.toml.
minimumReleaseAge = 604800
# No lifecycle scripts. The host installs only our toolchain, and a postinstall here
# would run next to the app's secrets.
ignoreScripts = true
"""


def write_host_bunfig(project_dir: str | Path) -> None:
    """Write the host floor into ``project_dir``, displacing any bunfig already there.

    Call this IMMEDIATELY BEFORE spawning ``bun install`` on the API host. Logs when
    it displaces one, since an authored bunfig.toml is expected only in the sandbox.
    """
    target = Path(project_dir, BUILD_BUNFIG_REL)
    if target.is_file():
        logger.warning(
            "sites.build.bunfig_displaced dir=%s — a project-supplied bunfig.toml was "
            "overwritten by the host build's supply-chain floor",
            project_dir,
        )
    target.write_text(HOST_BUNFIG, encoding="utf-8")
