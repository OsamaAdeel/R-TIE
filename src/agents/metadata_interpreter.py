"""
RTIE Metadata Interpreter Agent.

Resolves PL/SQL objects in Oracle metadata or from local SQL files,
fetches source code (from Redis cache, Oracle ALL_SOURCE, or disk),
and builds recursive dependency call trees. All Oracle queries are
retried and validated through SQLGuardian.
"""

import glob
import hashlib
import os
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
    """Raised when a PL/SQL object cannot be found in Oracle metadata or disk.

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


# Paths to search for PL/SQL source files — both RTIE/db/modules/ and parent R-TIE/db/modules/
_RTIE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
MODULES_DIRS = [
    os.path.join(_RTIE_ROOT, "db", "modules"),
    os.path.join(os.path.dirname(_RTIE_ROOT), "db", "modules"),
]


def _scan_modules_for_file(object_name: str) -> Optional[str]:
    """Scan all module directories for a SQL file matching the object name.

    Searches RTIE/db/modules/ and the parent R-TIE/db/modules/ for a
    .sql file whose name (case-insensitive) matches the given object_name.

    Args:
        object_name: The PL/SQL object name to find.

    Returns:
        Full file path if found, None otherwise.
    """
    for modules_dir in MODULES_DIRS:
        if not os.path.isdir(modules_dir):
            continue
        pattern = os.path.join(modules_dir, "**", "*.sql")
        for filepath in glob.glob(pattern, recursive=True):
            basename = os.path.splitext(os.path.basename(filepath))[0]
            if basename.upper() == object_name.upper():
                return filepath
    return None


def _read_sql_file(filepath: str) -> List[Dict[str, Any]]:
    """Read a SQL file and return numbered source lines.

    Args:
        filepath: Path to the .sql file.

    Returns:
        List of dicts with 'line' (int) and 'text' (str) keys.
    """
    with open(filepath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    return [{"line": i + 1, "text": line} for i, line in enumerate(lines)]


def _detect_object_type(source_text: str) -> str:
    """Detect PL/SQL object type from source code.

    Args:
        source_text: The raw PL/SQL source code.

    Returns:
        'FUNCTION', 'PROCEDURE', or 'PACKAGE' based on the source.
    """
    upper = source_text.upper()
    if "CREATE OR REPLACE FUNCTION" in upper or "FUNCTION" in upper.split("BEGIN")[0]:
        return "FUNCTION"
    if "CREATE OR REPLACE PROCEDURE" in upper:
        return "PROCEDURE"
    if "CREATE OR REPLACE PACKAGE" in upper:
        return "PACKAGE"
    return "FUNCTION"


class MetadataInterpreter:
    """Agent for Oracle metadata resolution and PL/SQL source fetching.

    Handles three core responsibilities:
    1. Resolving objects in Oracle ALL_OBJECTS (with fallback to disk files)
    2. Fetching source code with Redis caching (with fallback to disk files)
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
        """Resolve a PL/SQL object in Oracle metadata or local files.

        First queries ALL_OBJECTS via TMPL_OBJECT_EXISTS. If not found in
        Oracle, falls back to scanning db/modules/ for a matching .sql file.

        Args:
            state: Current pipeline state with object_name and schema.

        Returns:
            Updated state with object_type confirmed.

        Raises:
            ObjectNotFoundError: If the object is not found anywhere.
        """
        correlation_id = get_correlation_id()
        schema = state.get("schema") or self._default_schema
        object_name = state["object_name"]

        logger.info(
            f"Resolving object: {schema}.{object_name} | "
            f"correlation_id={correlation_id}"
        )

        # Try Oracle first
        rows = await self._schema_tools.execute_query(
            "TMPL_OBJECT_EXISTS",
            {"schema": schema, "object_name": object_name},
        )

        if rows:
            resolved_name, object_type, last_ddl_time = rows[0]
            state["object_name"] = resolved_name
            state["object_type"] = object_type
            state["schema"] = schema
            logger.info(
                f"Object resolved from Oracle: {schema}.{resolved_name} "
                f"type={object_type} | correlation_id={correlation_id}"
            )
            return state

        # Fallback: scan db/modules/ for SQL file
        logger.info(
            f"Object not in Oracle ALL_OBJECTS, scanning db/modules/ | "
            f"correlation_id={correlation_id}"
        )
        filepath = _scan_modules_for_file(object_name)

        if filepath:
            source_lines = _read_sql_file(filepath)
            source_text = "".join(line["text"] for line in source_lines)
            object_type = _detect_object_type(source_text)

            state["object_name"] = object_name.upper()
            state["object_type"] = object_type
            state["schema"] = schema

            logger.info(
                f"Object resolved from disk: {filepath} "
                f"type={object_type} | correlation_id={correlation_id}"
            )
            return state

        logger.error(
            f"Object not found anywhere: {schema}.{object_name} | "
            f"correlation_id={correlation_id}"
        )
        raise ObjectNotFoundError(object_name, schema)

    async def fetch_logic(self, state: LogicState) -> LogicState:
        """Fetch PL/SQL source code from cache, Oracle, or disk.

        Priority: Redis cache -> Oracle ALL_SOURCE -> db/modules/ .sql files.
        If Redis is unavailable, falls back gracefully.

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

        # 1. Try Redis cache first
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

        # 2. Try Oracle ALL_SOURCE
        logger.info(
            f"Cache MISS for {schema}.{object_name} — trying Oracle | "
            f"correlation_id={correlation_id}"
        )
        rows = await self._schema_tools.execute_query(
            "TMPL_FETCH_SOURCE",
            {"schema": schema, "object_name": object_name},
        )

        if rows:
            source_lines = [{"line": row[0], "text": row[1]} for row in rows]
            await self._cache_source(
                schema, object_name, source_lines, cache_key_parts, correlation_id
            )
            state["source_code"] = source_lines
            state["cache_hit"] = False
            state["cache_stale"] = False
            logger.info(
                f"Fetched {len(source_lines)} lines from Oracle | "
                f"correlation_id={correlation_id}"
            )
            return state

        # 3. Fallback: read from db/modules/ on disk
        logger.info(
            f"Not in Oracle ALL_SOURCE, trying db/modules/ | "
            f"correlation_id={correlation_id}"
        )
        filepath = _scan_modules_for_file(object_name)

        if filepath:
            source_lines = _read_sql_file(filepath)
            await self._cache_source(
                schema, object_name, source_lines, cache_key_parts, correlation_id
            )
            state["source_code"] = source_lines
            state["cache_hit"] = False
            state["cache_stale"] = False
            logger.info(
                f"Fetched {len(source_lines)} lines from disk: {filepath} | "
                f"correlation_id={correlation_id}"
            )
            return state

        # Nothing found — return empty (resolve_object should have caught this)
        state["source_code"] = []
        state["cache_hit"] = False
        state["cache_stale"] = False
        logger.warning(
            f"No source found for {schema}.{object_name} | "
            f"correlation_id={correlation_id}"
        )
        return state

    async def _cache_source(
        self,
        schema: str,
        object_name: str,
        source_lines: List[Dict[str, Any]],
        cache_key_parts: tuple,
        correlation_id: str,
    ) -> None:
        """Cache source code in Redis with a version stamp.

        Args:
            schema: Oracle schema name.
            object_name: PL/SQL object name.
            source_lines: List of line dicts to cache.
            cache_key_parts: Redis key tuple.
            correlation_id: Request correlation ID for logging.
        """
        source_text = "".join(
            line["text"] if isinstance(line, dict) else str(line)
            for line in source_lines
        )
        version_hash = hashlib.sha256(source_text.encode()).hexdigest()[:16]

        cache_payload = {
            "source_code": source_lines,
            "cached_at": datetime.utcnow().isoformat(),
            "oracle_last_ddl_time": None,
            "version_hash": version_hash,
        }
        await self._cache.set_json(cache_payload, *cache_key_parts)
        logger.info(
            f"Cached {len(source_lines)} lines for {schema}.{object_name} "
            f"(hash={version_hash}) | correlation_id={correlation_id}"
        )

    async def fetch_multi_logic(self, state: LogicState) -> LogicState:
        """Fetch source code for multiple functions from semantic search results.

        Iterates over search_results in state, fetches each function's source
        via the existing pipeline (Redis -> Oracle -> disk), and stores all
        sources in state['multi_source'].

        Args:
            state: Pipeline state with search_results containing function names.

        Returns:
            Updated state with multi_source dict mapping function_name to source info.
        """
        correlation_id = get_correlation_id()
        search_results = state.get("search_results", [])
        schema = state.get("schema") or self._default_schema
        multi_source: Dict[str, Any] = {}

        for result in search_results:
            fn_name = result["function_name"]
            logger.info(
                f"Fetching source for {fn_name} | correlation_id={correlation_id}"
            )

            # Build a mini-state for existing fetch_logic
            mini_state: Dict[str, Any] = {
                "schema": schema,
                "object_name": fn_name,
                "source_code": [],
                "cache_hit": False,
                "cache_stale": False,
            }
            try:
                fetched = await self.fetch_logic(mini_state)
                multi_source[fn_name] = {
                    "source_code": fetched["source_code"],
                    "description": result.get("description", ""),
                    "tables_read": result.get("tables_read", ""),
                    "tables_written": result.get("tables_written", ""),
                    "score": result.get("score", 0.0),
                }
            except Exception as exc:
                logger.warning(
                    f"Failed to fetch source for {fn_name}: {exc} | "
                    f"correlation_id={correlation_id}"
                )
                multi_source[fn_name] = {
                    "source_code": [],
                    "description": result.get("description", ""),
                    "tables_read": result.get("tables_read", ""),
                    "tables_written": result.get("tables_written", ""),
                    "score": result.get("score", 0.0),
                    "error": str(exc),
                }

        state["multi_source"] = multi_source
        state["source_code"] = []
        state["cache_hit"] = False
        state["cache_stale"] = False

        logger.info(
            f"Fetched source for {len(multi_source)} functions | "
            f"correlation_id={correlation_id}"
        )
        return state

    async def fetch_dependencies(self, state: LogicState) -> LogicState:
        """Build a recursive call tree of function dependencies.

        Scans the PL/SQL source code for function/procedure call patterns,
        then recursively fetches each dependency's source (from Oracle or
        disk, max depth 3).

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
            "INSERT", "UPDATE", "DELETE", "SELECT", "FROM", "WHERE",
            "GROUP", "ORDER", "HAVING", "VALUES", "INTO", "SET",
            "COMMIT", "ROLLBACK", "DBMS_OUTPUT", "PUT_LINE",
            "EXTRACT", "MONTH", "YEAR", "DAY",
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

        Tries Oracle ALL_SOURCE first, then falls back to db/modules/ files.

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
            source_lines = None

            try:
                # Try Oracle first
                rows = await self._schema_tools.execute_query(
                    "TMPL_FETCH_SOURCE",
                    {"schema": schema, "object_name": fn_name},
                )

                if rows:
                    source_lines = [{"line": r[0], "text": r[1]} for r in rows]
            except Exception as exc:
                logger.warning(f"Oracle fetch failed for {fn_name}: {exc}")

            # Fallback to disk
            if not source_lines:
                filepath = _scan_modules_for_file(fn_name)
                if filepath:
                    source_lines = _read_sql_file(filepath)

            if not source_lines:
                tree[fn_name] = {"status": "not_found", "depth": depth}
                continue

            source_text = "".join(
                line["text"] if isinstance(line, dict) else str(line)
                for line in source_lines
            )
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

        return tree
