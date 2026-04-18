# Generic database adapter — one adapter for any SQL database.
# Created: 2026-03-30
# Supports: PostgreSQL, MySQL, MariaDB, SQLite, MSSQL, CockroachDB, etc.
# Uses sqlalchemy async engine under the hood — single dependency for all databases.

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

# Maps YAML connector names to their driver/dialect info.
# Users supply credentials, we build the connection URL.
DB_DIALECTS: dict[str, dict[str, Any]] = {
    "postgresql": {
        "display_name": "PostgreSQL",
        "driver": "postgresql+asyncpg",
        "pip": "asyncpg",
        "default_port": 5432,
        "creds": ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSL"],
        # Also accept PG_* prefixed credentials from the YAML definitions
        "aliases": {
            "PG_HOST": "DB_HOST",
            "PG_PORT": "DB_PORT",
            "PG_DATABASE": "DB_NAME",
            "PG_USER": "DB_USER",
            "PG_PASSWORD": "DB_PASSWORD",
            "PG_SSL": "DB_SSL",
        },
    },
    "mysql": {
        "display_name": "MySQL",
        "driver": "mysql+aiomysql",
        "pip": "aiomysql",
        "default_port": 3306,
        "creds": ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSL"],
    },
    "mssql": {
        "display_name": "SQL Server",
        "driver": "mssql+aioodbc",
        "pip": "aioodbc",
        "default_port": 1433,
        "creds": ["DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"],
    },
    "sqlite": {
        "display_name": "SQLite",
        "driver": "sqlite+aiosqlite",
        "pip": "aiosqlite",
        "default_port": 0,
        "creds": ["DB_PATH"],
    },
}


