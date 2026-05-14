"""Unit tests for W89 — VARIABLE_TRACE chain ordering by manifest task_order.

The helper ``order_chain_by_manifest`` sorts a list of function names by
``(batch, process, sub_process_path, task_order)`` using the hierarchy
metadata each function's graph blob carries. Functions whose graph has
no hierarchy block — or no ``task_order`` — sort to the end in their
original input order so unmanifested entries are never dropped.

Tests use a tiny in-memory fake Redis that mimics ``redis.Redis.get``
for ``graph:{schema}:{fn}`` keys, returning MessagePack-encoded graphs
containing only the ``hierarchy`` field (which is the field the
ordering helper actually reads).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

import msgpack
import pytest

from src.agents.chain_ordering import (
    order_chain_by_manifest,
    reorder_multi_source,
)


# ---------------------------------------------------------------------
# In-memory fake Redis. ``get`` returns MessagePack-encoded bytes for
# matching ``graph:<schema>:<fn>`` keys; everything else returns None.
# get_function_graph (which order_chain_by_manifest calls into) wraps
# ``redis.get`` then ``msgpack.unpackb`` — the fake matches that.
# ---------------------------------------------------------------------


class _FakeRedis:
    def __init__(
        self, data: Dict[str, Any], raise_on: Optional[set[str]] = None
    ) -> None:
        # Pre-encode values so we mirror what the real store keeps in Redis.
        self._encoded: Dict[str, bytes] = {
            k: msgpack.packb(v, use_bin_type=True) for k, v in data.items()
        }
        self._raise_on = raise_on or set()

    def get(self, key: str) -> Optional[bytes]:
        if key in self._raise_on:
            raise RuntimeError(f"simulated redis failure on {key}")
        return self._encoded.get(key)


def _graph_with(hierarchy: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Build the minimal graph dict order_chain_by_manifest will read."""
    return {"hierarchy": hierarchy} if hierarchy is not None else {}


def _hierarchy(
    *,
    batch: str = "BATCH_1",
    process: str = "PROC_A",
    sub_process_path: tuple[str, ...] = ("SUB_A",),
    task_order: Optional[int] = 1,
) -> Dict[str, Any]:
    return {
        "batch": batch,
        "process": process,
        "sub_process": sub_process_path[-1] if sub_process_path else "",
        "sub_process_path": list(sub_process_path),
        "task_order": task_order,
        "task_name": "X",
        "task_id": None,
        "task_type": "FUNCTION",
        "active": True,
        "inactive_reason": None,
    }


def _redis_from(
    entries: Dict[str, Dict[str, Any]],
    *,
    schema: str = "OFSMDM",
    raise_on: Optional[set[str]] = None,
) -> _FakeRedis:
    """Build a fake Redis populated from {fn_name: hierarchy} entries."""
    data = {
        f"graph:{schema}:{fn_name.upper()}": _graph_with(h)
        for fn_name, h in entries.items()
    }
    return _FakeRedis(data, raise_on=raise_on)


# ---------------------------------------------------------------------
# POSITIVE cases — W89 should reorder
# ---------------------------------------------------------------------


def test_simple_chain_sorts_by_task_order():
    redis_client = _redis_from(
        {
            "FN_C": _hierarchy(task_order=3),
            "FN_A": _hierarchy(task_order=1),
            "FN_B": _hierarchy(task_order=2),
        }
    )
    out = order_chain_by_manifest(
        ["FN_C", "FN_A", "FN_B"],
        redis_client=redis_client,
        schemas="OFSMDM",
    )
    assert out == ["FN_A", "FN_B", "FN_C"]


def test_multi_batch_chain_sorts_by_batch_then_task_order():
    redis_client = _redis_from(
        {
            "FN_B_HIGH": _hierarchy(batch="BATCH_2", task_order=1),
            "FN_A_LOW": _hierarchy(batch="BATCH_1", task_order=2),
            "FN_A_HIGH": _hierarchy(batch="BATCH_1", task_order=1),
            "FN_B_LOW": _hierarchy(batch="BATCH_2", task_order=2),
        }
    )
    out = order_chain_by_manifest(
        ["FN_B_HIGH", "FN_A_LOW", "FN_A_HIGH", "FN_B_LOW"],
        redis_client=redis_client,
        schemas="OFSMDM",
    )
    # BATCH_1 functions first (alphabetical batch), each batch sorted
    # by task_order internally.
    assert out == ["FN_A_HIGH", "FN_A_LOW", "FN_B_HIGH", "FN_B_LOW"]


