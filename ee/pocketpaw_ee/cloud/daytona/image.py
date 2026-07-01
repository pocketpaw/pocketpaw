"""Custom Docker image definitions for Daytona sandboxes.

Provides pre-built images with common development tools pre-installed so
every project VM comes ready to run code without manual setup.

Default image includes:
  - Python 3.12 + pip + venv + UV
  - GCC / G++ / build-essential (for C, C++, Rust compilation)
  - Node.js 20 LTS + npm
  - Docker CE + Compose plugin
  - Git, curl, wget, and other common CLI utilities

The image can be overridden with the ``DAYTONA_SANDBOX_IMAGE`` env var
(e.g. ``DAYTONA_SANDBOX_IMAGE=node:20``), or set to ``"standard"`` to
explicitly use the pre-built environment below.

Added: 2026-06-25
"""

from __future__ import annotations

import logging
import os

from daytona import Image

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variable override
# ---------------------------------------------------------------------------

_ENV_IMAGE = "DAYTONA_SANDBOX_IMAGE"


def sandbox_image_override() -> str | None:
    """Return the image string from the env var, or ``None``.

    The env var ``DAYTONA_SANDBOX_IMAGE`` can be set to:
      - A regular Docker image reference (e.g. ``"node:20"``,
        ``"python:3.12-slim"``) — used as-is.
      - ``"standard"`` or ``"default"`` — explicitly uses the
        pre-built Paw development image defined here.
      - Empty/unset — falls back to the pre-built image.
    """
    val = os.environ.get(_ENV_IMAGE, "").strip()
    if not val or val.lower() in ("standard", "default"):
        return None  # use the pre-built image
    logger.info("Using custom DAYTONA_SANDBOX_IMAGE=%s", val)
    return val


# ---------------------------------------------------------------------------
# Pre-built development image
# ---------------------------------------------------------------------------


def build_paw_dev_image() -> Image:
    """Build a custom Docker image with all common dev tools pre-installed.

    Starts from ``python:3.12-slim-bookworm`` (Debian 12 bookworm with
    Python 3.12) which already includes GCC, gfortran, and build-essential
    via the ``Image.debian_slim()`` factory.

    Adds:
      * Node.js 20 LTS (via NodeSource)
      * Docker CE + docker-compose-plugin
      * UV (fast Python package installer, from Astral)
      * Git, curl, wget, unzip, and other CLI essentials

    Returns:
        An ``Image`` object that the Daytona SDK will build at sandbox
        creation time.  The dockerfile is generated lazily.
    """
    img = (
        Image.debian_slim("3.12")
        # ── CLI essentials ──────────────────────────────────────────
        .run_commands(
            "apt-get update && apt-get install -y --no-install-recommends "
            "git curl wget ca-certificates gnupg lsb-release unzip",
        )
        # ── Node.js 20 LTS ──────────────────────────────────────────
        .run_commands(
            "curl -fsSL https://deb.nodesource.com/setup_20.x | bash -",
            "apt-get install -y nodejs",
            "corepack enable",
        )
        # ── Docker CE (Docker-in-Docker style) ──────────────────────
        .run_commands(
            "install -m 0755 -d /etc/apt/keyrings",
            "curl -fsSL https://download.docker.com/linux/debian/gpg"
            " -o /etc/apt/keyrings/docker.asc",
            "chmod a+r /etc/apt/keyrings/docker.asc",
            'echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] '
            "https://download.docker.com/linux/debian "
            '$(lsb_release -cs) stable" > /etc/apt/sources.list.d/docker.list',
            "apt-get update && apt-get install -y docker-ce docker-ce-cli containerd.io "
            "docker-compose-plugin",
        )
        # ── UV (Astral) ─────────────────────────────────────────────
        .run_commands(
            "pip install --upgrade pip uv",
        )
        # ── Final cleanup ───────────────────────────────────────────
        .run_commands(
            "apt-get clean && rm -rf /var/lib/apt/lists/*",
        )
        .workdir("/home/daytona")
    )

    logger.debug("Built Paw dev image dockerfile:\n%s", img.dockerfile())
    return img


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_sandbox_image() -> str | Image:
    """Return the image to use for new sandboxes.

    Priority:
      1. ``DAYTONA_SANDBOX_IMAGE`` env var (if set to a non-standard value)
      2. Pre-built Paw dev image (default)
    """
    override = sandbox_image_override()
    if override is not None:
        return override
    return build_paw_dev_image()
