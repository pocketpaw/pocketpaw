"""YAML loader for mounts.yaml — sorted + validated."""

from __future__ import annotations

from pathlib import Path

import yaml

from pocketpaw_ee.cloud.files.dto import MountConfig

_DEFAULT_PATH = Path(__file__).parent / "mounts.yaml"


def load_mounts(path: Path | None = None) -> list[MountConfig]:
    src = path or _DEFAULT_PATH
    raw = yaml.safe_load(src.read_text()) or []
    configs = [MountConfig(**row) for row in raw]
    configs.sort(key=lambda c: c.order)
    return configs


def resolve_template(template: str, variables: dict[str, str]) -> str:
    return template.format(**variables)
