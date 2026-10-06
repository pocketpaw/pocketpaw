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
#     packages or authored build-shell files (``HostInstallRefused``, keyed on
#     ``dependency_manifest.requires_sandbox``). That host holds the app's secrets,
#     so as defence in depth its bunfig (:data:`HOST_BUNFIG`) pins the public npm
#     registry, keeps the 7-day release-age floor and ``ignoreScripts``; any project
#     ``.npmrc`` is deleted before install; and install + build run with
#     :func:`host_build_env`, an allowlisted environment with no API secrets.
"""Install-time bunfig for generated Paw Site builds.

The developer machines' protections live in ``~/.npmrc`` / ``~/.bunfig.toml``, in no
repo, so neither a fresh container nor the deployed runtime image inherits them. The
host bunfig is written at the build boundary (not templated into the project) so a
template change cannot drop it, and it DISPLACES any project-supplied bunfig.toml so
an author file cannot lower the host floor.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
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
# The public registry, always. An authored bunfig/.npmrc never reaches the host, and
# this pin means one that did still could not point the install anywhere else.
registry = "https://registry.npmjs.org/"
# 7 days, in seconds. Matches the floor the dev machines enforce via ~/.bunfig.toml.
minimumReleaseAge = 604800
# No lifecycle scripts. The host installs only our toolchain, and a postinstall here
# would run next to the app's secrets.
ignoreScripts = true
"""


def write_host_bunfig(project_dir: str | Path) -> None:
    """Write the host floor into ``project_dir``, displacing any bunfig already there,
    and delete any project ``.npmrc`` (any case spelling).

    Call this IMMEDIATELY BEFORE spawning ``bun install`` on the API host. Logs when
    it displaces a file, since authored install config is expected only in the sandbox.
    """
    for entry in Path(project_dir).iterdir() if Path(project_dir).is_dir() else ():
        if entry.name.casefold() == ".npmrc" and entry.is_file():
            logger.warning(
                "sites.build.npmrc_removed dir=%s — a project-supplied .npmrc was deleted "
                "before the host install",
                project_dir,
            )
            entry.unlink()
    target = Path(project_dir, BUILD_BUNFIG_REL)
    if target.is_file():
        logger.warning(
            "sites.build.bunfig_displaced dir=%s — a project-supplied bunfig.toml was "
            "overwritten by the host build's supply-chain floor",
            project_dir,
        )
    target.write_text(HOST_BUNFIG, encoding="utf-8")


#: Environment variables a host ``bun install`` / ``bun run build`` may see. Toolchain
#: plumbing only (search path, home and temp dirs, Windows system dirs, locale, bun's
#: cache, TLS trust and proxy settings); never an API secret. Matched
#: case-insensitively because Windows env names are.
HOST_BUILD_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "PATHEXT",
        "HOME",
        "USERPROFILE",
        "HOMEDRIVE",
        "HOMEPATH",
        "APPDATA",
        "LOCALAPPDATA",
        "TMP",
        "TEMP",
        "TMPDIR",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PROGRAMDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE",
        "OS",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TZ",
        "TERM",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "BUN_INSTALL",
        "BUN_INSTALL_CACHE_DIR",
        "BUN_RUNTIME_TRANSPILER_CACHE_PATH",
        "NODE_EXTRA_CA_CERTS",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
    }
)


def host_build_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The scrubbed environment for a host ``bun install`` / ``bun run build``.

    Only :data:`HOST_BUILD_ENV_ALLOWLIST` names pass, so the API's secrets (cloud
    keys, database URLs, LLM keys) never reach a package manager or a build that
    runs site code.
    """
    src = os.environ if environ is None else environ
    return {k: v for k, v in src.items() if k.upper() in HOST_BUILD_ENV_ALLOWLIST}