def test_multi_process_within_batch():
    redis_client = _redis_from(
        {
            "FN_B2": _hierarchy(process="PROC_B", task_order=2),
            "FN_A1": _hierarchy(process="PROC_A", task_order=1),
            "FN_B1": _hierarchy(process="PROC_B", task_order=1),
            "FN_A2": _hierarchy(process="PROC_A", task_order=2),
        }
    )
    out = order_chain_by_manifest(
        ["FN_B2", "FN_A1", "FN_B1", "FN_A2"],
        redis_client=redis_client,
        schemas="OFSMDM",
    )
    # PROC_A functions before PROC_B (alphabetical process); within
    # each, ordered by task_order.
    assert out == ["FN_A1", "FN_A2", "FN_B1", "FN_B2"]


def test_multi_sub_process_within_process_uses_sub_path():
    redis_client = _redis_from(
        {
            "FN_BETA_1": _hierarchy(
                sub_process_path=("SUB_BETA",), task_order=1
            ),
            "FN_ALPHA_2": _hierarchy(
                sub_process_path=("SUB_ALPHA",), task_order=2
            ),
            "FN_ALPHA_1": _hierarchy(
                sub_process_path=("SUB_ALPHA",), task_order=1
            ),
        }
    )
    out = order_chain_by_manifest(
        ["FN_BETA_1", "FN_ALPHA_2", "FN_ALPHA_1"],
        redis_client=redis_client,
        schemas="OFSMDM",
    )
    # SUB_ALPHA before SUB_BETA; task_order ascending within.
    assert out == ["FN_ALPHA_1", "FN_ALPHA_2", "FN_BETA_1"]


def test_unmanifested_function_goes_to_end():
    redis_client = _redis_from(
        {
            "FN_A": _hierarchy(task_order=2),
            # FN_NOMETA: present in Redis but with NO hierarchy block.
            "FN_NOMETA": None,
            "FN_B": _hierarchy(task_order=1),
        }
    )
    out = order_chain_by_manifest(
        ["FN_A", "FN_NOMETA", "FN_B"],
        redis_client=redis_client,
        schemas="OFSMDM",
    )
    # Manifested entries first (FN_B then FN_A by task_order),
    # unmanifested at the end.
    assert out == ["FN_B", "FN_A", "FN_NOMETA"]


# ---------------------------------------------------------------------
# NEGATIVE cases — W89 must NOT change behavior
# ---------------------------------------------------------------------


def test_empty_chain_returns_empty():
    redis_client = _redis_from({})
    assert order_chain_by_manifest(
        [], redis_client=redis_client, schemas="OFSMDM"
    ) == []


def test_single_function_unchanged():
    redis_client = _redis_from({"FN_A": _hierarchy(task_order=1)})
    out = order_chain_by_manifest(
        ["FN_A"], redis_client=redis_client, schemas="OFSMDM"
    )
    assert out == ["FN_A"]


def test_already_sorted_chain_unchanged():
    redis_client = _redis_from(
        {
            "FN_A": _hierarchy(task_order=1),
            "FN_B": _hierarchy(task_order=2),
        }
    )
    out = order_chain_by_manifest(
        ["FN_A", "FN_B"], redis_client=redis_client, schemas="OFSMDM"
    )
    assert out == ["FN_A", "FN_B"]


def test_all_unmanifested_returns_input_order():
    redis_client = _redis_from(
        {"FN_C": None, "FN_A": None, "FN_B": None}
    )
    # When no manifest entries exist, the original semantic-rank order
    # is preserved (no destructive shuffle).
    out = order_chain_by_manifest(
        ["FN_C", "FN_A", "FN_B"],
        redis_client=redis_client,
        schemas="OFSMDM",
    )
    assert out == ["FN_C", "FN_A", "FN_B"]


# ---------------------------------------------------------------------
# EDGE cases
# ---------------------------------------------------------------------


