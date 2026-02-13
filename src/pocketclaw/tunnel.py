import asyncio
import logging
import re
import shutil
from typing import Optional

logger = logging.getLogger(__name__)


class TunnelManager:
    """
    Manages a Cloudflare Tunnel (cloudflared) process to expose the local server.
    """

    def __init__(self, port: int = 8888):
        self.port = port
        self.process: Optional[asyncio.subprocess.Process] = None
        self.public_url: Optional[str] = None
        self._shutdown_event = asyncio.Event()

    def is_installed(self) -> bool:
        """Check if cloudflared is installed."""
        return shutil.which("cloudflared") is not None

    async def install(self) -> bool:
        """Attempt to install cloudflared via Homebrew."""
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
            logger.error(f"Installation failed: {e}")
            return False

    async def start(self) -> str:
        """
        Start the tunnel.
        Returns the public URL if successful.
        Raises RuntimeError if start fails.
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
                return self.public_url
            # Process running but no URL yet? Stop and restart.
            await self.stop()

        logger.info(f"Starting Cloudflare Tunnel for localhost:{self.port}...")

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
            logger.info(f"Tunnel established at: {self.public_url}")
            return self.public_url
        except Exception as e:
            logger.error(f"Failed to start tunnel: {e}")
            await self.stop()
            raise

    async def _wait_for_url(self, timeout: int = 30) -> str:
        """Monitor stderr for the trycloudflare.com URL."""
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
                    logger.debug(f"[cloudflared] {line}")

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

        raise RuntimeError("Stream ended without finding URL")

    async def stop(self):
        """Stop the tunnel process."""
        if self.process:
            logger.info("Stopping Cloudflare Tunnel...")
            try:
                self.process.terminate()
                try:
                    await asyncio.wait_for(self.process.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self.process.kill()
            except ProcessLookupError:
                pass  # Already dead
            finally:
                self.process = None
                self.public_url = None

    def get_status(self) -> dict:
        """Get current tunnel status."""
        active = (
            self.process is not None
            and self.process.returncode is None
            and self.public_url is not None
        )
        return {"active": active, "url": self.public_url, "installed": self.is_installed()}


# Global instance
_tunnel_instance: Optional[TunnelManager] = None


def get_tunnel_manager(port: int = 8888) -> TunnelManager:
    global _tunnel_instance
    if _tunnel_instance is None:
        _tunnel_instance = TunnelManager(port=port)

        from pocketclaw.lifecycle import register

        def _reset():
            global _tunnel_instance
            _tunnel_instance = None

        register("tunnel", shutdown=_tunnel_instance.stop, reset=_reset)
    return _tunnel_instance
