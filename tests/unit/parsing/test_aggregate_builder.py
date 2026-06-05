"""
Unit tests for src.parsing.aggregate_builder — Redis-sourced aggregate
rebuild for resumable indexing.

Covers degeneracy detection (healthy / low-count / missing / empty), the
atomic temp-key + MULTI/EXEC rename swap, the no-overwrite-on-empty guard,
temp-key cleanup on failure, and the reconcile entry point.
"""

import fnmatch

import pytest

from src.parsing.aggregate_builder import (
    detect_degenerate_aggregate,
    rebuild_aggregates_from_redis,
    reconcile_aggregates,
)
from src.parsing.store import (
    store_function_graph,
    store_full_graph,
    get_full_graph,
    get_column_index,
)
from src.parsing.keyspace import SchemaAwareKeyspace


# ---------------------------------------------------------------------------
# Fake Redis — dict-backed, implements only what aggregate_builder touches.
# ---------------------------------------------------------------------------

class _FakePipeline:
    def __init__(self, store, fail_on_execute=False):
        self._store = store
        self._ops = []
        self._fail = fail_on_execute

    def rename(self, src, dst):
        self._ops.append((src, dst))
        return self

    def execute(self):
        if self._fail:
            raise RuntimeError("simulated transaction failure")
        for src, dst in self._ops:
            if src not in self._store:
                raise RuntimeError(f"no such key: {src}")
            self._store[dst] = self._store.pop(src)
        results = [True] * len(self._ops)
        self._ops = []
        return results


class FakeRedis:
    """Minimal sync-Redis stand-in backed by a str-keyed dict."""

    def __init__(self, fail_pipeline=False):
        self.store = {}
        self._fail_pipeline = fail_pipeline

    def set(self, key, value):
        self.store[key] = value
        return True

    def get(self, key):
        return self.store.get(key)

    def delete(self, *keys):
        n = 0
        for k in keys:
            if k in self.store:
                del self.store[k]
                n += 1
        return n

    def scan_iter(self, match=None):
        pat = match.decode() if isinstance(match, (bytes, bytearray)) else match
        # Real redis (decode_responses=False) yields bytes — mimic that so
        # the module's decode path is exercised.
        for k in list(self.store.keys()):
            if pat is None or fnmatch.fnmatchcase(k, pat):
                yield k.encode()

    def pipeline(self, transaction=True):
        return _FakePipeline(self.store, fail_on_execute=self._fail_pipeline)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

SCHEMA = "OFSERM"


def _graph(fn_name, target, source, col):
    """Build a minimal per-function graph dict the builders accept."""
    return {
        "function": fn_name,
        "schema": SCHEMA,
        "nodes": [
            {
                "id": f"{fn_name}_N1",
                "type": "INSERT",
                "target_table": target,
                "source_tables": [source],
                "column_maps": {"mapping": {col: f"{source}.{col}"}},
            }
        ],
        "edges": [],
        "column_index": {col: [f"{fn_name}_N1"]},
    }


def _seed_per_function_graphs(redis, n):
    """Store *n* per-function graphs; FN_1 writes T_OUT, FN_2 reads it."""
    for i in range(1, n + 1):
        if i == 1:
            g = _graph(f"FN_{i}", "T_OUT", "T_IN", "C1")
        else:
            g = _graph(f"FN_{i}", f"T_{i}", "T_OUT", "C1")
        store_function_graph(redis, SCHEMA, f"FN_{i}", g)


# ---------------------------------------------------------------------------
# detect_degenerate_aggregate
# ---------------------------------------------------------------------------

def test_detect_healthy_aggregate():
    redis = FakeRedis()
    _seed_per_function_graphs(redis, 5)
    store_full_graph(redis, SCHEMA, {"function_count": 5})

    result = detect_degenerate_aggregate(redis, SCHEMA)
    assert result["is_degenerate"] is False
    assert result["per_function_count"] == 5
    assert result["full_function_count"] == 5
    assert result["ratio"] == 1.0


def test_detect_degenerate_low_count():
    """graph:full with far fewer functions than exist is degenerate."""
    redis = FakeRedis()
    _seed_per_function_graphs(redis, 10)
    store_full_graph(redis, SCHEMA, {"function_count": 4})  # the 4-node state

    result = detect_degenerate_aggregate(redis, SCHEMA)
    assert result["is_degenerate"] is True
    assert result["per_function_count"] == 10
    assert result["full_function_count"] == 4
    assert result["ratio"] == pytest.approx(0.4)


