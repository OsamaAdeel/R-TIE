"""
Query-time graph filtering and LLM payload assembly.
No LLM calls here — pure Python.
All inputs come from Redis.
"""

from typing import Any

from src.parsing.store import (
    get_column_index,
    get_function_graph,
    get_full_graph,
    get_raw_source,
)
from src.parsing.keyspace import SchemaAwareKeyspace
from src.parsing.serializer import from_json
from src.logger import get_logger

logger = get_logger(__name__, concern="app")
_w43_diag = get_logger("rtie.w43_diag", concern="app")


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
    _w43_diag.info(
        "[W43_DIAG] stage=resolve_query_to_nodes_entry"
        " query_type=%r qt=%r target_variable=%r function_name=%r"
        " table_name=%r schema=%r",
        query_type, qt,
        target_variable[:80] if target_variable else None,
        function_name[:80] if function_name else None,
        table_name or None,
        schema,
    )
    if qt == "variable":
        result = resolve_variable_nodes(target_variable, schema, redis_client)
        _w43_diag.info(
            "[W43_DIAG] stage=resolve_query_to_nodes_result branch=variable"
            " node_count=%d", len(result),
        )
        return result
    if qt == "function":
        result = resolve_function_nodes(function_name, schema, redis_client)
        _w43_diag.info(
            "[W43_DIAG] stage=resolve_query_to_nodes_result branch=function"
            " node_count=%d", len(result),
        )
        return result
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
        key = SchemaAwareKeyspace.graph_aliases_key(schema)
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

def _is_inactive_node(node: dict | None) -> bool:
    """Return True if *node* carries manifest hierarchy with ``active=False``.

    Nodes without a ``hierarchy`` block (legacy, or modules without a
    ``manifest.yaml``) are always treated as active so the default
    behaviour is unchanged when no manifest is in play.
    """
    if not node:
        return False
    hierarchy = node.get("hierarchy")
    if not hierarchy:
        return False
    return hierarchy.get("active") is False


def _node_by_id(graph: dict | None, node_id: str) -> dict | None:
    """Return the node dict inside *graph* whose id matches *node_id*."""
    if not graph:
        return None
    for node in graph.get("nodes", []):
        if node.get("id") == node_id:
            return node
    return None


