"""
Schema discovery — single source of truth for "what schemas does RTIE know about?"

Phase 1 introduces this module so callers stop hardcoding
``("OFSMDM", "OFSERM")`` tuples (e.g. ``orchestrator._PRECHECK_SCHEMAS``).
Discovery sources, in order:

1. **Redis SCAN** over ``graph:*`` keys, decoded via
   :meth:`SchemaAwareKeyspace.parse_graph_key`. The schemas seen become the
   authoritative list — this is what the loader populated last time it ran,
   so it accurately reflects the live state.
2. **Manifest fallback** to :data:`src.parsing.manifest.RECOGNIZED_SCHEMAS`
   when Redis is unavailable, errors mid-scan, or hasn't been populated yet
   (clean container). Keeps the constant load-bearing during cold-start
   while still letting Redis be authoritative once the loader has run.

This module does NOT cache results — Redis can change underneath us
(loader re-run, migration, schema added). Callers that need a stable list
within one request scope should snapshot the return value once.
"""

from __future__ import annotations

from typing import Optional

from src.parsing.keyspace import SchemaAwareKeyspace
from src.parsing.manifest import RECOGNIZED_SCHEMAS
from src.parsing.store import (
    get_column_index,
    get_function_graph,
    get_raw_source,
)
from src.logger import get_logger

logger = get_logger(__name__)


def discovered_schemas(redis_client) -> list[str]:
    """Return the sorted list of schemas RTIE currently knows about.

    Iterates ``graph:*`` keys in Redis and collects the schema segment from
    each per-function key (family keys like ``graph:meta:*``,
    ``graph:full:*`` are filtered out by
    :meth:`SchemaAwareKeyspace.parse_graph_key`).

    Falls back to :data:`RECOGNIZED_SCHEMAS` (sorted) when ``redis_client``
    is None, the SCAN fails, or Redis is empty (no per-function keys yet).

    The returned list is always non-empty and deterministically sorted.
    """
    if redis_client is None:
        logger.warning(
            "discovered_schemas: redis_client is None; falling back to "
            "manifest RECOGNIZED_SCHEMAS"
        )
        return sorted(RECOGNIZED_SCHEMAS)

    schemas: set[str] = set()
    try:
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(
                cursor=cursor, match="graph:*", count=500
            )
            for raw in keys:
                key = (
                    raw.decode("utf-8", errors="ignore")
                    if isinstance(raw, (bytes, bytearray))
                    else str(raw)
                )
                parsed = SchemaAwareKeyspace.parse_graph_key(key)
                if parsed is not None:
                    schemas.add(parsed[0])
            if cursor == 0:
                break
    except Exception as exc:
        logger.warning(
            "discovered_schemas: Redis scan failed (%s); falling back to "
            "manifest RECOGNIZED_SCHEMAS",
            exc,
        )
        return sorted(RECOGNIZED_SCHEMAS)

    if not schemas:
        # Loader hasn't populated anything yet (clean Redis) — use the
        # bootstrap list so callers don't iterate an empty set.
        return sorted(RECOGNIZED_SCHEMAS)

    return sorted(schemas)


def schema_for_function(
    function_name: str, redis_client, schemas: Optional[list[str]] = None
) -> Optional[str]:
    """Return the schema that owns *function_name*, or ``None`` if not found.

    Probes ``graph:<schema>:<FN_UPPER>`` for each schema in *schemas*
    (default: :func:`discovered_schemas`). Returns the first schema with a
    hit. Case-insensitive on *function_name* — uses
    :meth:`SchemaAwareKeyspace.normalize_function_name` to canonicalize.

    *schemas* lets callers pass an explicit list if they've already
    snapshotted ``discovered_schemas()`` for the current request — saves a
    second SCAN.
    """
    if not function_name or redis_client is None:
        return None

    try:
        fn_upper = SchemaAwareKeyspace.normalize_function_name(function_name)
    except ValueError:
        return None

    if schemas is None:
        schemas = discovered_schemas(redis_client)

    for schema in schemas:
        try:
            if get_function_graph(redis_client, schema, fn_upper) is not None:
                return schema
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# Phase 1 explicit-fallback helper
# ---------------------------------------------------------------------------

# Default schema used when no schema can be resolved from state. Set to
# OFSMDM to match the historical hardcoded fallback. Phase 4 will replace
# every fallback site with a proper schema_for_function() resolution; this
# constant exists so the fallback is grep-able and overridable in tests.
DEFAULT_FALLBACK_SCHEMA: str = "OFSMDM"


def fallback_to_default_schema(callsite: str, correlation_id: str = "") -> str:
    """Phase 1 explicit-fallback for sites that historically said
    ``state.get("schema") or "OFSMDM"``.

    Returns :data:`DEFAULT_FALLBACK_SCHEMA` and emits a single ``WARNING``
    log line identifying *callsite*. The intent is observability: every
    place where RTIE silently defaulted a missing schema now emits a
    greppable signal, so when Phase 4 audits these sites the corpus of
    cases is visible in the logs.

    Used as the right-hand side of an ``or`` in fallback-site code so the
    warning fires only when the upstream value is falsy:

        schema = state.get("schema") or fallback_to_default_schema(
            "main.semantic_search", correlation_id
        )

    Phase 4 will replace each call site with proper resolution (probably
    via :func:`schema_for_function`) and then this helper can be removed.
    """
    logger.warning(
        "schema not resolved upstream; falling back to %s at %s "
        "(correlation_id=%s). Phase 4 will replace this with "
        "schema_for_function() resolution.",
        DEFAULT_FALLBACK_SCHEMA,
        callsite,
        correlation_id or "?",
    )
    return DEFAULT_FALLBACK_SCHEMA


