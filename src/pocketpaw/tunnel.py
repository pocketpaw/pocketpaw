import asyncio
import logging
import re
import shutil
from typing import Optional

logger: logging.Logger = logging.getLogger(__name__)


class TunnelManager:
    """Manage a transient Cloudflare Tunnel (``cloudflared``) process.

    The tunnel exposes a local PocketPaw dashboard (typically running on
    ``http://localhost:<port>``) to the public internet using a one-off
    "quick tunnel" URL on ``*.trycloudflare.com``. This is intended for
    short-lived remote access sessions and **not** as a long-term
    production ingress replacement.

    Instances of this class are usually accessed via
    :func:`get_tunnel_manager`, which wires the manager into the global
    lifecycle registry so tunnels are stopped on application shutdown.
    """

    def __init__(self, port: int = 8888) -> None:
        """Create a new manager bound to the given local HTTP port."""
        self.port: int = port
        self.process: Optional[asyncio.subprocess.Process] = None
        self.public_url: Optional[str] = None
        self._shutdown_event: asyncio.Event = asyncio.Event()

    def is_installed(self) -> bool:
        """Return ``True`` if ``cloudflared`` is available on ``$PATH``."""
        return shutil.which("cloudflared") is not None

    async def install(self) -> bool:
        """Attempt to install ``cloudflared`` via Homebrew (macOS only).

        This helper is best-effort and primarily for developer convenience.
        In CI or production environments, ``cloudflared`` should be managed
        by the system's package manager or deployment tooling instead.
        """
        if self.is_installed():
            return True

        logger.info("cloudflared not found. Attempting installation via Homebrew...")
        try:
            # Check for brew first
            if shutil.which("brew") is None:
                logger.error("Homebrew not found. Cannot auto-install cloudflared.")
                return False

            proc = await asyncio.create_subprocess_exec(
                "brew",
                "install",
                "cloudflared",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                logger.info("cloudflared installed successfully!")
                return True
            else:
                logger.error(f"Failed to install cloudflared: {stderr.decode()}")
                return False
        except Exception as e:
            logger.error("Installation of cloudflared failed: %s", e)
            return False

    async def start(self) -> str:
        """Start a new Cloudflare tunnel and return its public URL.

        If a tunnel is already running and a public URL has been discovered,
        the existing URL is returned. If the ``cloudflared`` binary is not
        present and auto‑installation fails, a :class:`RuntimeError` is
        raised.
        """
        if not self.is_installed():
            logger.info("cloudflared missing, attempting auto-install...")
            installed = await self.install()
            if not installed:
                raise RuntimeError(
                    "cloudflared is not installed and auto-installation failed. Please run 'brew install cloudflared'."
                )

        if self.process:
            if self.public_url:
                logger.info("Reusing existing Cloudflare Tunnel at %s", self.public_url)
                return self.public_url
            # Process running but no URL yet? Stop and restart.
            logger.warning(
                "cloudflared process already running for port %s but no URL discovered; restarting.",
                self.port,
            )
            await self.stop()

        logger.info("Starting Cloudflare Tunnel for http://localhost:%s ...", self.port)

        # cloudflared tunnel --url http://localhost:8888
        # Output is printed to stderr usually.
        self.process = await asyncio.create_subprocess_exec(
            "cloudflared",
            "tunnel",
            "--url",
            f"http://localhost:{self.port}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            # Wait for URL in stderr
            self.public_url = await self._wait_for_url()
            logger.info("Cloudflare Tunnel established at %s", self.public_url)
            return self.public_url
        except Exception as e:
            logger.error("Failed to start Cloudflare Tunnel: %s", e)
            await self.stop()
            raise

    async def _wait_for_url(self, timeout: int = 30) -> str:
        """Monitor ``cloudflared`` stderr for the ``trycloudflare.com`` URL.

        Reads stderr line-by-line until a matching URL is found, the process
        exits unexpectedly, or the timeout is reached.
        """
        if not self.process or not self.process.stderr:
            raise RuntimeError("Process not started correctly")

        # We need to read line by line without blocking the loop forever
        # and also not consuming the stream entirely if we want to log it?
        # Actually, extracting the URL is the main goal.

        # Regex to find: https://[random].trycloudflare.com
        url_pattern = re.compile(r"https://[a-zA-Z0-9-]+\.trycloudflare\.com")

        start_time = asyncio.get_event_loop().time()

        while True:
            if asyncio.get_event_loop().time() - start_time > timeout:
                raise TimeoutError("Timed out waiting for Cloudflare Tunnel URL")

            if self.process.returncode is not None:
                # Process exited prematurely
                stderr_out = await self.process.stderr.read()
                raise RuntimeError(f"cloudflared exited unexpectedly: {stderr_out.decode()}")

            try:
                # Read line
                line_bytes = await asyncio.wait_for(self.process.stderr.readline(), timeout=1.0)
                if not line_bytes:
                    break  # EOF

                line = line_bytes.decode("utf-8", errors="ignore").strip()
                if line:
                    logger.debug("[cloudflared] %s", line)

                # Check for URL
                # Example output: ... trycloudflare.com ...
                # or: +--------------------------------------------------------------------------------------------+
                #     |  Your quick Tunnel has been created! Visit it at (it may take some time to be reachable):  |
                #     |  https://musical-example-domain.trycloudflare.com                                       |
                #     +--------------------------------------------------------------------------------------------+

                match = url_pattern.search(line)
                if match:
                    found_url = match.group(0)
                    # Simple verification it looks right
                    if "trycloudflare.com" in found_url:
                        return found_url

            except asyncio.TimeoutError:
                continue

        raise RuntimeError("Stream ended without finding Cloudflare Tunnel URL")

    async def stop(self) -> None:
        """Stop the tunnel process if it is currently running.

        This method is idempotent and safe to call multiple times.
        """
        if self.process:
            logger.info("Stopping Cloudflare Tunnel...")
            try:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self.process.kill()
            except ProcessLookupError:
                # Process already exited; nothing to do.
                logger.debug("cloudflared process already stopped.")
            finally:
                self.process = None
                self.public_url = None
                logger.info("Cloudflare Tunnel stopped.")

    def get_status(self) -> dict:
        """Return a JSON-serializable view of the current tunnel status.

        The returned dict is designed to be consumed by the dashboard
        (`/api/remote/status`) and never includes sensitive information.
        """
        active = (
            self.process is not None
            and self.process.returncode is None
            and self.public_url is not None
        )
        return {"active": active, "url": self.public_url, "installed": self.is_installed()}


# Global process-wide TunnelManager instance used by the dashboard.
_tunnel_instance: Optional[TunnelManager] = None


def get_tunnel_manager(port: int = 8888) -> TunnelManager:
    """Return the singleton :class:`TunnelManager` bound to the given port.

    On first call, creates a new manager for ``port`` and registers it with
    :mod:`pocketpaw.lifecycle` so the tunnel is shut down on application
    teardown and reset between tests.
    """
    global _tunnel_instance
    if _tunnel_instance is None:
        _tunnel_instance = TunnelManager(port=port)

        from pocketpaw.lifecycle import register

        def _reset() -> None:
            global _tunnel_instance
            _tunnel_instance = None

        register("tunnel", shutdown=_tunnel_instance.stop, reset=_reset)
    return _tunnel_instance
