"""W80 — vector retrieval embedding input poisoning fix.

Pre-W80 the classifier stamped ``state["object_name"]`` with a
concatenated blob (raw_query + intent + search_terms) which then fed
the vector-search embedding at [src/main.py:1084] / [src/pipeline/
logic_graph.py:135]. For anchorless queries that blob became a diffuse,
classifier-restated centroid pulled away from the correct function
semantics — surfaced concretely by stakeholder test 2's near-100%
retrieval miss on the significant-investment trace.

W80 v1 fix:

  1. ``Orchestrator.classify_query`` no longer writes object_name.
  2. ``object_name`` is owned exclusively by the two post-passes that
     produce clean function names — ``apply_named_function_anchor``
     (W76) and ``apply_bi_routing`` (BI literal-index resolution).
  3. The embedding sites resolve their input via
     ``anchor_resolution.resolve_search_query`` which falls back to
     ``raw_query`` when object_name is empty.

These tests pin (1)–(3) so a regression to the blob shape is caught
before it reaches stakeholder canaries.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from src.agents.anchor_resolution import resolve_search_query
from src.agents.orchestrator import Orchestrator


# ---------------------------------------------------------------------------
# resolve_search_query — the helper called by both embedding sites
# ---------------------------------------------------------------------------


class TestResolveSearchQuery:
    """The embedding-input resolver.

    Precedence: clean ``object_name`` → ``raw_query`` → "".
    """

    def test_w76_anchor_resolved_returns_clean_function_name(self):
        """W76 has stamped a clean function name on object_name. The
        embedding input is that function name verbatim, not the user's
        rambling raw query."""
        state = {
            "object_name": "FN_LOAD_OPS_RISK_DATA",
            "raw_query": "tell me about how the ops risk thing works yeah",
        }
        assert resolve_search_query(state) == "FN_LOAD_OPS_RISK_DATA"

    def test_bi_routing_resolved_returns_clean_function_name(self):
        """BI routing has resolved a CAP-code to a specific function and
        stamped object_name. That function name is the embedding input."""
        state = {
            "object_name": (
                "CS_Regulatory_Adjustments_NonRegulatoryConsolidationEntity_Adjustment"
            ),
            "raw_query": "How is CAP973 calculated?",
        }
        assert resolve_search_query(state) == (
            "CS_Regulatory_Adjustments_NonRegulatoryConsolidationEntity_Adjustment"
        )

    def test_falls_back_to_raw_query_when_object_name_missing(self):
        """No anchor, no BI routing — object_name absent from state.
        Embedding input is the user's verbatim question."""
        state = {
            "raw_query": (
                "trace insignificant investments through deduction"
            ),
        }
        assert resolve_search_query(state) == (
            "trace insignificant investments through deduction"
        )

    def test_falls_back_to_raw_query_when_object_name_is_empty_string(self):
        """Initial state seeds object_name to "". An empty string must
        be treated as "no anchor" and fall through, not returned as-is."""
        state = {
            "object_name": "",
            "raw_query": "trace insignificant investments through deduction",
        }
        assert resolve_search_query(state) == (
            "trace insignificant investments through deduction"
        )

    def test_falls_back_to_raw_query_when_object_name_is_none(self):
        """None is also falsy — same fall-through as empty string."""
        state = {
            "object_name": None,
            "raw_query": "trace insignificant investments through deduction",
        }
        assert resolve_search_query(state) == (
            "trace insignificant investments through deduction"
        )

    def test_function_name_with_underscores_preserved_verbatim(self):
        """Underscores, digits, and case are all preserved. The vector
        store keys functions by uppercase + underscore form; mangling
        those would break the match."""
        state = {
            "object_name": "ABL_SIGNIFCNT_INVSTMNT_IN_NON_REG_CONSL_ENTITY_DATA_POP",
            "raw_query": "Trace N_SIGNIFICANT_INVST_AMT through deduction.",
        }
        assert resolve_search_query(state) == (
            "ABL_SIGNIFCNT_INVSTMNT_IN_NON_REG_CONSL_ENTITY_DATA_POP"
        )

    def test_returns_empty_string_when_both_fields_missing(self):
        """Defensive case — raw_query is set at endpoint entry so this
        is unreachable in production, but the helper must not raise."""
        assert resolve_search_query({}) == ""

    def test_returns_empty_string_when_both_fields_empty(self):
        """Both empty — defensive only."""
        assert resolve_search_query({"object_name": "", "raw_query": ""}) == ""

    def test_object_name_not_stripped(self):
        """The helper does not strip whitespace from object_name. W76 /
        BI routing produce names without surrounding whitespace; if a
        whitespace value somehow reached object_name (it shouldn't),
        we'd rather see it explode loudly downstream than be silently
        coerced into a fall-back. Pin the current contract."""
        state = {"object_name": "  ", "raw_query": "fallback"}
        # Non-empty whitespace is truthy in Python — returns as-is.
        assert resolve_search_query(state) == "  "


