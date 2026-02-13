# PocketPaw Desktop Launcher — Auto-Start Manager
# Manages start-on-login across macOS (launchd), Windows (registry), Linux (.desktop).
# Created: 2026-02-11

from __future__ import annotations

import logging
import platform
import plistlib
import subprocess
import sys
from pathlib import Path

from installer.launcher.common import POCKETCLAW_HOME

logger = logging.getLogger(__name__)

APP_ID = "com.pocketpaw.launcher"
APP_NAME = "PocketPaw"


def get_executable_path() -> str:
    """Get the path to the current executable (frozen or source).

    For frozen (PyInstaller) builds, returns the executable path.
    For source runs, returns the Python interpreter path.
    Callers should check _is_frozen() to know whether to append module args.
    """
    if getattr(sys, "frozen", False):
        # PyInstaller frozen executable — single binary
        return sys.executable
    # Running from source: return the Python interpreter.
    # Callers append "-m installer.launcher" for the full command.
    return sys.executable


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


class AutoStartManager:
    """Cross-platform auto-start manager."""

    def __init__(self) -> None:
        self._system = platform.system()

    def is_enabled(self) -> bool:
        """Check if auto-start is currently enabled."""
        if self._system == "Darwin":
            return self._macos_is_enabled()
        elif self._system == "Windows":
            return self._windows_is_enabled()
        elif self._system == "Linux":
            return self._linux_is_enabled()
        return False

    def enable(self) -> bool:
        """Enable auto-start. Returns True on success."""
        if self._system == "Darwin":
            return self._macos_enable()
        elif self._system == "Windows":
            return self._windows_enable()
        elif self._system == "Linux":
            return self._linux_enable()
        logger.warning("Auto-start not supported on %s", self._system)
        return False

    def disable(self) -> bool:
        """Disable auto-start. Returns True on success."""
        if self._system == "Darwin":
            return self._macos_disable()
        elif self._system == "Windows":
            return self._windows_disable()
        elif self._system == "Linux":
            return self._linux_disable()
        logger.warning("Auto-start not supported on %s", self._system)
        return False

    # ── macOS: launchd plist ────────────────────────────────────────────

    def _macos_plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{APP_ID}.plist"

    def _macos_plist_content(self) -> dict:
        exe = get_executable_path()
        if _is_frozen():
            program_args = [exe]
        else:
            program_args = [exe, "-m", "installer.launcher"]

        return {
            "Label": APP_ID,
            "ProgramArguments": program_args,
            "RunAtLoad": True,
            "KeepAlive": False,
            "StandardOutPath": str(POCKETCLAW_HOME / "logs" / "launcher-launchd.log"),
            "StandardErrorPath": str(POCKETCLAW_HOME / "logs" / "launcher-launchd.log"),
        }

    def _macos_is_enabled(self) -> bool:
        return self._macos_plist_path().exists()

    def _macos_enable(self) -> bool:
        try:
            plist_path = self._macos_plist_path()
            plist_path.parent.mkdir(parents=True, exist_ok=True)

            with open(plist_path, "wb") as f:
                plistlib.dump(self._macos_plist_content(), f)

            # Load the plist
            subprocess.run(
                ["launchctl", "load", str(plist_path)],
                capture_output=True,
                timeout=10,
            )
            logger.info("Auto-start enabled (launchd): %s", plist_path)
            return True
        except Exception as exc:
            logger.error("Failed to enable auto-start: %s", exc)
            return False

    def _macos_disable(self) -> bool:
        try:
            plist_path = self._macos_plist_path()
            if plist_path.exists():
                subprocess.run(
                    ["launchctl", "unload", str(plist_path)],
                    capture_output=True,
                    timeout=10,
                )
                plist_path.unlink()
            logger.info("Auto-start disabled (launchd)")
            return True
        except Exception as exc:
            logger.error("Failed to disable auto-start: %s", exc)
            return False

    # ── Windows: Registry ───────────────────────────────────────────────

    _WIN_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
    _WIN_REG_NAME = "PocketPaw"

    def _windows_is_enabled(self) -> bool:
        try:
            import winreg

            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, self._WIN_REG_KEY, 0, winreg.KEY_READ)
            try:
                winreg.QueryValueEx(key, self._WIN_REG_NAME)
                return True
            except FileNotFoundError:
                return False
            finally:
                winreg.CloseKey(key)
        except Exception:
            return False

    def _windows_enable(self) -> bool:
        try:
            import winreg

            exe = get_executable_path()
            if _is_frozen():
                value = f'"{exe}"'
            else:
                value = f'"{exe}" -m installer.launcher'

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self._WIN_REG_KEY, 0, winreg.KEY_SET_VALUE
            )
            try:
                winreg.SetValueEx(key, self._WIN_REG_NAME, 0, winreg.REG_SZ, value)
            finally:
                winreg.CloseKey(key)

            logger.info("Auto-start enabled (registry): %s", value)
            return True
        except Exception as exc:
            logger.error("Failed to enable auto-start: %s", exc)
            return False

    def _windows_disable(self) -> bool:
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER, self._WIN_REG_KEY, 0, winreg.KEY_SET_VALUE
            )
            try:
                winreg.DeleteValue(key, self._WIN_REG_NAME)
            except FileNotFoundError:
                pass
            finally:
                winreg.CloseKey(key)

            logger.info("Auto-start disabled (registry)")
            return True
        except Exception as exc:
            logger.error("Failed to disable auto-start: %s", exc)
            return False

    # ── Linux: .desktop file ───────────────────────────────────────────

    def _linux_desktop_path(self) -> Path:
        xdg_config = Path.home() / ".config"
        return xdg_config / "autostart" / "pocketpaw.desktop"

    def _linux_desktop_content(self) -> str:
        exe = get_executable_path()
        if _is_frozen():
            exec_line = exe
        else:
            exec_line = f"{exe} -m installer.launcher"

        return (
            "[Desktop Entry]\n"
            f"Name={APP_NAME}\n"
            f"Exec={exec_line}\n"
            "Type=Application\n"
            "X-GNOME-Autostart-enabled=true\n"
            "Comment=PocketPaw AI Agent Launcher\n"
            "Terminal=false\n"
        )

    def _linux_is_enabled(self) -> bool:
        return self._linux_desktop_path().exists()

    def _linux_enable(self) -> bool:
        try:
            desktop_path = self._linux_desktop_path()
            desktop_path.parent.mkdir(parents=True, exist_ok=True)
            desktop_path.write_text(self._linux_desktop_content(), encoding="utf-8")
            logger.info("Auto-start enabled (desktop file): %s", desktop_path)
            return True
        except Exception as exc:
            logger.error("Failed to enable auto-start: %s", exc)
            return False

    def _linux_disable(self) -> bool:
        try:
            desktop_path = self._linux_desktop_path()
            if desktop_path.exists():
                desktop_path.unlink()
            logger.info("Auto-start disabled (desktop file)")
            return True
        except Exception as exc:
            logger.error("Failed to disable auto-start: %s", exc)
            return False
