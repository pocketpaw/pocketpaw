# PocketPaw Desktop Launcher — Entry Point
# Double-click this (packaged as .exe/.app) to:
#   1. First run: show splash, bootstrap Python/venv, install pocketpaw
#   2. Every run: start server, open browser, show system tray icon
# Created: 2026-02-10

from __future__ import annotations

import argparse
import importlib
import logging
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .bootstrap import Bootstrap
    from .server import ServerManager

# ---------------------------------------------------------------------------
# PyInstaller frozen-exe fixup: when PyInstaller runs __main__.py as a script,
# __package__ is None, so relative imports (from .bootstrap …) fail.  We fix
# this by registering the launcher directory as the "launcher" package and
# setting __package__ so Python resolves the dot-imports correctly.
# ---------------------------------------------------------------------------
if __package__ is None or __package__ == "":
    # Determine the directory that contains this __main__.py
    _this_dir = Path(__file__).resolve().parent

    # Make sure the *parent* of that directory is on sys.path so that
    # "import launcher" finds the right folder.
    _parent_dir = str(_this_dir.parent)
    if _parent_dir not in sys.path:
        sys.path.insert(0, _parent_dir)

    # Register the package so relative imports work
    __package__ = "launcher"
    try:
        importlib.import_module("launcher")
    except ImportError:
        # If there's no __init__.py reachable, create a virtual package
        import types

        pkg = types.ModuleType("launcher")
        pkg.__path__ = [str(_this_dir)]
        pkg.__package__ = "launcher"
        sys.modules["launcher"] = pkg

# Set up logging before imports
from installer.launcher.common import POCKETCLAW_HOME

LOG_DIR = POCKETCLAW_HOME / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "launcher.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("pocketpaw.launcher")


