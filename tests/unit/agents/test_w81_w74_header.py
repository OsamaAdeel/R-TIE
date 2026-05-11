"""W81 + W74 — hierarchy header renderer fixes.

Two header issues bundled because they share the same renderer
(:meth:`src.agents.logic_explainer.LogicExplainer.hierarchy_header`):

  * **W81** — when ``state["multi_source"]`` spans more than one
    process, the single-function header misframes cross-flow answers.
    Renderer now consults :func:`detect_cross_process_response` and
    suppresses the header (stamps ``state["w81_suppressed"] = True``)
    when the helper returns True.

  * **W74** — the renderer used to emit only the innermost
    sub-process from the hierarchy block, dropping intermediate
    parents. Manifest's ``TaskEntry.to_node_hierarchy()`` already
    publishes the full chain as ``sub_process_path``; the renderer
    now consumes that list and falls back to the single
    ``sub_process`` field for legacy / fixture graphs that only
    carry the leaf.

The tests stub ``src.parsing.store.get_function_graph`` so they can
inject any hierarchy shape without touching Redis. Tests assert
behavior on the renderer's return value (the markdown string) and
on ``state["w81_suppressed"]`` to keep the diagnostic stamp visible.
"""

from __future__ import annotations

import pytest

from src.agents import logic_explainer as le_mod
from src.agents.logic_explainer import (
    LogicExplainer,
    detect_cross_process_response,
)
from src.parsing import store as store_mod


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


_SENTINEL_REDIS = object()  # any non-None marker; never dereferenced.


def _make_state(
    *,
    multi_source: dict | None = None,
    object_name: str = "",
    schema: str = "OFSMDM",
) -> dict:
    """Minimal LogicState shape sufficient for the renderer tests.

    LogicState is a TypedDict, so a plain dict satisfies it for tests."""
    return {
        "multi_source": multi_source or {},
        "object_name": object_name,
        "schema": schema,
        "correlation_id": "test-correlation",
    }


def _patch_graphs(monkeypatch, graphs: dict[tuple[str, str], dict | None]):
    """Stub ``store.get_function_graph`` to read from a (schema, fn) map.

    ``graphs`` is keyed by ``(schema, FUNCTION_NAME_UPPER)`` because the
    renderer / detector both upper-case the function name before lookup.
    Patches both the real store module and the local import inside
    ``hierarchy_header``.
    """
    def fake(_redis, schema, fn):
        return graphs.get((schema, fn))

    monkeypatch.setattr(store_mod, "get_function_graph", fake)


def _build_explainer(monkeypatch, graphs: dict[tuple[str, str], dict | None]):
    _patch_graphs(monkeypatch, graphs)
    explainer = LogicExplainer()
    explainer.set_redis_client(_SENTINEL_REDIS)
    return explainer


# ---------------------------------------------------------------------------
# W74 — full sub_process_path rendering
# ---------------------------------------------------------------------------


