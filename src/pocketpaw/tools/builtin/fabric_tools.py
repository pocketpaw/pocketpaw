# Fabric tools — agent tools for querying and managing the ontology.
# Created: 2026-03-28 — Lets the agent create objects, query links, reason across data.

import logging
from typing import Any

from pocketpaw.tools.protocol import BaseTool

logger = logging.getLogger(__name__)


def _get_fabric_store():
    """Lazy import to avoid circular deps and missing ee/ module."""
    try:
        from ee.api import get_fabric_store

        return get_fabric_store()
    except ImportError:
        return None


async def _emit_trace_events(event_type: str, entries: list[dict[str, Any]]) -> None:
    """Publish one SystemEvent per entry so TraceCollector can aggregate them.

    Silent in the common case — the message bus only has subscribers when a
    proposal is actively being traced. Any failure is swallowed so tool calls
    never break because telemetry is sick.
    """
    if not entries:
        return
    try:
        from pocketpaw.bus import get_message_bus
        from pocketpaw.bus.events import SystemEvent

        bus = get_message_bus()
        for entry in entries:
            await bus.publish_system(SystemEvent(event_type=event_type, data=entry))
    except Exception:
        logger.debug("Trace event emission skipped (event_type=%s)", event_type)


class FabricQueryTool(BaseTool):
    """Query objects in the Fabric ontology."""

    @property
    def name(self) -> str:
        return "fabric_query"

    @property
    def description(self) -> str:
        return (
            "Query the Fabric ontology to find business objects and their relationships. "
            "Search by object type (e.g., 'Customer', 'Order', 'Inventory'), filter by properties, "
            "or traverse links between objects. Returns matching objects with their properties."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "type_name": {
                    "type": "string",
                    "description": "Object type to search (e.g., 'Customer', 'Order')",
                },
                "linked_to": {
                    "type": "string",
                    "description": "Find objects linked to this object ID",
                },
                "link_type": {
                    "type": "string",
                    "description": "Filter links by type (e.g., 'has_order', 'belongs_to')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default: 20)",
                    "default": 20,
                },
            },
        }

    async def execute(
        self,
        type_name: str | None = None,
        linked_to: str | None = None,
        link_type: str | None = None,
        limit: int = 20,
    ) -> str:
        store = _get_fabric_store()
        if not store:
            return "Fabric is not available (enterprise feature)."

        try:
            from ee.fabric.models import FabricQuery

            result = await store.query(
                FabricQuery(
                    type_name=type_name,
                    linked_to=linked_to,
                    link_type=link_type,
                    limit=min(limit, 50),
                )
            )

            # Emit a trace event per object so decision-time snapshots can
            # capture what this query actually returned. The collector is only
            # active when an InstinctProposeTool wraps the reasoning, so this
            # is a no-op in all other contexts.
            await _emit_trace_events(
                "fabric_query",
                [{"object_id": obj.id, "object_type": obj.type_name} for obj in result.objects],
            )

            if not result.objects:
                query_desc = type_name or f"linked to {linked_to}" or "all"
                return f"No objects found matching: {query_desc}"

            lines = [f"Found {result.total} object(s):\n"]
            for obj in result.objects:
                props_str = ", ".join(f"{k}: {v}" for k, v in obj.properties.items())
                lines.append(f"  [{obj.type_name}] {obj.id} — {props_str}")
                if obj.source_connector:
                    lines.append(f"    Source: {obj.source_connector} ({obj.source_id})")

            return "\n".join(lines)
        except Exception as e:
            logger.error("fabric_query failed: %s", e)
            return f"Error querying Fabric: {e}"


