"""
RTIE Cache Manager Agent.

Handles slash commands for cache inspection (``/cache-list``,
``/cache-status``) over the Phase-3 loader-managed ``graph:source:*``
namespace, plus schema DDL-snapshot management (``/refresh-schema``).

The legacy ``rtie:logic:`` source cache was retired in Phase 8: the
``/refresh-cache``, ``/refresh-cache-all`` and ``/cache-clear`` commands
now return deprecation messages directing users to FLUSHDB + restart for
a full corpus rebuild. ``graph:source:`` is loader-managed at startup
and is the sole source-of-source after Phase 8.
"""

import json
import os
from datetime import datetime
from typing import Any, Dict, Optional

from src.tools.schema_tools import SchemaTools
from src.tools.cache_tools import CacheClient
from src.parsing.store import get_raw_source
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id

logger = get_logger(__name__, concern="commands")

# Path to db/schemas/ directory
SCHEMAS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))),
    "db", "schemas",
)


class CacheManager:
    """Agent for cache-related slash command operations.

    Provides tools for inspecting the Phase-3 loader-managed source
    cache (``graph:source:*``) and managing schema DDL snapshots.
    """

    def __init__(
        self,
        schema_tools: SchemaTools,
        cache_client: CacheClient,
    ) -> None:
        """Initialize the CacheManager with Oracle and Redis clients.

        Args:
            schema_tools: Oracle query execution tools.
            cache_client: Async Redis client (used for schema-snapshot
                storage under ``rtie:schema:snapshot:*``; no longer used
                for source caching after Phase 8).
        """
        self._schema_tools = schema_tools
        self._cache = cache_client
        self._graph_redis = None

    def set_graph_redis_client(self, graph_redis_client) -> None:
        """Wire the sync graph Redis client used to inspect ``graph:source:*``.

        Required by ``list_cached_objects`` and ``get_cache_status``
        post-Phase-8. Mirrors :meth:`MetadataInterpreter.set_graph_redis_client`.
        """
        self._graph_redis = graph_redis_client

    async def refresh_logic_cache(
        self, object_name: str, schema: str
    ) -> Dict[str, Any]:
        """Phase 8 deprecation stub: ``/refresh-cache <object>``.

        ``rtie:logic:`` was retired in Phase 8. ``graph:source:`` is rebuilt
        at startup from the loader corpus, not via per-object refresh —
        single-object cache invalidation is no longer a meaningful
        workflow. Returns a structured deprecation payload so the
        frontend chip surface continues to render rather than 404.
        """
        correlation_id = get_correlation_id()
        message = (
            "RTIE rebuilds the source cache at startup. Run "
            "`docker exec -it rtie-redis redis-cli FLUSHDB` and restart "
            "the backend to refresh."
        )
        logger.info(
            "/refresh-cache deprecated stub invoked for %s.%s | "
            "correlation_id=%s",
            schema, object_name, correlation_id,
        )
        return {
            "status": "deprecated",
            "object_name": object_name,
            "schema": schema,
            "message": message,
        }

    async def refresh_all_logic_cache(self, schema: str) -> Dict[str, Any]:
        """Phase 8 deprecation stub: ``/refresh-cache-all``.

        See :meth:`refresh_logic_cache` — same rationale at schema scale.
        """
        correlation_id = get_correlation_id()
        message = (
            "RTIE rebuilds the source cache at startup. Run "
            "`docker exec -it rtie-redis redis-cli FLUSHDB` and restart "
            "the backend to refresh."
        )
        logger.info(
            "/refresh-cache-all deprecated stub invoked for schema %s | "
            "correlation_id=%s",
            schema, correlation_id,
        )
        return {
            "status": "deprecated",
            "schema": schema,
            "message": message,
        }

    async def get_cache_status(
        self,
        object_name: Optional[str],
        schema: str,
    ) -> Dict[str, Any]:
        """Inspect the Phase-3 loader-managed source cache.

        - With ``object_name``: report whether
          ``graph:source:<schema>:<object_name>`` and
          ``graph:<schema>:<object_name>`` are present, plus the cached
          source line count.
        - Without ``object_name``: report aggregate counts of
          ``graph:source:<schema>:*`` and ``graph:<schema>:*`` keys for
          the schema.

        Args:
            object_name: Specific function name to check, or None/empty
                for aggregate counts.
            schema: Oracle schema name.

        Returns:
            Dict with presence flags / aggregate counts. Returns a
            ``redis_unavailable`` status if the sync graph Redis client
            isn't wired.
        """
        correlation_id = get_correlation_id()

        if self._graph_redis is None:
            logger.warning(
                "/cache-status invoked without graph_redis wired | "
                "correlation_id=%s",
                correlation_id,
            )
            return {
                "status": "redis_unavailable",
                "schema": schema,
                "object_name": object_name,
            }

        if object_name:
            normalized = object_name.upper()
            source_lines = get_raw_source(
                self._graph_redis, schema, normalized
            )
            graph_key = f"graph:{schema}:{normalized}"
            try:
                graph_present = bool(self._graph_redis.exists(graph_key))
            except Exception as exc:
                logger.warning(
                    "graph:%s:%s exists() failed: %s | correlation_id=%s",
                    schema, normalized, exc, correlation_id,
                )
                graph_present = False

            result = {
                "status": "ok",
                "schema": schema,
                "object_name": normalized,
                "graph_source_present": source_lines is not None,
                "graph_source_lines": (
                    len(source_lines) if source_lines is not None else 0
                ),
                "graph_present": graph_present,
            }
            logger.info(
                "/cache-status result: %s | correlation_id=%s",
                json.dumps(result), correlation_id,
            )
            return result

        # Aggregate mode — count the two namespaces for the schema.
        source_pattern = f"graph:source:{schema}:*"
        graph_pattern = f"graph:{schema}:*"
        try:
            source_count = sum(
                1 for _ in self._graph_redis.scan_iter(match=source_pattern)
            )
            graph_count = sum(
                1 for _ in self._graph_redis.scan_iter(match=graph_pattern)
            )
        except Exception as exc:
            logger.warning(
                "/cache-status SCAN failed for %s: %s | correlation_id=%s",
                schema, exc, correlation_id,
            )
            return {
                "status": "scan_failed",
                "schema": schema,
                "error": str(exc),
            }

        result = {
            "status": "ok",
            "schema": schema,
            "graph_source_count": source_count,
            "graph_count": graph_count,
        }
        logger.info(
            "/cache-status aggregate: %s | correlation_id=%s",
            json.dumps(result), correlation_id,
        )
        return result

    async def list_cached_objects(self, schema: str) -> Dict[str, Any]:
        """Enumerate function names cached under ``graph:source:<schema>:*``.

        Args:
            schema: Oracle schema name.

        Returns:
            Dict with the count and the sorted list of function names.
        """
        correlation_id = get_correlation_id()
        logger.info(
            "/cache-list for schema %s | correlation_id=%s",
            schema, correlation_id,
        )

        if self._graph_redis is None:
            logger.warning(
                "/cache-list invoked without graph_redis wired | "
                "correlation_id=%s",
                correlation_id,
            )
            return {
                "status": "redis_unavailable",
                "schema": schema,
                "count": 0,
                "objects": [],
            }

        prefix = f"graph:source:{schema}:"
        pattern = f"{prefix}*"
        try:
            names = []
            for key in self._graph_redis.scan_iter(match=pattern):
                if isinstance(key, bytes):
                    key = key.decode("utf-8", errors="replace")
                if key.startswith(prefix):
                    names.append(key[len(prefix):])
            names.sort()
        except Exception as exc:
            logger.warning(
                "/cache-list SCAN failed for %s: %s | correlation_id=%s",
                schema, exc, correlation_id,
            )
            return {
                "status": "scan_failed",
                "schema": schema,
                "error": str(exc),
            }

        result = {
            "status": "ok",
            "schema": schema,
            "count": len(names),
            "objects": names,
        }
        logger.info(
            "/cache-list found %d entries | correlation_id=%s",
            len(names), correlation_id,
        )
        return result

    async def clear_cache_entry(
        self, object_name: str, schema: str
    ) -> Dict[str, Any]:
        """Phase 8 deprecation stub: ``/cache-clear <object>``.

        Single-key cache invalidation is no longer supported — the
        loader corpus is rebuilt atomically at startup.
        """
        correlation_id = get_correlation_id()
        message = (
            "Single-key cache invalidation is no longer supported. Use "
            "`docker exec -it rtie-redis redis-cli FLUSHDB` + restart "
            "for a full rebuild."
        )
        logger.info(
            "/cache-clear deprecated stub invoked for %s.%s | "
            "correlation_id=%s",
            schema, object_name, correlation_id,
        )
        return {
            "status": "deprecated",
            "object_name": object_name,
            "schema": schema,
            "message": message,
        }

    async def refresh_schema_snapshot(self, schema: str) -> Dict[str, Any]:
        """Detect and sync DDL changes in the Oracle schema.

        Queries TMPL_SCHEMA_SNAPSHOT from Oracle, compares against the
        cached snapshot in Redis, detects changes (new/dropped tables,
        added/removed columns, data type changes), regenerates changed
        DDL files on disk, and updates the Redis snapshot baseline.

        Args:
            schema: Oracle schema name.

        Returns:
            Dict with a diff report of all detected changes.
        """
        correlation_id = get_correlation_id()
        logger.info(
            f"/refresh-schema for {schema} | correlation_id={correlation_id}"
        )

        # Fetch current schema from Oracle
        rows = await self._schema_tools.execute_query(
            "TMPL_SCHEMA_SNAPSHOT",
            {"schema": schema},
        )

        # Build current schema structure
        current_schema = self._build_schema_dict(rows)

        # Get previous snapshot from Redis
        previous = await self._cache.get_json("schema", "snapshot", schema)
        previous_schema = previous.get("tables", {}) if previous else {}

        # Compute diff
        changes = self._compute_schema_diff(previous_schema, current_schema, schema)

        # Regenerate DDL files for changed tables
        if changes["has_changes"]:
            self._regenerate_ddl_files(schema, current_schema, changes)

        # Update Redis snapshot
        snapshot_payload = {
            "tables": current_schema,
            "snapshot_at": datetime.utcnow().isoformat(),
            "table_count": len(current_schema),
        }
        await self._cache.set_json(snapshot_payload, "schema", "snapshot", schema)

        result = {
            "status": "completed",
            "schema": schema,
            "changes": changes,
            "report": changes.get("report", "No changes detected."),
        }

        logger.info(
            f"/refresh-schema completed: {changes.get('summary', 'no changes')} | "
            f"correlation_id={correlation_id}"
        )
        return result

    def _build_schema_dict(self, rows: list) -> Dict[str, Dict[str, Any]]:
        """Build a schema dictionary from ALL_TAB_COLUMNS query results.

        Args:
            rows: List of tuples from TMPL_SCHEMA_SNAPSHOT query.

        Returns:
            Dict mapping table_name -> {columns: {col_name: col_info}}.
        """
        tables: Dict[str, Dict[str, Any]] = {}

        for row in rows:
            table_name, col_name, col_id, data_type, data_length, \
                data_precision, data_scale, nullable = row

            if table_name not in tables:
                tables[table_name] = {"columns": {}}

            tables[table_name]["columns"][col_name] = {
                "column_id": col_id,
                "data_type": data_type,
                "data_length": data_length,
                "data_precision": data_precision,
                "data_scale": data_scale,
                "nullable": nullable,
            }

        return tables

    def _compute_schema_diff(
        self,
        old_schema: Dict[str, Any],
        new_schema: Dict[str, Any],
        schema_name: str,
    ) -> Dict[str, Any]:
        """Compute differences between two schema snapshots.

        Args:
            old_schema: Previous schema dictionary.
            new_schema: Current schema dictionary.
            schema_name: Schema name for report formatting.

        Returns:
            Dict with change details and a formatted report string.
        """
        old_tables = set(old_schema.keys())
        new_tables = set(new_schema.keys())

        added_tables = new_tables - old_tables
        dropped_tables = old_tables - new_tables
        common_tables = old_tables & new_tables

        report_lines = [f"{schema_name} schema changes detected:"]
        column_changes = []

        # New tables
        for table in sorted(added_tables):
            report_lines.append(f"+ NEW TABLE: {table}")

        # Dropped tables
        for table in sorted(dropped_tables):
            report_lines.append(f"- DROPPED TABLE: {table}")

        # Column-level changes in existing tables
        for table in sorted(common_tables):
            old_cols = old_schema[table].get("columns", {})
            new_cols = new_schema[table].get("columns", {})

            old_col_names = set(old_cols.keys())
            new_col_names = set(new_cols.keys())

            # Added columns
            for col in sorted(new_col_names - old_col_names):
                col_info = new_cols[col]
                type_str = self._format_column_type(col_info)
                report_lines.append(
                    f"~ {table}: column {col} added ({type_str})"
                )
                column_changes.append({
                    "table": table, "column": col, "change": "added",
                    "new_type": type_str,
                })

            # Removed columns
            for col in sorted(old_col_names - new_col_names):
                report_lines.append(f"~ {table}: column {col} removed")
                column_changes.append({
                    "table": table, "column": col, "change": "removed",
                })

            # Modified columns
            for col in sorted(old_col_names & new_col_names):
                old_type = self._format_column_type(old_cols[col])
                new_type = self._format_column_type(new_cols[col])
                if old_type != new_type:
                    report_lines.append(
                        f"~ {table}: {col} changed {old_type} -> {new_type}"
                    )
                    column_changes.append({
                        "table": table, "column": col, "change": "modified",
                        "old_type": old_type, "new_type": new_type,
                    })

        has_changes = bool(added_tables or dropped_tables or column_changes)

        if not has_changes:
            report_lines = [f"{schema_name} schema: no changes detected."]

        return {
            "has_changes": has_changes,
            "new_tables": sorted(added_tables),
            "dropped_tables": sorted(dropped_tables),
            "column_changes": column_changes,
            "report": "\n".join(report_lines),
            "summary": (
                f"{len(added_tables)} new, {len(dropped_tables)} dropped, "
                f"{len(column_changes)} column changes"
            ),
        }

    def _format_column_type(self, col_info: Dict[str, Any]) -> str:
        """Format a column's data type string.

        Args:
            col_info: Column metadata dict with data_type, data_length, etc.

        Returns:
            Formatted type string (e.g. 'VARCHAR2(100)', 'NUMBER(22,3)').
        """
        dtype = col_info.get("data_type", "UNKNOWN")
        precision = col_info.get("data_precision")
        scale = col_info.get("data_scale")
        length = col_info.get("data_length")

        if precision is not None and scale is not None:
            return f"{dtype}({precision},{scale})"
        elif precision is not None:
            return f"{dtype}({precision})"
        elif dtype in ("VARCHAR2", "CHAR", "RAW") and length is not None:
            return f"{dtype}({length})"
        return dtype

    def _regenerate_ddl_files(
        self,
        schema: str,
        current_schema: Dict[str, Any],
        changes: Dict[str, Any],
    ) -> None:
        """Regenerate DDL files on disk for changed tables.

        Only regenerates the create_tables.sql file for schemas that have
        detected changes.

        Args:
            schema: Oracle schema name.
            current_schema: Full current schema dictionary.
            changes: Change diff dictionary.
        """
        schema_dir = os.path.join(SCHEMAS_DIR, schema)
        os.makedirs(schema_dir, exist_ok=True)

        ddl_path = os.path.join(schema_dir, "create_tables.sql")
        ddl_lines = [
            f"-- =============================================================================",
            f"-- {schema} Schema — Auto-generated by RTIE /refresh-schema",
            f"-- Generated at: {datetime.utcnow().isoformat()}",
            f"-- =============================================================================",
            "",
        ]

        for table_name in sorted(current_schema.keys()):
            columns = current_schema[table_name].get("columns", {})
            col_defs = []

            for col_name in sorted(columns.keys(), key=lambda c: columns[c].get("column_id", 0)):
                col_info = columns[col_name]
                type_str = self._format_column_type(col_info)
                nullable = "" if col_info.get("nullable") == "Y" else " NOT NULL"
                col_defs.append(f"    {col_name} {type_str}{nullable}")

            ddl_lines.append(f"CREATE TABLE {schema}.{table_name} (")
            ddl_lines.append(",\n".join(col_defs))
            ddl_lines.append(");")
            ddl_lines.append("")

        with open(ddl_path, "w", encoding="utf-8") as f:
            f.write("\n".join(ddl_lines))

        logger.info(
            f"Regenerated DDL file: {ddl_path} "
            f"({len(current_schema)} tables)"
        )