class TestW74FullSubProcessPath:
    """Renderer joins every level of ``sub_process_path``."""

    def test_flat_task_renders_unchanged(self, monkeypatch):
        """A task with one sub_process layer renders the same shape as
        before W74 — manifest publishes ``sub_process_path = ["SUB"]``,
        renderer joins it as a single tail segment."""
        graphs = {
            ("OFSMDM", "FN_FLAT"): {
                "hierarchy": {
                    "batch": "BATCH_X",
                    "process": "PROC_Y",
                    "sub_process": "SUB_Z",
                    "sub_process_path": ["SUB_Z"],
                    "task_order": 3,
                    "active": True,
                },
            },
        }
        explainer = _build_explainer(monkeypatch, graphs)
        state = _make_state(
            multi_source={"FN_FLAT": {"score": 0.1, "schema": "OFSMDM"}},
        )
        header = explainer.hierarchy_header(state)
        assert header.startswith(
            "This function runs in BATCH_X → PROC_Y → SUB_Z (task #3)."
        )

    def test_nested_three_level_renders_all_layers(self, monkeypatch):
        """A 3-level nested task renders every intermediate layer in
        outer→inner order. This is the design proof for W74; current
        OFSAA manifests are mostly flat, so a unit test is the
        canonical verification."""
        graphs = {
            ("OFSMDM", "FN_DEEP"): {
                "hierarchy": {
                    "batch": "BATCH_A",
                    "process": "PROC_B",
                    "sub_process": "SUB_INNER",
                    "sub_process_path": ["SUB_OUTER", "SUB_MID", "SUB_INNER"],
                    "task_order": 7,
                    "active": True,
                },
            },
        }
        explainer = _build_explainer(monkeypatch, graphs)
        state = _make_state(
            multi_source={"FN_DEEP": {"score": 0.1, "schema": "OFSMDM"}},
        )
        header = explainer.hierarchy_header(state)
        assert (
            "This function runs in BATCH_A → PROC_B → SUB_OUTER → "
            "SUB_MID → SUB_INNER (task #7)."
        ) in header

    def test_two_level_nested_renders_both(self, monkeypatch):
        graphs = {
            ("OFSMDM", "FN_TWO"): {
                "hierarchy": {
                    "batch": "B1",
                    "process": "P1",
                    "sub_process": "SUB_INNER",
                    "sub_process_path": ["SUB_OUTER", "SUB_INNER"],
                    "task_order": 1,
                    "active": True,
                },
            },
        }
        explainer = _build_explainer(monkeypatch, graphs)
        state = _make_state(
            multi_source={"FN_TWO": {"score": 0.0, "schema": "OFSMDM"}},
        )
        header = explainer.hierarchy_header(state)
        assert "B1 → P1 → SUB_OUTER → SUB_INNER (task #1)." in header

    def test_empty_sub_process_path_falls_back_to_single_field(self, monkeypatch):
        """Legacy / fixture graphs that only carry ``sub_process`` (not
        the list) still render — backward-compatible fallback."""
        graphs = {
            ("OFSMDM", "FN_LEGACY"): {
                "hierarchy": {
                    "batch": "BX",
                    "process": "PX",
                    "sub_process": "SX",
                    # sub_process_path absent intentionally
                    "task_order": 2,
                    "active": True,
                },
            },
        }
        explainer = _build_explainer(monkeypatch, graphs)
        state = _make_state(
            multi_source={"FN_LEGACY": {"score": 0.0, "schema": "OFSMDM"}},
        )
        header = explainer.hierarchy_header(state)
        assert "BX → PX → SX (task #2)." in header

    def test_no_sub_processes_at_all_renders_batch_and_process_only(
        self, monkeypatch
    ):
        """A task at the process level (no sub-processes) renders only
        the layers that exist — never crashes."""
        graphs = {
            ("OFSMDM", "FN_TOP"): {
                "hierarchy": {
                    "batch": "BT",
                    "process": "PT",
                    "sub_process": "",
                    "sub_process_path": [],
                    "task_order": 0,
                    "active": True,
                },
            },
        }
        explainer = _build_explainer(monkeypatch, graphs)
        state = _make_state(
            multi_source={"FN_TOP": {"score": 0.0, "schema": "OFSMDM"}},
        )
        header = explainer.hierarchy_header(state)
        assert "BT → PT" in header
        assert "→ SUB" not in header  # no spurious empty layer


# ---------------------------------------------------------------------------
# W81 — detect_cross_process_response unit tests
# ---------------------------------------------------------------------------