class FabricCreateTool(BaseTool):
    """Create objects and links in the Fabric ontology."""

    @property
    def name(self) -> str:
        return "fabric_create"

    @property
    def description(self) -> str:
        return (
            "Create a new business object in the Fabric ontology, or define a new object type. "
            "Use this when data from connectors needs to be stored as structured objects "
            "(e.g., creating Customer, Order, or Inventory objects with typed properties)."
        )

    @property
    def trust_level(self) -> str:
        return "medium"

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["define_type", "create_object", "link"],
                    "description": (
                        "What to create: define_type (new object type),"
                        " create_object (new instance),"
                        " link (connect two objects)"
                    ),
                },
                "type_name": {
                    "type": "string",
                    "description": (
                        "For define_type: the type name."
                        " For create_object: which type"
                        " to instantiate."
                    ),
                },
                "properties": {
                    "type": "object",
                    "description": (
                        "For define_type: property definitions. For create_object: property values."
                    ),
                },
                "from_id": {
                    "type": "string",
                    "description": "For link: source object ID",
                },
                "to_id": {
                    "type": "string",
                    "description": "For link: target object ID",
                },
                "link_type": {
                    "type": "string",
                    "description": "For link: relationship type (e.g., 'has_order', 'belongs_to')",
                },
                "source_connector": {
                    "type": "string",
                    "description": "For create_object: which connector provided this data",
                },
                "source_id": {
                    "type": "string",
                    "description": "For create_object: original ID in the source system",
                },
            },
            "required": ["action"],
        }

    async def execute(
        self,
        action: str,
        type_name: str | None = None,
        properties: dict[str, Any] | None = None,
        from_id: str | None = None,
        to_id: str | None = None,
        link_type: str | None = None,
        source_connector: str | None = None,
        source_id: str | None = None,
    ) -> str:
        store = _get_fabric_store()
        if not store:
            return "Fabric is not available (enterprise feature)."

        try:
            if action == "define_type":
                if not type_name:
                    return "type_name is required for define_type"
                from ee.fabric.models import PropertyDef

                prop_defs = []
                if properties:
                    for name, ptype in properties.items():
                        if isinstance(ptype, dict):
                            prop_defs.append(PropertyDef(**ptype))
                        else:
                            prop_defs.append(PropertyDef(name=name, type=str(ptype)))
                obj_type = await store.define_type(name=type_name, properties=prop_defs)
                return (
                    f"Created object type '{obj_type.name}'"
                    f" (ID: {obj_type.id})"
                    f" with {len(prop_defs)} properties."
                )

            elif action == "create_object":
                if not type_name:
                    return "type_name is required for create_object"
                obj_type = await store.get_type_by_name(type_name)
                if not obj_type:
                    return (
                        f"Object type '{type_name}' not found."
                        " Define it first with action='define_type'."
                    )
                obj = await store.create_object(
                    type_id=obj_type.id,
                    properties=properties or {},
                    source_connector=source_connector,
                    source_id=source_id,
                )
                props_str = ", ".join(f"{k}: {v}" for k, v in obj.properties.items())
                return f"Created {type_name} object (ID: {obj.id}): {props_str}"

            elif action == "link":
                if not from_id or not to_id or not link_type:
                    return "from_id, to_id, and link_type are all required for link"
                lnk = await store.link(from_id, to_id, link_type)
                return f"Linked {from_id} → {to_id} (type: {link_type}, link ID: {lnk.id})"

            else:
                return f"Unknown action: {action}. Use define_type, create_object, or link."

        except Exception as e:
            logger.error("fabric_create failed: %s", e)
            return f"Error: {e}"


class FabricStatsTool(BaseTool):
    """Get Fabric ontology statistics."""

    @property
    def name(self) -> str:
        return "fabric_stats"

    @property
    def description(self) -> str:
        return (
            "Get statistics about the Fabric ontology: number of object types, objects, and links."
        )

    @property
    def trust_level(self) -> str:
        return "high"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self) -> str:
        store = _get_fabric_store()
        if not store:
            return "Fabric is not available (enterprise feature)."
        try:
            stats = await store.stats()
            types = await store.list_types()
            lines = [
                f"Fabric: {stats['types']} types,"
                f" {stats['objects']} objects,"
                f" {stats['links']} links"
            ]
            if types:
                lines.append("Types: " + ", ".join(t.name for t in types))
            return "\n".join(lines)
        except Exception as e:
            return f"Error: {e}"