# ---------------------------------------------------------------------------
# Orchestrator.classify_query — must NOT set object_name to the blob
# ---------------------------------------------------------------------------


def _classification_response(
    *,
    query_type: str = "COLUMN_LOGIC",
    intent: str = "User wants to understand a function",
    search_terms = ("function", "logic", "explain"),
    target_variable=None,
    schema_name: str = "OFSMDM",
) -> AIMessage:
    """Build a fake LLM AIMessage carrying a ClassificationResult JSON."""
    body = {
        "query_type": query_type,
        "intent": intent,
        "search_terms": list(search_terms),
        "target_variable": target_variable,
        "schema_name": schema_name,
        "confidence": 0.9,
    }
    return AIMessage(content=json.dumps(body))


def _empty_state(raw_query: str) -> dict:
    """Mirror the initial LogicState shape created at /v1/stream entry."""
    return {
        "session_id": "test",
        "correlation_id": "test-cid",
        "raw_query": raw_query,
        "query_type": "",
        "object_name": "",
        "object_type": "",
        "schema": "",
        "source_code": [],
        "call_tree": {},
        "cache_hit": False,
        "cache_stale": False,
        "explanation": {},
        "validated": False,
        "confidence": 0.0,
        "warnings": [],
        "search_results": [],
        "multi_source": {},
        "target_variable": "",
        "variable_chain": {},
        "llm_payload": "",
        "graph_node_ids": [],
        "graph_available": False,
        "bi_routing": {},
        "schema_scope": "ALL",
        "schemas_searched": [],
        "output": {},
        "partial_flag": False,
    }


@pytest.mark.asyncio
async def test_classify_query_never_sets_object_name_to_blob():
    """W80 regression guard: the classifier must not stamp the enriched
    blob (raw_query + intent + search_terms) onto object_name. After
    classify_query, object_name remains "" — the post-passes
    (apply_named_function_anchor, apply_bi_routing) own that field."""
    orch = Orchestrator(temperature=0, max_tokens=100)
    state = _empty_state(
        "trace insignificant investments through deduction"
    )

    fake_llm = MagicMock()
    fake_llm.ainvoke = AsyncMock(
        return_value=_classification_response(
            query_type="VARIABLE_TRACE",
            intent="Trace flow through deduction stages",
            search_terms=["insignificant", "investment", "deduction"],
            target_variable="N_INSIGNIFICANT_INVST_AMT",
        )
    )

    with patch.object(orch, "_get_llm", return_value=fake_llm):
        result = await orch.classify_query(state["raw_query"], state)

    # The blob must NOT appear in object_name.
    assert "trace insignificant investments" not in result["object_name"]
    assert "Trace flow through deduction stages" not in result["object_name"]
    assert "insignificant investment deduction" not in result["object_name"]
    # In fact, object_name should be empty — initial state seeded "".
    assert result["object_name"] == ""


@pytest.mark.asyncio
async def test_classify_query_still_sets_query_type():
    """Regression guard: the classifier's routing role is unchanged.
    query_type still gets populated from the LLM output."""
    orch = Orchestrator(temperature=0, max_tokens=100)
    state = _empty_state("How does FN_LOAD_OPS_RISK_DATA work?")

    fake_llm = MagicMock()
    fake_llm.ainvoke = AsyncMock(
        return_value=_classification_response(query_type="COLUMN_LOGIC")
    )

    with patch.object(orch, "_get_llm", return_value=fake_llm):
        result = await orch.classify_query(state["raw_query"], state)

    assert result["query_type"] == "COLUMN_LOGIC"


@pytest.mark.asyncio
async def test_classify_query_still_sets_schema_and_target_variable():
    """Regression guard: the classifier's other output fields are
    unchanged. Only object_name was poisoned and only object_name is
    being cleaned up — every other field continues to flow through."""
    orch = Orchestrator(temperature=0, max_tokens=100)
    state = _empty_state("How is N_EOP_BAL set in FN_LOAD_OPS_RISK_DATA?")

    fake_llm = MagicMock()
    fake_llm.ainvoke = AsyncMock(
        return_value=_classification_response(
            query_type="VARIABLE_TRACE",
            target_variable="N_EOP_BAL",
            schema_name="OFSMDM",
        )
    )

    with patch.object(orch, "_get_llm", return_value=fake_llm):
        result = await orch.classify_query(state["raw_query"], state)

    assert result["target_variable"] == "N_EOP_BAL"
    assert result["schema"] == "OFSMDM"


