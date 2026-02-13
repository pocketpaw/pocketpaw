# PocketPaw Desktop Launcher — System Tray
# Cross-platform tray icon using pystray.
# Menu: version, dashboard, start/stop/restart, auto-start, updates, logs,
#       uninstall, quit.
# Created: 2026-02-10

from __future__ import annotations

import logging
import os
import platform
import subprocess
import threading
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .server import ServerManager
    from .updater import Updater

# pystray and PIL are the only external deps the launcher needs.
# They're installed alongside the launcher, not in the pocketpaw venv.
try:
    import pystray
    from PIL import Image

    HAS_TRAY = True
except ImportError:
    HAS_TRAY = False
    logger.warning("pystray or Pillow not available — tray icon disabled")

from installer.launcher.common import POCKETCLAW_HOME

LOG_FILE = POCKETCLAW_HOME / "logs" / "launcher.log"


def _get_version() -> str:
    """Get the launcher version string."""
    try:
        from . import __version__

        return __version__
    except Exception:
        return "0.1.0"


def _load_icon() -> Image.Image | None:
    """Load the tray icon image."""
    if not HAS_TRAY:
        return None

    icon_path = Path(__file__).parent / "assets" / "icon.png"
    if icon_path.exists():
        try:
            img = Image.open(icon_path)
            # Resize to standard tray size
            img = img.resize((64, 64), Image.Resampling.LANCZOS)
            return img
        except Exception as exc:
            logger.warning("Failed to load icon: %s", exc)

    # Fallback: generate a simple colored square
    img = Image.new("RGBA", (64, 64), (108, 99, 255, 255))
    return img


