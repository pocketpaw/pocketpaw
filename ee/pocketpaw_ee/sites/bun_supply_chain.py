# bun_supply_chain.py — the ONE place the site-build install-time supply-chain
# floor lives.
#
# Created: 2026-09-12 (fix/sites-install-supply-chain-floor) — extracted from
# ``daytona_runner.py``, which owned ``SANDBOX_BUNFIG`` / ``SANDBOX_BUNFIG_REL``
# because the sandbox was the only build host that wrote one. It was not the only
# build host that INSTALLS. ``generator_client._SubprocessRunner.install`` runs
# ``bun install`` too — on the path ``dev_server``, ``draft_markup``, ``service``
# and the deployed Coolify image all use — and it wrote no bunfig at all, so that
# path resolved from the open registry with lifecycle scripts enabled. A second
# installer with no copy of the floor is how a floor stops being a floor, so the
# constant moved HERE and both runners call it. ``daytona_runner`` re-exports both
# names unchanged (the same re-export pattern ``sites_create`` uses for
# ``react_paths``), so nothing that imported them from there had to change.
"""Install-time supply-chain floor for generated Paw Site builds.

WHY THIS FILE HAS TO EXIST AT ALL. This workspace's protections live in a
DEVELOPER'S HOME DIR (``~/.npmrc``, ``~/.bunfig.toml``) and are in no repo, so
neither a fresh Daytona container nor the Coolify runtime image inherits any of
them: no release-age floor, no ``ignoreScripts``. A build host that resolves from
the open registry with lifecycle scripts enabled is strictly weaker than the
runtime image beside it.

``minimumReleaseAge`` is the same 7-day floor the dev machines enforce, expressed
in SECONDS because that is bun's unit — 604800. It is the control that would catch
a compromised fresh publish of an already-vetted package, which the allowlist
cannot: the allowlist pins WHICH packages, and a caret pin still floats the
VERSION.

``ignoreScripts`` matters more on a build host than on a laptop. A postinstall
script here runs with the host's network and filesystem, next to the artifact we
are about to deploy — and on the Coolify path, as the backend user in the
container holding the app's secrets. Nothing in the vetted set needs one.

DELIBERATELY WRITTEN AT THE BUILD BOUNDARY, NOT TEMPLATED INTO THE GENERATED
PROJECT. Two reasons: it is a property of the BUILD HOST, not of the customer's
site, so it has no business in their source tree; and applying it at this boundary
means a template change cannot silently drop it. It lands in the project dir
because that is where bun looks.

DISPLACES rather than merges. A project-emitted ``bunfig.toml`` is overwritten, not
respected and not merged, so a future template change cannot lower the floor by
shipping its own. No generated project emits one today; this is a guard against
the change that would.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where bun looks, relative to the project root it installs in.
BUILD_BUNFIG_REL = "bunfig.toml"

#: The floor itself. Read the module docstring before changing either value.
BUILD_BUNFIG = """# Written by pocketpaw's build lane — NOT part of your site's source.
# Install-time supply-chain floor for this build. See bun_supply_chain.BUILD_BUNFIG.
[install]
# 7 days, in seconds. Matches the floor the dev machines enforce via ~/.bunfig.toml.
minimumReleaseAge = 604800
# No lifecycle scripts. Nothing in the vetted dependency set needs one, and a
# postinstall here would run beside the artifact we are about to deploy.
ignoreScripts = true
"""


def write_build_bunfig(project_dir: str | Path) -> None:
    """Write the floor into ``project_dir``, displacing any bunfig already there.

    Call this IMMEDIATELY BEFORE spawning ``bun install``. Written after the install
    has started, it is not a floor — it is a file.

    Logs when it displaces one, because a generated project that started emitting a
    bunfig is a template change someone should hear about rather than a condition to
    swallow silently.
    """
    target = Path(project_dir, BUILD_BUNFIG_REL)
    if target.is_file():
        logger.warning(
            "sites.build.bunfig_displaced dir=%s — a project-supplied bunfig.toml was "
            "overwritten by the build lane's supply-chain floor",
            project_dir,
        )
    target.write_text(BUILD_BUNFIG, encoding="utf-8")
