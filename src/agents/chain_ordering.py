"""
W89: Manifest-driven ordering for VARIABLE_TRACE chains.

The variable-trace assembly path retrieves functions in semantic-rank order
and previously walked them alphabetically in the narrative. Stakeholder
test 2 (2026-05-14) surfaced that the narrative then read functions in a
non-execution order — e.g. deduction → phase-in → deduction (insig) —
instead of classification → aggregation → threshold → deduction.

The W39 manifest already publishes execution order via the ``task_order``
field on each function's graph-stored hierarchy block. This module
threads that signal into the VARIABLE_TRACE pipeline so the chain
presented to the LLM (and surfaced in ``functions_analyzed``) is
manifest-ordered.

Sort key (lexicographic, ascending):
    1. batch_name       (alphabetical; cross-batch chains are rare but stable)
    2. process          (depth-first traversal order; alphabetical within a batch)
    3. sub_process_path (tuple — preserves nesting; alphabetical within a process)
    4. task_order       (integer ascending — manifest declared order within the
                         innermost sub-process)

Functions without manifest entries sort to the end in their original
input order so unindexed-but-retrieved functions don't get dropped.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from src.logger import get_logger
from src.parsing.store import get_function_graph

logger = get_logger(__name__, concern="app")


# Sentinel placed in the sort key so manifested entries sort BEFORE
# unmanifested entries. Manifested tuples start with (0, ...);
# unmanifested with (1, original_index). Python tuple ordering then
# gives the desired stable ordering for free.
_MANIFESTED = 0
_UNMANIFESTED = 1


def _hierarchy_for(
    function_name: str,
    schema: str,
    redis_client: Any,
) -> Optional[Dict[str, Any]]:
    """Fetch the manifest hierarchy block for one function.

    Returns ``None`` when:
      - the function graph is not in Redis under the given schema, OR
      - the graph has no ``hierarchy`` block (legacy / no manifest), OR
      - Redis raises an exception (network blip, decode error, etc.).

    Resilience is deliberate: ordering must NEVER crash the trace
    pipeline. A missing hierarchy lookup just means the function gets
    sorted to the end of the chain.
    """
    if not function_name or not schema or redis_client is None:
        return None
    try:
        graph = get_function_graph(redis_client, schema, function_name.upper())
    except Exception as exc:
        logger.debug(
            "W89 hierarchy fetch failed for %s.%s: %s",
            schema, function_name, exc,
        )
        return None
    if graph is None:
        return None
    hierarchy = graph.get("hierarchy")
    if not isinstance(hierarchy, dict):
        return None
    return hierarchy


def _normalize_sub_process_path(value: Any) -> Tuple[str, ...]:
    """Coerce the manifest's ``sub_process_path`` into a comparable tuple.

    Persistence layer stores it as a list of strings; defensive
    handling so a corrupt entry (None, single string, etc.) doesn't
    raise during sort-key construction.
    """
    if isinstance(value, (list, tuple)):
        return tuple(str(p) for p in value if p is not None)
    if isinstance(value, str) and value:
        return (value,)
    return ()


def _is_manifested(hierarchy: Dict[str, Any]) -> bool:
    """A hierarchy is considered ordering-usable when ``task_order`` is set.

    Some functions have partial hierarchy (batch + process but no
    task_order, e.g. when the manifest registered a name but the parser
    couldn't pin its order). Treat those as unmanifested so they sort
    to the end rather than colliding at ``task_order=0``.
    """
    order = hierarchy.get("task_order")
    return isinstance(order, int)


def _sort_key_for(
    hierarchy: Dict[str, Any],
    fallback_index: int,
) -> Tuple:
    """Build the lexicographic sort key for one function's hierarchy.

    Manifested entries get ``(0, batch, process, sub_process_path,
    task_order)``. The leading 0 keeps them ahead of unmanifested
    entries, which get ``(1, fallback_index)`` so they preserve
    input order amongst themselves.
    """
    if not _is_manifested(hierarchy):
        return (_UNMANIFESTED, fallback_index)
    batch = str(hierarchy.get("batch") or "")
    process = str(hierarchy.get("process") or "")
    sub_process_path = _normalize_sub_process_path(
        hierarchy.get("sub_process_path")
    )
    task_order = int(hierarchy.get("task_order"))
    return (_MANIFESTED, batch, process, sub_process_path, task_order)


def order_chain_by_manifest(
    functions: Sequence[str],
    *,
    redis_client: Any,
    schemas: Dict[str, str] | str | None = None,
) -> List[str]:
    """Reorder a function chain by manifest-declared execution order.

    Functions with manifest entries are sorted by
    ``(batch, process, sub_process_path, task_order)``. Functions
    without manifest entries are appended at the end in their original
    input order (stable).

    Args:
        functions: Function names in their original (e.g. semantic-rank)
            order. Treated as case-preserving for display; lookups
            uppercase internally.
        redis_client: Redis client for hierarchy lookups. ``None`` is
            tolerated — the function returns the input order unchanged
            in that case.
        schemas: Either
              - a ``dict[function_name -> schema]`` for cross-schema
                chains (the common VARIABLE_TRACE case, since
                ``multi_source`` carries per-entry schemas), OR
              - a single ``str`` schema applied to every function
                (used by unit tests with a uniform schema), OR
              - ``None`` (returns input order unchanged — there's no
                schema to look up against).

    Returns:
        A new list with the same elements as ``functions``, reordered.
        Never raises; on any Redis failure the input order is preserved.
    """
    if not functions:
        return []
    if redis_client is None or schemas is None:
        return list(functions)

    if isinstance(schemas, str):
        schema_for = {fn: schemas for fn in functions}
    else:
        schema_for = dict(schemas)

    decorated: List[Tuple[Tuple, int, str]] = []
    for idx, fn in enumerate(functions):
        schema = schema_for.get(fn) or ""
        hierarchy = _hierarchy_for(fn, schema, redis_client) or {}
        key = _sort_key_for(hierarchy, fallback_index=idx)
        decorated.append((key, idx, fn))

    # Stable sort: identical primary keys preserve original input order
    # via the secondary `idx` tiebreaker. Python's sort is already
    # stable, but the explicit `idx` makes that contract visible.
    decorated.sort(key=lambda triple: (triple[0], triple[1]))

    ordered = [fn for _, _, fn in decorated]
    if ordered != list(functions):
        logger.info(
            "W89 chain reorder: %s -> %s",
            list(functions), ordered,
        )
    return ordered


def reorder_multi_source(
    multi_source: Dict[str, Any],
    *,
    redis_client: Any,
) -> Dict[str, Any]:
    """Return a new ``multi_source`` dict with keys reordered by manifest.

    Convenience wrapper for the common case where each entry carries
    its own ``schema`` field (Phase 3 behaviour stamped by
    ``MetadataInterpreter.fetch_multi_logic``). Falls back to input
    order when there's nothing to reorder.

    The returned dict is fresh — the input is not mutated. Python's
    insertion-order semantics (3.7+) carry through to downstream
    callers that iterate ``multi_source.items()`` or
    ``list(multi_source.keys())``.
    """
    if not multi_source:
        return multi_source
    if redis_client is None:
        return multi_source

    schemas: Dict[str, str] = {}
    for fn_name, entry in multi_source.items():
        entry_schema = ""
        if isinstance(entry, dict):
            entry_schema = (entry.get("schema") or "").strip()
        schemas[fn_name] = entry_schema

    ordered_keys = order_chain_by_manifest(
        list(multi_source.keys()),
        redis_client=redis_client,
        schemas=schemas,
    )
    if ordered_keys == list(multi_source.keys()):
        return multi_source
    return {k: multi_source[k] for k in ordered_keys}