@pytest.mark.asyncio
async def test_classify_query_does_not_store_blob_in_any_state_field():
    """W80 stronger regression: confirm the blob isn't relocated to a
    different state field by accident. Discovery established no
    consumer reads ``enriched_query`` — this test guarantees we
    didn't introduce one as a hidden field along the fix path."""
    orch = Orchestrator(temperature=0, max_tokens=100)
    state = _empty_state("anchorless rambling about deduction stages")

    fake_llm = MagicMock()
    fake_llm.ainvoke = AsyncMock(
        return_value=_classification_response(
            intent="UNIQUE_BLOB_SENTINEL_VALUE",
            search_terms=["UNIQUE_BLOB_SENTINEL_VALUE"],
        )
    )

    with patch.object(orch, "_get_llm", return_value=fake_llm):
        result = await orch.classify_query(state["raw_query"], state)

    # The unique sentinel must not appear in any string state field —
    # confirms the blob isn't quietly stashed elsewhere.
    for key, value in result.items():
        if isinstance(value, str):
            assert "UNIQUE_BLOB_SENTINEL_VALUE" not in value, (
                f"Blob sentinel leaked into state[{key!r}]"
            )


# ---------------------------------------------------------------------------
# End-to-end precedence: W76 anchor → embedding input
# ---------------------------------------------------------------------------


class TestPrecedenceWithW76Anchor:
    """W76 fires first; its clean object_name wins as the embedding input."""

    def test_w76_anchor_then_resolve_returns_clean_anchor(self):
        """Full pipeline shape: classify_query leaves object_name == "";
        apply_named_function_anchor stamps a clean name; the embedding
        site resolves to that clean name."""
        orch = Orchestrator()
        state = {
            "raw_query": (
                "In FN_LOAD_OPS_RISK_DATA, how is N_EOP_BAL set?"
            ),
            "query_type": "VARIABLE_TRACE",
            "object_name": "",
            "target_variable": "N_EOP_BAL",
            "schema": "OFSMDM",
        }

        orch.apply_named_function_anchor(state)

        # W76 fired — object_name is the clean function name.
        assert state["object_name"] == "FN_LOAD_OPS_RISK_DATA"
        # And the embedding resolver picks that up.
        assert resolve_search_query(state) == "FN_LOAD_OPS_RISK_DATA"

    def test_no_anchor_then_resolve_returns_raw_query(self):
        """W76 didn't fire (no prefix anchor, no alias-literal recovery).
        object_name stays empty; resolver falls back to raw_query —
        the exact stakeholder-test-2 shape."""
        orch = Orchestrator()
        state = {
            "raw_query": (
                "Trace N_SIGNIFICANT_INVST_AMT from classification "
                "through deduction."
            ),
            "query_type": "VARIABLE_TRACE",
            "object_name": "",
            "target_variable": "N_SIGNIFICANT_INVST_AMT",
            "schema": "OFSERM",
        }

        orch.apply_named_function_anchor(state)

        # No anchor fired.
        assert state["object_name"] == ""
        # Resolver returns the user's verbatim query, NOT a classifier
        # blob. This is the W80 fix's positive surface for anchorless
        # cross-table multi-stage traces.
        assert resolve_search_query(state) == (
            "Trace N_SIGNIFICANT_INVST_AMT from classification "
            "through deduction."
        )

    def test_anchorless_resolver_input_carries_no_classifier_restatement(self):
        """The raw_query fall-back is structurally narrower than the
        old blob: just the user's words, no classifier-restated noise.
        Pin this so a future "compose object_name from intent" attempt
        regresses visibly."""
        state = {
            "raw_query": "trace insignificant investments through deduction",
            "object_name": "",
        }
        out = resolve_search_query(state)
        # The classifier's typical noise patterns must not appear.
        assert "intent" not in out.lower()
        assert "search_terms" not in out.lower()
        # And it's still the user's literal question.
        assert out == "trace insignificant investments through deduction"


# ---------------------------------------------------------------------------
# End-to-end precedence: W76 anchor still wins after this PR
# ---------------------------------------------------------------------------


class TestW76AnchorStillWins:
    """Regression guard: W80 must not weaken W76's write-through. When
    both signals are present (a clean object_name from a previous turn
    AND a W76 anchor on this turn), W76's name is the one that survives
    into resolve_search_query."""

    def test_w76_overwrites_any_prior_object_name(self):
        orch = Orchestrator()
        state = {
            "raw_query": (
                "In FN_LOAD_OPS_RISK_DATA, how is N_EOP_BAL set?"
            ),
            "query_type": "VARIABLE_TRACE",
            # Simulate some stale prior value (e.g. a checkpointed session
            # state — though in production this is always "" at entry).
            "object_name": "STALE_FUNCTION_NAME",
            "target_variable": "N_EOP_BAL",
            "schema": "OFSMDM",
        }

        orch.apply_named_function_anchor(state)

        assert state["object_name"] == "FN_LOAD_OPS_RISK_DATA"
        assert resolve_search_query(state) == "FN_LOAD_OPS_RISK_DATA"