def test_redis_lookup_failure_falls_back_to_input_order():
    redis_client = _redis_from(
        {
            "FN_A": _hierarchy(task_order=2),
            "FN_B": _hierarchy(task_order=1),
        },
        raise_on={"graph:OFSMDM:FN_A", "graph:OFSMDM:FN_B"},
    )
    # When every Redis lookup explodes, treat every function as
    # unmanifested and preserve input order. No exception bubbles up.
    out = order_chain_by_manifest(
        ["FN_A", "FN_B"], redis_client=redis_client, schemas="OFSMDM"
    )
    assert out == ["FN_A", "FN_B"]


def test_partial_manifest_missing_task_order_treated_as_unmanifested():
    redis_client = _redis_from(
        {
            "FN_PARTIAL": _hierarchy(task_order=None),
            "FN_FULL": _hierarchy(task_order=1),
        }
    )
    out = order_chain_by_manifest(
        ["FN_PARTIAL", "FN_FULL"],
        redis_client=redis_client,
        schemas="OFSMDM",
    )
    # FN_PARTIAL has no task_order — appended at end; FN_FULL leads.
    assert out == ["FN_FULL", "FN_PARTIAL"]


def test_identical_sort_keys_preserve_input_order():
    redis_client = _redis_from(
        {
            "FN_FIRST": _hierarchy(task_order=1),
            "FN_SECOND": _hierarchy(task_order=1),
        }
    )
    # Same batch + process + task_order: stable sort keeps input order.
    out = order_chain_by_manifest(
        ["FN_FIRST", "FN_SECOND"],
        redis_client=redis_client,
        schemas="OFSMDM",
    )
    assert out == ["FN_FIRST", "FN_SECOND"]
    # And the reverse input also stays put.
    out_rev = order_chain_by_manifest(
        ["FN_SECOND", "FN_FIRST"],
        redis_client=redis_client,
        schemas="OFSMDM",
    )
    assert out_rev == ["FN_SECOND", "FN_FIRST"]


def test_cross_schema_chain_uses_per_function_schema():
    # FN_X lives in OFSMDM, FN_Y lives in OFSERM. The order helper
    # should consult each entry's own schema, not a single shared one.
    data = {
        "graph:OFSMDM:FN_X": _graph_with(_hierarchy(task_order=2)),
        "graph:OFSERM:FN_Y": _graph_with(
            _hierarchy(batch="BATCH_1", process="PROC_A", task_order=1)
        ),
    }
    redis_client = _FakeRedis(data)
    out = order_chain_by_manifest(
        ["FN_X", "FN_Y"],
        redis_client=redis_client,
        schemas={"FN_X": "OFSMDM", "FN_Y": "OFSERM"},
    )
    assert out == ["FN_Y", "FN_X"]


def test_none_redis_client_returns_input_order():
    out = order_chain_by_manifest(
        ["FN_C", "FN_A", "FN_B"],
        redis_client=None,
        schemas="OFSMDM",
    )
    assert out == ["FN_C", "FN_A", "FN_B"]


# ---------------------------------------------------------------------
# reorder_multi_source — wrapper used by main.py
# ---------------------------------------------------------------------


def test_reorder_multi_source_uses_per_entry_schema():
    redis_client = _FakeRedis(
        {
            "graph:OFSMDM:FN_X": _graph_with(_hierarchy(task_order=2)),
            "graph:OFSERM:FN_Y": _graph_with(
                _hierarchy(batch="BATCH_1", process="PROC_A", task_order=1)
            ),
        }
    )
    multi_source = {
        "FN_X": {
            "source_code": [],
            "schema": "OFSMDM",
            "score": 0.1,
        },
        "FN_Y": {
            "source_code": [],
            "schema": "OFSERM",
            "score": 0.2,
        },
    }
    out = reorder_multi_source(multi_source, redis_client=redis_client)
    # Keys reordered; values preserved intact for the corresponding key.
    assert list(out.keys()) == ["FN_Y", "FN_X"]
    assert out["FN_X"]["schema"] == "OFSMDM"
    assert out["FN_Y"]["schema"] == "OFSERM"


