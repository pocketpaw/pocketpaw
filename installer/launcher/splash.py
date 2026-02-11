# PocketPaw Desktop Launcher — Splash / Progress Window
# Lightweight tkinter window for first-run install progress.
# No external dependencies — tkinter ships with Python.
# Created: 2026-02-10

from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import ttk


class SplashWindow:
    """First-run progress window using tkinter.

    Shows a progress bar and status messages while bootstrapping.
    Runs the install in a background thread so the UI stays responsive.
    """

    WINDOW_WIDTH = 420
    WINDOW_HEIGHT = 220

    def __init__(self) -> None:
        self._root: tk.Tk | None = None
        self._progress_var: tk.IntVar | None = None
        self._status_var: tk.StringVar | None = None
        self._done = False
        self._error: str | None = None

    def run(self, install_fn: Callable[[Callable[[str, int], None]], None]) -> bool:
        """Show the splash and run the install function in a background thread.

        Args:
            install_fn: Function that takes a progress callback(message, percent)
                       and performs the installation.

        Returns:
            True if install completed without error.
        """
        self._root = tk.Tk()
        self._root.title("PocketPaw Setup")
        self._root.resizable(False, False)

        # Center on screen
        screen_w = self._root.winfo_screenwidth()
        screen_h = self._root.winfo_screenheight()
        x = (screen_w - self.WINDOW_WIDTH) // 2
        y = (screen_h - self.WINDOW_HEIGHT) // 2
        self._root.geometry(f"{self.WINDOW_WIDTH}x{self.WINDOW_HEIGHT}+{x}+{y}")

        # Try to set icon
        icon_path = Path(__file__).parent / "assets" / "icon.png"
        if icon_path.exists():
            try:
                icon = tk.PhotoImage(file=str(icon_path))
                self._root.iconphoto(True, icon)
            except Exception:
                pass

        # Dark-ish theme
        self._root.configure(bg="#1a1a2e")

        # Title
        title_label = tk.Label(
            self._root,
            text="PocketPaw",
            font=("Helvetica", 20, "bold"),
            fg="#e0e0ff",
            bg="#1a1a2e",
        )
        title_label.pack(pady=(25, 5))

        subtitle = tk.Label(
            self._root,
            text="Setting up your AI agent...",
            font=("Helvetica", 11),
            fg="#8888aa",
            bg="#1a1a2e",
        )
        subtitle.pack(pady=(0, 20))

        # Progress bar
        style = ttk.Style()
        style.theme_use("default")
        style.configure(
            "Custom.Horizontal.TProgressbar",
            troughcolor="#2a2a4e",
            background="#6c63ff",
            thickness=8,
        )

        self._progress_var = tk.IntVar(value=0)
        progress_bar = ttk.Progressbar(
            self._root,
            variable=self._progress_var,
            maximum=100,
            length=350,
            style="Custom.Horizontal.TProgressbar",
        )
        progress_bar.pack(pady=(0, 15))

        # Status text
        self._status_var = tk.StringVar(value="Initializing...")
        status_label = tk.Label(
            self._root,
            textvariable=self._status_var,
            font=("Helvetica", 10),
            fg="#aaaacc",
            bg="#1a1a2e",
        )
        status_label.pack()

        # Start the install in a background thread
        thread = threading.Thread(
            target=self._run_install,
            args=(install_fn,),
            daemon=True,
        )
        thread.start()

        # Poll for completion
        self._root.after(100, self._check_done)

        # Handle window close
        self._root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._root.mainloop()
        return self._error is None

    def update_progress(self, message: str, percent: int) -> None:
        """Thread-safe progress update. Call from the install thread."""
        if self._root:
            self._root.after(0, self._do_update, message, percent)

    # ── Internal ───────────────────────────────────────────────────────

    def _do_update(self, message: str, percent: int) -> None:
        """Update UI on the main thread."""
        if self._progress_var:
            self._progress_var.set(min(percent, 100))
        if self._status_var:
            self._status_var.set(message)

    def _run_install(self, install_fn: Callable[[Callable[[str, int], None]], None]) -> None:
        """Run the install function in a background thread."""
        try:
            install_fn(self.update_progress)
        except Exception as exc:
            self._error = str(exc)
        self._done = True

    def _check_done(self) -> None:
        """Poll whether the install thread finished."""
        if self._done:
            if self._error:
                self._show_error()
            else:
                # Brief pause to show 100% before closing
                self._root.after(800, self._close)
        else:
            self._root.after(200, self._check_done)

    def _show_error(self) -> None:
        """Show the error and a close button."""
        if self._status_var:
            self._status_var.set(f"Error: {self._error}")

        close_btn = tk.Button(
            self._root,
            text="Close",
            command=self._close,
            bg="#ff4444",
            fg="white",
            font=("Helvetica", 10),
            relief="flat",
            padx=20,
            pady=5,
        )
        close_btn.pack(pady=10)

    def _close(self) -> None:
        """Close the window."""
        if self._root:
            self._root.destroy()
            self._root = None

    def _on_close(self) -> None:
        """Handle window close button."""
        self._close()
