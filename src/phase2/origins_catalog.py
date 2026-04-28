"""
Auto-derived catalog of data origins and override patterns.
Built at startup by scanning the graph in Redis -- NOT hardcoded.
Automatically adapts when new batches or modules are added.

Phase 2 makes the catalog **per-schema**: ``OriginsCatalog`` is built
once per schema discovered in Redis (OFSMDM, OFSERM, ...) and the
per-schema instances are kept in the module-level ``_catalogs`` dict.
The builder also persists a snapshot of each catalog under
``graph:origins:<schema>:<facet>`` keys so the populated state is
inspectable from ``redis-cli`` without a Python REPL.

Reader functions (``classify_origin``, ``is_gl_blocked``,
``get_eop_override``) accept an optional ``schema`` argument. When
omitted they iterate all built catalogs — the historical behaviour is
preserved because OFSMDM was the only schema populated pre-Phase-2.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from src.parsing.keyspace import SchemaAwareKeyspace
from src.parsing.schema_discovery import discovered_schemas
from src.parsing.serializer import to_msgpack
from src.parsing.store import get_function_graph
from src.logger import get_logger

logger = get_logger(__name__)


_QUOTED_LITERAL_RE = re.compile(r"'([^']*)'")
_IN_LIST_RE = re.compile(
    r"\bV_GL_CODE\s+IN\s*\((.*?)\)",
    re.IGNORECASE | re.DOTALL,
)
_CASE_BRANCH_IN_RE = re.compile(
    r"V_GL_CODE\s+IN\s*\((.*?)\)",
    re.IGNORECASE | re.DOTALL,
)


class CatalogBuildError(RuntimeError):
    """Raised when a catalog build does not produce a usable catalog.

    Either the extraction raised mid-scan, or the post-build completeness
    validation failed. Either way, the per-schema entry in ``_catalogs``
    is NOT updated when this is raised.
    """


class OriginsCatalog:
    """
    Extracts origin patterns from the graph at startup, scoped to a
    single Oracle schema.

    Rebuilds automatically whenever the graph is refreshed.

    No hardcoded V_DATA_ORIGIN values.
    No hardcoded GL codes.
    No hardcoded function names.

    Everything derived from the parsed graph in Redis.
    """

    def __init__(self, redis_client, schema: str):
        self.redis = redis_client
        self.schema = schema

        self.plsql_origins: dict = {}
        self.etl_origins: dict = {}
        self.gl_block_list: set = set()
        self.gl_eop_overrides: dict = {}
        self.known_functions: set = set()

    # -----------------------------------------------------------------
    # Entry point
    # -----------------------------------------------------------------

    def build(self) -> dict:
        """Scan every function graph in Redis and populate the catalog.

        On success, returns a summary dict of the populated counts. On any
        extraction failure or completeness check failure, raises
        ``CatalogBuildError`` (or lets a lower-level exception propagate) so
        the caller can refuse to swap the per-schema entry in ``_catalogs``.
        """
        pattern = SchemaAwareKeyspace.graph_scan_pattern(self.schema)
        raw_keys = self.redis.keys(pattern) or []

        # Enumerate the expected function names from Redis keys first, so
        # we can validate afterwards that every function got processed.
        # SchemaAwareKeyspace.parse_graph_key returns None for family keys
        # (graph:meta:*, graph:full:*, graph:source:*, ...) — they are
        # filtered out automatically.
        expected_functions: set[str] = set()
        for raw_key in raw_keys:
            key = (
                raw_key.decode("utf-8", errors="ignore")
                if isinstance(raw_key, (bytes, bytearray))
                else str(raw_key)
            )
            parsed = SchemaAwareKeyspace.parse_graph_key(key)
            if parsed is None or parsed[0] != self.schema:
                continue
            expected_functions.add(parsed[1])

        for function_name in expected_functions:
            graph = get_function_graph(self.redis, self.schema, function_name)
            if not graph:
                continue

            self.known_functions.add(function_name)

            self.plsql_origins.update(
                self.extract_plsql_origins_from_graph(graph, function_name)
            )
            self.gl_block_list.update(
                self.extract_gl_block_list_from_graph(graph)
            )
            self.gl_eop_overrides.update(
                self.extract_eop_overrides_from_graph(graph, function_name)
            )

        self.etl_origins = {
            code: dict(info) for code, info in BOOTSTRAP_ETL_ORIGINS.items()
        }

        self._validate_completeness(expected_functions)

        return {
            "plsql_origin_count": len(self.plsql_origins),
            "etl_origin_count": len(self.etl_origins),
            "gl_block_count": len(self.gl_block_list),
            "eop_override_count": len(self.gl_eop_overrides),
            "function_count": len(self.known_functions),
        }

    def _validate_completeness(self, expected_functions: set[str]) -> None:
        """Fail loudly if the built catalog is not fit to serve requests.

        Checks:
          * Every key in ``BOOTSTRAP_ETL_ORIGINS`` must appear in
            ``etl_origins``. Missing entries mean the bootstrap seeding
            did not run — classify_origin would silently miss T24 etc.
          * ``known_functions`` must equal the set of function-graph keys
            present in Redis. A mismatch means one or more graphs failed
            to load mid-scan.
          * If any functions are known, ``plsql_origins`` must be non-empty.
            A populated function set with zero V_DATA_ORIGIN literals is
            a bug in extraction, not a legitimate empty state. **Skipped
            for OFSERM and other secondary schemas** — those corpora carry
            CAP-style override merges rather than V_DATA_ORIGIN literals,
            so an empty plsql_origins set there is expected (Phase 0
            diagnostic Section 2.5 issue #1). The check still fires for
            OFSMDM, where a regression in extraction would be silent.
        """
        missing_bootstrap = [
            code for code in BOOTSTRAP_ETL_ORIGINS.keys()
            if code not in self.etl_origins
        ]
        if missing_bootstrap:
            raise CatalogBuildError(
                "OriginsCatalog completeness check failed: bootstrap ETL "
                f"origins missing from etl_origins: {sorted(missing_bootstrap)}. "
                "The bootstrap seeding in build() did not run — the catalog "
                "would silently misclassify ETL-origin rows."
            )

        missing_functions = sorted(expected_functions - self.known_functions)
        if missing_functions:
            raise CatalogBuildError(
                "OriginsCatalog completeness check failed: "
                f"{len(missing_functions)} function graph(s) present in "
                f"Redis but not processed by build(): {missing_functions}. "
                "The scan loop terminated early."
            )

        if (
            self.schema == _PRIMARY_SCHEMA
            and self.known_functions
            and not self.plsql_origins
        ):
            raise CatalogBuildError(
                f"OriginsCatalog completeness check failed for "
                f"{self.schema}: {len(self.known_functions)} function "
                "graph(s) processed but zero PL/SQL origins extracted. "
                "The extraction logic is broken — classify_origin would "
                "misclassify every PL/SQL-produced row as UNKNOWN."
            )

    # -----------------------------------------------------------------
    # Redis persistence (Phase 2)
    # -----------------------------------------------------------------

    def to_redis(self, redis_client) -> dict[str, int]:
        """Persist the built catalog to Redis under
        ``graph:origins:<schema>:<facet>`` keys.

        Each facet (plsql, etl, gl_blocked, eop_overrides, meta) is stored
        as a separate msgpack-encoded key so an operator can spot-check any
        slice from ``redis-cli`` (``GET graph:origins:OFSERM:plsql`` etc.).
        Returns the byte counts written, useful for log lines and tests.

        Writes are best-effort: a Redis exception is logged and re-raised
        so the caller can refuse to register the catalog when persistence
        fails. The in-memory catalog is unchanged either way.
        """
        if redis_client is None:
            return {}

        plsql_key = SchemaAwareKeyspace.origins_key(self.schema, "plsql")
        etl_key = SchemaAwareKeyspace.origins_key(self.schema, "etl")
        gl_blocked_key = SchemaAwareKeyspace.origins_key(self.schema, "gl_blocked")
        eop_key = SchemaAwareKeyspace.origins_key(self.schema, "eop_overrides")
        meta_key = SchemaAwareKeyspace.origins_key(self.schema, "meta")

        plsql_payload = to_msgpack(dict(self.plsql_origins))
        etl_payload = to_msgpack(dict(self.etl_origins))
        gl_blocked_payload = to_msgpack(sorted(self.gl_block_list))
        eop_payload = to_msgpack(dict(self.gl_eop_overrides))
        meta_payload = to_msgpack({
            "schema": self.schema,
            "plsql_origin_count": len(self.plsql_origins),
            "etl_origin_count": len(self.etl_origins),
            "gl_block_count": len(self.gl_block_list),
            "eop_override_count": len(self.gl_eop_overrides),
            "function_count": len(self.known_functions),
        })

        redis_client.set(plsql_key, plsql_payload)
        redis_client.set(etl_key, etl_payload)
        redis_client.set(gl_blocked_key, gl_blocked_payload)
        redis_client.set(eop_key, eop_payload)
        redis_client.set(meta_key, meta_payload)

        return {
            plsql_key: len(plsql_payload),
            etl_key: len(etl_payload),
            gl_blocked_key: len(gl_blocked_payload),
            eop_key: len(eop_payload),
            meta_key: len(meta_payload),
        }

    # -----------------------------------------------------------------
    # PLSQL origin extraction
    # -----------------------------------------------------------------

    def extract_plsql_origins_from_graph(
        self, graph: dict, function_name: str
    ) -> dict:
        """Collect every V_DATA_ORIGIN literal produced inside this function."""
        result: dict = {}

        for node in graph.get("nodes", []) or []:
            node_type = (node.get("type") or "").upper()
            if node_type not in ("INSERT", "UPDATE", "MERGE"):
                continue

            target_table = node.get("target_table", "") or ""
            node_id = f"{function_name}:{node.get('id', '')}"

            for expr, origin in _iter_node_value_expressions(node):
                core = _strip_trailing_alias(expr, "V_DATA_ORIGIN")
                if core is None:
                    # Also consider structured calculations tagged with column
                    if isinstance(origin, dict) and (
                        (origin.get("column") or "").upper() == "V_DATA_ORIGIN"
                    ):
                        core = origin.get("expression", "") or ""
                    else:
                        continue
                for literal in _extract_origin_literals(core):
                    result.setdefault(literal, {
                        "function": function_name,
                        "node_id": node_id,
                        "target_table": target_table,
                        "description": (
                            f"V_DATA_ORIGIN literal in {node_id}"
                        ),
                    })
        return result

    # -----------------------------------------------------------------
    # GL block list extraction
    # -----------------------------------------------------------------

    def extract_gl_block_list_from_graph(self, graph: dict) -> set:
        """Collect GL codes forced to F_EXPOSURE_ENABLED_IND = 'N'."""
        codes: set = set()
        for node in graph.get("nodes", []) or []:
            node_type = (node.get("type") or "").upper()
            if node_type not in ("UPDATE", "MERGE", "INSERT"):
                continue

            # Pattern A: simple SET F_EXPOSURE_ENABLED_IND = 'N' + WHERE V_GL_CODE IN (...)
            expr = _get_column_expression(node, "F_EXPOSURE_ENABLED_IND")
            if expr is not None and _extract_quoted_literal(expr) == "N":
                for cond in node.get("conditions", []) or []:
                    cond_text = cond if isinstance(cond, str) else cond.get("expression", "")
                    for literal in _extract_in_list_literals(cond_text):
                        codes.add(literal)

            # Pattern B: CONDITIONAL calculation whose branch sets it to 'N'
            for calc in node.get("calculation", []) or []:
                if not isinstance(calc, dict):
                    continue
                if (calc.get("column") or "").upper() != "F_EXPOSURE_ENABLED_IND":
                    continue
                if (calc.get("type") or "").upper() != "CONDITIONAL":
                    continue
                for branch in calc.get("branches", []) or []:
                    if _extract_quoted_literal(branch.get("then", "")) != "N":
                        continue
                    when_text = str(branch.get("when", ""))
                    for literal in _extract_in_list_literals(when_text):
                        codes.add(literal)
        return codes

    # -----------------------------------------------------------------
    # EOP override extraction
    # -----------------------------------------------------------------

    def extract_eop_overrides_from_graph(
        self, graph: dict, function_name: str
    ) -> dict:
        """Collect GL codes whose balance override forces the result to 0.

        Detects DECODE(V_GL_CODE, 'CODE', 0, ...) patterns that produce a
        zero balance. Handles both single-column and composite-key forms.
        """
        result: dict = {}
        for node in graph.get("nodes", []) or []:
            node_id = f"{function_name}:{node.get('id', '')}"

            for expr, origin in _iter_node_value_expressions(node):
                line = (
                    origin.get("line") if isinstance(origin, dict) else None
                ) or node.get("line_start")
                for gl_code, meta in _find_zero_gl_overrides(expr):
                    result.setdefault(gl_code, {
                        "function": function_name,
                        "node_id": node_id,
                        "line": line,
                        "reason": meta,
                    })
        return result


# ---------------------------------------------------------------------
# Module-level catalog registry (populated at startup)
# ---------------------------------------------------------------------

# Primary schema. Used only to scope the strict "PL/SQL origins must be
# non-empty if any function was processed" completeness check, which is
# OFSMDM-specific (see :meth:`OriginsCatalog._validate_completeness`).
# Phase 2 keeps this at OFSMDM to preserve the existing safety net; later
# phases may revisit if a different schema becomes the V_DATA_ORIGIN
# producer.
_PRIMARY_SCHEMA: str = "OFSMDM"

# Per-schema registry. Replaces the Phase-1 module global ``_catalog``.
# Key: schema name (e.g. "OFSMDM"). Value: built ``OriginsCatalog``.
_catalogs: dict[str, OriginsCatalog] = {}


def get_catalog(schema: Optional[str] = None) -> OriginsCatalog:
    """Return a built catalog.

    With *schema*, returns the catalog for that schema. Without, returns
    an arbitrary built catalog — used by callers that don't yet know
    which schema they're operating against; preserved for backwards
    compatibility with the Phase-1 single-schema API.

    Raises ``RuntimeError`` if no catalog has been built (or if *schema*
    is given but its catalog is missing). Lookup-style callers should
    prefer the public ``classify_origin`` / ``is_gl_blocked`` /
    ``get_eop_override`` helpers, which gracefully fall back across
    schemas.
    """
    if not _catalogs:
        raise RuntimeError(
            "OriginsCatalog not built -- "
            "call build_catalog() during application startup"
        )
    if schema is not None:
        catalog = _catalogs.get(schema)
        if catalog is None:
            raise RuntimeError(
                f"OriginsCatalog not built for schema {schema!r}; "
                f"available: {sorted(_catalogs.keys())}"
            )
        return catalog
    # No schema requested: return any built catalog. Insertion order is
    # deterministic in CPython 3.7+, so the first-built schema wins.
    return next(iter(_catalogs.values()))


def get_known_schemas() -> list[str]:
    """Return the sorted list of schemas that have a built catalog."""
    return sorted(_catalogs.keys())


def build_catalog(
    redis_client,
    schema: Optional[str] = None,
) -> OriginsCatalog | dict[str, OriginsCatalog]:
    """Build the catalog by scanning the graph. Call once at startup.

    With *schema*, builds only that schema's catalog and returns the
    single ``OriginsCatalog``. Without *schema*, iterates every schema
    discovered in Redis and builds one catalog per schema, returning the
    full ``{schema: OriginsCatalog}`` dict.

    The per-schema entry in ``_catalogs`` is updated ATOMICALLY: each
    catalog is built into a local variable, and only assigned to the
    registry after a successful build + completeness validation. On any
    failure for a given schema:

    * If this is the first build of that schema (no prior entry),
      ``_catalogs`` is left without an entry and ``get_catalog(schema)``
      raises RuntimeError.
    * If a previous build succeeded, the previous entry is preserved —
      a failing refresh does not degrade a running system.

    A failing schema does NOT abort the iteration over other schemas:
    OFSMDM continuing to work when OFSERM has a transient outage is the
    explicit goal. Failures are logged and the exception is re-raised at
    the end so the caller can decide whether to treat it as fatal.

    Each successfully built catalog is also written to Redis under
    ``graph:origins:<schema>:<facet>`` keys via :meth:`OriginsCatalog.to_redis`.
    Persistence failure is logged but does not invalidate the in-memory
    catalog — the registry is the source of truth, the keys are an
    observability snapshot.
    """
    if schema is not None:
        return _build_one_schema(redis_client, schema)

    schemas = discovered_schemas(redis_client)
    built: dict[str, OriginsCatalog] = {}
    failures: list[tuple[str, BaseException]] = []

    for sch in schemas:
        try:
            built[sch] = _build_one_schema(redis_client, sch)
        except Exception as exc:
            failures.append((sch, exc))
            logger.exception(
                "OriginsCatalog build failed for schema %s; "
                "previous catalog preserved (was %s)",
                sch,
                "absent" if sch not in _catalogs else "present",
            )

    if failures and not built:
        # Every schema failed -- propagate the first error so the caller
        # can refuse to start. The exception was already logged above.
        first_schema, first_exc = failures[0]
        raise CatalogBuildError(
            f"OriginsCatalog build failed for every discovered schema: "
            f"{[s for s, _ in failures]}"
        ) from first_exc

    if failures:
        logger.warning(
            "OriginsCatalog: %d/%d schemas failed (%s); continuing with "
            "%d successfully built schema(s): %s",
            len(failures),
            len(schemas),
            [s for s, _ in failures],
            len(built),
            sorted(built.keys()),
        )

    return built


def _build_one_schema(redis_client, schema: str) -> OriginsCatalog:
    """Build (or rebuild) the catalog for a single schema.

    On success, the per-schema entry in ``_catalogs`` is replaced and the
    catalog is persisted to Redis. On failure, the existing entry (if
    any) is preserved. See :func:`build_catalog` for the full contract.
    """
    new_catalog = OriginsCatalog(redis_client, schema)
    try:
        summary = new_catalog.build()
    except Exception:
        # Propagate to the caller; _catalogs[schema] is intentionally
        # untouched.
        logger.exception(
            "OriginsCatalog build failed for schema %s; existing "
            "catalog (if any) preserved",
            schema,
        )
        raise

    # Successful build — atomically swap in the new catalog BEFORE logging
    # the summary or persisting to Redis. The "built" log line is a
    # trustworthy signal that the live catalog is serving that catalog's
    # data; persistence is an observability snapshot, not the source of
    # truth.
    _catalogs[schema] = new_catalog

    try:
        new_catalog.to_redis(redis_client)
    except Exception as exc:
        # Log but do not fail: the in-memory catalog is the source of
        # truth, the Redis snapshot is for inspectability.
        logger.warning(
            "OriginsCatalog: failed to persist %s snapshot to Redis "
            "(non-fatal): %s",
            schema,
            exc,
        )

    logger.info(
        "OriginsCatalog.build summary for %s: "
        "plsql=%d etl=%d gl_blocked=%d eop_overrides=%d functions=%d",
        schema,
        summary["plsql_origin_count"],
        summary["etl_origin_count"],
        summary["gl_block_count"],
        summary["eop_override_count"],
        summary["function_count"],
    )
    return new_catalog


# ---------------------------------------------------------------------
# Public helper functions (same signatures as known_origins.py)
# ---------------------------------------------------------------------

def _iter_catalogs(schema: Optional[str]) -> Iterable[OriginsCatalog]:
    """Yield catalogs to consult for a public lookup.

    With *schema*, yields only that schema's catalog (or raises if it
    doesn't exist). Without, yields every built catalog in insertion
    order — primary schema first, then secondaries.
    """
    if not _catalogs:
        raise RuntimeError(
            "OriginsCatalog not built -- "
            "call build_catalog() during application startup"
        )
    if schema is not None:
        catalog = _catalogs.get(schema)
        if catalog is None:
            raise RuntimeError(
                f"OriginsCatalog not built for schema {schema!r}; "
                f"available: {sorted(_catalogs.keys())}"
            )
        yield catalog
        return
    yield from _catalogs.values()


def classify_origin(
    v_data_origin: str | None,
    schema: Optional[str] = None,
) -> dict:
    """Return PLSQL, ETL, or UNKNOWN classification for a V_DATA_ORIGIN.

    *schema* scopes the lookup to a single schema's catalog. When
    omitted, every built catalog is consulted in insertion order and the
    first hit wins — preserves the historical single-schema behaviour
    for callers that don't yet thread a schema through.
    """
    if v_data_origin is None:
        # Force registry presence even on the no-op path so a call before
        # build_catalog() is loud rather than silently UNKNOWN.
        list(_iter_catalogs(schema))
        return {"category": "UNKNOWN", "details": {}, "traceable": False}

    value = str(v_data_origin).strip()

    for catalog in _iter_catalogs(schema):
        if value in catalog.plsql_origins:
            details = catalog.plsql_origins[value]
            return {
                "category": "PLSQL",
                "details": details,
                "traceable": bool(details.get("node_id")),
            }
        if value in catalog.etl_origins:
            return {
                "category": "ETL",
                "details": catalog.etl_origins[value],
                "traceable": False,
            }

    return {
        "category": "UNKNOWN",
        "details": {"raw_value": value},
        "traceable": False,
    }


def is_gl_blocked(
    gl_code: str | None,
    schema: Optional[str] = None,
) -> bool:
    """Return True if the GL code is on any built catalog's block list.

    *schema* scopes the check to a single schema's catalog. When
    omitted, the GL is "blocked" if any built catalog has it on its
    block list — matches the earlier single-schema semantics in a
    multi-schema world.
    """
    if not gl_code:
        # Force registry presence even on the no-op path so a call before
        # build_catalog() is loud rather than silently False.
        list(_iter_catalogs(schema))
        return False
    normalized = str(gl_code).strip().upper()
    for catalog in _iter_catalogs(schema):
        if normalized in {code.upper() for code in catalog.gl_block_list}:
            return True
    return False


def get_eop_override(
    gl_code: str | None,
    schema: Optional[str] = None,
) -> dict | None:
    """Return the override record if any catalog forces this GL to 0.

    *schema* scopes the lookup to a single schema. When omitted, every
    built catalog is consulted and the first hit is returned.
    """
    if not gl_code:
        return None
    key = str(gl_code).strip()
    for catalog in _iter_catalogs(schema):
        record = catalog.gl_eop_overrides.get(key)
        if record is not None:
            return record
    return None


# ---------------------------------------------------------------------
# Bootstrap list: OFSAA universal ETL conventions
# ---------------------------------------------------------------------

BOOTSTRAP_ETL_ORIGINS: dict[str, dict] = {
    "OF": {
        "source": "OFSAA Data Foundation",
        "description": "Standard OFSAA ODF data loader",
        "fix_path": "Investigate ODF ETL job logs for the MIS date",
    },
    "T24": {
        "source": "T24 core banking",
        "description": "T24 ETL direct load",
        "fix_path": "Investigate T24 extract logs, verify GL position",
    },
    "IBG": {
        "source": "IBG branch system",
        "description": "Islamic Banking Group branch ETL",
        "fix_path": "Check IBG branch ETL extraction",
    },
    "CBS": {
        "source": "Core Banking System",
        "description": "Core Banking System feed",
        "fix_path": "Check CBS ETL pipeline",
    },
    "SWIFT": {
        "source": "SWIFT messaging",
        "description": "SWIFT interbank feed",
        "fix_path": "Check SWIFT ETL pipeline",
    },
}


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------

def _get_column_expression(node: dict, column_name: str) -> str | None:
    """Return the expression assigned to *column_name* in the node, or None."""
    col_maps = node.get("column_maps") or {}
    return _get_mapping_expression(col_maps, column_name)


def _get_mapping_expression(col_maps: dict, column_name: str) -> str | None:
    target = column_name.upper()
    if not isinstance(col_maps, dict):
        return None

    if "mapping" in col_maps:
        mapping = col_maps.get("mapping") or {}
        for col, expr in mapping.items():
            if isinstance(col, str) and col.upper() == target:
                return expr if isinstance(expr, str) else None

    if "assignments" in col_maps:
        for col, expr in col_maps.get("assignments") or []:
            if isinstance(col, str) and col.upper() == target:
                return expr if isinstance(expr, str) else None

    # Flat dict fallback
    for col, expr in col_maps.items():
        if col in ("mapping", "assignments", "columns", "values"):
            continue
        if isinstance(col, str) and col.upper() == target and isinstance(expr, str):
            return expr
    return None


def _extract_quoted_literal(expression: Any) -> str | None:
    """Return the single-quoted literal if *expression* is exactly one."""
    if not isinstance(expression, str):
        return None
    stripped = expression.strip()
    if not stripped:
        return None
    # Accept both "'VALUE'" and unquoted VALUE? No -- must be quoted literal.
    m = re.fullmatch(r"'([^']*)'", stripped)
    if m:
        return m.group(1)
    return None


def _extract_in_list_literals(text: str) -> list[str]:
    """Extract quoted literals from a `V_GL_CODE IN (...)` fragment."""
    if not text:
        return []
    match = _IN_LIST_RE.search(text)
    if not match:
        return []
    inside = match.group(1)
    return _QUOTED_LITERAL_RE.findall(inside)


def _is_zero_literal(value: Any) -> bool:
    if value is None:
        return False
    s = str(value).strip().strip("'").strip('"').strip()
    if not s:
        return False
    try:
        return float(s) == 0.0
    except ValueError:
        return False


def _strip_quotes(value: str) -> str:
    s = value.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


# ---------------------------------------------------------------------
# Expression iteration + parsing helpers
# ---------------------------------------------------------------------

_TRAILING_ALIAS_RE = re.compile(r"(?:\s+AS)?\s+([A-Za-z_]\w*)\s*$", re.IGNORECASE)
_CASE_WHEN_THEN_RE = re.compile(
    r"\bTHEN\b\s*((?:'[^']*')|\w+)", re.IGNORECASE
)
_DECODE_OPEN_RE = re.compile(r"\bDECODE\s*\(", re.IGNORECASE)


def _iter_node_value_expressions(node: dict):
    """Yield `(expression_string, origin_hint)` tuples for every SQL value
    expression carried by *node*, including UNION arms and calculations."""
    col_maps = node.get("column_maps") or {}
    for item in _iter_column_maps_values(col_maps):
        yield item
    for calc in node.get("calculation", []) or []:
        if isinstance(calc, dict):
            expr = calc.get("expression")
            if isinstance(expr, str) and expr:
                yield (expr, calc)
    for arm in node.get("union_arms", []) or []:
        arm_maps = arm.get("column_maps") or {}
        for item in _iter_column_maps_values(arm_maps):
            yield item
        for calc in arm.get("calculations", []) or []:
            if isinstance(calc, dict):
                expr = calc.get("expression")
                if isinstance(expr, str) and expr:
                    yield (expr, calc)


def _iter_column_maps_values(col_maps: dict):
    if not isinstance(col_maps, dict):
        return
    for v in col_maps.get("values", []) or []:
        if isinstance(v, str) and v:
            yield (v, None)
    mapping = col_maps.get("mapping") or {}
    if isinstance(mapping, dict):
        for col, expr in mapping.items():
            if isinstance(expr, str) and expr:
                yield (expr, {"column": col, "expression": expr})
    for col, expr in col_maps.get("assignments") or []:
        if isinstance(expr, str) and expr:
            yield (expr, {"column": col, "expression": expr})


def _strip_trailing_alias(expression: str, target_column: str) -> str | None:
    """If *expression* ends with an alias matching *target_column* (optionally
    prefixed with AS), return the expression with the alias stripped.
    Otherwise return None.
    """
    if not isinstance(expression, str):
        return None
    stripped = expression.rstrip().rstrip(",").rstrip()
    m = _TRAILING_ALIAS_RE.search(stripped)
    if not m:
        return None
    if m.group(1).upper() != target_column.upper():
        return None
    return stripped[: m.start()].rstrip()


def _extract_origin_literals(expression: str) -> list[str]:
    """Extract target literals from an expression that produces a column value.

    Handles direct `'LIT'`, CASE WHEN ... THEN 'LIT', and DECODE(...) results.
    """
    if not isinstance(expression, str) or not expression.strip():
        return []
    expr = expression.strip()
    expr = re.sub(r"^\(\s*", "", expr)
    expr = re.sub(r"\s*\)$", "", expr)

    literals: list[str] = []

    direct = _extract_quoted_literal(expr)
    if direct is not None:
        return [direct]

    upper = expr.upper()
    if "CASE" in upper:
        for m in _CASE_WHEN_THEN_RE.finditer(expr):
            token = m.group(1).strip()
            lit = _extract_quoted_literal(token)
            if lit is not None:
                literals.append(lit)
        else_m = re.search(r"\bELSE\b\s*((?:'[^']*')|\w+)", expr, re.IGNORECASE)
        if else_m:
            lit = _extract_quoted_literal(else_m.group(1).strip())
            if lit is not None:
                literals.append(lit)

    for decode_inner in _iter_decode_inner_contents(expr):
        literals.extend(_extract_decode_result_literals(decode_inner))

    # Deduplicate while preserving order
    seen: set = set()
    deduped: list[str] = []
    for lit in literals:
        if lit not in seen:
            seen.add(lit)
            deduped.append(lit)
    return deduped


def _find_zero_gl_overrides(expression: str) -> list[tuple[str, str]]:
    """Find DECODE(...) overrides in *expression* whose result is 0 and whose
    decoded expression references V_GL_CODE. Returns (gl_code, reason) tuples.
    """
    if not isinstance(expression, str) or not expression.strip():
        return []

    results: list[tuple[str, str]] = []
    for decode_inner in _iter_decode_inner_contents(expression):
        parts = _split_top_level(decode_inner, ",")
        if len(parts) < 3:
            continue
        decode_expr = parts[0].strip()
        if "V_GL_CODE" not in decode_expr.upper():
            continue
        is_composite = "||" in decode_expr
        reason_kind = (
            "Composite-key N_EOP_BAL zero-override"
            if is_composite
            else "Single-column N_EOP_BAL zero-override"
        )
        i = 1
        while i + 1 < len(parts):
            search = parts[i].strip()
            result = parts[i + 1].strip()
            if _is_zero_literal(result):
                lit = _strip_quotes(search)
                if lit:
                    results.append((lit, f"{reason_kind} via {decode_expr}"))
            i += 2
    return results


def _iter_decode_inner_contents(expression: str):
    """Yield the contents inside each DECODE(...) in *expression*."""
    for m in _DECODE_OPEN_RE.finditer(expression):
        start = m.end()
        depth = 1
        i = start
        while i < len(expression) and depth > 0:
            if expression[i] == "(":
                depth += 1
            elif expression[i] == ")":
                depth -= 1
            i += 1
        if depth == 0:
            yield expression[start: i - 1]


def _extract_decode_result_literals(decode_inner: str) -> list[str]:
    parts = _split_top_level(decode_inner, ",")
    if len(parts) < 3:
        return []
    literals: list[str] = []
    i = 1
    while i + 1 < len(parts):
        result = parts[i + 1].strip()
        lit = _extract_quoted_literal(result)
        if lit is not None:
            literals.append(lit)
        i += 2
    # Trailing default argument
    if i < len(parts):
        lit = _extract_quoted_literal(parts[i].strip())
        if lit is not None:
            literals.append(lit)
    return literals


def _split_top_level(text: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    in_quote = False
    quote_char = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if in_quote:
            current.append(ch)
            if ch == quote_char:
                in_quote = False
        elif ch in ("'", '"'):
            in_quote = True
            quote_char = ch
            current.append(ch)
        elif ch == "(":
            depth += 1
            current.append(ch)
        elif ch == ")":
            depth -= 1
            current.append(ch)
        elif depth == 0 and text[i: i + len(delimiter)] == delimiter:
            parts.append("".join(current))
            current = []
            i += len(delimiter)
            continue
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts
