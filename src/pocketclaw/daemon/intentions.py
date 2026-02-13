"""
IntentionStore - JSON persistence for user-defined intentions.

Intentions define proactive behaviors: what to do (prompt),
when to do it (trigger), and what context to gather.
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def get_intentions_path() -> Path:
    """Get path to intentions JSON file."""
    config_dir = Path.home() / ".pocketclaw"
    config_dir.mkdir(exist_ok=True)
    return config_dir / "intentions.json"


def load_intentions() -> list[dict]:
    """Load intentions from JSON file."""
    path = get_intentions_path()
    if not path.exists():
        return []

    try:
        with open(path) as f:
            data = json.load(f)
            return data.get("intentions", [])
    except (OSError, json.JSONDecodeError) as e:
        logger.error(f"Failed to load intentions: {e}")
        return []


def save_intentions(intentions: list[dict]) -> None:
    """Save intentions to JSON file."""
    path = get_intentions_path()
    data = {"intentions": intentions, "updated_at": datetime.now(tz=UTC).isoformat()}

    try:
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
    except OSError as e:
        logger.error(f"Failed to save intentions: {e}")


class IntentionStore:
    """
    Manages CRUD operations for intentions.

    Intention schema:
    {
        "id": "uuid",
        "name": "Morning Standup",
        "prompt": "Good morning! What are your top 3 priorities today?",
        "trigger": {"type": "cron", "schedule": "0 8 * * 1-5"},
        "context_sources": ["system_status"],
        "enabled": true,
        "created_at": "ISO timestamp",
        "last_run": "ISO timestamp or null"
    }
    """

    def __init__(self):
        self.intentions: list[dict] = []
        self._load()

    def _load(self) -> None:
        """Load intentions from disk."""
        self.intentions = load_intentions()
        logger.info(f"Loaded {len(self.intentions)} intentions")

    def _save(self) -> None:
        """Persist intentions to disk."""
        save_intentions(self.intentions)

    def get_all(self) -> list[dict]:
        """Get all intentions."""
        return self.intentions.copy()

    def get_enabled(self) -> list[dict]:
        """Get all enabled intentions."""
        return [i for i in self.intentions if i.get("enabled", True)]

    def get_by_id(self, intention_id: str) -> dict | None:
        """Get intention by ID."""
        for intention in self.intentions:
            if intention["id"] == intention_id:
                return intention
        return None

    def create(
        self,
        name: str,
        prompt: str,
        trigger: dict,
        context_sources: list[str] | None = None,
        enabled: bool = True,
    ) -> dict:
        """
        Create a new intention.

        Args:
            name: Human-readable name for the intention
            prompt: The prompt to send to the agent (can include {{variables}})
            trigger: Trigger configuration (e.g., {"type": "cron", "schedule": "0 8 * * *"})
            context_sources: List of context sources to gather (e.g., ["system_status"])
            enabled: Whether the intention is active

        Returns:
            The created intention dict
        """
        intention = {
            "id": str(uuid.uuid4()),
            "name": name,
            "prompt": prompt,
            "trigger": trigger,
            "context_sources": context_sources or [],
            "enabled": enabled,
            "created_at": datetime.now(tz=UTC).isoformat(),
            "last_run": None,
        }

        self.intentions.append(intention)
        self._save()

        logger.info(f"Created intention: {name} ({intention['id']})")
        return intention

    def update(self, intention_id: str, updates: dict) -> dict | None:
        """
        Update an existing intention.

        Args:
            intention_id: ID of the intention to update
            updates: Dict of fields to update

        Returns:
            Updated intention or None if not found
        """
        for i, intention in enumerate(self.intentions):
            if intention["id"] == intention_id:
                # Prevent updating id and created_at
                updates.pop("id", None)
                updates.pop("created_at", None)

                self.intentions[i] = {**intention, **updates}
                self._save()

                logger.info(f"Updated intention: {intention_id}")
                return self.intentions[i]

        return None

    def delete(self, intention_id: str) -> bool:
        """
        Delete an intention.

        Args:
            intention_id: ID of the intention to delete

        Returns:
            True if deleted, False if not found
        """
        for i, intention in enumerate(self.intentions):
            if intention["id"] == intention_id:
                deleted = self.intentions.pop(i)
                self._save()

                logger.info(f"Deleted intention: {deleted['name']} ({intention_id})")
                return True

        return False

    def toggle(self, intention_id: str) -> dict | None:
        """
        Toggle enabled state of an intention.

        Args:
            intention_id: ID of the intention to toggle

        Returns:
            Updated intention or None if not found
        """
        intention = self.get_by_id(intention_id)
        if intention:
            return self.update(intention_id, {"enabled": not intention.get("enabled", True)})
        return None

    def mark_run(self, intention_id: str) -> None:
        """Update last_run timestamp for an intention."""
        self.update(intention_id, {"last_run": datetime.now(tz=UTC).isoformat()})

    def reload(self) -> None:
        """Reload intentions from disk."""
        self._load()


# Singleton pattern
_intention_store: IntentionStore | None = None


def get_intention_store() -> IntentionStore:
    """Get singleton IntentionStore instance."""
    global _intention_store
    if _intention_store is None:
        _intention_store = IntentionStore()
    return _intention_store
