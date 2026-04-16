"""
Query-time graph filtering and LLM payload assembly.
No LLM calls here — pure Python.
All inputs come from Redis.
"""

from typing import Any

from src.tools.graph.store import (
    get_column_index,
    get_function_graph,
    get_full_graph,
    get_raw_source,
)
from src.tools.graph.serializer import from_json
from src.logger import get_logger

logger = get_logger(__name__, concern="app")


# ---------------------------------------------------------------------------
# 1. Top-level query router
# ---------------------------------------------------------------------------

def resolve_query_to_nodes(
    query_type: str,
    target_variable: str,
    function_name: str,
    table_name: str,
    schema: str,
    redis_client: Any,
) -> list[str]:
    """Route to the appropriate resolver based on *query_type*.

    Parameters
    ----------
    query_type:
        One of ``"variable"``, ``"function"``, ``"table"``.
    target_variable:
        Column or variable name (used when query_type is ``"variable"``).
    function_name:
        Function name (used when query_type is ``"function"``).
    table_name:
        Table name (used when query_type is ``"table"``).
    schema:
        The schema namespace for Redis lookups.
    redis_client:
        Active Redis connection.

    Returns
    -------
    list[str]
        Matching node IDs.
    """
    qt = query_type.strip().lower()
    if qt == "variable":
        return resolve_variable_nodes(target_variable, schema, redis_client)
    if qt == "function":
        return resolve_function_nodes(function_name, schema, redis_client)
    if qt == "table":
        return resolve_table_nodes(table_name, schema, redis_client)

    logger.warning("Unknown query_type '%s'; falling back to variable resolution", query_type)
    return resolve_variable_nodes(target_variable, schema, redis_client)


# ---------------------------------------------------------------------------
# 2. Alias resolution
# ---------------------------------------------------------------------------

def resolve_aliases(term: str, schema: str, redis_client: Any) -> list[str]:
    """Look up *term* in the alias map stored at ``graph:aliases:{schema}``.

    The alias map is expected to be a dict of ``{alias_upper: [canonical, ...]}``.
    Returns a list of canonical names the term maps to, or ``[term.upper()]``
    if no alias entry exists.
    """
    try:
        key = f"graph:aliases:{schema}"
        raw = redis_client.get(key)
        if raw is None:
            return [term.upper()]
        alias_map: dict = from_json(raw) if isinstance(raw, (str, bytes)) else raw
    except Exception as e:
        logger.warning("Failed to read alias map for schema %s: %s", schema, e)
        return [term.upper()]

    term_upper = term.strip().upper()

    # Try case-insensitive lookup: keys in the map may be mixed-case
    for map_key, aliases in alias_map.items():
        if map_key.upper() == term_upper:
            if isinstance(aliases, list):
                return aliases
            return [aliases]

    # Also check if the term appears as a *value* in any alias list
    for map_key, aliases in alias_map.items():
        targets = aliases if isinstance(aliases, list) else [aliases]
        for target in targets:
            if str(target).upper() == term_upper:
                return [map_key.upper()]

    return [term.upper()]


# ---------------------------------------------------------------------------
# 3. Variable node resolution
# ---------------------------------------------------------------------------

def resolve_variable_nodes(
    target_variable: str,
    schema: str,
    redis_client: Any,
) -> list[str]:
    """Resolve a variable/column name to node IDs via alias expansion and
    column-index lookup.

    Returns a deduplicated list of node IDs.
    """
    aliases = resolve_aliases(target_variable, schema, redis_client)

    col_index = get_column_index(redis_client, schema)
    if col_index is None:
        logger.warning("No column index found for schema %s", schema)
        return []

    seen: set[str] = set()
    result: list[str] = []

    for alias in aliases:
        alias_upper = alias.upper()
        node_ids = col_index.get(alias_upper, [])
        for nid in node_ids:
            if nid not in seen:
                seen.add(nid)
                result.append(nid)

    return result


# ---------------------------------------------------------------------------
# 4. Function node resolution
# ---------------------------------------------------------------------------