class DatabaseAdapter:
    """Generic SQL database connector.

    Works with any database that SQLAlchemy supports via async drivers.
    One class handles PostgreSQL, MySQL, SQLite, MSSQL, etc.
    """

    def __init__(self, connector_name: str) -> None:
        self._connector_name = connector_name
        self._dialect = DB_DIALECTS.get(connector_name, DB_DIALECTS["postgresql"])
        self._engine: Any = None
        self._config: dict[str, str] = {}
        self._connected = False

    @property
    def name(self) -> str:
        return self._connector_name

    @property
    def display_name(self) -> str:
        return self._dialect["display_name"]

    def _normalize_config(self, config: dict[str, Any]) -> dict[str, str]:
        """Normalize credential keys using dialect aliases."""
        normalized: dict[str, str] = {}
        aliases = self._dialect.get("aliases", {})
        for key, val in config.items():
            mapped = aliases.get(key, key)
            normalized[mapped] = str(val)
        return normalized

    def _build_url(self, config: dict[str, str]) -> str:
        """Build SQLAlchemy connection URL from credentials."""
        driver = self._dialect["driver"]

        # SQLite is special — path-based
        if self._connector_name == "sqlite":
            path = config.get("DB_PATH", ":memory:")
            return f"sqlite+aiosqlite:///{path}"

        # Allow a raw connection string to skip URL building
        if config.get("DB_URL"):
            return config["DB_URL"]

        host = config.get("DB_HOST", "localhost")
        port = config.get("DB_PORT", str(self._dialect["default_port"]))
        name = config.get("DB_NAME", "")
        user = config.get("DB_USER", "")
        password = config.get("DB_PASSWORD", "")

        # URL-encode password in case it has special chars
        from urllib.parse import quote_plus

        safe_pass = quote_plus(password) if password else ""

        if user and safe_pass:
            return f"{driver}://{user}:{safe_pass}@{host}:{port}/{name}"
        elif user:
            return f"{driver}://{user}@{host}:{port}/{name}"
        else:
            return f"{driver}://{host}:{port}/{name}"

    async def connect(self, pocket_id: str, config: dict[str, Any]) -> ConnectionResult:
        try:
            from sqlalchemy.ext.asyncio import create_async_engine
        except ImportError:
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=(
                    "sqlalchemy[asyncio] not installed. Run: uv pip install 'sqlalchemy[asyncio]'"
                ),
            )

        normalized = self._normalize_config(config)
        self._config = normalized

        # Check the async driver is installed
        pip_pkg = self._dialect.get("pip", "")
        try:
            url = self._build_url(normalized)
            ssl_args: dict[str, Any] = {}
            if normalized.get("DB_SSL", "").lower() == "true":
                ssl_args["connect_args"] = {"ssl": True}

            self._engine = create_async_engine(
                url,
                pool_size=3,
                max_overflow=2,
                pool_timeout=10,
                **ssl_args,
            )

            # Test connection
            from sqlalchemy import text

            async with self._engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

            self._connected = True
            db_name = normalized.get("DB_NAME", normalized.get("DB_PATH", "database"))
            host = normalized.get("DB_HOST", "local")
            return ConnectionResult(
                success=True,
                connector_name=self.name,
                status=ConnectorStatus.CONNECTED,
                message=f"Connected to {db_name}@{host}",
            )
        except ImportError:
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=f"Database driver not installed. Run: uv pip install {pip_pkg}",
            )
        except Exception as e:
            self._engine = None
            return ConnectionResult(
                success=False,
                connector_name=self.name,
                status=ConnectorStatus.ERROR,
                message=f"Connection failed: {e}",
            )

    async def disconnect(self, pocket_id: str) -> bool:
        if self._engine:
            await self._engine.dispose()
            self._engine = None
        self._connected = False
        return True

    async def actions(self) -> list[ActionSchema]:
        return [
            ActionSchema(
                name="execute_query",
                description="Execute a SQL query",
                method="LOCAL",
                parameters={
                    "query": {"type": "string", "required": True},
                    "limit": {"type": "integer", "default": 100},
                },
                trust_level=TrustLevel.CONFIRM,
            ),
            ActionSchema(
                name="list_tables",
                description="List all tables in the database",
                method="LOCAL",
                parameters={"schema": {"type": "string", "default": "public"}},
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="describe_table",
                description="Show column definitions for a table",
                method="LOCAL",
                parameters={
                    "table": {"type": "string", "required": True},
                    "schema": {"type": "string", "default": "public"},
                },
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="preview_table",
                description="Preview rows from a table",
                method="LOCAL",
                parameters={
                    "table": {"type": "string", "required": True},
                    "limit": {"type": "integer", "default": 20},
                },
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="list_schemas",
                description="List schemas / databases",
                method="LOCAL",
                parameters={},
                trust_level=TrustLevel.AUTO,
            ),
            ActionSchema(
                name="table_stats",
                description="Get row counts for tables",
                method="LOCAL",
                parameters={"schema": {"type": "string", "default": "public"}},
                trust_level=TrustLevel.AUTO,
            ),
        ]

    async def execute(self, action: str, params: dict[str, Any]) -> ActionResult:
        if not self._connected or not self._engine:
            return ActionResult(success=False, error="Not connected")

        try:
            from sqlalchemy import text

            async with self._engine.connect() as conn:
                if action == "execute_query":
                    return await self._execute_query(conn, text, params)
                elif action == "list_tables":
                    return await self._list_tables(conn, text, params)
                elif action == "describe_table":
                    return await self._describe_table(conn, text, params)
                elif action == "preview_table":
                    return await self._preview_table(conn, text, params)
                elif action == "list_schemas":
                    return await self._list_schemas(conn, text)
                elif action == "table_stats":
                    return await self._table_stats(conn, text, params)
                else:
                    return ActionResult(success=False, error=f"Unknown action: {action}")
        except Exception as e:
            return ActionResult(success=False, error=str(e))

    # -- Action implementations (use standard SQL / information_schema) -----------

    async def _execute_query(self, conn: Any, text: Any, params: dict[str, Any]) -> ActionResult:
        query = params.get("query", "").strip().rstrip(";")
        if not query:
            return ActionResult(success=False, error="No query provided")

        limit = int(params.get("limit", 100))
        upper = query.upper()

        # Auto-add LIMIT for unbounded SELECTs
        if upper.startswith("SELECT") and "LIMIT" not in upper:
            query = f"{query} LIMIT {limit}"

        result = await conn.execute(text(query))

        if result.returns_rows:
            rows = [dict(r._mapping) for r in result.fetchall()]
            return ActionResult(success=True, data=rows, records_affected=len(rows))
        else:
            await conn.commit()
            return ActionResult(
                success=True, data={"rowcount": result.rowcount}, records_affected=result.rowcount
            )

    async def _list_tables(self, conn: Any, text: Any, params: dict[str, Any]) -> ActionResult:
        schema = params.get("schema", "public")

        # information_schema works across PostgreSQL, MySQL, MSSQL
        result = await conn.execute(
            text(
                "SELECT table_name, table_type "
                "FROM information_schema.tables "
                f"WHERE table_schema = '{schema}' "
                "ORDER BY table_name"
            )
        )
        rows = [dict(r._mapping) for r in result.fetchall()]
        return ActionResult(success=True, data=rows, records_affected=len(rows))

    async def _describe_table(self, conn: Any, text: Any, params: dict[str, Any]) -> ActionResult:
        table = params.get("table", "")
        schema = params.get("schema", "public")
        if not table:
            return ActionResult(success=False, error="No table specified")

        result = await conn.execute(
            text(
                "SELECT column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns "
                f"WHERE table_schema = '{schema}' AND table_name = '{table}' "
                "ORDER BY ordinal_position"
            )
        )
        rows = [dict(r._mapping) for r in result.fetchall()]
        return ActionResult(success=True, data=rows, records_affected=len(rows))

    async def _preview_table(self, conn: Any, text: Any, params: dict[str, Any]) -> ActionResult:
        table = params.get("table", "")
        limit = int(params.get("limit", 20))
        if not table:
            return ActionResult(success=False, error="No table specified")

        # Quote identifiers to prevent SQL injection
        result = await conn.execute(text(f'SELECT * FROM "{table}" LIMIT {limit}'))
        rows = [dict(r._mapping) for r in result.fetchall()]
        return ActionResult(success=True, data=rows, records_affected=len(rows))

    async def _list_schemas(self, conn: Any, text: Any) -> ActionResult:
        result = await conn.execute(
            text(
                "SELECT schema_name FROM information_schema.schemata "
                "WHERE schema_name NOT IN ('pg_catalog', 'information_schema', 'pg_toast', "
                "'mysql', 'performance_schema', 'sys') "
                "ORDER BY schema_name"
            )
        )
        rows = [dict(r._mapping) for r in result.fetchall()]
        return ActionResult(success=True, data=rows, records_affected=len(rows))

    async def _table_stats(self, conn: Any, text: Any, params: dict[str, Any]) -> ActionResult:
        schema = params.get("schema", "public")

        # Get table names first, then count rows individually
        tables_result = await conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                f"WHERE table_schema = '{schema}' AND table_type = 'BASE TABLE' "
                "ORDER BY table_name"
            )
        )
        tables = [r._mapping["table_name"] for r in tables_result.fetchall()]

        stats = []
        for t in tables[:50]:  # cap at 50 to avoid slow queries
            try:
                count_result = await conn.execute(text(f'SELECT COUNT(*) AS cnt FROM "{t}"'))
                count = count_result.scalar()
                stats.append({"table_name": t, "row_count": count})
            except Exception:
                stats.append({"table_name": t, "row_count": "error"})

        return ActionResult(success=True, data=stats, records_affected=len(stats))

    async def sync(self, pocket_id: str) -> SyncResult:
        return SyncResult(success=False, connector_name=self.name, error="Sync not supported")

    async def schema(self) -> dict[str, Any]:
        return {"table": None, "mapping": {}, "schedule": "manual"}
