# MongoDB adapter — document database connector via motor (async pymongo).
# Created: 2026-03-30

from __future__ import annotations

from typing import Any

from pocketpaw.connectors.protocol import (
    ActionResult,
    ActionSchema,
    ConnectionResult,
    ConnectorStatus,
    SyncResult,
    TrustLevel,
)


class MongoDBAdapter:
    """Native MongoDB connector using motor (async pymongo)."""

    def __init__(self) -> None:
        self._client: Any = None
        self._db: Any = None
        self._config: dict[str, str] = {}
        self._connected = False

    @property
    def name(self) -> str:
        return "mongodb"

    @property
    def display_name(self) -> str:
        return "MongoDB"

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        try:
            from motor.motor_asyncio import AsyncIOMotorClient
        except ImportError:
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message="motor is not installed. Run: uv pip install motor",
            )

        self._config = {k: str(v) for k, v in config.items()}

        # Accept either a full URI or individual fields
        uri = self._config.get("MONGO_URI", "")
        if not uri:
            host = self._config.get("MONGO_HOST", "localhost")
            port = self._config.get("MONGO_PORT", "27017")
            user = self._config.get("MONGO_USER", "")
            password = self._config.get("MONGO_PASSWORD", "")

            if user and password:
                from urllib.parse import quote_plus

                uri = f"mongodb://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}"
            else:
                uri = f"mongodb://{host}:{port}"

        db_name = self._config.get("MONGO_DATABASE", "")
        if not db_name:
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message="Missing required credential: MONGO_DATABASE",
            )

        try:
            self._client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=5000)
            # Test connection
            await self._client.admin.command("ping")
            self._db = self._client[db_name]
            self._connected = True
            return ConnectionResult(
                success=True,
                connector_name=self.name,
                status=ConnectorStatus.CONNECTED,
                message=f"Connected to {db_name}",
            )
        except Exception as e:
            self._client = None
            self._db = None
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=f"Connection failed: {e}",
            )

    async def disconnect(self, pocket_id: str) -> bool:
        if self._client:
            self._client.close()
            self._client = None
            self._db = None
        self._connected = False
        return True

    async def actions(self) -> list[ActionSchema]:
        return [
            ActionSchema(
                name="list_collections",
                description="List all collections in the database",
                method="LOCAL",
                parameters={},
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="find",
                description="Query documents in a collection",
                method="LOCAL",
                parameters={
                    "collection": {"type": "string", "required": True},
                    "filter": {
                        "type": "object",
                        "default": {},
                        "description": "MongoDB query filter (JSON)",
                    },
                    "limit": {"type": "integer", "default": 20},
                    "sort": {"type": "object", "description": 'Sort spec, e.g. {"created": -1}'},
                    "projection": {"type": "object", "description": "Fields to include/exclude"},
                },
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="find_one",
                description="Get a single document by filter or _id",
                method="LOCAL",
                parameters={
                    "collection": {"type": "string", "required": True},
                    "filter": {
                        "type": "object",
                        "required": True,
                        "description": 'Query filter or {"_id": "..."}',
                    },
                },
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="count",
                description="Count documents in a collection",
                method="LOCAL",
                parameters={
                    "collection": {"type": "string", "required": True},
                    "filter": {"type": "object", "default": {}},
                },
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="distinct",
                description="Get distinct values for a field",
                method="LOCAL",
                parameters={
                    "collection": {"type": "string", "required": True},
                    "field": {"type": "string", "required": True},
                    "filter": {"type": "object", "default": {}},
                },
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="aggregate",
                description="Run an aggregation pipeline",
                method="LOCAL",
                parameters={
                    "collection": {"type": "string", "required": True},
                    "pipeline": {
                        "type": "object",
                        "required": True,
                        "description": "Aggregation pipeline array",
                    },
                },
                trust_level=TrustLevel.CONFIRM,
            ),
            ActionSchema(
                name="collection_stats",
                description="Get stats for all collections (doc count, size)",
                method="LOCAL",
                parameters={},
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="indexes",
                description="List indexes on a collection",
                method="LOCAL",
                parameters={
                    "collection": {"type": "string", "required": True},
                },
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="insert_one",
                description="Insert a document into a collection",
                method="LOCAL",
                parameters={
                    "collection": {"type": "string", "required": True},
                    "document": {"type": "object", "required": True},
                },
                trust_level=TrustLevel.CONFIRM,
            ),
            ActionSchema(
                name="update_many",
                description="Update documents matching a filter",
                method="LOCAL",
                parameters={
                    "collection": {"type": "string", "required": True},
                    "filter": {"type": "object", "required": True},
                    "update": {
                        "type": "object",
                        "required": True,
                        "description": 'Update operators, e.g. {"$set": {...}}',
                    },
                },
                trust_level=TrustLevel.CONFIRM,
            ),
            ActionSchema(
                name="delete_many",
                description="Delete documents matching a filter",
                method="LOCAL",
                parameters={
                    "collection": {"type": "string", "required": True},
                    "filter": {"type": "object", "required": True},
                },
                trust_level=TrustLevel.RESTRICTED,
            ),
        ]

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        if not self._connected or self._db is None:
            return ActionResult(success=False, error="Not connected")

        try:
            dispatch = {
                "list_collections": self._list_collections,
                "find": self._find,
                "find_one": self._find_one,
                "count": self._count,
                "distinct": self._distinct,
                "aggregate": self._aggregate,
                "collection_stats": self._collection_stats,
                "indexes": self._indexes,
                "insert_one": self._insert_one,
                "update_many": self._update_many,
                "delete_many": self._delete_many,
            }
            handler = dispatch.get(action)
            if not handler:
                return ActionResult(success=False, error=f"Unknown action: {action}")
            return await handler(params)
        except Exception as e:
            return ActionResult(success=False, error=str(e))

    def _parse_json_param(self, params: dict[str, Any], key: str, default: Any = None) -> Any:
        """Parse a param that could be a JSON string or already a dict/list."""
        val = params.get(key, default)
        if isinstance(val, str):
            import json

            try:
                return json.loads(val)
            except json.JSONDecodeError:
                return default
        return val

    def _serialize(self, doc: Any) -> Any:
        """Make MongoDB documents JSON-safe (ObjectId → str, datetime → iso)."""
        from datetime import datetime

        from bson import ObjectId

        if isinstance(doc, dict):
            return {k: self._serialize(v) for k, v in doc.items()}
        if isinstance(doc, list):
            return [self._serialize(v) for v in doc]
        if isinstance(doc, ObjectId):
            return str(doc)
        if isinstance(doc, datetime):
            return doc.isoformat()
        if isinstance(doc, bytes):
            return doc.hex()
        return doc

    async def _list_collections(self, params: dict[str, Any]) -> ActionResult:
        names = await self._db.list_collection_names()
        names.sort()
        return ActionResult(success=True, data=names, records_affected=len(names))

    async def _find(self, params: dict[str, Any]) -> ActionResult:
        coll = params.get("collection", "")
        if not coll:
            return ActionResult(success=False, error="No collection specified")

        query = self._parse_json_param(params, "filter", {})
        limit = int(params.get("limit", 20))
        sort = self._parse_json_param(params, "sort")
        projection = self._parse_json_param(params, "projection")

        cursor = self._db[coll].find(query, projection)
        if sort:
            cursor = cursor.sort(list(sort.items()))
        cursor = cursor.limit(limit)

        docs = [self._serialize(doc) async for doc in cursor]
        return ActionResult(success=True, data=docs, records_affected=len(docs))

    async def _find_one(self, params: dict[str, Any]) -> ActionResult:
        coll = params.get("collection", "")
        if not coll:
            return ActionResult(success=False, error="No collection specified")

        query = self._parse_json_param(params, "filter", {})

        # Support looking up by _id string
        if "_id" in query and isinstance(query["_id"], str) and len(query["_id"]) == 24:
            try:
                from bson import ObjectId

                query["_id"] = ObjectId(query["_id"])
            except Exception:
                pass

        doc = await self._db[coll].find_one(query)
        if doc:
            return ActionResult(success=True, data=self._serialize(doc), records_affected=1)
        return ActionResult(success=True, data=None, records_affected=0)

    async def _count(self, params: dict[str, Any]) -> ActionResult:
        coll = params.get("collection", "")
        if not coll:
            return ActionResult(success=False, error="No collection specified")

        query = self._parse_json_param(params, "filter", {})
        n = await self._db[coll].count_documents(query)
        return ActionResult(success=True, data={"count": n}, records_affected=n)

    async def _distinct(self, params: dict[str, Any]) -> ActionResult:
        coll = params.get("collection", "")
        field = params.get("field", "")
        if not coll or not field:
            return ActionResult(success=False, error="collection and field are required")

        query = self._parse_json_param(params, "filter", {})
        values = await self._db[coll].distinct(field, query)
        values = [self._serialize(v) for v in values]
        return ActionResult(success=True, data=values, records_affected=len(values))

    async def _aggregate(self, params: dict[str, Any]) -> ActionResult:
        coll = params.get("collection", "")
        if not coll:
            return ActionResult(success=False, error="No collection specified")

        pipeline = self._parse_json_param(params, "pipeline", [])
        if not isinstance(pipeline, list):
            return ActionResult(success=False, error="pipeline must be an array")

        cursor = self._db[coll].aggregate(pipeline)
        docs = [self._serialize(doc) async for doc in cursor]
        return ActionResult(success=True, data=docs, records_affected=len(docs))

    async def _collection_stats(self, params: dict[str, Any]) -> ActionResult:
        names = await self._db.list_collection_names()
        stats = []
        for name in sorted(names):
            try:
                s = await self._db.command("collStats", name)
                stats.append(
                    {
                        "collection": name,
                        "documents": s.get("count", 0),
                        "size_bytes": s.get("size", 0),
                        "indexes": s.get("nindexes", 0),
                    }
                )
            except Exception:
                stats.append({"collection": name, "documents": "error"})
        return ActionResult(success=True, data=stats, records_affected=len(stats))

    async def _indexes(self, params: dict[str, Any]) -> ActionResult:
        coll = params.get("collection", "")
        if not coll:
            return ActionResult(success=False, error="No collection specified")

        indexes = []
        async for idx in self._db[coll].list_indexes():
            indexes.append(self._serialize(idx))
        return ActionResult(success=True, data=indexes, records_affected=len(indexes))

    async def _insert_one(self, params: dict[str, Any]) -> ActionResult:
        coll = params.get("collection", "")
        if not coll:
            return ActionResult(success=False, error="No collection specified")

        doc = self._parse_json_param(params, "document")
        if not doc or not isinstance(doc, dict):
            return ActionResult(success=False, error="document must be a JSON object")

        result = await self._db[coll].insert_one(doc)
        return ActionResult(
            success=True, data={"inserted_id": str(result.inserted_id)}, records_affected=1
        )

    async def _update_many(self, params: dict[str, Any]) -> ActionResult:
        coll = params.get("collection", "")
        if not coll:
            return ActionResult(success=False, error="No collection specified")

        query = self._parse_json_param(params, "filter", {})
        update = self._parse_json_param(params, "update")
        if not update:
            return ActionResult(success=False, error="update is required")

        result = await self._db[coll].update_many(query, update)
        return ActionResult(
            success=True,
            data={"matched": result.matched_count, "modified": result.modified_count},
            records_affected=result.modified_count,
        )

    async def _delete_many(self, params: dict[str, Any]) -> ActionResult:
        coll = params.get("collection", "")
        if not coll:
            return ActionResult(success=False, error="No collection specified")

        query = self._parse_json_param(params, "filter", {})
        if not query:
            return ActionResult(
                success=False,
                error=(
                    "Empty filter — refusing to delete all documents."
                    ' Pass {"_confirm_delete_all": true} to override.'
                ),
            )

        if query == {"_confirm_delete_all": True}:
            query = {}

        result = await self._db[coll].delete_many(query)
        return ActionResult(
            success=True,
            data={"deleted": result.deleted_count},
            records_affected=result.deleted_count,
        )

    async def sync(self, pocket_id: str) -> SyncResult:
        return SyncResult(success=False, connector_name=self.name, error="Sync not supported")

    async def schema(self) -> dict[str, Any]:
        return {"table": None, "mapping": {}, "schedule": "manual"}
