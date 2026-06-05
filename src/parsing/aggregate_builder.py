"""
Redis-sourced aggregate rebuild for resumable indexing.

The startup loader (``src/parsing/loader.py:load_all_functions``) builds the
per-schema aggregates ``graph:full:<schema>`` and ``graph:index:<schema>``
from the *in-memory* ``primary_graphs`` list it accumulates during its parse
loop. That path is fine at startup but unreachable from ``cli.py``: an
offline ``cli.py index --resume`` run has no in-memory graphs to merge.

This module rebuilds the same two aggregates by reading the per-function
graphs back out of Redis (``graph:<schema>:<fn>``), then reusing the exact
same pure builders the loader uses
(:func:`src.parsing.indexer.build_cross_function_graph` and
:func:`src.parsing.indexer.build_global_column_index`). It exists for the
recovery case the loader cannot cover: an interrupted full re-index where the
per-function graphs survive (loader-owned, durable across Redis restarts) but
the aggregates were left missing or degenerate (the ``graph:full`` = 4 nodes
state).

Two interruption-safety properties this module guarantees that the loader's
incremental path does not:

* **Atomic swap.** The rebuilt aggregates are written to temp keys first, then
  swapped over the live keys inside a single ``MULTI/EXEC`` transaction. A
  reader sees either the old complete aggregate or the new complete aggregate
  — never a half-written one.
* **Complete-input rebuild.** The merge always runs over the FULL set of
  per-function graphs present in Redis, so the result can never be the partial
  ``graph:full`` that the loader would produce if its in-memory set were
  incomplete.

Per-schema scoping (deliberate divergence from the loader): this module is
**schema-keyed** — it rebuilds one schema's aggregate from every
``graph:<schema>:<fn>`` key in that namespace, so each function contributes
only to its own schema's rollup. The loader is **module-scoped**: it filters
its in-memory parse batch to ``primary_schema`` (loader.py:489-501) and
overwrites ``graph:full:<primary>`` once per ``load_all_functions`` call (per
module folder). These converge for the current corpus — exactly one module
folder per schema (ABL_CAR_CSTM_V4→OFSERM, OFSDMINFO_ABL_DATA_PREPARATION→
OFSMDM) with no cross-schema stray definitions, so each schema namespace holds
exactly its one module's functions. They would diverge only if a future module
folder mixed schemas: the loader excludes a stray (schema-B function in a
schema-A module) from B's rollup, whereas this rebuild routes it to
``graph:full:B`` by its key namespace. That divergence is intentional and is
the more correct behavior — the loader itself flags its module-scoping as a
temporary limitation (its ``until full multi-schema support lands (W35)``
note), and matching it bit-for-bit would require per-module manifest
membership, which has no place in a Redis-sourced rebuild that sees only the
schema namespace.

Scope (W-resumable-indexing): this is invoked from ``cli.py --resume`` only.
Retrofitting the gated/atomic rebuild into the startup loader is a tracked
follow-up — this module does NOT change the startup contract.
"""

from __future__ import annotations

from typing import Any

from src.parsing.indexer import (
    build_cross_function_graph,
    build_global_column_index,
)
from src.parsing.keyspace import SchemaAwareKeyspace
from src.parsing.serializer import to_msgpack
from src.parsing.store import get_full_graph, get_function_graph
from src.logger import get_logger

logger = get_logger(__name__, concern="app")

# Suffix appended to the live aggregate key to form its build-staging key.
# Chosen so the temp key is NOT matched by the per-function SCAN pattern
# (``graph:<schema>:*``) and is rejected by ``parse_graph_key`` (its second
# segment is the reserved ``full`` / ``index`` subkey), so a temp key left
# behind by a crash can never be mistaken for a per-function graph.
_REBUILD_SUFFIX = ":__rebuild"