def resolve_variable_nodes(
    target_variable: str,
    schema: str,
    redis_client: Any,
    include_inactive: bool = False,
) -> list[str]:
    """Resolve a variable/column name to node IDs via alias expansion,
    column-index lookup, and cross-function edge traversal.

    After finding direct matches in the column index, the full graph is
    checked for ALL edges — if any edge's ``from_node`` or ``to_node`` is
    in the direct-match set, the OTHER end of that edge is added to the
    result.  Additionally, function-name prefix matching is used: if a
    direct match belongs to ``FN_A`` and an edge connects ``FN_A:node_Y``
    to ``FN_B:node_Z``, then ``FN_B:node_Z`` is added when ``FN_B`` has
    the target variable in its column index.

    When *include_inactive* is False (default) nodes whose hierarchy
    metadata marks them ``active=false`` — via an authored
    ``manifest.yaml`` — are excluded from both the direct matches and
    cross-function edge traversal. This prevents tasks OFSAA has removed
    from production from polluting upstream/downstream analyses.

    Returns a deduplicated list of node IDs.
    """
    aliases = resolve_aliases(target_variable, schema, redis_client)

    _w43_diag.info(
        "[W43_DIAG] stage=resolve_variable_nodes_entry"
        " target_variable=%r schema=%r resolved_aliases=%r"
        " target_looks_like_function=%s",
        target_variable[:80] if target_variable else None,
        schema,
        aliases,
        bool(target_variable and "_" in target_variable and target_variable == target_variable.upper() and len(target_variable) > 8),
    )

    col_index = get_column_index(redis_client, schema)
    if col_index is None:
        _w43_diag.info(
            "[W43_DIAG] stage=resolve_variable_nodes_result"
            " target_variable=%r col_index_present=false node_count=0",
            target_variable[:80] if target_variable else None,
        )
        logger.warning("No column index found for schema %s", schema)
        return []

    _w43_diag.info(
        "[W43_DIAG] stage=resolve_variable_nodes_index_lookup"
        " schema=%r col_index_size=%d aliases=%r"
        " alias_hits=%r",
        schema,
        len(col_index),
        aliases,
        {a: col_index.get(a.upper(), []) for a in aliases},
    )

    # Cache per-function graphs so the inactive-filter lookup stays O(1)
    # after a one-time fetch per function.
    fn_graph_cache: dict[str, dict | None] = {}

    def _is_inactive(nid: str) -> bool:
        if include_inactive:
            return False
        bare = nid.split(":", 1)[1] if ":" in nid else nid
        fn = _extract_function_name(bare)
        if fn not in fn_graph_cache:
            fn_graph_cache[fn] = get_function_graph(redis_client, schema, fn)
        node = _node_by_id(fn_graph_cache[fn], bare)
        return _is_inactive_node(node)

    seen: set[str] = set()
    direct_nodes: list[str] = []

    for alias in aliases:
        alias_upper = alias.upper()
        node_ids = col_index.get(alias_upper, [])
        for nid in node_ids:
            if nid in seen:
                continue
            if _is_inactive(nid):
                continue
            seen.add(nid)
            direct_nodes.append(nid)

    logger.debug("resolve_variable_nodes: aliases=%s, direct_matches=%s", aliases, direct_nodes)

    result: list[str] = list(direct_nodes)

    # --- Cross-function traversal (column-aware) ---
    # Only follow cross-function edges whose matching_columns overlap
    # with the target variable's aliases.  This prevents pulling in
    # every node from functions that merely share a table.
    if result:
        full_graph = get_full_graph(redis_client, schema)
        if full_graph is not None:
            edges = full_graph.get("edges", [])
            direct_ids: set[str] = set(result)
            for nid in list(direct_ids):
                direct_ids.add(nid.split(":", 1)[1] if ":" in nid else nid)

            alias_set = {a.upper() for a in aliases}
            alias_set.add(target_variable.strip().upper())

            for edge in edges:
                from_node = edge.get("from_node", edge.get("from", ""))
                to_node = edge.get("to_node", edge.get("to", ""))

                # Only follow edges whose matching_columns overlap the aliases
                matching_cols = edge.get("matching_columns", [])
                matching_upper = {c.upper() for c in matching_cols} if matching_cols else set()
                if not (matching_upper & alias_set):
                    continue

                from_matches = from_node in direct_ids
                to_matches = to_node in direct_ids
                if from_matches and to_node not in seen and not _is_inactive(to_node):
                    seen.add(to_node)
                    result.append(to_node)
                if to_matches and from_node not in seen and not _is_inactive(from_node):
                    seen.add(from_node)
                    result.append(from_node)

    logger.debug("resolve_variable_nodes: after edge walk, total=%d", len(result))

    _w43_diag.info(
        "[W43_DIAG] stage=resolve_variable_nodes_result"
        " target_variable=%r direct_nodes=%d after_edge_walk=%d",
        target_variable[:80] if target_variable else None,
        len(direct_nodes),
        len(result),
    )

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
    candidate_key = SchemaAwareKeyspace.graph_key(schema, function_name)
    _w43_diag.info(
        "[W43_DIAG] stage=resolve_function_nodes_entry"
        " function_name=%r schema=%r redis_key_attempted=%r"
        " function_name_len=%d function_name_is_multiword=%s",
        function_name[:80] if function_name else None,
        schema,
        candidate_key[:120],
        len(function_name) if function_name else 0,
        " " in (function_name or ""),
    )
    graph = get_function_graph(redis_client, schema, function_name)
    if graph is None:
        _w43_diag.info(
            "[W43_DIAG] stage=resolve_function_nodes_result"
            " redis_key=%r cache_hit=false node_count=0"
            " diagnosis='key_miss_or_deserialize_error'",
            candidate_key[:120],
        )
        logger.warning("No graph found for function %s in schema %s", function_name, schema)
        return []

    nodes = graph.get("nodes", [])
    fn = graph.get("function", function_name)
    node_types = list({n.get("type", "UNKNOWN") for n in nodes})
    has_hierarchy = any("hierarchy" in n for n in nodes)
    _w43_diag.info(
        "[W43_DIAG] stage=resolve_function_nodes_result"
        " redis_key=%r cache_hit=true node_count=%d"
        " node_types=%r has_hierarchy=%s",
        candidate_key[:120],
        len(nodes),
        node_types,
        has_hierarchy,
    )
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
    include_upstream: bool = True,
    include_inactive: bool = False,
) -> list[dict]:
    """Fetch node dicts for each ID, grouped by function.

    Returns a list of
    ``{"function": fn_name, "node": node_dict, "execution_condition": dict|None}``.
    The ``execution_condition`` is the function-level execution condition
    (e.g. IF/CASE guard) stored at the top of the function graph.

    When *include_upstream* is True, the full graph is inspected for edges
    whose ``to_node`` matches one of the requested *node_ids*.  If the
    ``from_node`` of such an edge corresponds to a ``SCALAR_COMPUTE`` node,
    that node is fetched and included in the result with
    ``"is_upstream": True``.

    When *include_inactive* is False (default) nodes whose hierarchy
    metadata marks them ``active=false`` are omitted from the result.
    Set to True when explicitly debugging a removed OFSAA task.
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

        # Extract function-level execution_condition (may be None)
        exec_cond = graph.get("execution_condition", None)

        id_set = set(ids_in_fn)
        for node in graph.get("nodes", []):
            if node.get("id") in id_set:
                if not include_inactive and _is_inactive_node(node):
                    continue
                results.append({
                    "function": fn_name,
                    "node": node,
                    "execution_condition": exec_cond,
                })

    # --- Upstream SCALAR_COMPUTE discovery ---
    if include_upstream:
        # Phase 1: edge-based discovery (original logic)
        full_graph = get_full_graph(redis_client, schema)
        if full_graph is not None:
            requested_bare: set[str] = set()
            for nid in node_ids:
                requested_bare.add(nid.split(":", 1)[1] if ":" in nid else nid)

            upstream_ids: set[str] = set()
            for edge in full_graph.get("edges", []):
                to_n = edge.get("to_node", edge.get("to", ""))
                from_n = edge.get("from_node", edge.get("from", ""))
                if to_n in requested_bare and from_n not in requested_bare:
                    upstream_ids.add(from_n)

            if upstream_ids:
                up_fn_groups: dict[str, list[str]] = {}
                for uid in upstream_ids:
                    fn_name = _extract_function_name(uid)
                    up_fn_groups.setdefault(fn_name, []).append(uid)

                for fn_name, ids_in_fn in up_fn_groups.items():
                    graph = get_function_graph(redis_client, schema, fn_name)
                    if graph is None:
                        continue
                    exec_cond = graph.get("execution_condition", None)
                    id_set = set(ids_in_fn)
                    for node in graph.get("nodes", []):
                        if node.get("id") in id_set and node.get("type", "").upper() == "SCALAR_COMPUTE":
                            results.append({
                                "function": fn_name,
                                "node": node,
                                "execution_condition": exec_cond,
                                "is_upstream": True,
                            })

        # Phase 2: text-matching discovery — finds SCALAR_COMPUTE nodes
        # whose output_variable appears in the column_maps or calculations
        # of the main (non-upstream) nodes.  Runs iteratively to catch
        # transitive references (e.g. TOT1 references LN_TOTAL_DEDUCT).
        found_upstream_ids: set[str] = {
            entry["node"]["id"] for entry in results if entry.get("is_upstream")
        }
        main_ids: set[str] = {
            nid.split(":", 1)[1] if ":" in nid else nid for nid in node_ids
        }

        for fn_name in list(fn_groups.keys()):
            graph = get_function_graph(redis_client, schema, fn_name)
            if graph is None:
                continue

            # Collect all SCALAR_COMPUTE nodes in this function
            sc_nodes: list[dict] = []
            for node in graph.get("nodes", []):
                nid = node.get("id", "")
                if (node.get("type", "").upper() == "SCALAR_COMPUTE"
                        and nid not in found_upstream_ids
                        and nid not in main_ids):
                    sc_nodes.append(node)

            if not sc_nodes:
                continue

            # Build reference text from main + already-found upstream nodes
            ref_text = ""
            for entry in results:
                n = entry.get("node", entry)
                if entry.get("function") == fn_name:
                    ref_text += str(n.get("column_maps", {})).upper() + " "
                    for calc in (n.get("calculation") or []):
                        if isinstance(calc, dict):
                            ref_text += (calc.get("expression", "") + " ").upper()

            exec_cond = graph.get("execution_condition")
            changed = True
            while changed:
                changed = False
                for node in list(sc_nodes):
                    out_var = (node.get("output_variable") or "").upper()
                    if out_var and out_var in ref_text:
                        results.append({
                            "function": fn_name,
                            "node": node,
                            "execution_condition": exec_cond,
                            "is_upstream": True,
                        })
                        found_upstream_ids.add(node["id"])
                        sc_nodes.remove(node)
                        # Add this node's expression to ref_text for transitive lookup
                        for calc in (node.get("calculation") or []):
                            if isinstance(calc, dict):
                                ref_text += (calc.get("expression", "") + " ").upper()
                        changed = True

    return results


# ---------------------------------------------------------------------------
# 7. Fetch relevant edges
# ---------------------------------------------------------------------------

def fetch_relevant_edges(
    node_ids: list[str],
    schema: str,
    redis_client: Any,
) -> list[dict]:
    """Return ALL edges (intra-function and cross-function) from the full
    graph where either ``from_node`` or ``to_node`` is in *node_ids*.

    Matching is done both by exact node ID and by function-name prefix so
    that cross-function edges are not missed when only one end of the edge
    was resolved.

    Both the full merged graph and per-function graphs are consulted so
    that intra-function edges are never missed.
    """
    # Normalise IDs: strip "FN:" prefix
    normalised: set[str] = set()
    for nid in node_ids:
        normalised.add(nid.split(":", 1)[1] if ":" in nid else nid)

    # Collect function-name prefixes from the node IDs
    fn_prefixes: set[str] = set()
    for nid in node_ids:
        fn = nid.split(":")[0]
        fn_prefixes.add(fn)

    node_id_set = normalised  # alias for clarity in matching

    seen_edges: set[tuple[str, str]] = set()
    matching: list[dict] = []

    def _collect(edges: list[dict]) -> None:
        for edge in edges:
            from_n = edge.get("from_node", edge.get("from", ""))
            to_n = edge.get("to_node", edge.get("to", ""))
            edge_key = (from_n, to_n)
            if edge_key in seen_edges:
                continue
            # Exact match
            if from_n in node_id_set or to_n in node_id_set:
                seen_edges.add(edge_key)
                matching.append(edge)
                continue
            # Function-level match
            for nid in node_ids:
                fn = nid.split(":")[0]
                if fn in from_n or fn in to_n:
                    seen_edges.add(edge_key)
                    matching.append(edge)
                    break

    # 1. Full (merged) graph — contains cross-function edges
    full_graph = get_full_graph(redis_client, schema)
    if full_graph is not None:
        _collect(full_graph.get("edges", []))
    else:
        logger.warning("No full graph found for schema %s", schema)

    # 2. Per-function graphs — may contain intra-function edges not in full graph
    fn_names: set[str] = set()
    for nid in normalised:
        fn_names.add(_extract_function_name(nid))

    for fn_name in fn_names:
        fn_graph = get_function_graph(redis_client, schema, fn_name)
        if fn_graph is not None:
            _collect(fn_graph.get("edges", []))

    logger.debug("fetch_relevant_edges: %d edges found for %d nodes", len(matching), len(node_ids))

    return matching


# ---------------------------------------------------------------------------
# 8. Topological sort (execution order)
# ---------------------------------------------------------------------------

def determine_execution_order(
    nodes: list[dict],
    edges: list[dict],
) -> list[dict]:
    """Order *nodes* by data-flow using a topological sort on *edges*.

    Nodes from functions earlier in the dependency chain appear first.
    Cross-function edges are used to determine function-level ordering;
    within a function nodes are further sorted by ``line_start``.

    Falls back to ordering by ``line_start`` when no edges connect the nodes.
    Returns a list of node dicts in execution order.
    """
    # Build node map keyed by bare node ID
    node_map: dict[str, dict] = {}
    for entry in nodes:
        node = entry.get("node", entry)
        nid = node.get("id", "")
        node_map[nid] = entry

    # --- Determine function-level ordering from edges ---
    # Collect which function each node belongs to
    node_fn: dict[str, str] = {}
    for nid, entry in node_map.items():
        fn = entry.get("function", _extract_function_name(nid))
        node_fn[nid] = fn

    # Build a function-level DAG from edges (cross-function edges imply
    # the source function must execute before the target function)
    fn_adj: dict[str, set[str]] = {}
    fn_in: dict[str, int] = {}
    all_fns: set[str] = set(node_fn.values())
    for fn in all_fns:
        fn_adj.setdefault(fn, set())
        fn_in.setdefault(fn, 0)

    for edge in edges:
        src = edge.get("from", "")
        dst = edge.get("to", "")
        src_fn = node_fn.get(src, _extract_function_name(src))
        dst_fn = node_fn.get(dst, _extract_function_name(dst))
        if src_fn != dst_fn and dst_fn not in fn_adj.get(src_fn, set()):
            fn_adj.setdefault(src_fn, set()).add(dst_fn)
            fn_in[dst_fn] = fn_in.get(dst_fn, 0) + 1

    # Topological sort of functions (Kahn's)
    fn_queue = sorted([f for f, d in fn_in.items() if d == 0])
    fn_order: list[str] = []
    while fn_queue:
        fn = fn_queue.pop(0)
        fn_order.append(fn)
        for neighbour in sorted(fn_adj.get(fn, set())):
            fn_in[neighbour] -= 1
            if fn_in[neighbour] == 0:
                fn_queue.append(neighbour)
        fn_queue.sort()

    # Append any functions missed (cycles / disconnected)
    for fn in sorted(all_fns):
        if fn not in fn_order:
            fn_order.append(fn)

    fn_rank: dict[str, int] = {fn: idx for idx, fn in enumerate(fn_order)}

    # --- Kahn's algorithm on the node-level graph ---
    in_degree: dict[str, int] = {nid: 0 for nid in node_map}
    adj: dict[str, list[str]] = {nid: [] for nid in node_map}

    for edge in edges:
        src = edge.get("from", "")
        dst = edge.get("to", "")
        if src in node_map and dst in node_map:
            adj[src].append(dst)
            in_degree[dst] = in_degree.get(dst, 0) + 1

    def _sort_key(nid: str) -> tuple[int, int]:
        """Sort by function rank first, then by line_start within the function."""
        entry = node_map.get(nid, {})
        node = entry.get("node", entry)
        line = node.get("line_start", 0) or 0
        fn = node_fn.get(nid, "")
        return (fn_rank.get(fn, 999), line)

    queue: list[str] = sorted(
        [nid for nid, deg in in_degree.items() if deg == 0],
        key=_sort_key,
    )

    ordered: list[dict] = []
    while queue:
        nid = queue.pop(0)
        ordered.append(node_map[nid])
        for neighbour in sorted(adj.get(nid, []), key=_sort_key):
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)
        queue.sort(key=_sort_key)

    # Append any nodes missed (cycles or disconnected), ordered by function
    # rank then line_start
    ordered_ids = {
        (entry.get("node", entry)).get("id", "") for entry in ordered
    }
    remaining = [
        node_map[nid] for nid in node_map
        if nid not in ordered_ids
    ]
    remaining.sort(key=lambda e: _sort_key((e.get("node", e)).get("id", "")))
    ordered.extend(remaining)

    return ordered


# ---------------------------------------------------------------------------
# 9. Assemble LLM payload
# ---------------------------------------------------------------------------

_MAX_PAYLOAD_CHARS = 4000
_COLUMN_MAP_TRUNCATE_THRESHOLD = 8


def _date_predicate(column: str, bind_key: str) -> str:
    """Build a WHERE clause predicate that binds a DATE column safely.

    Oracle DATE columns require TO_DATE conversion when the bind value
    is a string. This helper keeps the behaviour consistent across the
    query_engine and phase2 SQL generation paths.
    """
    return f"{column} = TO_DATE(:{bind_key}, 'YYYY-MM-DD')"


def _node_mentions_variable(node: dict, aliases: set[str]) -> bool:
    """Return True if *node* references any alias in column_maps,
    calculation, output_variable, or conditions."""
    # column_maps (serialised to string for broad matching)
    column_maps = node.get("column_maps", {})
    if column_maps and isinstance(column_maps, dict):
        text = str(column_maps).upper()
        for alias in aliases:
            if alias in text:
                return True

    # calculation expressions
    for calc in (node.get("calculation") or []):
        if isinstance(calc, dict):
            expr = calc.get("expression", "")
            if expr:
                expr_upper = expr.upper()
                for alias in aliases:
                    if alias in expr_upper:
                        return True

    # output_variable (SCALAR_COMPUTE nodes)
    out_var = (node.get("output_variable") or "").upper()
    if out_var and out_var in aliases:
        return True

    # conditions
    for cond in (node.get("conditions") or []):
        cond_text = (cond if isinstance(cond, str) else cond.get("expression", "")).upper()
        for alias in aliases:
            if alias in cond_text:
                return True

    return False


def _is_passthrough_node(node: dict, target_var_upper: str) -> bool:
    """Return True if *node* copies the target variable without modification."""
    cm = node.get("column_maps", {})
    if not cm or not isinstance(cm, dict):
        return True

    mapping = cm.get("mapping", {})
    if mapping:
        val = mapping.get(target_var_upper)
        if val is None:
            # Also try case-insensitive
            for k, v in mapping.items():
                if k.strip().upper() == target_var_upper:
                    val = v
                    break
        if val is None:
            return True  # column not in mapping — copied unchanged
        if str(val).strip().upper() == target_var_upper:
            return True  # direct copy
        return False

    assignments = cm.get("assignments", [])
    for col, expr in assignments:
        if col.strip().upper() == target_var_upper:
            if expr.strip().upper() == target_var_upper:
                return True
            return False

    # Note: the catch-all `return True` below relies on production
    # parser output ALWAYS using a wrapped 'mapping' or 'assignments'
    # shape. Flat-dict column_maps would be misclassified here, but
    # is not produced by any current parser path. (W52 — verified
    # by inventory; revisit if a flat-dict producer is ever added.)
    return True


def _build_incoming_edge_index(edges: list[dict]) -> dict[str, list[dict]]:
    """Return a mapping from ``to_node`` to list of edges targeting it."""
    index: dict[str, list[dict]] = {}
    for edge in edges:
        to_node = edge.get("to", "")
        index.setdefault(to_node, []).append(edge)
    return index


def assemble_llm_payload(
    nodes: list[dict],
    edges: list[dict],
    target_variable: str,
    user_query: str,
    execution_order: list[dict],
) -> str:
    """Build a compact text payload for the LLM.

    The payload is structured for easy comprehension by the model, with each
    relevant node rendered as a numbered step.  Includes:

    * **Execution condition** at the top of each function's section.
    * **Intermediate variables** when a node has incoming edges from
      ``SCALAR_COMPUTE`` nodes.
    * **PASS-THROUGH** label for steps where a column is copied unchanged
      (``DIRECT`` type).

    Column mappings are truncated when there are too many entries to keep
    the total under ~2000 characters.
    """
    lines: list[str] = []
    lines.append(f"Query: {user_query}")
    lines.append(f"Target variable: {target_variable}")
    lines.append("")

    # Use execution_order if provided, otherwise fall back to nodes
    effective_order = execution_order if execution_order else nodes

    # Pre-build a lookup of all nodes by ID (for intermediate-variable resolution)
    all_node_lookup: dict[str, dict] = {}
    for entry in effective_order:
        node = entry.get("node", entry)
        all_node_lookup[node.get("id", "")] = node

    # Also include upstream SCALAR_COMPUTE nodes that were fetched via
    # include_upstream=True in fetch_nodes_by_ids — they carry
    # ``is_upstream: True`` on the entry dict.
    upstream_entries: list[dict] = []
    non_upstream_order: list[dict] = []
    for entry in effective_order:
        if entry.get("is_upstream"):
            upstream_entries.append(entry)
            node = entry.get("node", entry)
            all_node_lookup[node.get("id", "")] = node
        else:
            non_upstream_order.append(entry)

    # --- Relevance filter: drop nodes that don't mention the target variable ---
    target_aliases: set[str] = {target_variable.strip().upper()}
    filtered_order: list[dict] = []
    for entry in non_upstream_order:
        node = entry.get("node", entry)
        if _node_mentions_variable(node, target_aliases):
            filtered_order.append(entry)
    if filtered_order:
        non_upstream_order = filtered_order

    # --- Build upstream lookup by output_variable for text-based matching ---
    upstream_by_var: dict[str, dict] = {}  # output_variable_upper -> node
    for entry in upstream_entries:
        node = entry.get("node", entry)
        all_node_lookup[node.get("id", "")] = node
        out_var = (node.get("output_variable") or "").upper()
        if out_var:
            upstream_by_var[out_var] = node

    incoming_idx = _build_incoming_edge_index(edges)
    tv_upper = target_variable.strip().upper()

    # --- Consolidate consecutive same-function pass-through nodes ---
    consolidated: list = []  # entries or {"consolidated": True, ...} dicts
    i = 0
    while i < len(non_upstream_order):
        entry = non_upstream_order[i]
        node = entry.get("node", entry)
        fn = entry.get("function", "")

        if _is_passthrough_node(node, tv_upper):
            group = [entry]
            j = i + 1
            while j < len(non_upstream_order):
                nxt = non_upstream_order[j]
                nxt_node = nxt.get("node", nxt)
                nxt_fn = nxt.get("function", "")
                if nxt_fn == fn and _is_passthrough_node(nxt_node, tv_upper):
                    group.append(nxt)
                    j += 1
                else:
                    break
            if len(group) > 1:
                # Merge into one consolidated pass-through entry
                all_src: list[str] = []
                all_tgt: list[str] = []
                min_line = 999999
                max_line = 0
                for g in group:
                    gn = g.get("node", g)
                    for s in (gn.get("source_tables") or []):
                        if s not in all_src:
                            all_src.append(s)
                    t = gn.get("target_table", "")
                    if t and t not in all_tgt:
                        all_tgt.append(t)
                    ls = gn.get("line_start", 0) or 0
                    le = gn.get("line_end", 0) or 0
                    if ls and ls < min_line:
                        min_line = ls
                    if le and le > max_line:
                        max_line = le
                consolidated.append({
                    "consolidated": True,
                    "function": fn,
                    "execution_condition": entry.get("execution_condition"),
                    "source_tables": all_src,
                    "target_tables": all_tgt,
                    "line_start": min_line,
                    "line_end": max_line,
                    "count": len(group),
                })
            else:
                consolidated.append(entry)
            i = j
        else:
            consolidated.append(entry)
            i += 1

    # Track which functions already had their header emitted
    fn_header_emitted: set[str] = set()

    for step_num, entry in enumerate(consolidated, start=1):
        # --- Handle consolidated pass-through ---
        if isinstance(entry, dict) and entry.get("consolidated"):
            fn_name = entry["function"]
            if fn_name not in fn_header_emitted:
                fn_header_emitted.add(fn_name)
                lines.append(f"--- FUNCTION: {fn_name} ---")
                exec_cond = entry.get("execution_condition")
                if exec_cond and isinstance(exec_cond, dict):
                    plain = exec_cond.get("plain_text", exec_cond.get("description", ""))
                    if plain:
                        lines.append(f"EXECUTION CONDITION: {plain}")
                elif exec_cond and isinstance(exec_cond, str):
                    lines.append(f"EXECUTION CONDITION: {exec_cond}")
                lines.append("")

            _sql_funcs = {"TO_DATE", "TO_NUMBER", "TO_CHAR", "ADD_MONTHS", "NVL",
                          "SYSDATE", "DECODE", "TRUNC", "ROUND", "FIC_MIS_DATE"}
            tables = [t for t in entry["source_tables"] + entry["target_tables"]
                      if t.upper() not in _sql_funcs]
            lines.append(f"--- STEP {step_num} [PASS-THROUGH]: {fn_name} ---")
            lines.append(f"Copies {target_variable} unchanged through: {', '.join(tables)}")
            lines.append(f"The value is not transformed -- this function date-adjusts historical records.")
            lines.append(f"Source: lines {entry['line_start']}-{entry['line_end']}")
            lines.append("")
            continue

        node = entry.get("node", entry)
        fn_name = entry.get("function", _extract_function_name(node.get("id", "")))

        # --- Function header with execution condition (once per function) ---
        if fn_name not in fn_header_emitted:
            fn_header_emitted.add(fn_name)
            lines.append(f"--- FUNCTION: {fn_name} ---")
            exec_cond = entry.get("execution_condition")
            if exec_cond and isinstance(exec_cond, dict):
                plain = exec_cond.get("plain_text", exec_cond.get("description", ""))
                expr = exec_cond.get("expression", "")
                if plain:
                    lines.append(f"EXECUTION CONDITION: {plain}")
                if expr:
                    lines.append(f"Expression: {expr}")
            elif exec_cond and isinstance(exec_cond, str):
                lines.append(f"EXECUTION CONDITION: {exec_cond}")
            lines.append("")

        # --- Intermediate variables (text-matching against upstream SCALAR_COMPUTE) ---
        nid = node.get("id", "")
        # Build text from this node's column_maps + calculations
        node_text = str(node.get("column_maps", {})).upper()
        for calc in (node.get("calculation") or []):
            if isinstance(calc, dict):
                node_text += " " + (calc.get("expression", "")).upper()

        intermediates: list[dict] = []
        seen_vars: set[str] = set()
        # Find upstream vars referenced in this node's text
        for var_upper, sc_node in upstream_by_var.items():
            if var_upper in node_text and var_upper not in seen_vars:
                intermediates.append(sc_node)
                seen_vars.add(var_upper)

        if intermediates:
            lines.append(f"--- INTERMEDIATE VARIABLES (used in Step {step_num}) ---")
            for inode in intermediates:
                var_name = inode.get("output_variable", inode.get("variable_name", inode.get("id", "?")))
                formula = ""
                i_calcs = inode.get("calculation", [])
                if i_calcs:
                    first = i_calcs[0] if isinstance(i_calcs, list) else i_calcs
                    formula = first.get("expression", str(first)) if isinstance(first, dict) else str(first)
                i_sources = inode.get("source_tables", [])
                i_conds = inode.get("conditions", [])
                i_line = inode.get("line_start", "?")
                lines.append(f"{var_name} = {formula}")
                if i_sources:
                    lines.append(f"  Source: {', '.join(i_sources)}")
                if i_conds:
                    cond_strs = []
                    for c in i_conds:
                        cond_strs.append(c if isinstance(c, str) else c.get("expression", str(c)))
                    lines.append(f"  Filter: {'; '.join(cond_strs)}")
                lines.append(f"  (line {i_line})")
            lines.append("")

        node_type = node.get("type", "UNKNOWN")
        target_table = node.get("target_table", "")
        source_tables = node.get("source_tables", [])
        column_maps = node.get("column_maps", {})
        calculations = node.get("calculation", [])
        conditions = node.get("conditions", [])
        committed = node.get("committed_after", False)
        line_start = node.get("line_start", "?")
        line_end = node.get("line_end", "?")

        step_label = f"STEP {step_num}"

        lines.append(f"--- {step_label}: {fn_name} ---")
        lines.append(f"Operation: {node_type}")

        source_str = ", ".join(source_tables) if source_tables else "N/A"
        target_str = target_table or "N/A"
        lines.append(f"Tables: {source_str} -> {target_str}")

        if column_maps and isinstance(column_maps, dict):
            actual_map: dict = {}
            if "mapping" in column_maps:
                actual_map = column_maps.get("mapping", {})
            elif "assignments" in column_maps:
                actual_map = {col: expr for col, expr in column_maps.get("assignments", [])}
            else:
                actual_map = column_maps

            if actual_map:
                lines.append("Column mapping:")
                map_items = list(actual_map.items())
                display_items = map_items
                truncated = False
                if len(map_items) > _COLUMN_MAP_TRUNCATE_THRESHOLD:
                    display_items = map_items[:_COLUMN_MAP_TRUNCATE_THRESHOLD]
                    truncated = True
                for col, src in display_items:
                    lines.append(f"  {col} <- {src}")
                if truncated:
                    lines.append(f"  ... (+{len(map_items) - _COLUMN_MAP_TRUNCATE_THRESHOLD} more)")

        if calculations:
            lines.append("Calculation:")
            for calc in calculations:
                if isinstance(calc, dict):
                    calc_type = calc.get("type", "UNKNOWN")
                    lines.append(f"  Type: {calc_type}")
                    expr = calc.get("expression", "")
                    if expr:
                        lines.append(f"  {_truncate(expr, 120)}")

        if conditions:
            lines.append("Conditions:")
            for cond in conditions:
                cond_text = cond if isinstance(cond, str) else cond.get("expression", str(cond))
                lines.append(f"  {_truncate(cond_text, 100)}")

        lines.append(f"Committed after: {'yes' if committed else 'no'}")
        lines.append(f"Source: lines {line_start}-{line_end}")

        lines.append("")

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