def resolve_function_nodes(
    function_name: str,
    schema: str,
    redis_client: Any,
) -> list[str]:
    """Return all node IDs from a specific function's graph.

    Node IDs are returned as ``"FUNCTION_NAME:node_id"``.
    """
    graph = get_function_graph(redis_client, schema, function_name)
    if graph is None:
        logger.warning("No graph found for function %s in schema %s", function_name, schema)
        return []

    nodes = graph.get("nodes", [])
    fn = graph.get("function", function_name)
    return [f"{fn}:{node['id']}" for node in nodes if "id" in node]


# ---------------------------------------------------------------------------
# 5. Table node resolution
# ---------------------------------------------------------------------------

def resolve_table_nodes(
    table_name: str,
    schema: str,
    redis_client: Any,
) -> list[str]:
    """Return node IDs that reference *table_name* (as target or source).

    Looks up the table name in the column index — the index typically
    registers table-associated columns, so we search for the table name
    itself as a key.
    """
    col_index = get_column_index(redis_client, schema)
    if col_index is None:
        logger.warning("No column index found for schema %s", schema)
        return []

    table_upper = table_name.strip().upper()
    node_ids = col_index.get(table_upper, [])

    if node_ids:
        return list(node_ids)

    # Fallback: scan all index entries for node IDs whose names contain the
    # table name (node IDs embed function names which often reference tables).
    seen: set[str] = set()
    result: list[str] = []
    for _col, nids in col_index.items():
        for nid in nids:
            if table_upper in nid.upper() and nid not in seen:
                seen.add(nid)
                result.append(nid)

    return result


# ---------------------------------------------------------------------------
# 6. Fetch nodes by IDs
# ---------------------------------------------------------------------------

def _extract_function_name(node_id: str) -> str:
    """Derive the function name from a node ID.

    Node IDs follow the pattern ``FUNCTION_NAME_N1`` or
    ``FUNCTION_NAME:FUNCTION_NAME_N1``.
    """
    # Handle "FN:FN_N1" format from resolve_function_nodes
    if ":" in node_id:
        node_id = node_id.split(":", 1)[1]

    # Node IDs are FUNCTION_NAME_N<number> or FUNCTION_NAME_COMMENTED_<number>
    # Strip the trailing _N<num> or _COMMENTED_<num> or _OP<num> or _INNER
    import re
    m = re.match(r"^(.+?)(?:_N\d+|_COMMENTED_\d+|_OP\d+|_INNER)$", node_id)
    if m:
        return m.group(1)
    return node_id


def fetch_nodes_by_ids(
    node_ids: list[str],
    schema: str,
    redis_client: Any,
) -> list[dict]:
    """Fetch node dicts for each ID, grouped by function.

    Returns a list of ``{"function": fn_name, "node": node_dict}``.
    """
    # Group node IDs by function name
    fn_groups: dict[str, list[str]] = {}
    for nid in node_ids:
        # Normalise: strip "FN:" prefix if present
        bare_id = nid.split(":", 1)[1] if ":" in nid else nid
        fn_name = _extract_function_name(bare_id)
        fn_groups.setdefault(fn_name, []).append(bare_id)

    results: list[dict] = []

    for fn_name, ids_in_fn in fn_groups.items():
        graph = get_function_graph(redis_client, schema, fn_name)
        if graph is None:
            logger.warning("No graph for function %s in schema %s", fn_name, schema)
            continue

        id_set = set(ids_in_fn)
        for node in graph.get("nodes", []):
            if node.get("id") in id_set:
                results.append({"function": fn_name, "node": node})

    return results


# ---------------------------------------------------------------------------
# 7. Fetch relevant edges
# ---------------------------------------------------------------------------

def fetch_relevant_edges(
    node_ids: list[str],
    schema: str,
    redis_client: Any,
) -> list[dict]:
    """Return edges from the full graph where either endpoint is in *node_ids*."""
    graph = get_full_graph(redis_client, schema)
    if graph is None:
        logger.warning("No full graph found for schema %s", schema)
        return []

    # Normalise IDs: strip "FN:" prefix
    normalised: set[str] = set()
    for nid in node_ids:
        normalised.add(nid.split(":", 1)[1] if ":" in nid else nid)

    relevant: list[dict] = []
    for edge in graph.get("edges", []):
        from_node = edge.get("from", "")
        to_node = edge.get("to", "")
        if from_node in normalised or to_node in normalised:
            relevant.append(edge)

    return relevant