# Default degeneracy threshold: the stored ``graph:full`` is considered
# degenerate when its ``function_count`` drops below 90% of the number of
# per-function graphs actually present in Redis.
DEGENERATE_RATIO_THRESHOLD = 0.90


def _per_function_graph_names(redis_client, schema: str) -> list[str]:
    """Return the per-function graph key suffixes for *schema*.

    SCANs ``graph:<schema>:*`` and keeps only true per-function keys —
    family keys (``graph:full:*``, ``graph:index:*``, ``graph:source:*``,
    etc.) are filtered out via :meth:`SchemaAwareKeyspace.parse_graph_key`,
    mirroring the same guard ``index_all_loaded`` applies.
    """
    pattern = SchemaAwareKeyspace.graph_scan_pattern(schema)
    names: list[str] = []
    for raw in redis_client.scan_iter(match=pattern):
        key = (
            raw.decode("utf-8", errors="ignore")
            if isinstance(raw, (bytes, bytearray))
            else str(raw)
        )
        parsed = SchemaAwareKeyspace.parse_graph_key(key)
        if parsed is None or parsed[0] != schema:
            continue
        names.append(parsed[1])
    return names


def _load_per_function_graphs(redis_client, schema: str) -> list[dict]:
    """Load every per-function graph dict for *schema* from Redis.

    Graphs that fail to deserialize are skipped with a warning rather than
    aborting the whole rebuild — a single corrupt key must not block
    recovery of the aggregate for every other function.
    """
    graphs: list[dict] = []
    for fn_name in _per_function_graph_names(redis_client, schema):
        graph = get_function_graph(redis_client, schema, fn_name)
        if graph is None:
            logger.warning(
                "aggregate rebuild: graph:%s:%s unreadable; skipping",
                schema, fn_name,
            )
            continue
        graphs.append(graph)
    return graphs


def detect_degenerate_aggregate(
    redis_client,
    schema: str,
    threshold: float = DEGENERATE_RATIO_THRESHOLD,
) -> dict[str, Any]:
    """Report whether ``graph:full:<schema>`` is missing or degenerate.

    The aggregate is degenerate when per-function graphs exist but the
    stored full graph is absent, or its ``function_count`` is below
    *threshold* × (number of per-function graphs present). The common
    failure this catches: an interrupted re-index that left
    ``graph:full`` rebuilt from only a handful of functions (the
    ``graph:full`` = 4 nodes state).

    Returns a dict with: ``is_degenerate`` (bool), ``per_function_count``,
    ``full_function_count``, ``ratio`` (full/per-fn, or 0.0 when no
    per-fn graphs), and ``reason``.
    """
    per_fn = len(_per_function_graph_names(redis_client, schema))
    full = get_full_graph(redis_client, schema)
    full_count = int((full or {}).get("function_count", 0))

    if per_fn == 0:
        # Nothing to build from — not degenerate, just empty. Rebuilding
        # from zero graphs is a no-op the caller should skip.
        return {
            "is_degenerate": False,
            "per_function_count": 0,
            "full_function_count": full_count,
            "ratio": 0.0,
            "reason": "no per-function graphs present",
        }

    ratio = full_count / per_fn
    if full is None:
        reason = "graph:full missing while per-function graphs exist"
        is_degenerate = True
    elif ratio < threshold:
        reason = (
            f"graph:full function_count={full_count} is "
            f"{ratio:.0%} of {per_fn} per-function graphs "
            f"(threshold {threshold:.0%})"
        )
        is_degenerate = True
    else:
        reason = (
            f"graph:full healthy: {full_count}/{per_fn} "
            f"({ratio:.0%} >= {threshold:.0%})"
        )
        is_degenerate = False

    return {
        "is_degenerate": is_degenerate,
        "per_function_count": per_fn,
        "full_function_count": full_count,
        "ratio": ratio,
        "reason": reason,
    }