class TrayIcon:
    """System tray icon with PocketPaw controls."""

    def __init__(self, server: ServerManager, updater: Updater) -> None:
        self.server = server
        self.updater = updater
        self._icon: pystray.Icon | None = None
        self._update_available = False
        self._version = _get_version()

        # Auto-start manager (lazy init to avoid import errors if module is missing)
        self._autostart = None
        try:
            from .autostart import AutoStartManager

            self._autostart = AutoStartManager()
        except Exception:
            logger.debug("AutoStartManager not available")

    def run(self) -> None:
        """Start the tray icon. Blocks until quit is selected."""
        if not HAS_TRAY:
            logger.error("Cannot create tray icon: pystray not installed")
            return

        icon_image = _load_icon()
        if not icon_image:
            logger.error("Cannot create tray icon: no icon image")
            return

        self._icon = pystray.Icon(
            name="PocketPaw",
            icon=icon_image,
            title=self._get_tooltip(),
            menu=self._build_menu(),
        )

        # Start periodic update check in background
        check_thread = threading.Thread(target=self._periodic_update_check, daemon=True)
        check_thread.start()

        # Start tooltip updater
        tooltip_thread = threading.Thread(target=self._update_tooltip_loop, daemon=True)
        tooltip_thread.start()

        logger.info("System tray icon started")
        self._icon.run()

    def stop(self) -> None:
        """Stop the tray icon."""
        if self._icon:
            self._icon.stop()

    # ── Tooltip ─────────────────────────────────────────────────────────

    def _get_tooltip(self) -> str:
        """Dynamic tooltip showing current status."""
        if self.server.is_running():
            port = getattr(self.server, "port", None) or "8888"
            return f"PocketPaw v{self._version} — Running on port {port}"
        return f"PocketPaw v{self._version} — Stopped"

    def _update_tooltip_loop(self) -> None:
        """Periodically update the tooltip text."""
        import time

        while True:
            time.sleep(5)
            if self._icon:
                try:
                    self._icon.title = self._get_tooltip()
                except Exception:
                    pass

    # ── Menu ───────────────────────────────────────────────────────────

    def _build_menu(self) -> pystray.Menu:
        """Build the tray context menu.

        Layout:
          PocketPaw v0.x.x  (disabled)
          ──────────────────
          Open Dashboard     (default / double-click)
          ──────────────────
          Start/Stop Server
          Restart Server
          ──────────────────
          Start on Login     (checkable, if available)
          Check for Updates
          View Logs...
          ──────────────────
          Uninstall...
          Quit PocketPaw
        """
        items: list = [
            # Version display (disabled, informational)
            pystray.MenuItem(
                f"PocketPaw v{self._version}",
                None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Open Dashboard",
                self._on_open_dashboard,
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                self._server_toggle_text,
                self._on_toggle_server,
            ),
            pystray.MenuItem(
                "Restart Server",
                self._on_restart,
            ),
            pystray.Menu.SEPARATOR,
        ]

        # Auto-start toggle (if available)
        if self._autostart is not None:
            items.append(
                pystray.MenuItem(
                    "Start on Login",
                    self._on_toggle_autostart,
                    checked=lambda item: self._autostart.is_enabled(),
                ),
            )

        items.extend(
            [
                pystray.MenuItem(
                    self._update_text,
                    self._on_check_update,
                ),
                pystray.MenuItem(
                    "View Logs...",
                    self._on_view_logs,
                ),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Uninstall...",
                    self._on_uninstall,
                ),
                pystray.MenuItem(
                    "Quit PocketPaw",
                    self._on_quit,
                ),
            ]
        )

        return pystray.Menu(*items)

    def _server_toggle_text(self, item: pystray.MenuItem) -> str:
        """Dynamic text for start/stop menu item."""
        if self.server.is_running():
            return "Stop Server"
        return "Start Server"

    def _update_text(self, item: pystray.MenuItem) -> str:
        """Dynamic text for update menu item."""
        if self._update_available:
            return "Update Available — Install Now"
        return "Check for Updates"

    # ── Actions ────────────────────────────────────────────────────────

    def _on_open_dashboard(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Open the web dashboard in the default browser."""
        if not self.server.is_running():
            # Start the server first
            threading.Thread(target=self._start_and_open, daemon=True).start()
        else:
            url = self.server.get_dashboard_url()
            webbrowser.open(url)

    def _start_and_open(self) -> None:
        """Start server then open browser."""
        if self.server.start():
            import time

            time.sleep(1)
            webbrowser.open(self.server.get_dashboard_url())

    def _on_toggle_server(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Start or stop the server."""
        if self.server.is_running():
            threading.Thread(target=self.server.stop, daemon=True).start()
        else:
            threading.Thread(target=self.server.start, daemon=True).start()
        # Refresh menu
        if self._icon:
            self._icon.update_menu()

    def _on_restart(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Restart the server."""
        threading.Thread(target=self.server.restart, daemon=True).start()

    def _on_toggle_autostart(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Toggle auto-start on login."""
        if self._autostart is None:
            return
        if self._autostart.is_enabled():
            self._autostart.disable()
        else:
            self._autostart.enable()
        if self._icon:
            self._icon.update_menu()

    def _on_check_update(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Check for updates and apply if available."""
        threading.Thread(target=self._do_update, daemon=True).start()

    def _do_update(self) -> None:
        """Check and apply update."""
        info = self.updater.check()
        if info.update_available:
            self._update_available = False
            was_running = self.server.is_running()
            if was_running:
                self.server.stop()
            self.updater.apply()
            if was_running:
                self.server.start()
            if self._icon:
                self._icon.update_menu()
                self._notify("PocketPaw Updated", f"Updated to v{info.latest_version}")
        else:
            self._notify("No Updates", "PocketPaw is up to date")

    def _on_view_logs(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Open the launcher log file in the default text editor."""
        if not LOG_FILE.exists():
            self._notify("No Logs", "Log file not found")
            return

        try:
            system = platform.system()
            if system == "Darwin":
                subprocess.Popen(["open", str(LOG_FILE)])
            elif system == "Windows":
                os.startfile(str(LOG_FILE))  # noqa: S606
            else:
                subprocess.Popen(["xdg-open", str(LOG_FILE)])
        except Exception as exc:
            logger.warning("Failed to open log file: %s", exc)

    def _on_uninstall(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Launch interactive uninstaller in a console."""
        threading.Thread(target=self._do_uninstall, daemon=True).start()

    def _do_uninstall(self) -> None:
        """Run the uninstaller."""
        try:
            from .uninstall import Uninstaller

            self.server.stop()
            uninstaller = Uninstaller()
            # Non-interactive: remove safe components only
            results = uninstaller.uninstall(
                remove_venv=True,
                remove_uv=True,
                remove_python=True,
                remove_logs=True,
                remove_config=False,
                remove_memory=False,
            )
            for r in results:
                logger.info("Uninstall: %s", r)
            self._notify("Uninstall Complete", "PocketPaw components removed")
            self.stop()
        except Exception as exc:
            logger.error("Uninstall failed: %s", exc)
            self._notify("Uninstall Failed", str(exc))

    def _on_quit(self, icon: pystray.Icon, item: pystray.MenuItem) -> None:
        """Quit: stop server and exit."""
        self.server.stop()
        self.stop()

    # ── Background Tasks ───────────────────────────────────────────────

    def _periodic_update_check(self) -> None:
        """Check for updates every 6 hours."""
        import time

        # Wait 30 seconds after launch before first check
        time.sleep(30)
        while True:
            try:
                info = self.updater.check()
                if info.update_available:
                    self._update_available = True
                    if self._icon:
                        self._icon.update_menu()
                        self._notify(
                            "Update Available",
                            f"PocketPaw v{info.latest_version} is available",
                        )
            except Exception:
                pass
            # Check every 6 hours
            time.sleep(6 * 3600)

    def _notify(self, title: str, message: str) -> None:
        """Show a system notification if supported."""
        if self._icon and hasattr(self._icon, "notify"):
            try:
                self._icon.notify(message, title)
            except Exception:
                logger.debug("Notification not supported on this platform")