# ---------------------------------------------------------------------------
# 8. Topological sort (execution order)
# ---------------------------------------------------------------------------

def determine_execution_order(
    nodes: list[dict],
    edges: list[dict],
) -> list[dict]:
    """Order *nodes* by data-flow using a topological sort on *edges*.

    Falls back to ordering by ``line_start`` when no edges connect the nodes.
    Returns a list of node dicts in execution order.
    """
    # Build adjacency from the filtered edge set
    node_map: dict[str, dict] = {}
    for entry in nodes:
        node = entry.get("node", entry)
        nid = node.get("id", "")
        node_map[nid] = entry

    # Kahn's algorithm
    in_degree: dict[str, int] = {nid: 0 for nid in node_map}
    adj: dict[str, list[str]] = {nid: [] for nid in node_map}

    for edge in edges:
        src = edge.get("from", "")
        dst = edge.get("to", "")
        if src in node_map and dst in node_map:
            adj[src].append(dst)
            in_degree[dst] = in_degree.get(dst, 0) + 1

    # Seed the queue with zero in-degree nodes, sorted by line_start for
    # deterministic ordering
    def _line_start(nid: str) -> int:
        entry = node_map.get(nid, {})
        node = entry.get("node", entry)
        return node.get("line_start", 0) or 0

    queue: list[str] = sorted(
        [nid for nid, deg in in_degree.items() if deg == 0],
        key=_line_start,
    )

    ordered: list[dict] = []
    while queue:
        nid = queue.pop(0)
        ordered.append(node_map[nid])
        for neighbour in sorted(adj.get(nid, []), key=_line_start):
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)
        # Re-sort to maintain line_start ordering among equal-depth nodes
        queue.sort(key=_line_start)

    # If topological sort missed nodes (cycles or disconnected), append them
    # sorted by line_start
    ordered_ids = {
        (entry.get("node", entry)).get("id", "") for entry in ordered
    }
    remaining = [
        node_map[nid] for nid in node_map
        if nid not in ordered_ids
    ]
    remaining.sort(key=lambda e: _line_start((e.get("node", e)).get("id", "")))
    ordered.extend(remaining)

    return ordered


# ---------------------------------------------------------------------------
# 9. Assemble LLM payload
# ---------------------------------------------------------------------------

_MAX_PAYLOAD_CHARS = 2000
_COLUMN_MAP_TRUNCATE_THRESHOLD = 8


