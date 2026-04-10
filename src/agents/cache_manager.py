"""
RTIE Cache Manager Agent.

Handles all slash commands for cache operations including refreshing
individual objects, syncing entire schemas, checking cache status,
listing cached objects, clearing cache entries, and detecting schema
DDL changes.
"""

import hashlib
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.tools.schema_tools import SchemaTools
from src.tools.cache_tools import CacheClient
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

    Provides tools for refreshing, inspecting, listing, clearing, and
    syncing cached PL/SQL logic objects and schema snapshots.
    """

    def __init__(
        self,
        schema_tools: SchemaTools,
        cache_client: CacheClient,
    ) -> None:
        """Initialize the CacheManager with Oracle and Redis clients.

        Args:
            schema_tools: Oracle query execution tools.
            cache_client: Redis cache client.
        """
        self._schema_tools = schema_tools
        self._cache = cache_client

    async def refresh_logic_cache(self, object_name: str, schema: str) -> Dict[str, Any]:
        """Refresh a single object's cached source from Oracle.

        Fetches the latest source from ALL_SOURCE and updates the Redis
        cache with a new version stamp.

        Args:
            object_name: Name of the PL/SQL object.
            schema: Oracle schema name.

        Returns:
            Dict with refresh status, version_hash, and cached_at.
        """
        correlation_id = get_correlation_id()
        logger.info(
            f"/refresh-cache {schema}.{object_name} | "
            f"correlation_id={correlation_id}"
        )

        # Fetch latest source from Oracle
        rows = await self._schema_tools.execute_query(
            "TMPL_FETCH_SOURCE",
            {"schema": schema, "object_name": object_name},
        )

        if not rows:
            logger.warning(
                f"Object {schema}.{object_name} not found in Oracle | "
                f"correlation_id={correlation_id}"
            )
            return {
                "status": "not_found",
                "object_name": object_name,
                "schema": schema,
            }

        source_lines = [{"line": row[0], "text": row[1]} for row in rows]
        source_text = "".join(line["text"] for line in source_lines)
        version_hash = hashlib.sha256(source_text.encode()).hexdigest()[:16]

        # Get last DDL time
        obj_rows = await self._schema_tools.execute_query(
            "TMPL_OBJECT_EXISTS",
            {"schema": schema, "object_name": object_name},
        )
        last_ddl_time = str(obj_rows[0][2]) if obj_rows else None

        cached_at = datetime.utcnow().isoformat()

        # Update Redis
        cache_payload = {
            "source_code": source_lines,
            "cached_at": cached_at,
            "oracle_last_ddl_time": last_ddl_time,
            "version_hash": version_hash,
        }
        await self._cache.set_json(cache_payload, "logic", schema, object_name)

        result = {
            "status": "refreshed",
            "object_name": object_name,
            "schema": schema,
            "version_hash": version_hash,
            "cached_at": cached_at,
            "line_count": len(source_lines),
        }

        logger.info(
            f"/refresh-cache completed: {json.dumps(result)} | "
            f"correlation_id={correlation_id}"
        )
        return result

    async def refresh_all_logic_cache(self, schema: str) -> Dict[str, Any]:
        """Re-sync all functions and procedures for a schema.

        Queries ALL_OBJECTS for all functions and procedures in the schema,
        then refreshes each one's cache entry.

        Args:
            schema: Oracle schema name.

        Returns:
            Dict with total count and list of refreshed objects.
        """
        correlation_id = get_correlation_id()
        logger.info(
            f"/refresh-cache-all for schema {schema} | "
            f"correlation_id={correlation_id}"
        )

        # Get all functions and procedures in the schema
        sql = (
            "SELECT object_name, object_type "
            "FROM all_objects "
            "WHERE owner = :schema "
            "AND object_type IN ('FUNCTION', 'PROCEDURE')"
        )
        rows = await self._schema_tools.execute_raw(sql, {"schema": schema})

        refreshed = []
        errors = []

        for obj_name, obj_type in rows:
            try:
                result = await self.refresh_logic_cache(obj_name, schema)
                refreshed.append({"name": obj_name, "type": obj_type, "status": result["status"]})
            except Exception as exc:
                errors.append({"name": obj_name, "error": str(exc)})
                logger.error(
                    f"Failed to refresh {schema}.{obj_name}: {exc} | "
                    f"correlation_id={correlation_id}"
                )

        result = {
            "status": "completed",
            "schema": schema,
            "total_objects": len(rows),
            "refreshed_count": len(refreshed),
            "error_count": len(errors),
            "refreshed": refreshed,
            "errors": errors,
        }

        logger.info(
            f"/refresh-cache-all completed: {len(refreshed)} refreshed, "
            f"{len(errors)} errors | correlation_id={correlation_id}"
        )
        return result

    async def get_cache_status(self, object_name: str, schema: str) -> Dict[str, Any]:
        """Get the cache status for a specific object.

        Args:
            object_name: Name of the PL/SQL object.
            schema: Oracle schema name.

        Returns:
            Dict with cached_at, oracle_last_ddl_time, version_hash,
            and cache_hit status.
        """
        correlation_id = get_correlation_id()
        logger.info(
            f"/cache-status {schema}.{object_name} | "
            f"correlation_id={correlation_id}"
        )

        cached = await self._cache.get_json("logic", schema, object_name)

        if not cached:
            result = {
                "status": "not_cached",
                "object_name": object_name,
                "schema": schema,
                "cache_hit": False,
            }
        else:
            result = {
                "status": "cached",
                "object_name": object_name,
                "schema": schema,
                "cache_hit": True,
                "cached_at": cached.get("cached_at"),
                "oracle_last_ddl_time": cached.get("oracle_last_ddl_time"),
                "version_hash": cached.get("version_hash"),
            }

        logger.info(
            f"/cache-status result: {json.dumps(result)} | "
            f"correlation_id={correlation_id}"
        )
        return result

    async def list_cached_objects(self, schema: str) -> Dict[str, Any]:
        """List all cached logic objects for a schema.

        Args:
            schema: Oracle schema name.

        Returns:
            Dict with list of cached key names.
        """
        correlation_id = get_correlation_id()
        logger.info(
            f"/cache-list for schema {schema} | "
            f"correlation_id={correlation_id}"
        )

        keys = await self._cache.list_keys(f"logic:{schema}:*")

        result = {
            "status": "ok",
            "schema": schema,
            "count": len(keys),
            "keys": keys,
        }

        logger.info(
            f"/cache-list found {len(keys)} keys | "
            f"correlation_id={correlation_id}"
        )
        return result

    async def clear_cache_entry(self, object_name: str, schema: str) -> Dict[str, Any]:
        """Delete a specific object's cache entry from Redis.

        Args:
            object_name: Name of the PL/SQL object.
            schema: Oracle schema name.

        Returns:
            Dict with deletion status.
        """
        correlation_id = get_correlation_id()
        logger.info(
            f"/cache-clear {schema}.{object_name} | "
            f"correlation_id={correlation_id}"
        )

        deleted = await self._cache.delete_key("logic", schema, object_name)

        result = {
            "status": "cleared" if deleted else "not_found",
            "object_name": object_name,
            "schema": schema,
        }

        logger.info(
            f"/cache-clear result: {json.dumps(result)} | "
            f"correlation_id={correlation_id}"
        )
        return result

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
