"""
Auto-derived catalog of data origins and override patterns.
Built at startup by scanning the graph in Redis -- NOT hardcoded.
Automatically adapts when new batches or modules are added.
"""

from __future__ import annotations

import re
from typing import Any

from src.parsing.keyspace import SchemaAwareKeyspace
from src.parsing.store import get_function_graph
from src.logger import get_logger

logger = get_logger(__name__)


_FUNCTION_GRAPH_SUBKEYS = frozenset({"full", "index", "aliases", "source", "meta"})
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
    validation failed. Either way, the module-level ``_catalog`` is NOT
    updated when this is raised.
    """


class OriginsCatalog:
    """
    Extracts origin patterns from the graph at startup.
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
        the caller can refuse to swap the module global.
        """
        pattern = SchemaAwareKeyspace.graph_scan_pattern(self.schema)
        raw_keys = self.redis.keys(pattern) or []

        # Enumerate the expected function names from Redis keys first, so
        # we can validate afterwards that every function got processed.
        expected_functions: set[str] = set()
        for raw_key in raw_keys:
            key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
            parts = key.split(":")
            if len(parts) < 3 or parts[1] in _FUNCTION_GRAPH_SUBKEYS:
                continue
            expected_functions.add(parts[2])

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
            Twelve functions with zero V_DATA_ORIGIN literals is a bug in
            extraction, not a legitimate empty state.
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

        if self.known_functions and not self.plsql_origins:
            raise CatalogBuildError(
                "OriginsCatalog completeness check failed: "
                f"{len(self.known_functions)} function graph(s) processed "
                "but zero PL/SQL origins extracted. The extraction logic "
                "is broken — classify_origin would misclassify every "
                "PL/SQL-produced row as UNKNOWN."
            )

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
# Module-level catalog instance (populated at startup)
# ---------------------------------------------------------------------

_catalog: OriginsCatalog | None = None


def get_catalog() -> OriginsCatalog:
    """Return the module-level catalog (must be built first)."""
    if _catalog is None:
        raise RuntimeError(
            "OriginsCatalog not built -- "
            "call build_catalog() during application startup"
        )
    return _catalog


def build_catalog(redis_client, schema: str) -> OriginsCatalog:
    """Build the catalog by scanning the graph. Call once at startup.

    The module-level ``_catalog`` is updated ATOMICALLY: the new catalog
    is built into a local variable, and only assigned to the global after
    a successful build + completeness validation. On any failure:

    * If this is the first build (module global was None), ``_catalog``
      stays None and subsequent ``get_catalog()`` calls raise RuntimeError.
    * If a previous build succeeded, ``_catalog`` keeps the previously
      working instance — a failing refresh does not degrade a running
      system.

    This prevents the "half-initialised catalog" failure mode where a
    Redis outage mid-scan would leave the global pointing at an empty
    catalog that silently misclassifies every row.
    """
    global _catalog

    new_catalog = OriginsCatalog(redis_client, schema)
    try:
        summary = new_catalog.build()
    except Exception:
        # Propagate to the caller; _catalog is intentionally untouched.
        logger.exception(
            "OriginsCatalog build failed; module global _catalog left "
            "unchanged (was %s)",
            "None" if _catalog is None else "previously-built catalog",
        )
        raise

    # Successful build — atomically swap in the new catalog BEFORE logging
    # the summary. The "built" log line is therefore a trustworthy signal
    # that the live catalog is serving that catalog's data.
    _catalog = new_catalog
    logger.info(
        "OriginsCatalog.build summary: "
        "plsql=%d etl=%d gl_blocked=%d eop_overrides=%d functions=%d",
        summary["plsql_origin_count"],
        summary["etl_origin_count"],
        summary["gl_block_count"],
        summary["eop_override_count"],
        summary["function_count"],
    )
    return _catalog


# ---------------------------------------------------------------------
# Public helper functions (same signatures as known_origins.py)
# ---------------------------------------------------------------------

def classify_origin(v_data_origin: str | None) -> dict:
    """Return PLSQL, ETL, or UNKNOWN classification for a V_DATA_ORIGIN."""
    catalog = get_catalog()

    if v_data_origin is None:
        return {"category": "UNKNOWN", "details": {}, "traceable": False}

    value = str(v_data_origin).strip()

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


def is_gl_blocked(gl_code: str | None) -> bool:
    """Return True if the GL code is on the graph-derived block list."""
    if not gl_code:
        return False
    catalog = get_catalog()
    normalized = str(gl_code).strip().upper()
    return normalized in {code.upper() for code in catalog.gl_block_list}


def get_eop_override(gl_code: str | None) -> dict | None:
    """Return the override record if this GL forces N_EOP_BAL to 0."""
    if not gl_code:
        return None
    return get_catalog().gl_eop_overrides.get(str(gl_code).strip())


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
