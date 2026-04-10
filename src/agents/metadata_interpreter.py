"""
RTIE Metadata Interpreter Agent.

Resolves PL/SQL objects in Oracle metadata, fetches source code (from
Redis cache or Oracle ALL_SOURCE), and builds recursive dependency
call trees. All Oracle queries are retried and validated through SQLGuardian.
"""

import hashlib
import re
from datetime import datetime
from typing import Any, Dict, List, Optional

from src.graph.state import LogicState
from src.tools.schema_tools import SchemaTools
from src.tools.cache_tools import CacheClient
from src.logger import get_logger
from src.middleware.correlation_id import get_correlation_id

logger = get_logger(__name__, concern="oracle")


class ObjectNotFoundError(Exception):
    """Raised when a PL/SQL object cannot be found in Oracle metadata.

    Attributes:
        object_name: The name of the object that was not found.
        schema: The schema that was searched.
    """

    def __init__(self, object_name: str, schema: str) -> None:
        """Initialize with the missing object details.

        Args:
            object_name: Name of the object not found.
            schema: Schema that was searched.
        """
        self.object_name = object_name
        self.schema = schema
        super().__init__(
            f"Object '{object_name}' not found in schema '{schema}'"
        )


class MetadataInterpreter:
    """Agent for Oracle metadata resolution and PL/SQL source fetching.

    Handles three core responsibilities:
    1. Resolving objects in Oracle ALL_OBJECTS
    2. Fetching source code with Redis caching
    3. Building recursive dependency call trees
    """

    def __init__(
        self,
        schema_tools: SchemaTools,
        cache_client: CacheClient,
        default_schema: str = "OFSMDM",
    ) -> None:
        """Initialize the MetadataInterpreter.

        Args:
            schema_tools: Oracle query execution tools.
            cache_client: Redis cache client for source code caching.
            default_schema: Default Oracle schema. Defaults to 'OFSMDM'.
        """
        self._schema_tools = schema_tools
        self._cache = cache_client
        self._default_schema = default_schema

    async def resolve_object(self, state: LogicState) -> LogicState:
        """Resolve a PL/SQL object in Oracle metadata.

        Queries ALL_OBJECTS via TMPL_OBJECT_EXISTS to confirm the object
        exists and determine its type (FUNCTION, PROCEDURE, PACKAGE).

        Args:
            state: Current pipeline state with object_name and schema.

        Returns:
            Updated state with object_type confirmed.

        Raises:
            ObjectNotFoundError: If the object does not exist in the schema.
        """
        correlation_id = get_correlation_id()
        schema = state.get("schema") or self._default_schema
        object_name = state["object_name"]

        logger.info(
            f"Resolving object: {schema}.{object_name} | "
            f"correlation_id={correlation_id}"
        )

        rows = await self._schema_tools.execute_query(
            "TMPL_OBJECT_EXISTS",
            {"schema": schema, "object_name": object_name},
        )

        if not rows:
            logger.error(
                f"Object not found: {schema}.{object_name} | "
                f"correlation_id={correlation_id}"
            )
            raise ObjectNotFoundError(object_name, schema)

        # rows[0] = (object_name, object_type, last_ddl_time)
        resolved_name, object_type, last_ddl_time = rows[0]

        state["object_name"] = resolved_name
        state["object_type"] = object_type
        state["schema"] = schema

        logger.info(
            f"Object resolved: {schema}.{resolved_name} type={object_type} "
            f"last_ddl={last_ddl_time} | correlation_id={correlation_id}"
        )
        return state

    async def fetch_logic(self, state: LogicState) -> LogicState:
        """Fetch PL/SQL source code from Redis cache or Oracle.

        Checks Redis first using key logic:{schema}:{object_name}.
        On cache hit, reads source_code directly. On cache miss, queries
        Oracle via TMPL_FETCH_SOURCE and stores in Redis with version stamp.
        If Redis is unavailable, falls back to Oracle directly.

        Args:
            state: Current pipeline state with object_name and schema.

        Returns:
            Updated state with source_code, cache_hit, and cache_stale.
        """
        correlation_id = get_correlation_id()
        schema = state["schema"]
        object_name = state["object_name"]
        cache_key_parts = ("logic", schema, object_name)

        logger.info(
            f"Fetching logic for {schema}.{object_name} | "
            f"correlation_id={correlation_id}"
        )

        # Try Redis cache first
        cached = await self._cache.get_json(*cache_key_parts)
        if cached:
            state["source_code"] = cached["source_code"]
            state["cache_hit"] = True
            state["cache_stale"] = False
            logger.info(
                f"Cache HIT for {schema}.{object_name} "
                f"(cached_at={cached.get('cached_at')}) | "
                f"correlation_id={correlation_id}"
            )
            return state

        # Cache miss — fetch from Oracle
        logger.info(
            f"Cache MISS for {schema}.{object_name} — querying Oracle | "
            f"correlation_id={correlation_id}"
        )
        rows = await self._schema_tools.execute_query(
            "TMPL_FETCH_SOURCE",
            {"schema": schema, "object_name": object_name},
        )

        source_lines = [{"line": row[0], "text": row[1]} for row in rows]

        # Get last DDL time for version stamp
        obj_rows = await self._schema_tools.execute_query(
            "TMPL_OBJECT_EXISTS",
            {"schema": schema, "object_name": object_name},
        )
        last_ddl_time = str(obj_rows[0][2]) if obj_rows else None

        # Compute version hash
        source_text = "".join(line["text"] for line in source_lines)
        version_hash = hashlib.sha256(source_text.encode()).hexdigest()[:16]

        # Store in Redis (graceful degradation — never fail on Redis errors)
        cache_payload = {
            "source_code": source_lines,
            "cached_at": datetime.utcnow().isoformat(),
            "oracle_last_ddl_time": last_ddl_time,
            "version_hash": version_hash,
        }
        await self._cache.set_json(cache_payload, *cache_key_parts)

        state["source_code"] = source_lines
        state["cache_hit"] = False
        state["cache_stale"] = False

        logger.info(
            f"Fetched {len(source_lines)} lines from Oracle for "
            f"{schema}.{object_name} (hash={version_hash}) | "
            f"correlation_id={correlation_id}"
        )
        return state

    async def fetch_dependencies(self, state: LogicState) -> LogicState:
        """Build a recursive call tree of function dependencies.

        Scans the PL/SQL source code for function/procedure call patterns,
        then recursively fetches each dependency's source from Oracle
        (max depth 3).

        Args:
            state: Current pipeline state with source_code.

        Returns:
            Updated state with call_tree dict populated.
        """
        correlation_id = get_correlation_id()
        schema = state["schema"]
        source_lines = state["source_code"]
        object_name = state["object_name"]

        logger.info(
            f"Scanning dependencies for {schema}.{object_name} | "
            f"correlation_id={correlation_id}"
        )

        # Extract function calls from source code
        source_text = "".join(
            line["text"] if isinstance(line, dict) else str(line)
            for line in source_lines
        )
        called_functions = self._extract_function_calls(source_text)

        # Remove self-references
        called_functions = [
            f for f in called_functions
            if f.upper() != object_name.upper()
        ]

        logger.info(
            f"Found {len(called_functions)} potential dependencies: "
            f"{called_functions[:10]} | correlation_id={correlation_id}"
        )

        # Build call tree recursively
        call_tree = await self._build_call_tree(
            schema, called_functions, depth=0, max_depth=3, visited=set()
        )

        state["call_tree"] = {
            "root": object_name,
            "dependencies": call_tree,
        }

        logger.info(
            f"Call tree built for {object_name}: "
            f"{len(call_tree)} direct dependencies | "
            f"correlation_id={correlation_id}"
        )
        return state

    def _extract_function_calls(self, source_text: str) -> List[str]:
        """Extract function/procedure call names from PL/SQL source.

        Args:
            source_text: Concatenated PL/SQL source code.

        Returns:
            De-duplicated list of called function/procedure names.
        """
        # Match PL/SQL function calls: FN_NAME(...) or PKG.FN_NAME(...)
        pattern = r'\b([A-Z_][A-Z0-9_]*(?:\.[A-Z_][A-Z0-9_]*)?)\s*\('
        matches = re.findall(pattern, source_text, re.IGNORECASE)

        # Filter out common PL/SQL keywords that look like function calls
        plsql_keywords = {
            "IF", "ELSIF", "WHILE", "FOR", "LOOP", "CASE", "WHEN",
            "THEN", "ELSE", "END", "BEGIN", "DECLARE", "EXCEPTION",
            "RETURN", "IN", "OUT", "IS", "AS", "NOT", "AND", "OR",
            "NULL", "TRUE", "FALSE", "UPPER", "LOWER", "TRIM",
            "NVL", "NVL2", "DECODE", "TO_CHAR", "TO_DATE", "TO_NUMBER",
            "SUBSTR", "INSTR", "LENGTH", "REPLACE", "COALESCE",
            "COUNT", "SUM", "AVG", "MIN", "MAX", "ROUND", "TRUNC",
        }

        seen = set()
        result = []
        for name in matches:
            upper_name = name.upper()
            if upper_name not in plsql_keywords and upper_name not in seen:
                seen.add(upper_name)
                result.append(name)

        return result

    async def _build_call_tree(
        self,
        schema: str,
        function_names: List[str],
        depth: int,
        max_depth: int,
        visited: set,
    ) -> Dict[str, Any]:
        """Recursively build a dependency call tree.

        Args:
            schema: Oracle schema to search in.
            function_names: List of function names to resolve.
            depth: Current recursion depth.
            max_depth: Maximum recursion depth (default 3).
            visited: Set of already-visited function names to prevent cycles.

        Returns:
            Dictionary mapping function names to their dependency info.
        """
        tree: Dict[str, Any] = {}

        if depth >= max_depth:
            return tree

        for fn_name in function_names:
            upper_name = fn_name.upper()
            if upper_name in visited:
                tree[fn_name] = {"status": "circular_reference", "depth": depth}
                continue

            visited.add(upper_name)

            try:
                rows = await self._schema_tools.execute_query(
                    "TMPL_FETCH_SOURCE",
                    {"schema": schema, "object_name": fn_name},
                )

                if not rows:
                    tree[fn_name] = {"status": "not_found", "depth": depth}
                    continue

                source_lines = [{"line": r[0], "text": r[1]} for r in rows]
                source_text = "".join(line["text"] for line in source_lines)
                sub_calls = self._extract_function_calls(source_text)
                sub_calls = [
                    f for f in sub_calls
                    if f.upper() != upper_name and f.upper() not in visited
                ]

                sub_tree = await self._build_call_tree(
                    schema, sub_calls, depth + 1, max_depth, visited
                )

                tree[fn_name] = {
                    "status": "resolved",
                    "depth": depth,
                    "line_count": len(source_lines),
                    "source_code": source_lines,
                    "dependencies": sub_tree,
                }

            except Exception as exc:
                logger.warning(
                    f"Failed to resolve dependency {fn_name}: {exc}"
                )
                tree[fn_name] = {
                    "status": "error",
                    "depth": depth,
                    "error": str(exc),
                }

        return tree
