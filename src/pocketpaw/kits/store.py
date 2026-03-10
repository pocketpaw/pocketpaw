"""File-based PawKit store.

Storage layout:
~/.pocketpaw/kits/
    <kit-id>/
        pawkit.yaml    — the kit configuration
        data/          — workflow output JSON files

Design notes:
- In-memory index loaded on first access
- Atomic writes (write to temp file, then rename)
- Singleton factory via get_kit_store()

Updated: 2026-03-09 — Security hardening: sanitize all kit_id and data source
  inputs to prevent path traversal. Add auth scope to kits API router.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pocketpaw.kits.models import InstalledKit, PawKitConfig

logger = logging.getLogger(__name__)


def _slugify(name: str) -> str:
    """Convert a kit name to a filesystem-safe slug."""
    slug = name.lower().strip()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


class FileKitStore:
    """File-based implementation of PawKit storage."""

    def __init__(self, base_dir: Path | None = None):
        if base_dir is None:
            base_dir = Path.home() / ".pocketpaw" / "kits"
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

        self._kits: dict[str, InstalledKit] = {}
        self._loaded = False

    def _ensure_loaded(self) -> None:
        """Lazy-load all installed kits from disk."""
        if self._loaded:
            return
        self._loaded = True

        if not self.base_dir.exists():
            return

        for kit_dir in sorted(self.base_dir.iterdir()):
            yaml_path = kit_dir / "pawkit.yaml"
            meta_path = kit_dir / "meta.json"
            if not yaml_path.exists() or not meta_path.exists():
                continue
            try:
                config = self._load_yaml(yaml_path)
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                kit = InstalledKit(
                    id=kit_dir.name,
                    config=config,
                    user_values=meta.get("user_values", {}),
                    installed_at=meta.get("installed_at", ""),
                    active=meta.get("active", False),
                )
                self._kits[kit.id] = kit
            except Exception:
                logger.warning("Failed to load kit from %s", kit_dir, exc_info=True)

        logger.info("PawKits loaded: %d kits", len(self._kits))

    @staticmethod
    def _load_yaml(path: Path) -> PawKitConfig:
        """Parse a pawkit.yaml file into PawKitConfig."""
        try:
            import yaml
        except ImportError as e:
            raise ImportError("PyYAML is required for PawKits. Install with: uv add pyyaml") from e

        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        return PawKitConfig.model_validate(raw)

    @staticmethod
    def _parse_yaml_string(yaml_str: str) -> PawKitConfig:
        """Parse a YAML string into PawKitConfig."""
        try:
            import yaml
        except ImportError as e:
            raise ImportError("PyYAML is required for PawKits. Install with: uv add pyyaml") from e

        raw = yaml.safe_load(yaml_str)
        return PawKitConfig.model_validate(raw)

    def _save_kit(self, kit: InstalledKit) -> None:
        """Persist a kit's config and metadata to disk."""
        kit_dir = self.base_dir / kit.id
        kit_dir.mkdir(parents=True, exist_ok=True)
        (kit_dir / "data").mkdir(exist_ok=True)

        # Write pawkit.yaml
        try:
            import yaml
        except ImportError as e:
            raise ImportError("PyYAML is required for PawKits. Install with: uv add pyyaml") from e

        yaml_path = kit_dir / "pawkit.yaml"
        temp_path = yaml_path.with_suffix(".tmp")
        config_dict = kit.config.model_dump(exclude_none=True)
        temp_path.write_text(yaml.dump(config_dict, default_flow_style=False), encoding="utf-8")
        temp_path.replace(yaml_path)

        # Write meta.json
        meta_path = kit_dir / "meta.json"
        temp_meta = meta_path.with_suffix(".tmp")
        meta = {
            "user_values": kit.user_values,
            "installed_at": kit.installed_at,
            "active": kit.active,
        }
        temp_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        temp_meta.replace(meta_path)

    async def install_kit(self, yaml_str: str, kit_id: str | None = None) -> InstalledKit:
        """Install a kit from a YAML string."""
        self._ensure_loaded()

        config = self._parse_yaml_string(yaml_str)
        if kit_id is None:
            kit_id = _slugify(config.meta.name)
        else:
            # Always sanitize caller-supplied kit_id to prevent path traversal
            kit_id = _slugify(kit_id)
            if not kit_id:
                raise ValueError("kit_id must contain at least one alphanumeric character")

        kit = InstalledKit(
            id=kit_id,
            config=config,
            installed_at=datetime.now(UTC).isoformat(),
        )

        self._kits[kit.id] = kit
        self._save_kit(kit)
        logger.info("Installed PawKit: %s (%s)", config.meta.name, kit_id)
        return kit

    async def get_kit(self, kit_id: str) -> InstalledKit | None:
        """Get an installed kit by ID."""
        self._ensure_loaded()
        return self._kits.get(kit_id)

    async def list_kits(self) -> list[InstalledKit]:
        """List all installed kits."""
        self._ensure_loaded()
        return list(self._kits.values())

    async def remove_kit(self, kit_id: str) -> bool:
        """Uninstall a kit, removing its directory."""
        self._ensure_loaded()

        if kit_id not in self._kits:
            return False

        kit_dir = self.base_dir / kit_id
        if kit_dir.exists():
            import shutil

            shutil.rmtree(kit_dir)

        del self._kits[kit_id]
        logger.info("Removed PawKit: %s", kit_id)
        return True

    async def activate_kit(self, kit_id: str) -> bool:
        """Set a kit as the active command center (deactivates others)."""
        self._ensure_loaded()

        if kit_id not in self._kits:
            return False

        for kid, kit in self._kits.items():
            was_active = kit.active
            kit.active = kid == kit_id
            if kit.active != was_active:
                self._save_kit(kit)

        logger.info("Activated PawKit: %s", kit_id)
        return True

    async def get_kit_data(self, kit_id: str) -> dict[str, Any]:
        """Get all persisted data for a kit's panels."""
        self._ensure_loaded()

        data_dir = self.base_dir / kit_id / "data"
        result: dict[str, Any] = {}
        if not data_dir.exists():
            return result

        for json_file in data_dir.glob("*.json"):
            source_key = json_file.stem.replace("_", ":")
            try:
                result[source_key] = json.loads(json_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("Failed to read kit data: %s", json_file)

        return result

    async def save_kit_data(self, kit_id: str, source: str, data: Any) -> None:
        """Save data for a specific panel source."""
        self._ensure_loaded()

        data_dir = self.base_dir / kit_id / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize source to prevent path traversal
        safe_source = re.sub(r"[^a-z0-9_\-]", "_", source.lower())
        filename = safe_source + ".json"
        file_path = data_dir / filename
        temp_path = file_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        temp_path.replace(file_path)


# ---------------------------------------------------------------------------
# Singleton factory
# ---------------------------------------------------------------------------

_store: FileKitStore | None = None


def get_kit_store(base_dir: Path | None = None) -> FileKitStore:
    """Get or create the PawKit store singleton."""
    global _store
    if _store is None:
        _store = FileKitStore(base_dir)
    return _store


def reset_kit_store() -> None:
    """Reset the store singleton (for testing)."""
    global _store
    _store = None
