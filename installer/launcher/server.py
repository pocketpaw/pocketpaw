# PocketPaw Desktop Launcher — Server Manager
# Starts/stops the PocketPaw server process from the venv.
# Created: 2026-02-10
# Updated: 2026-02-10 — is_running() now cleans up stale PID files

from __future__ import annotations

import json
import logging
import os
import platform
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from installer.launcher.common import (
    POCKETCLAW_HOME,
    VENV_DIR,
    StatusCallback,
    noop_status,
)

logger = logging.getLogger(__name__)

PID_FILE = POCKETCLAW_HOME / "launcher.pid"
DEFAULT_PORT = 8888


class ServerManager:
    """Manage the PocketPaw server subprocess."""

    def __init__(self, port: int | None = None, on_status: StatusCallback | None = None) -> None:
        self.port = port or self._read_port_from_config() or DEFAULT_PORT
        self.on_status = on_status or noop_status
        self._process: subprocess.Popen | None = None
        self._log_fh = None
        self._lock = __import__("threading").Lock()

    # ── Public API ─────────────────────────────────────────────────────

    def start(self) -> bool:
        """Start the PocketPaw server. Returns True on success."""
        with self._lock:
            return self._start_locked()

    def _start_locked(self) -> bool:
        """Start the server (must be called under self._lock)."""
        if self.is_running():
            self.on_status("Server is already running")
            return True

        python = self._venv_python()
        if not python.exists():
            self.on_status("PocketPaw not installed. Run setup first.")
            return False

        # Find a free port if the default is taken
        if not self._is_port_free(self.port):
            self.port = self._find_free_port()
            logger.info("Default port busy, using port %d", self.port)

        self.on_status(f"Starting PocketPaw on port {self.port}...")
        logger.info("Starting server: %s -m pocketclaw --port %d", python, self.port)

        try:
            # Start the server process — redirect output to a log file
            # instead of PIPE to avoid OS pipe buffer deadlock (64KB limit)
            log_file = POCKETCLAW_HOME / "server.log"
            self._log_fh = open(log_file, "a", encoding="utf-8")  # noqa: SIM115
            env = self._build_env()
            self._process = subprocess.Popen(
                [str(python), "-m", "pocketclaw", "--port", str(self.port)],
                env=env,
                stdout=self._log_fh,
                stderr=self._log_fh,
                # Don't inherit the launcher's console on Windows
                creationflags=self._creation_flags(),
            )

            # Write PID file
            PID_FILE.write_text(str(self._process.pid))

            # Wait for the server to become healthy
            # pocketclaw needs ~25s for startup + internal setup
            if self._wait_for_healthy(timeout=60):
                self.on_status(f"PocketPaw running on port {self.port}")
                return True
            else:
                self.on_status("Server started but health check failed")
                # Still return True — the process is running
                return True

        except FileNotFoundError:
            self.on_status(f"Python not found at {python}")
            return False
        except Exception as exc:
            self.on_status(f"Failed to start: {exc}")
            logger.exception("Failed to start server")
            return False

    def stop(self) -> None:
        """Stop the PocketPaw server."""
        with self._lock:
            self.on_status("Stopping PocketPaw...")

            if self._process and self._process.poll() is None:
                self._graceful_shutdown(self._process)
                self._process = None
            else:
                # Try to stop via PID file
                self._stop_via_pid()

            # Close log file handle
            if self._log_fh:
                try:
                    self._log_fh.close()
                except Exception:
                    pass
                self._log_fh = None

            PID_FILE.unlink(missing_ok=True)
            self.on_status("PocketPaw stopped")

    def restart(self) -> bool:
        """Restart the server."""
        self.stop()
        time.sleep(1)
        return self.start()

    def is_running(self) -> bool:
        """Check if the server process is alive."""
        # Check our managed process
        if self._process and self._process.poll() is None:
            return True

        # Check PID file
        if PID_FILE.exists():
            try:
                pid = int(PID_FILE.read_text().strip())
                if self._pid_alive(pid):
                    return True
                # Stale PID — process is dead, clean up
                PID_FILE.unlink(missing_ok=True)
            except (ValueError, OSError):
                PID_FILE.unlink(missing_ok=True)

        return False

    def is_healthy(self) -> bool:
        """Check if the server responds to HTTP."""
        try:
            url = f"http://127.0.0.1:{self.port}/"
            req = urllib.request.Request(url, method="GET")
            resp = urllib.request.urlopen(req, timeout=3)
            return resp.status == 200
        except Exception:
            return False

    def get_dashboard_url(self) -> str:
        """Get the URL to open in the browser."""
        return f"http://127.0.0.1:{self.port}"

    # ── Internal ───────────────────────────────────────────────────────

    def _venv_python(self) -> Path:
        """Path to the venv Python executable."""
        if platform.system() == "Windows":
            return VENV_DIR / "Scripts" / "python.exe"
        return VENV_DIR / "bin" / "python"

    def _build_env(self) -> dict[str, str]:
        """Build environment variables for the server process."""
        env = dict(os.environ)
        # Ensure the venv's bin/Scripts is first on PATH
        if platform.system() == "Windows":
            venv_bin = str(VENV_DIR / "Scripts")
        else:
            venv_bin = str(VENV_DIR / "bin")

        # Also add the uv directory so pocketclaw's auto_install can find uv
        uv_dir = str(POCKETCLAW_HOME / "uv")
        env["PATH"] = venv_bin + os.pathsep + uv_dir + os.pathsep + env.get("PATH", "")

        env["VIRTUAL_ENV"] = str(VENV_DIR)
        # Force UTF-8 so emoji/unicode in pocketclaw output doesn't crash
        # on Windows (default cp1252 can't encode them)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"

        # Set UV_OVERRIDE so any uv invocation (including pocketclaw's
        # internal auto_install) uses our tiktoken override
        overrides_file = POCKETCLAW_HOME / "uv-overrides.txt"
        if not overrides_file.exists():
            # Ensure the overrides file exists even if bootstrap was skipped
            try:
                overrides_file.write_text("tiktoken>=0.7.0\n", encoding="utf-8")
            except OSError:
                pass
        env["UV_OVERRIDE"] = str(overrides_file)

        return env

    def _creation_flags(self) -> int:
        """Windows-specific process creation flags."""
        if platform.system() == "Windows":
            # CREATE_NO_WINDOW — don't show a console window
            return 0x08000000
        return 0

    def _wait_for_healthy(self, timeout: int = 30) -> bool:
        """Wait for the server to respond to health checks."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._process and self._process.poll() is not None:
                # Process died — read last lines from the log file
                rc = self._process.returncode
                log_tail = ""
                try:
                    log_file = POCKETCLAW_HOME / "server.log"
                    if log_file.exists():
                        log_tail = log_file.read_text(encoding="utf-8", errors="replace")[-2000:]
                except Exception:
                    pass
                logger.error(
                    "Server process exited with code %d\n%s",
                    rc,
                    log_tail,
                )
                return False
            if self.is_healthy():
                return True
            time.sleep(0.5)
        return False

    def _graceful_shutdown(self, proc: subprocess.Popen, timeout: int = 10) -> None:
        """Send SIGTERM, wait, then SIGKILL if needed."""
        try:
            if platform.system() == "Windows":
                proc.terminate()
            else:
                proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("Server didn't stop gracefully, killing")
            proc.kill()
            proc.wait(timeout=5)
        except Exception as exc:
            logger.warning("Error stopping server: %s", exc)

    def _stop_via_pid(self) -> None:
        """Stop the server using the PID file."""
        if not PID_FILE.exists():
            return
        try:
            pid = int(PID_FILE.read_text().strip())
            if self._pid_alive(pid):
                if platform.system() == "Windows":
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True,
                    )
                else:
                    os.kill(pid, signal.SIGTERM)
                    # Wait briefly
                    for _ in range(20):
                        if not self._pid_alive(pid):
                            break
                        time.sleep(0.5)
                    else:
                        os.kill(pid, signal.SIGKILL)
        except (ValueError, OSError, ProcessLookupError):
            pass

    def _pid_alive(self, pid: int) -> bool:
        """Check if a PID is alive."""
        try:
            if platform.system() == "Windows":
                result = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}"],
                    capture_output=True,
                    text=True,
                )
                return str(pid) in result.stdout
            else:
                os.kill(pid, 0)
                return True
        except (OSError, ProcessLookupError):
            return False

    def _is_port_free(self, port: int) -> bool:
        """Check if a port is available."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return True
        except OSError:
            return False

    def _find_free_port(self) -> int:
        """Find a free port starting from the default."""
        for port in range(self.port, self.port + 100):
            if self._is_port_free(port):
                return port
        # Last resort: let OS pick
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]

    def _read_port_from_config(self) -> int | None:
        """Read the web port from the PocketPaw config file."""
        config_path = POCKETCLAW_HOME / "config.json"
        if config_path.exists():
            try:
                config = json.loads(config_path.read_text())
                return config.get("web_port")
            except (json.JSONDecodeError, OSError):
                pass
        return None
