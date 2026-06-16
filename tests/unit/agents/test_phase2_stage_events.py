"""Phase 2 honest stage events + sub-instrumentation (W34a parity).

Part 1 — ``trace_value`` calls the optional ``on_stage(stage, message)``
hook at the TRUE start of each user-visible sub-stage, mirroring the
W34a guarantee ``answer_stream`` gives DATA_QUERY (see
test_data_query.py::test_w34a_stream_emits_stage_events_in_order):

    search   catalog / ambiguity lookup begins
    fetch    Oracle row fetch begins
    fetch    graph subgraph + per-step value resolution begins
             (graph_trace / partial_graph_trace routes only; same key so
             the frontend pipeline never steps backward)
    explain  explainer LLM call begins

Each event must precede the work it announces — asserted by interleaving
the hook's appends with the stubbed sub-stages' appends in one shared
event list. Keys are restricted to the frontend pipeline vocabulary
(frontend/src/lib/pipelineSteps.js STEP_DEFS) and emitted in pipeline
order.

Part 2 — the stage_timer sub-instrumentation inside the trace emits
[STAGE_TIMING] lines (phase2_row_fetch, phase2_evidence_build,
phase2_subgraph_resolve / phase2_value_chain_fetch on graph routes, and
phase2_explainer_llm inside Phase2Explainer._invoke_llm). Captured via a
handler attached directly to the ``rtie.stage_timer`` logger (it does
not propagate).

Regression — the returned dict keeps the exact pre-change shape, and the
hook is optional: omitted or raising hooks must not affect the trace.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.agents import value_tracer as vt_module
from src.agents.value_tracer import ValueTracerAgent


# ---------------------------------------------------------------------
# Stub harness
# ---------------------------------------------------------------------

_RESULT_KEYS = {
    "status", "row", "origin", "route", "evidence", "explanation",
    "sanity_warnings", "used_fallback", "verification_sql",
}


def _make_agent(events, monkeypatch, route_strategy="etl_explain",
                row_status="found"):
    """ValueTracerAgent with every sub-stage stubbed to record its
    execution into *events*, so stage-event/work interleaving is
    observable."""
    agent = ValueTracerAgent(
        schema_tools=MagicMock(),
        redis_client=MagicMock(),
        sql_guardian=MagicMock(),
    )

    # No ambiguity: empty catalog.
    monkeypatch.setattr(
        vt_module, "build_tables_to_columns",
        lambda redis_client, schema: {},
    )

    class _RowInspector:
        async def fetch_target_row(self, target_table, filters):
            events.append(("work", "row_fetch"))
            if row_status == "not_found":
                return {"status": "not_found"}
            return {"status": "found", "row": {"V_DATA_ORIGIN": "ETL"}}

    class _Classifier:
        def classify_row(self, row):
            events.append(("work", "origin_classify"))
            return {"origin_category": "etl", "traceable_via_graph": False}

    class _Router:
        def route(self, classification, row, filters):
            events.append(("work", "route"))
            return {"strategy": route_strategy}

    class _Evidence:
        def build_for_etl_origin(self, row, classification):
            events.append(("work", "evidence_build"))
            return {"verification_sql": "SELECT 1"}

        def build_for_plsql_trace(self, row, classification, graph_path,
                                  value_chain):
            events.append(("work", "evidence_build"))
            return {"verification_sql": "SELECT 1"}

        def build_for_missing_row(self, filters, eop_override=None,
                                  gl_blocked=False):
            events.append(("work", "evidence_build"))
            return {}

        def build_for_unknown_origin(self, row, classification):
            events.append(("work", "evidence_build"))
            return {"verification_sql": ""}

    class _Explainer:
        async def explain(self, **kwargs):
            events.append(("work", "explain"))
            return {
                "text": "the answer",
                "route": kwargs.get("route"),
                "sanity_warnings": [],
                "used_fallback": False,
            }

    agent._row_inspector = _RowInspector()
    agent._origin_classifier = _Classifier()
    agent._trace_router = _Router()
    agent._evidence_builder = _Evidence()
    agent._explainer = _Explainer()

    async def _fake_resolve(target_variable, filters, schema):
        events.append(("work", "graph_resolve"))
        return [], []

    agent._resolve_graph_and_values = _fake_resolve  # type: ignore[assignment]
    return agent


def _hook(events):
    def on_stage(stage, message):
        assert isinstance(message, str) and message
        events.append(("stage", stage))
    return on_stage


async def _run(agent, events, with_hook=True, **overrides):
    kwargs = dict(
        target_variable="N_EOP_BAL",
        filters={"account_number": "X", "mis_date": "2025-12-31"},
        schema="OFSMDM",
        user_query="why is N_EOP_BAL negative for account X on 2025-12-31?",
    )
    kwargs.update(overrides)
    if with_hook:
        kwargs["on_stage"] = _hook(events)
    return await agent.trace_value(**kwargs)


def _stages(events):
    return [name for kind, name in events if kind == "stage"]


def _index(events, item):
    assert item in events, f"{item!r} not in {events!r}"
    return events.index(item)


# ---------------------------------------------------------------------
# Part 1 — honest stage events
# ---------------------------------------------------------------------

async def test_etl_route_stage_events_in_order_and_before_work(monkeypatch):
    """search → fetch → explain, each firing BEFORE the work it
    announces (the W34a guarantee)."""
    events: list = []
    agent = _make_agent(events, monkeypatch)
    result = await _run(agent, events)

    assert _stages(events) == ["search", "fetch", "explain"]
    # Each stage event precedes its work.
    assert _index(events, ("stage", "fetch")) < _index(events, ("work", "row_fetch"))
    assert _index(events, ("stage", "explain")) < _index(events, ("work", "explain"))
    # 'explain' fires only once the evidence is built (true boundary).
    assert _index(events, ("work", "evidence_build")) < _index(events, ("stage", "explain"))

    assert set(result.keys()) == _RESULT_KEYS
    assert result["status"] == "untraceable_etl"
    assert result["explanation"] == "the answer"


async def test_graph_route_emits_second_fetch_before_graph_resolve(monkeypatch):
    """graph_trace adds a second 'fetch'-key event (same key — the
    frontend pipeline must never step backward) announcing subgraph +
    value resolution, before that work starts."""
    events: list = []
    agent = _make_agent(events, monkeypatch, route_strategy="graph_trace")
    result = await _run(agent, events)

    assert _stages(events) == ["search", "fetch", "fetch", "explain"]
    second_fetch_idx = [
        i for i, e in enumerate(events) if e == ("stage", "fetch")
    ][1]
    assert second_fetch_idx < _index(events, ("work", "graph_resolve"))
    assert result["status"] == "traced"


async def test_missing_row_route_still_announces_explain(monkeypatch):
    events: list = []
    agent = _make_agent(events, monkeypatch, row_status="not_found")
    result = await _run(agent, events)

    assert _stages(events) == ["search", "fetch", "explain"]
    assert _index(events, ("stage", "explain")) < _index(events, ("work", "explain"))
    assert result["status"] == "row_not_found"


async def test_stage_keys_monotonic_in_frontend_pipeline_order(monkeypatch):
    """pipelineSteps.js renders a fixed classify→search→fetch→explain
    pipeline; an out-of-order key would move the active step backward."""
    order = {"classify": 0, "search": 1, "fetch": 2, "explain": 3}
    for strategy in ("etl_explain", "graph_trace", "unknown"):
        events: list = []
        agent = _make_agent(events, monkeypatch, route_strategy=strategy)
        await _run(agent, events)
        indices = [order[s] for s in _stages(events)]
        assert indices == sorted(indices), (
            f"non-monotonic stage keys for {strategy}: {_stages(events)}"
        )


async def test_hook_is_optional_default_none(monkeypatch):
    events: list = []
    agent = _make_agent(events, monkeypatch)
    result = await _run(agent, events, with_hook=False)
    assert _stages(events) == []
    assert result["status"] == "untraceable_etl"
    assert result["explanation"] == "the answer"


async def test_raising_hook_never_breaks_the_trace(monkeypatch):
    events: list = []
    agent = _make_agent(events, monkeypatch)

    def bad_hook(stage, message):
        raise RuntimeError("hook exploded")

    result = await agent.trace_value(
        target_variable="N_EOP_BAL",
        filters={"account_number": "X", "mis_date": "2025-12-31"},
        schema="OFSMDM",
        on_stage=bad_hook,
    )
    # Every sub-stage still ran and the answer is intact.
    work = [name for kind, name in events if kind == "work"]
    assert work == [
        "row_fetch", "origin_classify", "route", "evidence_build", "explain",
    ]
    assert result["explanation"] == "the answer"


async def test_explain_difference_forwards_hook(monkeypatch):
    events: list = []
    agent = _make_agent(events, monkeypatch)
    result = await agent.explain_difference(
        target_variable="N_EOP_BAL",
        filters={"account_number": "X", "mis_date": "2025-12-31"},
        schema="OFSMDM",
        bank_value=52.0,
        system_value=50.0,
        on_stage=_hook(events),
    )
    assert _stages(events) == ["search", "fetch", "explain"]
    assert result["query_type"] == "DIFFERENCE_EXPLANATION"


# ---------------------------------------------------------------------
# Part 2 — stage_timer sub-instrumentation
# ---------------------------------------------------------------------

@pytest.fixture()
def timing_records():
    """Capture rtie.stage_timer records via a direct handler — the
    logger does not propagate (src/logger.py sets propagate=False), so
    caplog cannot see it."""
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Capture()
    timer_logger = logging.getLogger("rtie.stage_timer")
    timer_logger.addHandler(handler)
    try:
        yield records
    finally:
        timer_logger.removeHandler(handler)


def _timed_stages(records):
    out = []
    for r in records:
        msg = r.getMessage()
        if "[STAGE_TIMING]" in msg and "stage=" in msg:
            out.append(msg.split("stage=", 1)[1].split(" ", 1)[0])
    return out


async def test_trace_value_emits_substage_timing_lines(
    monkeypatch, timing_records
):
    events: list = []
    agent = _make_agent(events, monkeypatch)
    await _run(agent, events)
    timed = _timed_stages(timing_records)
    assert "phase2_row_fetch" in timed
    assert "phase2_evidence_build" in timed


async def test_graph_route_emits_resolve_and_value_chain_timers(
    monkeypatch, timing_records
):
    """The real _resolve_graph_and_values carries the subgraph/value-chain
    timers; exercise it with stubbed query-engine functions."""
    events: list = []
    agent = _make_agent(events, monkeypatch, route_strategy="graph_trace")

    # Un-stub the helper: use the real method with the module-level
    # query-engine functions patched out.
    del agent._resolve_graph_and_values
    monkeypatch.setattr(
        vt_module, "resolve_query_to_nodes",
        lambda **kwargs: ["FN_X:node1"],
    )
    monkeypatch.setattr(
        vt_module, "fetch_nodes_by_ids",
        lambda node_ids, schema, redis_client, include_upstream=True: [
            {"function": "FN_X", "node": {}, "execution_condition": None},
        ],
    )
    monkeypatch.setattr(
        vt_module, "fetch_relevant_edges",
        lambda node_ids, schema, redis_client: [],
    )
    monkeypatch.setattr(
        vt_module, "determine_execution_order",
        lambda nodes, edges: [{"function": "FN_X"}],
    )

    class _ValueFetcher:
        async def fetch_value_chain(self, graph_path, filters, target_column):
            return []

    agent._value_fetcher = _ValueFetcher()

    await _run(agent, events)
    timed = _timed_stages(timing_records)
    assert "phase2_subgraph_resolve" in timed
    assert "phase2_value_chain_fetch" in timed
    assert "phase2_row_fetch" in timed
    assert "phase2_evidence_build" in timed


async def test_explainer_llm_timer_fires(monkeypatch, timing_records):
    """Phase2Explainer._invoke_llm wraps ONLY the ainvoke in
    phase2_explainer_llm — behavior (return value) unchanged."""
    from src.phase2.explainer import Phase2Explainer

    class _FakeLLM:
        async def ainvoke(self, messages):
            return SimpleNamespace(content="  fine answer  ")

    monkeypatch.setattr(
        "src.phase2.explainer.create_llm", lambda **kwargs: _FakeLLM()
    )
    explainer = Phase2Explainer()
    text = await explainer._invoke_llm("prompt", None, None)
    assert text == "fine answer"
    assert "phase2_explainer_llm" in _timed_stages(timing_records)