def main() -> int:
    """Main entry point for the desktop launcher."""
    parser = argparse.ArgumentParser(description="PocketPaw Desktop Launcher")
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Don't open the browser automatically",
    )
    parser.add_argument(
        "--no-tray",
        action="store_true",
        help="Don't show the system tray icon",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override the web dashboard port",
    )
    parser.add_argument(
        "--extras",
        default="recommended",
        help="Comma-separated pip extras for first install (default: recommended)",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Force reinstall (delete venv and start fresh)",
    )
    parser.add_argument(
        "--autostart",
        action="store_true",
        default=None,
        help="Enable start-on-login and exit",
    )
    parser.add_argument(
        "--no-autostart",
        action="store_true",
        default=None,
        help="Disable start-on-login and exit",
    )
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Run interactive uninstaller and exit",
    )
    # Dev/testing flags — install from git or local source instead of PyPI
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Install from the 'dev' branch (shortcut for --branch dev)",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Install from a specific git branch (e.g. --branch dev)",
    )
    parser.add_argument(
        "--local",
        default=None,
        metavar="PATH",
        help="Install from a local directory in editable mode (e.g. --local /path/to/pocketpaw)",
    )
    args = parser.parse_args()

    # Resolve --dev as shortcut for --branch dev
    if args.dev and not args.branch:
        args.branch = "dev"

    logger.info("PocketPaw Desktop Launcher starting")

    # Handle --autostart / --no-autostart (exit immediately after)
    if args.autostart or args.no_autostart:
        from .autostart import AutoStartManager

        mgr = AutoStartManager()
        if args.autostart:
            ok = mgr.enable()
            print("Auto-start enabled." if ok else "Failed to enable auto-start.")
        else:
            ok = mgr.disable()
            print("Auto-start disabled." if ok else "Failed to disable auto-start.")
        return 0 if ok else 1

    # Handle --uninstall (exit immediately after)
    if args.uninstall:
        from .uninstall import Uninstaller

        Uninstaller().interactive_uninstall()
        return 0

    # Import our modules
    from .bootstrap import Bootstrap
    from .common import VENV_DIR
    from .server import ServerManager
    from .updater import Updater

    # Reset if requested
    if args.reset:
        import shutil
        import os
        import stat

        if VENV_DIR.exists():
            logger.info("Resetting: removing venv at %s", VENV_DIR)

            # Windows-safe delete: handle file locks and permission errors
            def handle_remove_error(func, path, exc_info):
                """Error handler for shutil.rmtree to handle Windows file locks."""
                # Make the file writable and try again
                try:
                    os.chmod(path, stat.S_IWRITE)
                    func(path)
                except Exception as e:
                    logger.debug("Retry failed for %s: %s", path, e)

            # Try to delete with retries
            max_attempts = 3
            for attempt in range(max_attempts):
                try:
                    shutil.rmtree(VENV_DIR, onerror=handle_remove_error)
                    if not VENV_DIR.exists():
                        logger.info("Venv successfully removed")
                        break
                except Exception as e:
                    logger.warning("Attempt %d/%d failed: %s", attempt + 1, max_attempts, e)

                if attempt < max_attempts - 1:
                    logger.info("Retrying in 2 seconds...")
                    time.sleep(2)

            # If venv still exists, rename it and create a new one
            if VENV_DIR.exists():
                backup_dir = VENV_DIR.parent / f"{VENV_DIR.name}.old.{int(time.time())}"
                logger.warning("Could not delete venv (files may be locked)")
                logger.info("Renaming %s -> %s", VENV_DIR, backup_dir.name)
                try:
                    VENV_DIR.rename(backup_dir)
                    logger.info("Old venv moved to %s — you can delete it later", backup_dir)
                except Exception as e:
                    logger.error("Failed to rename venv: %s", e)
                    logger.error("Please manually delete or rename: %s", VENV_DIR)
                    logger.error("Then run the launcher again")
                    return 1

    # Check if first run (no venv / pocketpaw not installed)
    bootstrap = Bootstrap()
    status = bootstrap.check_status()

    if status.needs_install or args.branch or args.local:
        reason = "First run" if status.needs_install else "Dev/branch install"
        logger.info("%s detected — starting bootstrap", reason)
        success = _first_run_install(
            bootstrap,
            extras=args.extras.split(",") if args.extras else ["recommended"],
            branch=args.branch,
            local_path=args.local,
        )
        if not success:
            logger.error("Bootstrap failed")
            return 1

    # Start the server
    server = ServerManager(port=args.port)
    updater = Updater()

    if not server.start():
        logger.error("Failed to start server")
        return 1

    # Open browser
    if not args.no_browser:
        time.sleep(1.5)
        url = server.get_dashboard_url()
        logger.info("Opening browser: %s", url)
        webbrowser.open(url)

    # Show system tray (blocks until quit)
    if not args.no_tray:
        try:
            from .tray import HAS_TRAY, TrayIcon

            if HAS_TRAY:
                tray = TrayIcon(server=server, updater=updater)
                tray.run()  # Blocks
            else:
                logger.warning("Tray not available, running headless")
                _run_headless(server)
        except ImportError:
            logger.warning("pystray not installed, running headless")
            _run_headless(server)
    else:
        _run_headless(server)

    return 0


def _first_run_install(
    bootstrap: Bootstrap,
    extras: list[str],
    branch: str | None = None,
    local_path: str | None = None,
) -> bool:
    """Run the first-time installation with a splash screen."""
    # Try to use the tkinter splash window
    try:
        from .splash import SplashWindow

        splash = SplashWindow()

        # Patch bootstrap to use splash's progress callback
        def run_with_splash(progress_cb):
            bootstrap.progress = progress_cb
            result = bootstrap.run(extras=extras, branch=branch, local_path=local_path)
            if result.error:
                raise RuntimeError(result.error)

        success = splash.run(run_with_splash)
        return success

    except Exception as exc:
        # Fallback: no GUI, just run in console
        logger.warning("Splash window failed (%s), falling back to console", exc)

        def console_progress(msg: str, pct: int) -> None:
            print(f"  [{pct:3d}%] {msg}")

        bootstrap.progress = console_progress
        result = bootstrap.run(extras=extras, branch=branch, local_path=local_path)
        if result.error:
            print(f"\n  Error: {result.error}\n")
            return False
        return True


def _run_headless(server: ServerManager) -> None:
    """Run without a tray icon. Block until Ctrl+C."""
    import signal

    print(f"\n  PocketPaw running at {server.get_dashboard_url()}")
    print("  Press Ctrl+C to stop.\n")

    # Handle Ctrl+C
    stop_event = threading.Event()

    def on_signal(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    stop_event.wait()
    server.stop()


if __name__ == "__main__":
    sys.exit(main())
