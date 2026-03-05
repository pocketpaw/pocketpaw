# Paw configuration — reads paw.yaml from project root.
# Created: 2026-03-02
# Supports env vars PAW_PROVIDER, PAW_SOUL_PATH for overrides.

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class PawConfig:
    """Configuration for a paw instance in a project directory."""

    project_root: Path
    soul_name: str = "Paw"
    soul_path: Path | None = None
    provider: str = "claude"  # claude, openai, ollama, none

    @classmethod
    def load(cls, project_root: Path | None = None) -> PawConfig:
        """Load config from paw.yaml in project_root, with env var overrides."""
        root = project_root or Path.cwd()
        config_file = root / "paw.yaml"

        data: dict[str, Any] = {}
        if config_file.exists():
            data = _load_yaml(config_file)

        # Env var overrides
        provider = os.environ.get("PAW_PROVIDER", data.get("provider", "claude"))
        soul_path_str = os.environ.get("PAW_SOUL_PATH", data.get("soul_path"))
        soul_path = Path(soul_path_str) if soul_path_str else None
        soul_name = data.get("soul_name", data.get("name", "Paw"))

        return cls(
            project_root=root,
            soul_name=soul_name,
            soul_path=soul_path,
            provider=provider,
        )

    @property
    def default_soul_path(self) -> Path:
        """Default location for the .soul file in the project."""
        return self.project_root / ".paw" / f"{self.soul_name.lower()}.soul"

    @property
    def paw_dir(self) -> Path:
        """The .paw directory in the project root."""
        return self.project_root / ".paw"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load YAML file, falling back to basic parsing if PyYAML unavailable."""
    try:
        import yaml

        with open(path) as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        # Minimal key: value parser for simple configs
        result: dict[str, Any] = {}
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    key, _, value = line.partition(":")
                    value = value.strip().strip("\"'")
                    if value.lower() in ("null", "~", ""):
                        result[key.strip()] = None
                    else:
                        result[key.strip()] = value
        return result