class TestDetectCrossProcessResponseHelper:
    """Unit-level tests on the detector itself, decoupled from the
    renderer. Each test states the multi_source shape and the graph
    table, then asserts the detector verdict."""

    def test_returns_false_when_redis_client_none(self):
        state = _make_state(
            multi_source={
                "FN_A": {"schema": "OFSMDM"},
                "FN_B": {"schema": "OFSMDM"},
            }
        )
        assert detect_cross_process_response(state, None) is False

    def test_returns_false_when_multi_source_empty(self, monkeypatch):
        _patch_graphs(monkeypatch, {})
        state = _make_state(multi_source={})
        assert detect_cross_process_response(state, _SENTINEL_REDIS) is False

    def test_returns_false_for_single_function(self, monkeypatch):
        graphs = {
            ("OFSMDM", "FN_A"): {"hierarchy": {"process": "PROC_X"}},
        }
        _patch_graphs(monkeypatch, graphs)
        state = _make_state(multi_source={"FN_A": {"schema": "OFSMDM"}})
        assert detect_cross_process_response(state, _SENTINEL_REDIS) is False

    def test_returns_false_when_all_share_one_process(self, monkeypatch):
        graphs = {
            ("OFSMDM", "FN_A"): {"hierarchy": {"process": "PROC_X"}},
            ("OFSMDM", "FN_B"): {"hierarchy": {"process": "PROC_X"}},
            ("OFSMDM", "FN_C"): {"hierarchy": {"process": "PROC_X"}},
        }
        _patch_graphs(monkeypatch, graphs)
        state = _make_state(
            multi_source={
                "FN_A": {"schema": "OFSMDM"},
                "FN_B": {"schema": "OFSMDM"},
                "FN_C": {"schema": "OFSMDM"},
            }
        )
        assert detect_cross_process_response(state, _SENTINEL_REDIS) is False

    def test_returns_true_when_two_processes_present(self, monkeypatch):
        graphs = {
            ("OFSMDM", "FN_A"): {
                "hierarchy": {"process": "OPS_RISK_PROCESSING"},
            },
            ("OFSMDM", "FN_B"): {
                "hierarchy": {"process": "CONSOLIDATION_DATA_POPULATION"},
            },
        }
        _patch_graphs(monkeypatch, graphs)
        state = _make_state(
            multi_source={
                "FN_A": {"schema": "OFSMDM"},
                "FN_B": {"schema": "OFSMDM"},
            }
        )
        assert detect_cross_process_response(state, _SENTINEL_REDIS) is True

    def test_missing_metadata_does_not_count_as_distinct_process(
        self, monkeypatch
    ):
        """An entry with no hierarchy / no process must NOT spuriously
        flip the count from 1 to 2."""
        graphs = {
            ("OFSMDM", "FN_A"): {"hierarchy": {"process": "PROC_X"}},
            ("OFSMDM", "FN_B"): {"hierarchy": {}},  # missing process
            ("OFSMDM", "FN_C"): {"hierarchy": {"process": "PROC_X"}},
        }
        _patch_graphs(monkeypatch, graphs)
        state = _make_state(
            multi_source={
                "FN_A": {"schema": "OFSMDM"},
                "FN_B": {"schema": "OFSMDM"},
                "FN_C": {"schema": "OFSMDM"},
            }
        )
        assert detect_cross_process_response(state, _SENTINEL_REDIS) is False

    def test_redis_miss_treated_as_missing_metadata(self, monkeypatch):
        """``get_function_graph`` returning None = treat as missing
        metadata; do not count toward the distinct-process tally."""
        graphs = {
            ("OFSMDM", "FN_A"): {"hierarchy": {"process": "PROC_X"}},
            # FN_B intentionally absent -> get_function_graph returns None
            ("OFSMDM", "FN_C"): {"hierarchy": {"process": "PROC_X"}},
        }
        _patch_graphs(monkeypatch, graphs)
        state = _make_state(
            multi_source={
                "FN_A": {"schema": "OFSMDM"},
                "FN_B": {"schema": "OFSMDM"},
                "FN_C": {"schema": "OFSMDM"},
            }
        )
        assert detect_cross_process_response(state, _SENTINEL_REDIS) is False

    def test_redis_exception_skipped_not_propagated(self, monkeypatch):
        """A Redis fetch error for one entry must NOT break detection
        for the rest — failure is silently skipped."""
        def fake(_redis, schema, fn):
            if fn == "FN_BOOM":
                raise RuntimeError("redis exploded")
            if (schema, fn) == ("OFSMDM", "FN_A"):
                return {"hierarchy": {"process": "PROC_X"}}
            if (schema, fn) == ("OFSMDM", "FN_C"):
                return {"hierarchy": {"process": "PROC_X"}}
            return None

        monkeypatch.setattr(store_mod, "get_function_graph", fake)
        state = _make_state(
            multi_source={
                "FN_A": {"schema": "OFSMDM"},
                "FN_BOOM": {"schema": "OFSMDM"},
                "FN_C": {"schema": "OFSMDM"},
            }
        )
        assert detect_cross_process_response(state, _SENTINEL_REDIS) is False

    def test_per_entry_schema_used_over_state_schema(self, monkeypatch):
        """Each multi_source entry's own ``schema`` field is preferred
        over ``state["schema"]`` — Phase 3 stamping behaviour."""
        graphs = {
            ("OFSERM", "FN_A"): {"hierarchy": {"process": "PROC_X"}},
            ("OFSMDM", "FN_B"): {"hierarchy": {"process": "PROC_Y"}},
        }
        _patch_graphs(monkeypatch, graphs)
        state = _make_state(
            multi_source={
                "FN_A": {"schema": "OFSERM"},
                "FN_B": {"schema": "OFSMDM"},
            },
            schema="OFSMDM",  # state-level fallback; should not apply to FN_A
        )
        assert detect_cross_process_response(state, _SENTINEL_REDIS) is True


# ---------------------------------------------------------------------------
# W81 — renderer suppression integration with detect_cross_process_response
# ---------------------------------------------------------------------------


