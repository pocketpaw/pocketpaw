# PocketPaw Desktop Launcher — Uninstaller
# Provides selective removal of PocketPaw components.
# Created: 2026-02-11

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

from installer.launcher.common import POCKETCLAW_HOME

logger = logging.getLogger(__name__)


@dataclass
class Component:
    """A removable component."""

    name: str
    description: str
    path: Path | None
    exists: bool


class Uninstaller:
    """Manages selective uninstallation of PocketPaw components."""

    def get_components(self) -> list[Component]:
        """List all removable components with their current status."""
        components = [
            Component(
                name="venv",
                description="Virtual environment (~/.pocketclaw/venv/)",
                path=POCKETCLAW_HOME / "venv",
                exists=(POCKETCLAW_HOME / "venv").exists(),
            ),
            Component(
                name="uv",
                description="uv package manager (~/.pocketclaw/uv/)",
                path=POCKETCLAW_HOME / "uv",
                exists=(POCKETCLAW_HOME / "uv").exists(),
            ),
            Component(
                name="python",
                description="Embedded Python (~/.pocketclaw/python/)",
                path=POCKETCLAW_HOME / "python",
                exists=(POCKETCLAW_HOME / "python").exists(),
            ),
            Component(
                name="logs",
                description="Log files (~/.pocketclaw/logs/)",
                path=POCKETCLAW_HOME / "logs",
                exists=(POCKETCLAW_HOME / "logs").exists(),
            ),
            Component(
                name="config",
                description="Configuration (~/.pocketclaw/config.json)",
                path=POCKETCLAW_HOME / "config.json",
                exists=(POCKETCLAW_HOME / "config.json").exists(),
            ),
            Component(
                name="memory",
                description="Memory & conversation history (~/.pocketclaw/memory/)",
                path=POCKETCLAW_HOME / "memory",
                exists=(POCKETCLAW_HOME / "memory").exists(),
            ),
            Component(
                name="audit",
                description="Audit log (~/.pocketclaw/audit.jsonl)",
                path=POCKETCLAW_HOME / "audit.jsonl",
                exists=(POCKETCLAW_HOME / "audit.jsonl").exists(),
            ),
            Component(
                name="pid",
                description="PID file (~/.pocketclaw/launcher.pid)",
                path=POCKETCLAW_HOME / "launcher.pid",
                exists=(POCKETCLAW_HOME / "launcher.pid").exists(),
            ),
        ]
        return components

    def uninstall(
        self,
        remove_venv: bool = True,
        remove_uv: bool = True,
        remove_python: bool = True,
        remove_logs: bool = True,
        remove_config: bool = False,
        remove_memory: bool = False,
        remove_audit: bool = False,
    ) -> list[str]:
        """Remove selected components.

        Returns:
            List of removal result messages.
        """
        results: list[str] = []

        # Remove auto-start entries first
        try:
            from .autostart import AutoStartManager

            mgr = AutoStartManager()
            if mgr.is_enabled():
                mgr.disable()
                results.append("Removed auto-start entry")
        except Exception:
            pass

        # Map flags to component names
        removals = {
            "venv": remove_venv,
            "uv": remove_uv,
            "python": remove_python,
            "logs": remove_logs,
            "config": remove_config,
            "memory": remove_memory,
            "audit": remove_audit,
        }

        # Always remove PID file
        removals["pid"] = True

        components = {c.name: c for c in self.get_components()}

        for name, should_remove in removals.items():
            if not should_remove:
                continue

            comp = components.get(name)
            if not comp or not comp.path:
                continue

            if not comp.path.exists():
                results.append(f"Skipped {comp.description} (not found)")
                continue

            try:
                if comp.path.is_dir():
                    shutil.rmtree(comp.path)
                else:
                    comp.path.unlink()
                results.append(f"Removed {comp.description}")
                logger.info("Removed: %s", comp.path)
            except Exception as exc:
                results.append(f"Failed to remove {comp.description}: {exc}")
                logger.error("Failed to remove %s: %s", comp.path, exc)

        return results

    def interactive_uninstall(self) -> None:
        """Console-mode interactive uninstall with prompts."""
        print("\n  PocketPaw Uninstaller")
        print("  " + "=" * 40)

        components = self.get_components()
        existing = [c for c in components if c.exists]

        if not existing:
            print("  Nothing to remove — PocketPaw is not installed.")
            return

        print("\n  Found components:\n")
        for c in existing:
            print(f"    - {c.description}")

        print()

        # Ask about safe removals
        remove_venv = _confirm("Remove virtual environment (reinstallable)?", default=True)
        remove_uv = _confirm("Remove uv package manager?", default=True)
        remove_python = _confirm("Remove embedded Python?", default=True)
        remove_logs = _confirm("Remove log files?", default=True)

        # Ask about data removals (default no)
        remove_config = _confirm("Remove configuration?", default=False)
        remove_memory = _confirm("Remove memory & conversation history?", default=False)
        remove_audit = _confirm("Remove audit log?", default=False)

        print()
        results = self.uninstall(
            remove_venv=remove_venv,
            remove_uv=remove_uv,
            remove_python=remove_python,
            remove_logs=remove_logs,
            remove_config=remove_config,
            remove_memory=remove_memory,
            remove_audit=remove_audit,
        )

        print("\n  Results:\n")
        for r in results:
            print(f"    {r}")
        print()


def _confirm(prompt: str, default: bool = False) -> bool:
    """Ask a yes/no question."""
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(f"  {prompt}{suffix}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return default

    if not answer:
        return default
    return answer in ("y", "yes")