def test_detect_missing_full_graph():
    redis = FakeRedis()
    _seed_per_function_graphs(redis, 3)
    # No graph:full stored at all.

    result = detect_degenerate_aggregate(redis, SCHEMA)
    assert result["is_degenerate"] is True
    assert "missing" in result["reason"]


def test_detect_no_per_function_graphs_is_not_degenerate():
    """Empty Redis is not degenerate — nothing to rebuild from."""
    redis = FakeRedis()
    result = detect_degenerate_aggregate(redis, SCHEMA)
    assert result["is_degenerate"] is False
    assert result["per_function_count"] == 0


def test_detect_threshold_boundary():
    """At exactly the threshold ratio the aggregate is healthy (>=)."""
    redis = FakeRedis()
    _seed_per_function_graphs(redis, 10)
    store_full_graph(redis, SCHEMA, {"function_count": 9})  # ratio 0.9

    result = detect_degenerate_aggregate(redis, SCHEMA, threshold=0.90)
    assert result["is_degenerate"] is False


# ---------------------------------------------------------------------------
# rebuild_aggregates_from_redis
# ---------------------------------------------------------------------------

def test_rebuild_produces_full_and_index():
    redis = FakeRedis()
    _seed_per_function_graphs(redis, 5)
    # Pre-existing degenerate full graph that must be replaced.
    store_full_graph(redis, SCHEMA, {"function_count": 4})

    summary = rebuild_aggregates_from_redis(redis, SCHEMA)
    assert summary["status"] == "rebuilt"
    assert summary["function_count"] == 5

    full = get_full_graph(redis, SCHEMA)
    assert full["function_count"] == 5

    col_index = get_column_index(redis, SCHEMA)
    assert col_index is not None
    # C1 and the table names are indexed.
    assert "C1" in col_index
    assert "T_OUT" in col_index


def test_rebuild_cleans_up_temp_keys_on_success():
    redis = FakeRedis()
    _seed_per_function_graphs(redis, 3)

    rebuild_aggregates_from_redis(redis, SCHEMA)

    tmp_full = SchemaAwareKeyspace.graph_full_key(SCHEMA) + ":__rebuild"
    tmp_index = SchemaAwareKeyspace.graph_index_key(SCHEMA) + ":__rebuild"
    assert tmp_full not in redis.store
    assert tmp_index not in redis.store


def test_rebuild_skips_when_no_graphs():
    """Never overwrite a live aggregate with an empty merge."""
    redis = FakeRedis()
    store_full_graph(redis, SCHEMA, {"function_count": 99})

    summary = rebuild_aggregates_from_redis(redis, SCHEMA)
    assert summary["status"] == "skipped"
    # Live aggregate untouched.
    assert get_full_graph(redis, SCHEMA)["function_count"] == 99


def test_rebuild_preserves_live_aggregate_on_swap_failure():
    """A failed MULTI/EXEC leaves the old aggregate intact + temp keys gone."""
    redis = FakeRedis(fail_pipeline=True)
    _seed_per_function_graphs(redis, 5)
    store_full_graph(redis, SCHEMA, {"function_count": 4, "marker": "OLD"})

    summary = rebuild_aggregates_from_redis(redis, SCHEMA)
    assert summary["status"] == "error"

    # Old aggregate still in place (rename never committed).
    full = get_full_graph(redis, SCHEMA)
    assert full["function_count"] == 4
    assert full["marker"] == "OLD"

    # Staging keys cleaned up.
    tmp_full = SchemaAwareKeyspace.graph_full_key(SCHEMA) + ":__rebuild"
    tmp_index = SchemaAwareKeyspace.graph_index_key(SCHEMA) + ":__rebuild"
    assert tmp_full not in redis.store
    assert tmp_index not in redis.store


# ---------------------------------------------------------------------------
# reconcile_aggregates
# ---------------------------------------------------------------------------

def test_reconcile_rebuilds_when_degenerate():
    redis = FakeRedis()
    _seed_per_function_graphs(redis, 6)
    store_full_graph(redis, SCHEMA, {"function_count": 2})

    outcome = reconcile_aggregates(redis, SCHEMA)
    assert outcome["action"] == "rebuilt"
    assert outcome["rebuild"]["status"] == "rebuilt"
    assert get_full_graph(redis, SCHEMA)["function_count"] == 6


def test_reconcile_noop_when_healthy():
    redis = FakeRedis()
    _seed_per_function_graphs(redis, 6)
    store_full_graph(redis, SCHEMA, {"function_count": 6})

    outcome = reconcile_aggregates(redis, SCHEMA)
    assert outcome["action"] == "none"
    assert "rebuild" not in outcome