class TestW81RendererSuppression:
    """``hierarchy_header`` returns "" and stamps state when the
    detector returns True."""

    def test_cross_process_suppresses_header_and_stamps_state(
        self, monkeypatch
    ):
        graphs = {
            ("OFSMDM", "FN_A"): {
                "hierarchy": {
                    "batch": "B",
                    "process": "OPS_RISK_PROCESSING",
                    "sub_process": "S",
                    "sub_process_path": ["S"],
                    "task_order": 1,
                    "active": True,
                },
            },
            ("OFSMDM", "FN_B"): {
                "hierarchy": {
                    "batch": "B",
                    "process": "CONSOLIDATION_DATA_POPULATION",
                    "sub_process": "S2",
                    "sub_process_path": ["S2"],
                    "task_order": 5,
                    "active": True,
                },
            },
        }
        explainer = _build_explainer(monkeypatch, graphs)
        state = _make_state(
            multi_source={
                "FN_A": {"score": 0.1, "schema": "OFSMDM"},
                "FN_B": {"score": 0.2, "schema": "OFSMDM"},
            }
        )

        header = explainer.hierarchy_header(state)

        assert header == ""
        assert state.get("w81_suppressed") is True

    def test_single_process_multi_function_keeps_header(self, monkeypatch):
        """Multiple functions all in one process — no suppression,
        header renders, no diagnostic stamp."""
        graphs = {
            ("OFSMDM", "FN_A"): {
                "hierarchy": {
                    "batch": "B",
                    "process": "PROC_X",
                    "sub_process": "S",
                    "sub_process_path": ["S"],
                    "task_order": 1,
                    "active": True,
                },
            },
            ("OFSMDM", "FN_B"): {
                "hierarchy": {
                    "batch": "B",
                    "process": "PROC_X",
                    "sub_process": "S",
                    "sub_process_path": ["S"],
                    "task_order": 2,
                    "active": True,
                },
            },
        }
        explainer = _build_explainer(monkeypatch, graphs)
        state = _make_state(
            multi_source={
                "FN_A": {"score": 0.1, "schema": "OFSMDM"},
                "FN_B": {"score": 0.2, "schema": "OFSMDM"},
            }
        )

        header = explainer.hierarchy_header(state)

        assert header.startswith("This function runs in B → PROC_X → S")
        assert state.get("w81_suppressed") in (None, False)

    def test_single_function_keeps_header(self, monkeypatch):
        """Single-function answer — no suppression possible."""
        graphs = {
            ("OFSMDM", "FN_A"): {
                "hierarchy": {
                    "batch": "B",
                    "process": "PROC_X",
                    "sub_process": "S",
                    "sub_process_path": ["S"],
                    "task_order": 1,
                    "active": True,
                },
            },
        }
        explainer = _build_explainer(monkeypatch, graphs)
        state = _make_state(
            multi_source={"FN_A": {"score": 0.1, "schema": "OFSMDM"}},
        )

        header = explainer.hierarchy_header(state)

        assert "This function runs in B → PROC_X → S (task #1)." in header
        assert state.get("w81_suppressed") in (None, False)

    def test_empty_multi_source_no_header_no_stamp(self, monkeypatch):
        """No retrieved functions and no object_name fallback — header
        is empty per existing behaviour, suppression does NOT fire."""
        _patch_graphs(monkeypatch, {})
        explainer = LogicExplainer()
        explainer.set_redis_client(_SENTINEL_REDIS)
        state = _make_state(multi_source={})

        header = explainer.hierarchy_header(state)

        assert header == ""
        # No suppression fired — the empty header is from the
        # existing primary_fn=="" guard, not from W81.
        assert state.get("w81_suppressed") in (None, False)

    def test_suppression_dominates_nested_path(self, monkeypatch):
        """Combined case: cross-process AND deeply nested — suppression
        wins. No partial nested header should leak through."""
        graphs = {
            ("OFSMDM", "FN_A"): {
                "hierarchy": {
                    "batch": "B",
                    "process": "PROC_X",
                    "sub_process": "S3",
                    "sub_process_path": ["S1", "S2", "S3"],
                    "task_order": 1,
                    "active": True,
                },
            },
            ("OFSMDM", "FN_B"): {
                "hierarchy": {
                    "batch": "B",
                    "process": "PROC_Y",
                    "sub_process": "S6",
                    "sub_process_path": ["S4", "S5", "S6"],
                    "task_order": 1,
                    "active": True,
                },
            },
        }
        explainer = _build_explainer(monkeypatch, graphs)
        state = _make_state(
            multi_source={
                "FN_A": {"score": 0.1, "schema": "OFSMDM"},
                "FN_B": {"score": 0.2, "schema": "OFSMDM"},
            }
        )

        header = explainer.hierarchy_header(state)

        assert header == ""
        assert state.get("w81_suppressed") is True


# ---------------------------------------------------------------------------
# Module-level integration: confirm helper is importable as named.
# ---------------------------------------------------------------------------


def test_detect_cross_process_response_is_module_level():
    """Diagnostic harnesses import the helper directly; lock the name."""
    assert callable(le_mod.detect_cross_process_response)