def assemble_llm_payload(
    nodes: list[dict],
    edges: list[dict],
    target_variable: str,
    user_query: str,
    execution_order: list[dict],
) -> str:
    """Build a compact text payload for the LLM.

    The payload is structured for easy comprehension by the model, with each
    relevant node rendered as a numbered step.  Column mappings are truncated
    when there are too many entries to keep the total under ~2000 characters.
    """
    lines: list[str] = []
    lines.append(f"Query: {user_query}")
    lines.append(f"Target variable: {target_variable}")
    lines.append("")

    # Track which functions had nodes vs. which were checked but empty
    functions_with_nodes: set[str] = set()

    for step_num, entry in enumerate(execution_order, start=1):
        node = entry.get("node", entry)
        fn_name = entry.get("function", _extract_function_name(node.get("id", "")))
        functions_with_nodes.add(fn_name)

        node_type = node.get("type", "UNKNOWN")
        target_table = node.get("target_table", "")
        source_tables = node.get("source_tables", [])
        column_maps = node.get("column_maps", {})
        calculations = node.get("calculation", [])
        conditions = node.get("conditions", [])
        committed = node.get("committed_after", False)
        line_start = node.get("line_start", "?")
        line_end = node.get("line_end", "?")

        lines.append(f"--- STEP {step_num}: {fn_name} ---")
        lines.append(f"Operation: {node_type}")

        # Tables
        source_str = ", ".join(source_tables) if source_tables else "N/A"
        target_str = target_table or "N/A"
        lines.append(f"Tables: {source_str} -> {target_str}")

        # Column mapping
        if column_maps:
            lines.append("Column mapping:")
            map_items = list(column_maps.items()) if isinstance(column_maps, dict) else []
            display_items = map_items
            truncated = False
            if len(map_items) > _COLUMN_MAP_TRUNCATE_THRESHOLD:
                display_items = map_items[:_COLUMN_MAP_TRUNCATE_THRESHOLD]
                truncated = True
            for col, src in display_items:
                lines.append(f"  {col} <- {src}")
            if truncated:
                lines.append(f"  ... (+{len(map_items) - _COLUMN_MAP_TRUNCATE_THRESHOLD} more)")

        # Calculations
        if calculations:
            lines.append("Calculation:")
            for calc in calculations:
                if isinstance(calc, dict):
                    calc_type = calc.get("type", "UNKNOWN")
                    lines.append(f"  Type: {calc_type}")
                    expr = calc.get("expression", "")
                    if expr:
                        lines.append(f"  {_truncate(expr, 120)}")

        # Conditions
        if conditions:
            lines.append("Conditions:")
            for cond in conditions:
                cond_text = cond if isinstance(cond, str) else cond.get("expression", str(cond))
                lines.append(f"  {_truncate(cond_text, 100)}")

        # Commit status and source location
        lines.append(f"Committed after: {'yes' if committed else 'no'}")
        lines.append(f"Source: lines {line_start}-{line_end}")
        lines.append("")

    # Add NOTE for edges referencing functions that had no matching nodes
    edge_functions: set[str] = set()
    for edge in edges:
        for endpoint in (edge.get("from", ""), edge.get("to", "")):
            fn = _extract_function_name(endpoint)
            edge_functions.add(fn)

    missing_fns = edge_functions - functions_with_nodes
    for fn in sorted(missing_fns):
        if fn:
            lines.append(f"NOTE: Function {fn} was checked but had no matching nodes.")

    payload = "\n".join(lines)

    # Trim if exceeding budget
    if len(payload) > _MAX_PAYLOAD_CHARS:
        payload = payload[:_MAX_PAYLOAD_CHARS - 20] + "\n... [truncated]"

    return payload


# ---------------------------------------------------------------------------
# 10. Fallback raw source lines
# ---------------------------------------------------------------------------

def get_fallback_raw_lines(
    node_ids: list[str],
    schema: str,
    redis_client: Any,
) -> dict:
    """Fetch raw source lines for the given nodes as a fallback.

    Returns ``{function_name: [{"line": N, "text": "..."}]}``.
    """
    # Group node IDs by function
    fn_groups: dict[str, list[str]] = {}
    for nid in node_ids:
        bare_id = nid.split(":", 1)[1] if ":" in nid else nid
        fn_name = _extract_function_name(bare_id)
        fn_groups.setdefault(fn_name, []).append(bare_id)

    result: dict[str, list[dict]] = {}

    for fn_name, ids_in_fn in fn_groups.items():
        # Get the function graph to find line ranges for the nodes
        graph = get_function_graph(redis_client, schema, fn_name)
        if graph is None:
            continue

        # Collect line ranges from matching nodes
        line_ranges: list[tuple[int, int]] = []
        id_set = set(ids_in_fn)
        for node in graph.get("nodes", []):
            if node.get("id") in id_set:
                start = node.get("line_start")
                end = node.get("line_end")
                if start is not None and end is not None:
                    line_ranges.append((int(start), int(end)))

        if not line_ranges:
            continue

        # Fetch raw source for this function
        raw_lines = get_raw_source(redis_client, schema, fn_name)
        if raw_lines is None:
            continue

        # Extract the relevant lines (line numbers are 1-based)
        fn_result: list[dict] = []
        for start, end in sorted(line_ranges):
            for line_num in range(start, end + 1):
                idx = line_num - 1  # convert to 0-based index
                if 0 <= idx < len(raw_lines):
                    fn_result.append({"line": line_num, "text": raw_lines[idx]})

        if fn_result:
            result[fn_name] = fn_result

    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _truncate(text: str, max_len: int) -> str:
    """Truncate *text* to *max_len* characters, appending '...' if cut."""
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."