def rebuild_aggregates_from_redis(redis_client, schema: str) -> dict[str, Any]:
    """Rebuild ``graph:full:<schema>`` and ``graph:index:<schema>`` from Redis.

    Reads every per-function graph for *schema*, merges them with the same
    pure builders the loader uses, and swaps the results over the live
    aggregate keys atomically (temp-key write + ``MULTI/EXEC`` rename).

    Returns a summary dict with ``status`` (``"rebuilt"`` / ``"skipped"`` /
    ``"error"``) and, on success, ``function_count`` / ``node_count`` /
    ``edge_count`` / ``index_keys`` from the rebuilt aggregate.

    A no-op (``"skipped"``) when no per-function graphs are present — there
    is nothing to merge and we must never overwrite a live aggregate with
    an empty one. On any failure the temp keys are cleaned up so a retry
    starts from a clean staging area.
    """
    graphs = _load_per_function_graphs(redis_client, schema)
    if not graphs:
        logger.warning(
            "aggregate rebuild: no per-function graphs for %s; skipping "
            "(refusing to overwrite live aggregates with an empty set)",
            schema,
        )
        return {
            "status": "skipped",
            "reason": "no per-function graphs present",
            "function_count": 0,
        }

    full_graph = build_cross_function_graph(graphs)
    column_index = build_global_column_index(graphs)

    full_key = SchemaAwareKeyspace.graph_full_key(schema)
    index_key = SchemaAwareKeyspace.graph_index_key(schema)
    tmp_full = full_key + _REBUILD_SUFFIX
    tmp_index = index_key + _REBUILD_SUFFIX

    try:
        # Stage both payloads first, then swap both over the live keys in a
        # single transaction so a reader never observes one aggregate
        # updated and the other stale.
        redis_client.set(tmp_full, to_msgpack(full_graph))
        redis_client.set(tmp_index, to_msgpack(column_index))

        pipe = redis_client.pipeline(transaction=True)
        pipe.rename(tmp_full, full_key)
        pipe.rename(tmp_index, index_key)
        pipe.execute()
    except Exception as exc:
        # Best-effort cleanup of staging keys; the live aggregates are
        # untouched because the rename never committed.
        try:
            redis_client.delete(tmp_full, tmp_index)
        except Exception:
            pass
        logger.error(
            "aggregate rebuild failed for %s: %s", schema, exc
        )
        return {"status": "error", "error": str(exc), "function_count": 0}

    logger.info(
        "aggregate rebuild for %s: %d functions, %d nodes, %d edges, "
        "%d index keys (atomic swap committed)",
        schema,
        full_graph.get("function_count", 0),
        full_graph.get("node_count", 0),
        full_graph.get("edge_count", 0),
        len(column_index),
    )
    return {
        "status": "rebuilt",
        "function_count": full_graph.get("function_count", 0),
        "node_count": full_graph.get("node_count", 0),
        "edge_count": full_graph.get("edge_count", 0),
        "index_keys": len(column_index),
    }


def reconcile_aggregates(
    redis_client,
    schema: str,
    threshold: float = DEGENERATE_RATIO_THRESHOLD,
) -> dict[str, Any]:
    """Detect a degenerate aggregate for *schema* and rebuild it if needed.

    The single entry point ``cli.py --resume`` calls per schema: runs
    :func:`detect_degenerate_aggregate`, and only when it reports
    degeneracy does it call :func:`rebuild_aggregates_from_redis`. Returns
    a merged dict carrying both the detection result and (when triggered)
    the rebuild summary under ``rebuild``.
    """
    detection = detect_degenerate_aggregate(redis_client, schema, threshold)
    if not detection["is_degenerate"]:
        detection["action"] = "none"
        return detection

    logger.info(
        "aggregate reconcile %s: degenerate (%s) — rebuilding",
        schema, detection["reason"],
    )
    detection["action"] = "rebuilt"
    detection["rebuild"] = rebuild_aggregates_from_redis(redis_client, schema)
    return detection