def test_reorder_multi_source_handles_none_redis():
    # When the graph Redis is unavailable, the dict comes back as-is —
    # neither shuffled nor crashed.
    multi_source = {"FN_X": {"schema": "OFSMDM"}, "FN_Y": {"schema": "OFSMDM"}}
    out = reorder_multi_source(multi_source, redis_client=None)
    assert list(out.keys()) == ["FN_X", "FN_Y"]


def test_reorder_multi_source_empty_input():
    # Empty input must return the same empty dict — no crash, no extra
    # bookkeeping.
    out = reorder_multi_source({}, redis_client=_FakeRedis({}))
    assert out == {}


# ---------------------------------------------------------------------
# build_transformation_chain — function_order honoured
# ---------------------------------------------------------------------


def test_build_transformation_chain_respects_function_order():
    from src.agents.variable_tracer import VariableTracer

    tracer = VariableTracer()
    # Three tagged lines, alphabetical fn order would be A, B, C.
    tagged = [
        {
            "function": "FN_A",
            "line": 10,
            "text": "X := 1;",
            "aliases_matched": ["X"],
            "operation": "ASSIGN",
            "commented": False,
        },
        {
            "function": "FN_B",
            "line": 20,
            "text": "X := 2;",
            "aliases_matched": ["X"],
            "operation": "ASSIGN",
            "commented": False,
        },
        {
            "function": "FN_C",
            "line": 30,
            "text": "X := 3;",
            "aliases_matched": ["X"],
            "operation": "ASSIGN",
            "commented": False,
        },
    ]
    # Caller-provided order — execution order C → A → B.
    chain = tracer.build_transformation_chain(
        target_variable="X",
        tagged_lines=tagged,
        seed_variables=["X"],
        function_order=["FN_C", "FN_A", "FN_B"],
    )
    # Chain blocks should appear in C, A, B order. Look for each
    # function header and check positions.
    pos_c = chain.find("=== FN_C")
    pos_a = chain.find("=== FN_A")
    pos_b = chain.find("=== FN_B")
    assert -1 < pos_c < pos_a < pos_b
    # Header line reports the same execution order.
    assert "FN_C, FN_A, FN_B" in chain


def test_build_transformation_chain_default_is_alphabetical():
    # Pre-W89 callers (no function_order) still get the alphabetical
    # ordering — this is the backwards-compatibility guarantee.
    from src.agents.variable_tracer import VariableTracer

    tracer = VariableTracer()
    tagged = [
        {
            "function": "FN_C",
            "line": 30,
            "text": "X := 3;",
            "aliases_matched": ["X"],
            "operation": "ASSIGN",
            "commented": False,
        },
        {
            "function": "FN_A",
            "line": 10,
            "text": "X := 1;",
            "aliases_matched": ["X"],
            "operation": "ASSIGN",
            "commented": False,
        },
    ]
    chain = tracer.build_transformation_chain(
        target_variable="X",
        tagged_lines=tagged,
        seed_variables=["X"],
    )
    pos_a = chain.find("=== FN_A")
    pos_c = chain.find("=== FN_C")
    assert -1 < pos_a < pos_c


def test_build_transformation_chain_appends_missing_functions():
    # If a function appears in tagged_lines but not in function_order,
    # it must still be emitted (at the end) — never dropped.
    from src.agents.variable_tracer import VariableTracer

    tracer = VariableTracer()
    tagged = [
        {
            "function": "FN_A",
            "line": 10,
            "text": "X := 1;",
            "aliases_matched": ["X"],
            "operation": "ASSIGN",
            "commented": False,
        },
        {
            "function": "FN_GHOST",
            "line": 20,
            "text": "X := 2;",
            "aliases_matched": ["X"],
            "operation": "ASSIGN",
            "commented": False,
        },
    ]
    chain = tracer.build_transformation_chain(
        target_variable="X",
        tagged_lines=tagged,
        seed_variables=["X"],
        function_order=["FN_A"],  # FN_GHOST deliberately omitted
    )
    assert "=== FN_A" in chain
    assert "=== FN_GHOST" in chain
    # FN_A should still come first since it was in the explicit order.
    assert chain.find("=== FN_A") < chain.find("=== FN_GHOST")