# ---------------------------------------------------------------------------
# Phase 4 multi-schema lookup helpers
# ---------------------------------------------------------------------------


def schemas_for_table(
    table_name: str,
    redis_client,
    schemas: Optional[list[str]] = None,
) -> list[str]:
    """Return the schemas whose parsed graph references *table_name*.

    A table "lives in" a schema when at least one function in that schema
    has a node with the table as its target_table or source_tables entry.
    Used by DATA_QUERY routing to decide which schema to qualify the SQL
    with when the user names a table that exists in OFSERM but not OFSMDM
    (or vice versa).

    Returns an empty list when *table_name* is missing from every
    discovered schema, when Redis is unreachable, or when the table token
    is empty/None. Lookup is case-insensitive on the table name.

    *schemas* lets callers reuse a snapshotted discovery list — saves a
    repeat SCAN when this is called from a code path that already
    enumerated schemas.
    """
    if not table_name or redis_client is None:
        return []

    table_upper = table_name.strip().upper()
    if not table_upper:
        return []

    if schemas is None:
        schemas = discovered_schemas(redis_client)

    matched: list[str] = []
    for schema in schemas:
        try:
            keys = redis_client.keys(
                SchemaAwareKeyspace.graph_scan_pattern(schema)
            ) or []
        except Exception as exc:
            logger.warning(
                "schemas_for_table: keys() failed for %s: %s", schema, exc
            )
            continue
        found_in_schema = False
        for raw_key in keys:
            key = (
                raw_key.decode("utf-8", errors="ignore")
                if isinstance(raw_key, (bytes, bytearray))
                else str(raw_key)
            )
            parsed = SchemaAwareKeyspace.parse_graph_key(key)
            if parsed is None or parsed[0] != schema:
                continue
            try:
                graph = get_function_graph(redis_client, schema, parsed[1])
            except Exception:
                continue
            if not graph:
                continue
            for node in graph.get("nodes", []) or []:
                target = (node.get("target_table") or "").strip().upper()
                if target == table_upper:
                    found_in_schema = True
                    break
                sources = node.get("source_tables") or []
                if any(
                    isinstance(s, str) and s.strip().upper() == table_upper
                    for s in sources
                ):
                    found_in_schema = True
                    break
                for arm in node.get("union_arms", []) or []:
                    arm_target = (arm.get("target_table") or "").strip().upper()
                    if arm_target == table_upper:
                        found_in_schema = True
                        break
                if found_in_schema:
                    break
            if found_in_schema:
                break
        if found_in_schema:
            matched.append(schema)
    return matched


def schemas_for_column(
    column_name: str,
    redis_client,
    schemas: Optional[list[str]] = None,
) -> list[str]:
    """Return the schemas whose ``graph:index:<schema>`` lists *column_name*.

    Resolves a bare column to the schemas that own functions touching it.
    Used by VARIABLE_TRACE routing to scope the column-index lookup to the
    right schema before running the existing graph traversal.

    Returns an empty list when the column is absent from every discovered
    schema's column index. Lookup is case-insensitive on the column name.
    """
    if not column_name or redis_client is None:
        return []

    col_upper = column_name.strip().upper()
    if not col_upper:
        return []

    if schemas is None:
        schemas = discovered_schemas(redis_client)

    matched: list[str] = []
    for schema in schemas:
        try:
            col_index = get_column_index(redis_client, schema)
        except Exception as exc:
            logger.warning(
                "schemas_for_column: index read failed for %s: %s", schema, exc
            )
            continue
        if col_index and col_upper in col_index and col_index[col_upper]:
            matched.append(schema)
    return matched


def identifier_grounded_in_any_schema(
    identifier: str,
    redis_client,
    schemas: Optional[list[str]] = None,
) -> bool:
    """Return True when *identifier* appears in any function's source body
    in any discovered schema.

    Used by the W45 detector as a multi-schema backstop — only flag an
    identifier as ungrounded when it is truly absent from every loaded
    function across every schema, not just the small batch of candidates
    semantic search returned for this query. Lookup is case-insensitive
    and matches a substring (so e.g. ``CAP943`` matches a WHERE-clause
    literal embedded in any function).

    Returns False when Redis is unreachable, when no source bodies are
    cached, or when *identifier* is empty.
    """
    if not identifier or redis_client is None:
        return False

    needle = identifier.strip().upper()
    if not needle:
        return False

    if schemas is None:
        schemas = discovered_schemas(redis_client)

    for schema in schemas:
        try:
            keys = redis_client.keys(f"graph:source:{schema}:*") or []
        except Exception as exc:
            logger.warning(
                "identifier_grounded_in_any_schema: keys() failed for %s: %s",
                schema, exc,
            )
            continue
        for raw_key in keys:
            key = (
                raw_key.decode("utf-8", errors="ignore")
                if isinstance(raw_key, (bytes, bytearray))
                else str(raw_key)
            )
            # Expect ``graph:source:<schema>:<fn>``; skip anything else.
            parts = key.split(":")
            if len(parts) != 4 or parts[0] != "graph" or parts[1] != "source":
                continue
            if parts[2] != schema:
                continue
            try:
                lines = get_raw_source(redis_client, schema, parts[3])
            except Exception:
                continue
            if not lines:
                continue
            for line in lines:
                text = line if isinstance(line, str) else str(line)
                if needle in text.upper():
                    return True
    return False
